"""DB 입출력 계층 — Phase 3.3.

collector 가 사용하는 저장소(repository). 정책 판단(재시도/halt/상태 결정)은 collector 가
담당하고, 이 모듈은 **순수 DB 입출력**만 책임진다: 설정 조회, 실행이력 기록,
bid_notice upsert, halt 플래그·last_success_dt 갱신.

- 세션은 호출자가 주입한다(`session: Session`). 모듈이 세션을 만들지 않는다.
- upsert 는 DB별 기능(ON CONFLICT 등)에 의존하지 않고 표준 select→insert/update 로 구현한다
  (PostgreSQL 이전 대비).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.models import (
    AnalysisResult,
    AppConfig,
    BidLicenseLimit,
    BidNotice,
    BidNoticeRaw,
    CollectionRun,
    ExcludeKeyword,
    ExportCartItem,
    ListRule,
    PreSpec,
)

# bid_notice 저장 컬럼 — 원문은 별도 테이블로 보내고 컬럼이 아닌 키는 무시한다.
_BID_NOTICE_COLUMNS: frozenset[str] = frozenset(
    c.name for c in BidNotice.__table__.columns if c.name != "raw_json"
)

# pre_spec 에 실제 존재하는 컬럼명 집합(upsert_pre_specs 화이트리스트).
_PRE_SPEC_COLUMNS: frozenset[str] = frozenset(
    c.name for c in PreSpec.__table__.columns
)


def _utcnow() -> datetime:
    """DB 저장용 naive UTC 시각."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    - 각 원소는 transform 결과 + 수집 시각(collected_at/updated_at)이 합쳐진 컬럼값 dict.
      matched_indstryty_cds 등 분류값은 분류 엔진이 갱신한다.
    - 공고·원문 각각 기존 PK 를 `IN` 한 번으로 조회해 N+1 을 피한다.
      raw_json 은 같은 세션의 bid_notice_raw 에만 저장한다.
    - 신규 행은 list_visible 이 없으면 False 로 시작하며, 기존 분류값은 없는 키로 덮지 않는다.
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
    existing_raw: dict[str, BidNoticeRaw] = {}
    if pks:
        rows = (
            session.execute(select(BidNotice).where(BidNotice.bid_ntce_no.in_(pks)))
            .scalars()
            .all()
        )
        existing = {r.bid_ntce_no: r for r in rows}
        raw_rows = session.execute(
            select(BidNoticeRaw).where(BidNoticeRaw.bid_ntce_no.in_(pks))
        ).scalars().all()
        existing_raw = {r.bid_ntce_no: r for r in raw_rows}

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
            insert_values = {k: v for k, v in values.items() if k in _BID_NOTICE_COLUMNS}
            insert_values.setdefault("list_visible", False)
            obj = BidNotice(**insert_values)
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
            if "raw_json" in values:
                row.raw_json = None  # 원문을 분리 저장하는 경우에만 호환 컬럼을 비운다.
            updated_count += 1

        if "raw_json" in values:
            raw = existing_raw.get(pk)
            if raw is None:
                raw = BidNoticeRaw(bid_ntce_no=pk)
                session.add(raw)
                existing_raw[pk] = raw  # 같은 배치 중복 PK 는 갱신으로 흡수.
            raw.raw_json = values["raw_json"]
            raw.updated_at = values["updated_at"]

    session.flush()
    return (new_count, updated_count, ord_changes)


