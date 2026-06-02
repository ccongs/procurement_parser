"""DB 입출력 계층 — Phase 3.3.

collector 가 사용하는 저장소(repository). 정책 판단(재시도/halt/상태 결정)은 collector 가
담당하고, 이 모듈은 **순수 DB 입출력**만 책임진다: 설정 조회, 실행이력 기록,
bid_notice upsert, halt 플래그·last_success_dt 갱신.

- 세션은 호출자가 주입한다(`session: Session`). 모듈이 세션을 만들지 않는다.
- upsert 는 DB별 기능(ON CONFLICT 등)에 의존하지 않고 표준 select→insert/update 로 구현한다
  (PostgreSQL 이전 대비).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import AppConfig, BidNotice, CollectionRun, PreSpec

# bid_notice 에 실제 존재하는 컬럼명 집합 — values dict 중 컬럼이 아닌 키는 무시한다.
_BID_NOTICE_COLUMNS: frozenset[str] = frozenset(
    c.name for c in BidNotice.__table__.columns
)

# pre_spec 에 실제 존재하는 컬럼명 집합(upsert_pre_specs 화이트리스트).
_PRE_SPEC_COLUMNS: frozenset[str] = frozenset(
    c.name for c in PreSpec.__table__.columns
)


# --- 설정 ----------------------------------------------------------------
def get_config(session: Session) -> AppConfig:
    """단일 설정 행(id=1)을 반환. 없으면 ValueError(init_db 시드 전제)."""
    cfg = session.get(AppConfig, 1)
    if cfg is None:
        raise ValueError(
            "app_config(id=1) 행이 없습니다. `python -m app.db` 로 init_db() 시드를 먼저 실행하세요."
        )
    return cfg


# --- 실행 이력 -----------------------------------------------------------
def create_run(
    session: Session,
    trigger: str,
    window_bgn: datetime,
    window_end: datetime,
    source: str = "bid",
) -> CollectionRun:
    """status='running' 으로 실행 이력을 insert 하고 즉시 commit(=id 확보·쓰기 잠금 해제) 후 반환.

    source: 수집원 구분(Phase 5.4) — "bid"(기본, 입찰) | "pre_spec"(사전규격).
    기본값 "bid" 이므로 입찰 collector 의 기존 호출은 무변경(하위호환).

    ⚠️ 운영 핫픽스(SQLite 동시성): 과거에는 `session.flush()` 로 쓰기 트랜잭션을 **연 채**
    이후의 긴 네트워크 페이징을 끝까지 진행하다가 `finish_run` 에서야 commit 했다. 입찰·사전규격
    두 잡이 동시 tick 으로 뜨면 한쪽이 이 트랜잭션의 쓰기 잠금을 길게 쥐고 있어 다른 잡의
    `create_run` INSERT 가 `database is locked` 로 실패했다. 여기서 **즉시 commit** 해
    "running" run 행을 영속화하고 쓰기 잠금을 바로 놓으면, 이후 네트워크 fetch 구간에는 열린
    쓰기 트랜잭션이 없어 동시 실행이 안전해진다(짧은 쓰기 경쟁은 db.py 의 busy_timeout 이 흡수).
    commit 으로 run 속성이 만료되지만 `run.id` 접근 시 자동 refresh 되고, `finish_run` 의
    `run.status=...` 후 commit·collector 의 `_detach_run`(refresh→expunge) 도 정상 동작한다.
    반환값은 그대로 status="running" 인 run(반환 타입·동작 불변).
    """
    run = CollectionRun(
        trigger=trigger,
        run_started_at=datetime.now(),
        window_bgn_dt=window_bgn,
        window_end_dt=window_end,
        status="running",
        total_fetched=0,
        total_new=0,
        total_updated=0,
        retry_count=0,
        source=source,
    )
    session.add(run)
    session.commit()  # 쓰기 잠금을 네트워크 fetch 전에 해제(id 는 commit 후 접근 시 자동 refresh)
    return run


def finish_run(
    session: Session,
    run: CollectionRun,
    *,
    status: str,
    total_fetched: int,
    total_new: int,
    total_updated: int,
    retry_count: int,
    error_code: str | None = None,
    error_msg: str | None = None,
    detail_json: str | None = None,
) -> None:
    """실행 이력의 종료 상태·결과를 기록하고 commit 한다."""
    run.run_finished_at = datetime.now()
    run.status = status
    run.total_fetched = total_fetched
    run.total_new = total_new
    run.total_updated = total_updated
    run.retry_count = retry_count
    run.error_code = error_code
    run.error_msg = error_msg
    run.detail_json = detail_json
    session.commit()


# --- bid_notice upsert ---------------------------------------------------
def _normalize_ord(value: Any) -> tuple[Any, bool]:
    """차수값을 정규화해 비교 가능한 형태로 변환.

    반환: (normalized_value, is_valid) — 정수 파싱 가능이면 (int, True),
    문자열로만 비교 가능하면 (strip된 str, True). None / 빈 문자열은 (None, False).
    """
    if value is None:
        return (None, False)
    s = str(value).strip()
    if not s:
        return (None, False)
    try:
        return (int(s), True)
    except ValueError:
        return (s, True)


def upsert_bid_notices(
    session: Session,
    values_list: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """bid_ntce_no(PK) 기준 upsert. (new_count, updated_count, ord_changes) 반환.

    - 각 원소는 transform 결과 + 메타(matched_indstryty_cds/collected_at/updated_at)가 합쳐진
      컬럼값 dict.
    - 기존 PK 는 `IN` 으로 한 번에 조회해 N+1 을 피한다.
    - **collected_at(최초 수집 시각)은 insert 시에만 설정하고 update 시 보존**한다.
      updated_at 등 나머지 컬럼은 항상 갱신.
    - ord_changes: update 경로에서 bid_ntce_ord 가 바뀐 공고 목록. 각 원소
      {"no": <bid_ntce_no>, "old": <저장차수 원본 str>, "new": <들어온차수 원본 str>}.
      비교는 정규화(int 파싱 가능이면 int, 아니면 strip 문자열) 후 수행해 오탐을 방지한다
      ("1"↔"01" 은 동일). new 가 None/빈값이면 기록하지 않는다.
    """
    if not values_list:
        return (0, 0, [])

    pks = [v["bid_ntce_no"] for v in values_list if v.get("bid_ntce_no")]
    existing: dict[str, BidNotice] = {}
    if pks:
        rows = (
            session.execute(select(BidNotice).where(BidNotice.bid_ntce_no.in_(pks)))
            .scalars()
            .all()
        )
        existing = {r.bid_ntce_no: r for r in rows}

    new_count = 0
    updated_count = 0
    ord_changes: list[dict[str, Any]] = []

    for values in values_list:
        pk = values.get("bid_ntce_no")
        if not pk:
            # PK 없는 값은 상위(collector)에서 걸러지지만 방어적으로 건너뛴다.
            continue

        row = existing.get(pk)
        if row is None:
            obj = BidNotice(
                **{k: v for k, v in values.items() if k in _BID_NOTICE_COLUMNS}
            )
            session.add(obj)
            existing[pk] = obj  # 같은 배치 내 PK 중복 방어(이후 update 로 흡수)
            new_count += 1
        else:
            # --- 차수변경 감지: update 경로에서만, 덮어쓰기 전에 캡처 ---
            incoming_ord_raw = values.get("bid_ntce_ord")
            new_norm, new_valid = _normalize_ord(incoming_ord_raw)
            if new_valid:
                old_raw = row.bid_ntce_ord
                old_norm, _ = _normalize_ord(old_raw)
                if new_norm != old_norm:
                    # 원본 문자열(strip만)로 기록. old가 None이면 None 그대로.
                    ord_changes.append({
                        "no": pk,
                        "old": str(old_raw).strip() if old_raw is not None else None,
                        "new": str(incoming_ord_raw).strip(),
                    })

            for key, value in values.items():
                if key not in _BID_NOTICE_COLUMNS:
                    continue
                if key == "collected_at":
                    continue  # 최초 수집 시각 보존
                setattr(row, key, value)
            updated_count += 1

    session.flush()
    return (new_count, updated_count, ord_changes)


# --- pre_spec upsert (Phase 5.3) ----------------------------------------
def upsert_pre_specs(
    session: Session,
    values_list: list[dict[str, Any]],
) -> tuple[int, int]:
    """bf_spec_rgst_no(PK) 기준 upsert. (new_count, updated_count) 반환.

    `upsert_bid_notices` 와 동형(PK만 다름):
    - 각 원소는 transform 결과 + 메타(collected_at/updated_at)가 합쳐진 컬럼값 dict.
    - 기존 PK 는 `IN` 으로 한 번에 조회해 N+1 을 피한다. 같은 배치 내 PK 중복 방어.
    - **collected_at(최초 수집 시각)은 insert 시에만 설정하고 update 시 보존**한다.
      updated_at 등 나머지 컬럼은 항상 갱신.
    - _PRE_SPEC_COLUMNS 화이트리스트 외 키는 무시. PK 없는 값은 방어적으로 skip.
    """
    if not values_list:
        return (0, 0)

    pks = [v["bf_spec_rgst_no"] for v in values_list if v.get("bf_spec_rgst_no")]
    existing: dict[str, PreSpec] = {}
    if pks:
        rows = (
            session.execute(select(PreSpec).where(PreSpec.bf_spec_rgst_no.in_(pks)))
            .scalars()
            .all()
        )
        existing = {r.bf_spec_rgst_no: r for r in rows}

    new_count = 0
    updated_count = 0
    for values in values_list:
        pk = values.get("bf_spec_rgst_no")
        if not pk:
            # PK 없는 값은 상위(collector)에서 걸러지지만 방어적으로 건너뛴다.
            continue

        row = existing.get(pk)
        if row is None:
            obj = PreSpec(
                **{k: v for k, v in values.items() if k in _PRE_SPEC_COLUMNS}
            )
            session.add(obj)
            existing[pk] = obj  # 같은 배치 내 PK 중복 방어(이후 update 로 흡수)
            new_count += 1
        else:
            for key, value in values.items():
                if key not in _PRE_SPEC_COLUMNS:
                    continue
                if key == "collected_at":
                    continue  # 최초 수집 시각 보존
                setattr(row, key, value)
            updated_count += 1

    session.flush()
    return (new_count, updated_count)


# --- halt / last_success ------------------------------------------------
def set_halt(session: Session, halt_code: str | None, halt_reason: str | None) -> None:
    """비재시도 에러로 스케줄 자동 중단 — auto_halted=True 와 사유를 기록하고 commit."""
    cfg = get_config(session)
    cfg.auto_halted = True
    cfg.halt_code = halt_code
    cfg.halt_reason = halt_reason
    cfg.updated_at = datetime.now()
    session.commit()


def clear_halt(session: Session) -> None:
    """중단 해제(재개) — auto_halted/halt_code/halt_reason 초기화.

    이번 단계(3.3)에서는 정의만 둔다. 화면(/config) 연결은 3.5.
    """
    cfg = get_config(session)
    cfg.auto_halted = False
    cfg.halt_code = None
    cfg.halt_reason = None
    cfg.updated_at = datetime.now()
    session.commit()


def update_last_success_dt(session: Session, dt: datetime) -> None:
    """마지막 성공 윈도우 종료 시각 갱신(전체 성공 시에만 collector 가 호출)."""
    cfg = get_config(session)
    cfg.last_success_dt = dt
    cfg.updated_at = datetime.now()
    session.commit()


def update_pre_spec_last_success_dt(session: Session, dt: datetime) -> None:
    """사전규격 마지막 성공 윈도우 종료 시각 갱신(Phase 5.4).

    `update_last_success_dt` 와 동형이되 사전규격 전용 컬럼만 갱신한다. 사전규격 수집이
    전체 성공(success) 일 때만 pre_spec_collector 가 호출한다(입찰 last_success_dt 와 분리).
    """
    cfg = get_config(session)
    cfg.pre_spec_last_success_dt = dt
    cfg.updated_at = datetime.now()
    session.commit()


# --- 화면(/list·/config)용 조회 — Phase 3.5 -----------------------------
# /config 설정 편집에서 갱신을 허용하는 컬럼 화이트리스트(이 외 키는 무시).
_CONFIG_UPDATABLE: frozenset[str] = frozenset(
    {
        "enabled",
        # Phase 5.4: 사전규격 잡 독립 토글(/config 토글 UI 는 5.5에서 연결).
        "pre_spec_enabled",
        "interval_minutes",
        "window_overlap_minutes",
        "backfill_days",
        "num_of_rows",
        "max_retries",
        "inqry_div",
        "intrntnl_div_cd",
        "indstryty_cds",
        # Phase 4.3: 참가제한지역코드(수집 request 필터). ""=전체 / "00"=전국 / 그 외 특정 지역.
        "prtcpt_lmt_rgn_cd",
        # Phase 4.2: 추정가격 기본 하한/상한(/list 가격 입력의 기본값으로 사용).
        "presmpt_prce_bgn",
        "presmpt_prce_end",
        # Phase 4.9-R2-D: 사전규격 배정예산액 기본 범위(/pre-spec 가격 기본값).
        "pre_spec_amt_bgn",
        "pre_spec_amt_end",
    }
)

# /list 헤더 정렬 허용값 → (정렬 컬럼, 내림차순 여부). NULL 은 항상 뒤로.
_SORT_COLUMNS: dict[str, tuple[str, bool]] = {
    "bid_ntce_dt_desc": ("bid_ntce_dt", True),
    "bid_ntce_dt_asc": ("bid_ntce_dt", False),
    "openg_dt_desc": ("openg_dt", True),
    "openg_dt_asc": ("openg_dt", False),
    "presmpt_prce_desc": ("presmpt_prce", True),
    "presmpt_prce_asc": ("presmpt_prce", False),
}

# 날짜 범위를 적용할 컬럼 허용값(공고일/개찰일).
_DATE_FIELDS: frozenset[str] = frozenset({"bid_ntce_dt", "openg_dt"})

# /pre-spec 헤더 정렬 허용값 → (정렬 컬럼, 내림차순 여부). NULL 은 항상 뒤로.
_PRE_SPEC_SORT_COLUMNS: dict[str, tuple[str, bool]] = {
    "rcpt_dt_desc": ("rcpt_dt", True),
    "rcpt_dt_asc": ("rcpt_dt", False),
    "opnin_rgst_clse_dt_desc": ("opnin_rgst_clse_dt", True),
    "opnin_rgst_clse_dt_asc": ("opnin_rgst_clse_dt", False),
    "asign_bdgt_amt_desc": ("asign_bdgt_amt", True),
    "asign_bdgt_amt_asc": ("asign_bdgt_amt", False),
}


def search_bid_notices(
    session: Session,
    *,
    q: str | None = None,
    dt_from: datetime | None = None,
    dt_to: datetime | None = None,
    date_field: str = "bid_ntce_dt",
    price_min: int | None = None,
    price_max: int | None = None,
    openg_only_future: bool = False,
    include_past_openg: bool = True,
    sort: str = "bid_ntce_dt_desc",
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
) -> tuple[list[BidNotice], int]:
    """저장된 공고를 필터·정렬·페이지네이션해서 (행 목록, 전체건수)로 반환.

    - q: bid_ntce_nm 부분검색(LIKE %q%, 대소문자 무시).
    - dt_from/dt_to: 날짜 범위(둘 중 하나만 와도 처리). 화면에서 dt_to 는
      그 날 23:59:59 로 만들어 넘긴다(여기서는 받은 값 그대로 <= 비교).
    - date_field(Phase 4.2): 날짜 범위를 적용할 컬럼. "bid_ntce_dt"(공고일, 기본) 또는
      "openg_dt"(개찰일). 허용값 외는 기본(bid_ntce_dt)으로 폴백.
    - price_min/price_max(Phase 4.2): 추정가격(presmpt_prce) 범위. 있는 쪽만 적용
      (>= min / <= max). presmpt_prce 는 Numeric — 정수로 비교.
    - openg_only_future=True: openg_dt >= now(개찰 임박/미래만, NULL 제외). now 는 테스트
      결정성을 위해 주입 가능(기본 None → datetime.now()).
    - include_past_openg(기본 True=하위호환): False 면 개찰 지난 공고를 숨긴다 —
      `(openg_dt >= now) OR (openg_dt IS NULL)`. **개찰일 미정(NULL)은 아직 유효하므로 표시.**
      openg_only_future 보다 완화된 조건(NULL 포함)이며 /list 기본 동작이 이것이다.
    - sort(Phase 4.2 확장): _SORT_COLUMNS 의 6종 허용
      (bid_ntce_dt/openg_dt/presmpt_prce × asc/desc). NULL 은 항상 뒤로.
      기본·미허용값은 "bid_ntce_dt_desc"(최신 공고순).
    - page/page_size: LIMIT/OFFSET 페이지네이션. 전체건수는 동일 필터로 count.
    """
    # 날짜 범위를 적용할 컬럼(허용값 외는 기본 공고일).
    if date_field not in _DATE_FIELDS:
        date_field = "bid_ntce_dt"
    date_col = getattr(BidNotice, date_field)

    conditions = []
    if q:
        conditions.append(BidNotice.bid_ntce_nm.ilike(f"%{q}%"))
    if dt_from is not None:
        conditions.append(date_col >= dt_from)
    if dt_to is not None:
        conditions.append(date_col <= dt_to)
    if price_min is not None:
        conditions.append(BidNotice.presmpt_prce >= price_min)
    if price_max is not None:
        conditions.append(BidNotice.presmpt_prce <= price_max)
    if openg_only_future:
        if now is None:
            now = datetime.now()
        conditions.append(BidNotice.openg_dt >= now)
    if not include_past_openg:
        if now is None:
            now = datetime.now()
        # 개찰 지난 공고 숨김. 개찰일 미정(NULL)은 표시(아직 유효).
        conditions.append(
            or_(BidNotice.openg_dt >= now, BidNotice.openg_dt.is_(None))
        )

    count_stmt = select(func.count()).select_from(BidNotice)
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = int(session.execute(count_stmt).scalar_one())

    stmt = select(BidNotice)
    for cond in conditions:
        stmt = stmt.where(cond)

    # 정렬: 허용 6종 → (컬럼, 내림차순). NULL 은 항상 뒤로(is-null 플래그를 1차 키로, 이식성↑).
    sort_col_name, descending = _SORT_COLUMNS.get(
        sort, _SORT_COLUMNS["bid_ntce_dt_desc"]
    )
    sort_col = getattr(BidNotice, sort_col_name)
    direction = sort_col.desc() if descending else sort_col.asc()
    stmt = stmt.order_by(
        case((sort_col.is_(None), 1), else_=0),
        direction,
    )

    page = max(1, int(page))
    page_size = max(1, int(page_size))
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)

    rows = session.execute(stmt).scalars().all()
    return list(rows), total


def search_pre_specs(
    session: Session,
    *,
    q: str | None = None,            # prdct_clsfc_no_nm 부분검색
    instt: str | None = None,        # order_instt_nm OR rl_dminstt_nm 부분검색
    dt_from: datetime | None = None,  # rcpt_dt >= dt_from
    dt_to: datetime | None = None,   # rcpt_dt <= dt_to (화면에서 23:59:59 로 넘김)
    price_min: int | None = None,    # asign_bdgt_amt >= price_min (Phase 4.9-R2-D)
    price_max: int | None = None,    # asign_bdgt_amt <= price_max (Phase 4.9-R2-D)
    include_past_opnin: bool = True,  # False 면 의견마감 지난 항목 숨김(NULL 은 표시)
    sort: str = "rcpt_dt_desc",
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,     # 테스트 결정성 위해 주입 가능
) -> tuple[list[PreSpec], int]:
    """저장된 사전규격을 필터·정렬·페이지네이션해서 (행 목록, 전체건수)로 반환.

    `search_bid_notices` 와 동형(컬럼·조건만 다름):
    - q: prdct_clsfc_no_nm 부분검색(LIKE %q%, 대소문자 무시).
    - instt: order_instt_nm 또는 rl_dminstt_nm 부분검색(한 입력으로 두 컬럼 OR LIKE).
    - dt_from/dt_to: rcpt_dt(접수일시) 범위(둘 중 하나만 와도 처리). 화면에서 dt_to 는
      그 날 23:59:59 로 만들어 넘긴다(여기서는 받은 값 그대로 <= 비교).
    - price_min/price_max(Phase 4.9-R2-D): 배정예산액(asign_bdgt_amt) 범위. 있는 쪽만 적용
      (>= min / <= max). asign_bdgt_amt 는 Numeric — 정수로 비교. `search_bid_notices` 가격
      필터 패턴과 동형.
    - include_past_opnin(기본 True=전부): False 면 의견등록마감 지난 항목을 숨긴다 —
      `(opnin_rgst_clse_dt >= now) OR (opnin_rgst_clse_dt IS NULL)`.
      **마감일 미정(NULL)은 아직 유효하므로 항상 표시.** now 는 테스트 결정성을 위해
      주입 가능(기본 None → datetime.now()).
    - sort: _PRE_SPEC_SORT_COLUMNS 의 6종 허용
      (rcpt_dt/opnin_rgst_clse_dt/asign_bdgt_amt × asc/desc). NULL 은 항상 뒤로.
      기본·미허용값은 "rcpt_dt_desc"(최신 접수순).
    - page/page_size: LIMIT/OFFSET 페이지네이션. 전체건수는 동일 필터로 count.
    """
    conditions = []
    if q:
        conditions.append(PreSpec.prdct_clsfc_no_nm.ilike(f"%{q}%"))
    if instt:
        conditions.append(
            or_(
                PreSpec.order_instt_nm.ilike(f"%{instt}%"),
                PreSpec.rl_dminstt_nm.ilike(f"%{instt}%"),
            )
        )
    if dt_from is not None:
        conditions.append(PreSpec.rcpt_dt >= dt_from)
    if dt_to is not None:
        conditions.append(PreSpec.rcpt_dt <= dt_to)
    if price_min is not None:
        conditions.append(PreSpec.asign_bdgt_amt >= price_min)
    if price_max is not None:
        conditions.append(PreSpec.asign_bdgt_amt <= price_max)
    if not include_past_opnin:
        if now is None:
            now = datetime.now()
        # 의견마감 지난 항목 숨김. 마감일 미정(NULL)은 표시(아직 유효).
        conditions.append(
            or_(
                PreSpec.opnin_rgst_clse_dt >= now,
                PreSpec.opnin_rgst_clse_dt.is_(None),
            )
        )

    count_stmt = select(func.count()).select_from(PreSpec)
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = int(session.execute(count_stmt).scalar_one())

    stmt = select(PreSpec)
    for cond in conditions:
        stmt = stmt.where(cond)

    # 정렬: 허용 6종 → (컬럼, 내림차순). NULL 은 항상 뒤로(is-null 플래그를 1차 키로, 이식성↑).
    sort_col_name, descending = _PRE_SPEC_SORT_COLUMNS.get(
        sort, _PRE_SPEC_SORT_COLUMNS["rcpt_dt_desc"]
    )
    sort_col = getattr(PreSpec, sort_col_name)
    direction = sort_col.desc() if descending else sort_col.asc()
    stmt = stmt.order_by(
        case((sort_col.is_(None), 1), else_=0),
        direction,
    )

    page = max(1, int(page))
    page_size = max(1, int(page_size))
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)

    rows = session.execute(stmt).scalars().all()
    return list(rows), total


def update_config(session: Session, **fields: Any) -> AppConfig:
    """허용 컬럼(_CONFIG_UPDATABLE)만 갱신 + updated_at. 비허용 키는 무시하고 commit."""
    cfg = get_config(session)
    for key, value in fields.items():
        if key in _CONFIG_UPDATABLE:
            setattr(cfg, key, value)
    cfg.updated_at = datetime.now()
    session.commit()
    return cfg


def list_recent_runs(session: Session, limit: int = 20) -> list[CollectionRun]:
    """최근 실행 이력을 id DESC 로 limit 건 반환(화면 표시용)."""
    stmt = select(CollectionRun).order_by(CollectionRun.id.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def get_pre_spec_files(session: Session, bf_spec_rgst_no: str) -> list[dict[str, Any]]:
    """저장된 사전규격의 첨부 파일 목록을 반환(Phase 4.9-B2 파일 다운로드용).

    - `spec_doc_file_url{i}`(i=1..5)에 URL 이 있으면 첨부 1건으로 본다.
    - 파일명 컬럼이 없으므로 name 은 `첨부{i}` 폴백.
    - 사전규격이 없거나 첨부가 없으면 빈 리스트.
    반환: [{"idx": i, "name": ..., "url": ...}, ...]
    """
    spec = session.get(PreSpec, bf_spec_rgst_no)
    if spec is None:
        return []
    files: list[dict[str, Any]] = []
    for i in range(1, 6):
        url = getattr(spec, f"spec_doc_file_url{i}", None)
        if not url:
            continue
        files.append({"idx": i, "name": f"첨부{i}", "url": url})
    return files


def get_notice_files(session: Session, bid_ntce_no: str) -> list[dict[str, Any]]:
    """저장된 공고의 첨부 규격서 목록을 반환(Phase 4.1 파일 다운로드용).

    - `ntce_spec_doc_url{i}`(i=1..10)에 URL 이 있으면 첨부 1건으로 본다.
    - name 은 `ntce_spec_file_nm{i}`, 비어 있으면 `첨부{i}` 로 폴백.
    - 공고가 없거나 첨부가 없으면 빈 리스트.
    반환: [{"idx": i, "name": ..., "url": ...}, ...]
    """
    notice = session.get(BidNotice, bid_ntce_no)
    if notice is None:
        return []
    files: list[dict[str, Any]] = []
    for i in range(1, 11):
        url = getattr(notice, f"ntce_spec_doc_url{i}", None)
        if not url:
            continue
        name = getattr(notice, f"ntce_spec_file_nm{i}", None) or f"첨부{i}"
        files.append({"idx": i, "name": name, "url": url})
    return files
