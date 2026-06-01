"""수집기 — Phase 3.3.

12번 용역 검색조건 조회(`getBidPblancListInfoServcPPSSrch`)를 업종코드 4종에 대해
**동시 병렬 호출 → 끝까지 페이징 → 병합·dedup(bidNtceNo) → transform → upsert →
collection_run 기록**까지 수행한다. 재시도/halt 정책(계획서 §2.2)을 적용한다.

이 단계의 collector 는 **윈도우(시작·종료 datetime)와 trigger 를 인자로 받는다.**
윈도우 자동 산정(last_success_dt 기반)·주기 실행은 3.4(scheduler), 화면은 3.5.

설계 메모:
- api_client.call_endpoint 는 동기(httpx.get)이므로 4코드 병렬은 ThreadPoolExecutor 로 한다.
- **워커(스레드)는 API 호출·페이징·item 수집만** 하고 DB 는 건드리지 않는다.
  transform·upsert·run·config 등 DB 쓰기는 병합 후 **메인 스레드 단일 세션**에서 처리한다.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import api_client, repository
from app.db import SessionLocal, init_db
from app.models import CollectionRun
from app.transform import item_to_bid_notice_values

logger = logging.getLogger(__name__)

# 12번 용역 검색조건 조회.
OPERATION = "getBidPblancListInfoServcPPSSrch"

# resultCode 분류(계획서 §2.2).
_OK_CODES: frozenset[str] = frozenset({"00", "03"})
_RETRY_CODES: frozenset[str] = frozenset({"01", "02", "04", "05"})

# 일시 장애 재시도 백오프(초). 잔여 미결(계획서 §9) → 짧은 선형 백오프 기본값.
_RETRY_BACKOFF_SECONDS = 2.0

# 페이징 안전장치(무한 루프 방지). 초과 시 로그로 알린다.
_MAX_PAGES_SAFETY = 1000


# --- 순수 헬퍼(네트워크/DB 비의존) --------------------------------------
def classify_result_code(code: str | None) -> str:
    """resultCode → 'ok' | 'retry' | 'halt'.

    - 00/03 → ok(성공, 03은 No Data)
    - 01/02/04/05 → retry(일시 장애)
    - None(HTTP/파싱 실패) → retry 에 준함
    - 그 외(06/07/08/10/11/12/20/22/30/31/32 등) → halt(사람 확인 필요)
    """
    if code in _OK_CODES:
        return "ok"
    if code is None or code in _RETRY_CODES:
        return "retry"
    return "halt"


def total_pages(total_count: Any, num_of_rows: int) -> int:
    """전체 건수/페이지크기로 전체 페이지 수 계산. 0건·비정상 입력 → 0."""
    try:
        tc = int(total_count)
    except (TypeError, ValueError):
        tc = 0
    if tc <= 0 or num_of_rows <= 0:
        return 0
    return (tc + num_of_rows - 1) // num_of_rows


def merge_and_dedup(
    results_by_cd: dict[str, list[dict]],
) -> tuple[list[dict], dict[str, str]]:
    """업종코드별 item 목록을 합치고 bidNtceNo 로 중복 제거.

    반환: (dedup된 item 리스트, matched_map)
      - matched_map[bidNtceNo] = 그 공고를 잡은 업종코드 합집합 CSV(정렬), 예: "1468,1470".
      - 같은 공고는 처음 본 item 을 유지한다.
    """
    deduped: dict[str, dict] = {}
    matched: dict[str, set[str]] = {}
    for cd, items in results_by_cd.items():
        for item in items:
            raw_no = item.get("bidNtceNo") if isinstance(item, dict) else None
            no = str(raw_no).strip() if raw_no is not None else ""
            if not no:
                continue
            if no not in deduped:
                deduped[no] = item
            matched.setdefault(no, set()).add(str(cd))

    deduped_items = list(deduped.values())
    matched_map = {no: ",".join(sorted(cds)) for no, cds in matched.items()}
    return deduped_items, matched_map


def fmt_dt(dt: datetime) -> str:
    """datetime → 'YYYYMMDDHHMM'(12자리, 초 없음). API 조회일시 형식."""
    return dt.strftime("%Y%m%d%H%M")


# --- 워커(스레드): 업종코드 1개를 끝까지 페이징 -------------------------
@dataclass(frozen=True)
class _ConfigSnapshot:
    """스레드에 넘기는 설정값 스냅샷(ORM 객체를 스레드 간 공유하지 않기 위함)."""

    inqry_div: str
    intrntnl_div_cd: str | None
    num_of_rows: int
    max_retries: int


def _call_with_retry(
    raw_params: dict[str, str], max_retries: int
) -> tuple[api_client.ApiResult, int, str]:
    """페이지 1건 호출 + 재시도. 반환: (마지막 ApiResult, 사용한 재시도 수, outcome).

    outcome ∈ {"ok","failed","halt"}. failed = 재시도 소진.
    """
    used_retries = 0
    while True:
        result = api_client.call_endpoint(OPERATION, raw_params, response_type="json")
        cls = classify_result_code(result.result_code)
        if cls == "ok":
            return result, used_retries, "ok"
        if cls == "halt":
            return result, used_retries, "halt"
        # retry
        if used_retries >= max_retries:
            return result, used_retries, "failed"
        used_retries += 1
        time.sleep(_RETRY_BACKOFF_SECONDS * used_retries)  # 선형 백오프


def _fetch_cd(
    cd: str,
    window_bgn: datetime,
    window_end: datetime,
    config: _ConfigSnapshot,
) -> dict[str, Any]:
    """업종코드 1개를 끝까지 페이징해 item 을 모은다. DB 는 건드리지 않는다.

    반환 dict: cd, items, pages, last_code, retry_count, outcome, halt_code, error_msg.
      outcome ∈ {"ok","failed","halt"}.
    """
    base_params: dict[str, str] = {
        "inqryDiv": config.inqry_div,
        "intrntnlDivCd": config.intrntnl_div_cd or "",  # 빈값이면 build_params 가 제거(=전체)
        "indstrytyCd": cd,
        "numOfRows": str(config.num_of_rows),
        "inqryBgnDt": fmt_dt(window_bgn),
        "inqryEndDt": fmt_dt(window_end),
    }

    items: list[dict] = []
    retry_count = 0
    pages_fetched = 0
    last_code: str | None = None
    max_page: int | None = None
    page = 1

    try:
        while True:
            params = {**base_params, "pageNo": str(page)}
            result, used_retries, outcome = _call_with_retry(params, config.max_retries)
            retry_count += used_retries
            last_code = result.result_code

            if outcome == "halt":
                halt_code = result.result_code
                return {
                    "cd": cd,
                    "items": items,
                    "pages": pages_fetched,
                    "last_code": last_code,
                    "retry_count": retry_count,
                    "outcome": "halt",
                    "halt_code": halt_code,
                    "error_msg": f"resultCode={halt_code} "
                    f"({api_client.ERROR_CODES.get(halt_code or '', '알 수 없는 코드')})",
                }
            if outcome == "failed":
                return {
                    "cd": cd,
                    "items": items,
                    "pages": pages_fetched,
                    "last_code": last_code,
                    "retry_count": retry_count,
                    "outcome": "failed",
                    "halt_code": None,
                    "error_msg": result.error
                    or f"일시 장애 재시도 소진(resultCode={last_code})",
                }

            # outcome == "ok"
            pages_fetched += 1
            if result.result_code == "03":  # No Data → 종료
                break
            items.extend(result.items)

            if max_page is None:
                max_page = total_pages(result.total_count, config.num_of_rows)

            if page >= max_page:
                break
            if page >= _MAX_PAGES_SAFETY:
                logger.warning(
                    "업종코드 %s: 안전 상한(%d페이지) 도달 — 페이징 중단(total_count=%s)",
                    cd,
                    _MAX_PAGES_SAFETY,
                    result.total_count,
                )
                break
            page += 1
    except api_client.ApiClientError as exc:
        # 파라미터/키 등 클라이언트 측 오류 — 일시 장애로 보지 않고 실패 처리.
        return {
            "cd": cd,
            "items": items,
            "pages": pages_fetched,
            "last_code": last_code,
            "retry_count": retry_count,
            "outcome": "failed",
            "halt_code": None,
            "error_msg": f"ApiClientError: {exc}",
        }

    return {
        "cd": cd,
        "items": items,
        "pages": pages_fetched,
        "last_code": last_code,
        "retry_count": retry_count,
        "outcome": "ok",
        "halt_code": None,
        "error_msg": None,
    }


# --- 실행 이력 detail ----------------------------------------------------
def _detach_run(session, run: CollectionRun) -> CollectionRun:
    """commit 으로 만료된 run 속성을 로드하고 세션에서 분리한다.

    collect_window 는 내부 세션을 finally 에서 닫으므로, 반환된 run 을 호출자가
    그대로 읽으면 DetachedInstanceError 가 난다. 닫기 전에 refresh 로 속성을 채우고
    expunge 로 분리해 두면 세션 종료 후에도 읽기 전용으로 안전하게 접근할 수 있다.
    """
    session.refresh(run)
    session.expunge(run)
    return run


def _build_detail_json(results: list[dict[str, Any]]) -> str:
    """업종코드별 결과를 detail_json(문자열)으로 직렬화."""
    detail = [
        {
            "indstrytyCd": r["cd"],
            "outcome": r["outcome"],
            "pages": r["pages"],
            "last_result_code": r["last_code"],
            "items": len(r["items"]),
            "retry_count": r["retry_count"],
        }
        for r in sorted(results, key=lambda x: str(x["cd"]))
    ]
    return json.dumps(detail, ensure_ascii=False)


# --- 진입 함수 -----------------------------------------------------------
def collect_window(
    window_bgn: datetime,
    window_end: datetime,
    trigger: str = "manual",
) -> CollectionRun | None:
    """주어진 윈도우로 12번 용역 공고를 수집·저장하고 CollectionRun(최종 상태)을 반환.

    auto_halted=True 면 아무 것도 하지 않고 None 을 반환(게이트 본체는 3.4, 여기선 방어적).
    """
    session = SessionLocal()
    try:
        config = repository.get_config(session)

        # halt 게이트(방어적) — 자동 중단 상태면 수집하지 않는다.
        if config.auto_halted:
            logger.warning(
                "auto_halted=True → 수집 건너뜀 (halt_code=%s, reason=%s)",
                config.halt_code,
                config.halt_reason,
            )
            return None

        cds = [c.strip() for c in (config.indstryty_cds or "").split(",") if c.strip()]
        if not cds:
            logger.warning("indstryty_cds 가 비어 있습니다 — 수집 대상 업종코드 없음.")

        snapshot = _ConfigSnapshot(
            inqry_div=config.inqry_div,
            intrntnl_div_cd=config.intrntnl_div_cd,
            num_of_rows=config.num_of_rows,
            max_retries=config.max_retries,
        )

        # collected_at/updated_at 은 run 시작 시각 한 값으로 일관되게 채운다.
        meta_ts = datetime.now()
        run = repository.create_run(session, trigger, window_bgn, window_end)

        logger.info(
            "수집 시작: trigger=%s window=%s~%s cds=%s",
            trigger,
            fmt_dt(window_bgn),
            fmt_dt(window_end),
            cds,
        )

        # --- 4코드 병렬 호출(워커는 DB 비접근) ---
        results: list[dict[str, Any]] = []
        if cds:
            with ThreadPoolExecutor(max_workers=len(cds)) as executor:
                futures = {
                    executor.submit(_fetch_cd, cd, window_bgn, window_end, snapshot): cd
                    for cd in cds
                }
                for fut in as_completed(futures):
                    results.append(fut.result())

        total_retry = sum(r["retry_count"] for r in results)
        detail_json = _build_detail_json(results)

        # --- halt 발생: 저장하지 않고 중단(failed) ---
        halt_results = [r for r in results if r["outcome"] == "halt"]
        if halt_results:
            h = halt_results[0]
            halt_code = h["halt_code"]
            halt_reason = (
                f"indstrytyCd={h['cd']} 에서 비재시도 에러 {h['error_msg']} — 스케줄 자동 중단."
            )
            logger.error("halt: %s", halt_reason)
            repository.set_halt(session, halt_code, halt_reason)
            repository.finish_run(
                session,
                run,
                status="failed",
                total_fetched=0,
                total_new=0,
                total_updated=0,
                retry_count=total_retry,
                error_code=halt_code,
                error_msg=halt_reason,
                detail_json=detail_json,
            )
            return _detach_run(session, run)

        # --- 병합·dedup·transform·메타 부여 ---
        results_by_cd = {r["cd"]: r["items"] for r in results}
        deduped_items, matched_map = merge_and_dedup(results_by_cd)

        values_list: list[dict[str, Any]] = []
        skipped = 0
        for item in deduped_items:
            try:
                values = item_to_bid_notice_values(item)
            except ValueError as exc:
                skipped += 1
                logger.warning("transform 건너뜀(PK 누락 등): %s", exc)
                continue
            no = str(item.get("bidNtceNo")).strip()
            values["matched_indstryty_cds"] = matched_map.get(no)
            values["collected_at"] = meta_ts
            values["updated_at"] = meta_ts
            values_list.append(values)

        new_count, updated_count = repository.upsert_bid_notices(session, values_list)
        total_fetched = len(deduped_items)

        # --- 상태 판정 ---
        failed_results = [r for r in results if r["outcome"] == "failed"]
        if failed_results:
            status = "partial"  # 일시 장애로 일부 실패 → last_success_dt 미갱신(다음 회차 재수집)
            error_code = next(
                (r["last_code"] for r in failed_results if r["last_code"]), None
            )
            error_msg = (
                f"{len(failed_results)}개 업종코드 일시 장애로 실패: "
                f"{[r['cd'] for r in failed_results]}"
            )
        else:
            status = "success"
            error_code = None
            error_msg = None

        repository.finish_run(
            session,
            run,
            status=status,
            total_fetched=total_fetched,
            total_new=new_count,
            total_updated=updated_count,
            retry_count=total_retry,
            error_code=error_code,
            error_msg=error_msg,
            detail_json=detail_json,
        )

        if status == "success":
            repository.update_last_success_dt(session, window_end)

        logger.info(
            "수집 종료: status=%s fetched=%d new=%d updated=%d skipped=%d retry=%d",
            status,
            total_fetched,
            new_count,
            updated_count,
            skipped,
            total_retry,
        )
        return _detach_run(session, run)
    finally:
        session.close()


# --- 수동 백필 진입점 ----------------------------------------------------
def _run_backfill() -> None:
    """최근 1개월 백필 1회 — 검증용. 주기 실행은 3.4."""
    init_db()  # 테이블·시드 보장
    with SessionLocal() as session:
        config = repository.get_config(session)
        backfill_days = config.backfill_days

    window_end = datetime.now()
    window_bgn = window_end - timedelta(days=backfill_days)

    run = collect_window(window_bgn, window_end, trigger="backfill")
    if run is None:
        print("수집이 실행되지 않았습니다(auto_halted 등 — 로그 확인).")
        return

    summary = (
        f"[backfill] status={run.status} "
        f"fetched={run.total_fetched} new={run.total_new} updated={run.total_updated} "
        f"retry={run.retry_count}"
    )
    if run.error_code:
        summary += f" error_code={run.error_code}"
    print(summary)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _run_backfill()