# --- 면허제한 upsert ----------------------------------------------------
def upsert_license_limits(
    session: Session, rows: list[dict[str, Any]], collected_at: datetime,
) -> tuple[int, int]:
    """면허 복합 PK 를 묶음 IN 조회해 upsert 하고 flush 만 한다(삭제 없음).

    같은 배치의 중복 PK 는 마지막 값으로 흡수하며 최초 collected_at 은 보존한다.
    """
    pk_names = ("bid_ntce_no", "bid_ntce_ord", "lmt_grp_no", "lmt_sno")
    deduped = {tuple(row[name] for name in pk_names): row for row in rows}
    pks = list(deduped)
    existing = {}
    pk_columns = tuple_(*(getattr(BidLicenseLimit, name) for name in pk_names))
    # 복합 PK 4개 × 200행으로 SQLite 바인드 수에도 여유를 둔다.
    for start in range(0, len(pks), 200):
        for row in session.scalars(select(BidLicenseLimit).where(
            pk_columns.in_(pks[start:start + 200])
        )):
            existing[tuple(getattr(row, name) for name in pk_names)] = row
    inserted = updated = 0
    columns = {column.name for column in BidLicenseLimit.__table__.columns}
    for pk, values in deduped.items():
        data = {key: value for key, value in values.items()
                if key in columns and key != "collected_at"}
        row = existing.get(pk)
        if row is None:
            session.add(BidLicenseLimit(**data, collected_at=collected_at))
            inserted += 1
        else:
            for key, value in data.items():
                setattr(row, key, value)
            updated += 1
    session.flush()
    return inserted, updated


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
        # 입찰공고 배정예산 기본 하한/상한(/list 가격 입력의 기본값으로 사용).
        "presmpt_prce_bgn",
        "presmpt_prce_end",
        # Phase 4.9-R2-D: 사전규격 배정예산액 기본 범위(/pre-spec 가격 기본값).
        "pre_spec_amt_bgn",
        "pre_spec_amt_end",
    }
)

# /list 정렬 허용값 → (정렬 컬럼, 내림차순 여부). NULL 은 항상 뒤로.
_SORT_COLUMNS: dict[str, tuple[str, bool]] = {
    "bid_ntce_dt_desc": ("bid_ntce_dt", True),
    "bid_ntce_dt_asc": ("bid_ntce_dt", False),
    "bid_begin_dt_desc": ("bid_begin_dt", True),
    "bid_begin_dt_asc": ("bid_begin_dt", False),
    "openg_dt_desc": ("openg_dt", True),
    "openg_dt_asc": ("openg_dt", False),
    "bid_clse_dt_desc": ("bid_clse_dt", True),
    "bid_clse_dt_asc": ("bid_clse_dt", False),
    "asign_bdgt_amt_desc": ("asign_bdgt_amt", True),
    "asign_bdgt_amt_asc": ("asign_bdgt_amt", False),
    # 추정가격은 목록 컬럼에서 제외했지만 기존 북마크 URL 호환을 위해 유지.
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


# --- 공고명 제외 키워드(Phase 10) 공용 헬퍼 ------------------------------
# 한 검색에 적용 가능한 제외 키워드 최대 개수(과도한 조건 누적 방지).
EXCLUDE_KEYWORD_MAX = 20
# 키워드 최대 길이(모델 String(50) 과 일치).
EXCLUDE_KEYWORD_MAX_LEN = 50


def normalize_keyword(raw: Any) -> str:
    """입력 키워드 정규화 — 콤마 제거(CSV 구분자) + 공백 정리 + 길이 제한.

    콤마는 화면 파라미터(`excl_kw`)의 구분자라 단어에 포함될 수 없어 공백으로 치환하고,
    연속 공백은 1칸으로 접는다(눈에 안 보이는 공백 차이로 매칭이 어긋나지 않게).
    """
    if raw is None:
        return ""
    text = " ".join(str(raw).replace(",", " ").split())
    return text[:EXCLUDE_KEYWORD_MAX_LEN]


def _like_escape(value: str) -> str:
    """LIKE 패턴 특수문자(`\\` `%` `_`)를 이스케이프한다(제외 조건 과잉매칭 방지)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exclude_keyword_conditions(column: Any, keywords: list[str] | None) -> list[Any]:
    """제외 키워드 목록 → `(col IS NULL) OR (col NOT LIKE %kw%)` 조건 목록.

    - 대소문자 무시 부분일치(ilike)로 매칭되는 행을 제외한다(AND 누적 = 모두 제외).
    - 이름이 NULL 인 행은 데이터 누락일 뿐이므로 제외하지 않는다(기존 필터 관례와 동일).
    - 빈 문자열·중복은 무시하고, 최대 EXCLUDE_KEYWORD_MAX 개까지만 적용한다.
    """
    conds: list[Any] = []
    seen: set[str] = set()
    for raw in keywords or []:
        kw = normalize_keyword(raw)
        if not kw or kw in seen:
            continue
        seen.add(kw)
        pattern = f"%{_like_escape(kw)}%"
        conds.append(
            or_(column.is_(None), ~column.ilike(pattern, escape="\\"))
        )
        if len(conds) >= EXCLUDE_KEYWORD_MAX:
            break
    return conds


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
    exclude_privt_cntrct: bool = False,
    exclude_keywords: list[str] | None = None,
    ntce_kinds: list[str] | None = None,
    sort: str = "bid_ntce_dt_desc",
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
    visible_only: bool = True,
) -> tuple[list[BidNotice], int]:
    """저장된 공고를 필터·정렬·페이지네이션해서 (행 목록, 전체건수)로 반환.

    - visible_only=True: 목록 표시 대상(list_visible=True)만 건수·행 조회에 포함.
    - q: bid_ntce_nm 부분검색(LIKE %q%, 대소문자 무시).
    - dt_from/dt_to: 날짜 범위(둘 중 하나만 와도 처리). 화면에서 dt_to 는
      그 날 23:59:59 로 만들어 넘긴다(여기서는 받은 값 그대로 <= 비교).
    - date_field(Phase 4.2): 날짜 범위를 적용할 컬럼. "bid_ntce_dt"(공고일, 기본) 또는
      "openg_dt"(개찰일). 허용값 외는 기본(bid_ntce_dt)으로 폴백.
    - price_min/price_max: 배정예산(asign_bdgt_amt) 범위. 있는 쪽만 적용
      (>= min / <= max). asign_bdgt_amt 는 Numeric — 정수로 비교.
    - openg_only_future=True: openg_dt >= now(개찰 임박/미래만, NULL 제외). now 는 테스트
      결정성을 위해 주입 가능(기본 None → datetime.now()).
    - include_past_openg(기본 True=하위호환): False 면 개찰 지난 공고를 숨긴다 —
      `(openg_dt >= now) OR (openg_dt IS NULL)`. **개찰일 미정(NULL)은 아직 유효하므로 표시.**
      openg_only_future 보다 완화된 조건(NULL 포함)이며 /list 기본 동작이 이것이다.
    - exclude_privt_cntrct=True: 계약체결방법명(cntrct_cncls_mthd_nm)이 "수의계약"인
      공고를 숨긴다. 방법 미상(NULL)은 데이터 누락일 뿐이므로 숨기지 않는다.
      /list 화면의 "수의계약제외" 체크박스(기본 체크)가 이 플래그를 넘긴다.
    - exclude_keywords(Phase 10): 공고명(bid_ntce_nm)에 해당 단어가 포함된 공고를 숨긴다
      (부분일치·대소문자 무시, 여러 개면 모두 제외). 공고명 NULL 은 숨기지 않는다.
    - ntce_kinds: 선택한 공고종류(ntce_kind_nm)만 표시하되 NULL 은 숨기지 않는다.
      None/빈 목록이면 공고종류 조건을 적용하지 않는다(기존 호출부 호환).
    - sort: _SORT_COLUMNS 의 12종 허용
      (bid_ntce_dt/bid_begin_dt/bid_clse_dt/openg_dt/asign_bdgt_amt/presmpt_prce × asc/desc).
      NULL 은 항상 뒤로. 추정가격 정렬은 기존 북마크 URL 호환을 위해 유지.
      기본·미허용값은 "bid_ntce_dt_desc"(최신 공고순).
    - page/page_size: LIMIT/OFFSET 페이지네이션. 전체건수는 동일 필터로 count.
    """
    # 날짜 범위를 적용할 컬럼(허용값 외는 기본 공고일).
    if date_field not in _DATE_FIELDS:
        date_field = "bid_ntce_dt"
    date_col = getattr(BidNotice, date_field)

    conditions = []
    if visible_only:
        conditions.append(BidNotice.list_visible.is_(True))
    if q:
        conditions.append(BidNotice.bid_ntce_nm.ilike(f"%{q}%"))
    if dt_from is not None:
        conditions.append(date_col >= dt_from)
    if dt_to is not None:
        conditions.append(date_col <= dt_to)
    if price_min is not None:
        conditions.append(BidNotice.asign_bdgt_amt >= price_min)
    if price_max is not None:
        conditions.append(BidNotice.asign_bdgt_amt <= price_max)
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
    if exclude_privt_cntrct:
        # 수의계약 숨김. 계약방법 미상(NULL)은 표시(데이터 누락을 필터로 오인하지 않게).
        conditions.append(
            or_(
                BidNotice.cntrct_cncls_mthd_nm.is_(None),
                BidNotice.cntrct_cncls_mthd_nm != "수의계약",
            )
        )
    # 공고명 제외 키워드(Phase 10) — 여러 개면 AND 로 모두 제외.
    conditions.extend(_exclude_keyword_conditions(BidNotice.bid_ntce_nm, exclude_keywords))
    if ntce_kinds:
        conditions.append(
            or_(BidNotice.ntce_kind_nm.in_(ntce_kinds), BidNotice.ntce_kind_nm.is_(None))
        )

    count_stmt = select(func.count()).select_from(BidNotice)
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = int(session.execute(count_stmt).scalar_one())

    stmt = select(BidNotice)
    for cond in conditions:
        stmt = stmt.where(cond)

    # 정렬: 허용 12종 → (컬럼, 내림차순). NULL 은 항상 뒤로(is-null 플래그를 1차 키로, 이식성↑).
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
    exclude_keywords: list[str] | None = None,  # 품명/사업명 제외 키워드(Phase 10)
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
    - exclude_keywords(Phase 10): 품명/사업명(prdct_clsfc_no_nm)에 해당 단어가 포함된
      항목을 숨긴다(부분일치·대소문자 무시, 여러 개면 모두 제외). NULL 은 숨기지 않는다.
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
    # 품명/사업명 제외 키워드(Phase 10) — /list 와 동일 규칙.
    conditions.extend(
        _exclude_keyword_conditions(PreSpec.prdct_clsfc_no_nm, exclude_keywords)
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


def update_config(session: Session, *, commit: bool = True, **fields: Any) -> AppConfig:
    """허용 컬럼만 갱신 + updated_at. 기본 커밋, 재분류와 묶을 때는 커밋을 미룬다."""
    cfg = get_config(session)
    for key, value in fields.items():
        if key in _CONFIG_UPDATABLE:
            setattr(cfg, key, value)
    cfg.updated_at = datetime.now()
    if commit:
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


# --- 분석 결과 영속화 (Phase 8.1) -----------------------------------------

def get_analysis(
    session: Session,
    source_type: str,
    source_id: str,
) -> AnalysisResult | None:
    """분석 결과 행 1건 조회. 없으면 None."""
    stmt = select(AnalysisResult).where(
        AnalysisResult.source_type == source_type,
        AnalysisResult.source_id == source_id,
    )
    return session.execute(stmt).scalar_one_or_none()


def start_analysis(
    session: Session,
    source_type: str,
    source_id: str,
    source_kind: str,
) -> None:
    """분석 시작 상태로 upsert 한다. 기존 완료/오류 결과는 새 분석으로 덮는다."""
    now = _utcnow()
    row = get_analysis(session, source_type, source_id)
    if row is None:
        row = AnalysisResult(
            source_type=source_type,
            source_id=source_id,
            status="analyzing",
            result_json=None,
            error_message=None,
            source_kind=source_kind,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "analyzing"
        row.result_json = None
        row.error_message = None
        row.source_kind = source_kind
        row.updated_at = now
    session.commit()


def set_analysis_done(
    session: Session,
    source_type: str,
    source_id: str,
    result_json: str,
) -> None:
    """분석 성공 결과 JSON을 저장하고 done 상태로 전환한다."""
    now = _utcnow()
    row = get_analysis(session, source_type, source_id)
    if row is None:
        row = AnalysisResult(
            source_type=source_type,
            source_id=source_id,
            status="done",
            result_json=result_json,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "done"
        row.result_json = result_json
        row.error_message = None
        row.updated_at = now
    session.commit()


def set_analysis_error(
    session: Session,
    source_type: str,
    source_id: str,
    message: str,
) -> None:
    """분석 실패 메시지를 저장하고 error 상태로 전환한다."""
    now = _utcnow()
    row = get_analysis(session, source_type, source_id)
    if row is None:
        row = AnalysisResult(
            source_type=source_type,
            source_id=source_id,
            status="error",
            result_json=None,
            error_message=message,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "error"
        row.error_message = message
        row.updated_at = now
    session.commit()


def reset_stale_analyzing(session: Session) -> int:
    """기동 시 남은 analyzing 행을 재시작 중단 오류로 정리하고 개수를 반환한다."""
    rows = list(
        session.execute(
            select(AnalysisResult).where(AnalysisResult.status == "analyzing")
        ).scalars()
    )
    now = _utcnow()
    for row in rows:
        row.status = "error"
        row.error_message = "서버 재시작으로 중단됨"
        row.updated_at = now
    session.commit()
    return len(rows)


def get_analysis_status_map(
    session: Session,
    source_type: str,
    source_ids: list[str],
) -> dict[str, str]:
    """목록 렌더용 상태 맵. source_ids 전체를 한 번의 SELECT로 조회한다."""
    if not source_ids:
        return {}
    stmt = select(AnalysisResult.source_id, AnalysisResult.status).where(
        AnalysisResult.source_type == source_type,
        AnalysisResult.source_id.in_(source_ids),
    )
    return {source_id: status for source_id, status in session.execute(stmt).all()}


# --- Phase 10: 공고명 제외 키워드 목록 관리 --------------------------------

def list_exclude_keywords(session: Session) -> list[dict[str, Any]]:
    """등록된 제외 키워드 전체 — [{id, keyword, is_default}] (기본적용 우선 · 가나다순)."""
    stmt = select(ExcludeKeyword).order_by(
        ExcludeKeyword.is_default.desc(), ExcludeKeyword.keyword.asc()
    )
    rows = session.execute(stmt).scalars().all()
    return [
        {"id": r.id, "keyword": r.keyword, "is_default": bool(r.is_default)}
        for r in rows
    ]


def get_default_exclude_keywords(session: Session) -> list[str]:
    """기본 적용(is_default=True) 키워드 문자열 목록 — 목록 화면 첫 진입 기본값."""
    stmt = (
        select(ExcludeKeyword.keyword)
        .where(ExcludeKeyword.is_default.is_(True))
        .order_by(ExcludeKeyword.keyword.asc())
    )
    return [k for (k,) in session.execute(stmt).all()]


def add_exclude_keyword(
    session: Session, keyword: str, *, is_default: bool = False
) -> bool:
    """키워드 1건 추가. 빈 값·중복(대소문자 무시)이면 저장하지 않고 False 를 반환한다."""
    kw = normalize_keyword(keyword)
    if not kw:
        return False
    exists = session.execute(
        select(ExcludeKeyword.id).where(func.lower(ExcludeKeyword.keyword) == kw.lower())
    ).first()
    if exists:
        return False
    session.add(ExcludeKeyword(keyword=kw, is_default=is_default, created_at=_utcnow()))
    session.commit()
    return True


def delete_exclude_keyword(session: Session, keyword_id: int) -> bool:
    """키워드 1건 삭제. 없으면 False."""
    row = session.get(ExcludeKeyword, keyword_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def set_default_exclude_keywords(session: Session, keyword_ids: list[int]) -> None:
    """기본 적용 체크 상태 일괄 반영 — 전달된 id 만 True, 나머지는 False."""
    wanted = {int(i) for i in keyword_ids}
    rows = session.execute(select(ExcludeKeyword)).scalars().all()
    for row in rows:
        row.is_default = row.id in wanted
    session.commit()


# --- 목록 표시 규칙 ------------------------------------------------------

def list_list_rules(session: Session) -> list[dict[str, Any]]:
    """종류별·id 순으로 목록 표시 규칙 전체를 반환한다."""
    rows = session.scalars(select(ListRule).order_by(ListRule.rule_type, ListRule.id))
    return [
        {"id": row.id, "rule_type": row.rule_type, "value": row.value,
         "enabled": bool(row.enabled)}
        for row in rows
    ]


def normalize_rule_value(raw: str) -> str:
    """규칙 값의 앞뒤·연속 공백을 정규화한다."""
    return " ".join(raw.split())


def find_list_rule_duplicate(session: Session, rule_type: str, value: str) -> str | None:
    """같은 종류에서 중복인 기존 값을 반환한다(키워드·제외어는 대소문자·공백 무시)."""
    def key(raw: str) -> str:
        normalized = normalize_rule_value(raw)
        return normalized if rule_type == "clsfc" else "".join(normalized.split()).lower()

    wanted = key(value)
    for existing in session.scalars(select(ListRule.value).where(ListRule.rule_type == rule_type)):
        if key(existing) == wanted:
            return existing
    return None


def add_list_rule(
    session: Session, rule_type: str, value: str, *, commit: bool = True,
) -> bool:
    """공백을 정규화해 규칙을 추가한다. 같은 종류의 중복이면 False."""
    if rule_type not in {"clsfc", "keyword", "negative"}:
        raise ValueError("알 수 없는 규칙 종류입니다.")
    value = normalize_rule_value(value)
    if not value:
        raise ValueError("추가할 규칙 값을 입력하세요.")
    if len(value) > 100:
        raise ValueError("규칙 값은 100자 이하여야 합니다.")
    if find_list_rule_duplicate(session, rule_type, value) is not None:
        return False
    session.add(ListRule(rule_type=rule_type, value=value, enabled=True))
    if commit:
        session.commit()
    return True


def delete_list_rule(session: Session, rule_id: int, *, commit: bool = True) -> bool:
    """규칙 1건 삭제. 없으면 False."""
    row = session.get(ListRule, rule_id)
    if row is None:
        return False
    session.delete(row)
    if commit:
        session.commit()
    return True


def set_list_rules_enabled(
    session: Session, rule_ids: list[int], enabled_ids: list[int], *, commit: bool = True,
) -> None:
    """화면에 렌더된 규칙만 체크 상태를 저장한다(나중에 추가된 규칙은 유지)."""
    wanted = set(enabled_ids)
    for row in session.scalars(select(ListRule).where(ListRule.id.in_(rule_ids))):
        row.enabled = row.id in wanted
    if commit:
        session.commit()


def list_clsfc_names(session: Session, limit: int = 200) -> list[str]:
    """저장된 공공조달분류명을 공고 수 내림차순으로 제안한다."""
    name = BidNotice.pub_prcrmnt_clsfc_nm
    stmt = (select(name).where(name.is_not(None), func.trim(name) != "")
            .group_by(name).order_by(func.count().desc(), name.asc()).limit(limit))
    return list(session.scalars(stmt))


# --- Phase 7.1: 검토 목록 장바구니 -----------------------------------------

def get_export_cart(session: Session) -> list[dict[str, Any]]:
    """장바구니 전체 조회. item_type+item_id로 BidNotice/PreSpec 메타 조인."""
    items = session.query(ExportCartItem).order_by(ExportCartItem.added_at).all()
    result = []
    for item in items:
        if item.item_type == "bid":
            notice = session.get(BidNotice, item.item_id)
            if notice:
                result.append({
                    "cart_id": item.id,
                    "item_type": "bid",
                    "item_id": item.item_id,
                    "title": notice.bid_ntce_nm or item.item_id,
                    "ntce_instt_nm": notice.ntce_instt_nm,
                    "dminstt_nm": notice.dminstt_nm,
                    "asign_bdgt_amt": notice.asign_bdgt_amt,
                    "presmpt_prce": notice.presmpt_prce,
                    "start_dt": notice.bid_ntce_dt,
                    "close_dt": notice.openg_dt,
                    "detail_url": notice.bid_ntce_dtl_url,
                    "file_count": len(get_notice_files(session, item.item_id)),
                })
        else:
            spec = session.get(PreSpec, item.item_id)
            if spec:
                result.append({
                    "cart_id": item.id,
                    "item_type": "pre_spec",
                    "item_id": item.item_id,
                    "title": spec.prdct_clsfc_no_nm or item.item_id,
                    "ntce_instt_nm": spec.order_instt_nm,
                    "dminstt_nm": spec.rl_dminstt_nm,
                    "asign_bdgt_amt": spec.asign_bdgt_amt,
                    "presmpt_prce": None,
                    "start_dt": spec.rcpt_dt,
                    "close_dt": spec.opnin_rgst_clse_dt,
                    "detail_url": None,
                    "file_count": len(get_pre_spec_files(session, item.item_id)),
                })
    return result


def add_export_cart_items(session: Session, items: list[dict[str, Any]]) -> int:
    """items: [{"item_type": "bid"|"pre_spec", "item_id": "..."}]. 중복 무시. 추가 건수 반환."""
    added = 0
    for it in items:
        exists = session.query(ExportCartItem).filter_by(
            item_type=it["item_type"], item_id=it["item_id"]
        ).first()
        if not exists:
            session.add(ExportCartItem(item_type=it["item_type"], item_id=it["item_id"]))
            added += 1
    session.commit()
    return added


def delete_export_cart_item(session: Session, cart_id: int) -> bool:
    """cart_id로 개별 삭제. 성공 True."""
    item = session.get(ExportCartItem, cart_id)
    if item:
        session.delete(item)
        session.commit()
        return True
    return False


def clear_export_cart(session: Session) -> None:
    """전체 비우기."""
    session.query(ExportCartItem).delete()
    session.commit()


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
