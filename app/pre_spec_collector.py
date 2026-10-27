"""사전규격(용역) 수집기 — Phase 5.3.

사전규격정보서비스 op15(`getPublicPrcureThngInfoServcPPSSrch`)를 **단일 스트림으로 페이징
호출 → 변환(`item_to_pre_spec_values`) → `pre_spec` upsert → `collection_run` 기록**한다.

입찰 `app/collector.py` 의 "수집기 + repository upsert" 단계와 동형이되, 사전규격은
**단일 op·업종코드 없음**이라 병렬 fan-out 이 아니라 **페이지네이션만** 한다.
수집필터는 §6 확정대로 `swBizObjYn=Y`(SW 용역만), 조회구분은 `inqryDiv=1`(접수일시) 고정.

이 단계의 수집기는 **윈도우(시작·종료 datetime)와 trigger 를 인자로 받는다.**
윈도우 자동 산정(last_success_dt)·주기 실행·스케줄러 통합·`/pre-spec` 화면은 5.4 이후.

**halt/last_success 정책(입찰과 분리, 5.4 갱신):**
- 전역 halt 를 두지 않는다(사전규격 독립). `app_config.auto_halted` 게이트를 **읽지도 쓰지도
  않으며**(게이트는 5.4 스케줄러의 `pre_spec_enabled` 단독), `repository.set_halt`·입찰
  `repository.update_last_success_dt` 를 **호출하지 않는다.**
- halt 코드 만나면 그 run 만 `failed`(저장 0). 재시도 소진(failed)이면 그때까지 받은
  페이지를 저장하고 run `partial`. 전 페이지 정상이면 저장 후 `success`.
- **5.4**: 전체 `success` 일 때만 `repository.update_pre_spec_last_success_dt(window_end)` 로
  사전규격 전용 last_success 를 갱신한다(partial/failed/halt 미갱신 → 다음 tick 재수집).
  `source="pre_spec"` 로 collection_run 을 태깅한다(입찰=`bid`).

순수 헬퍼(`classify_result_code`·`total_pages`·`fmt_dt`)는 입찰 `app.collector` 에서
import 재사용한다(collector.py 는 무수정). `_call_with_retry`·`_fetch_*`·`_detach_run` 은
입찰 OPERATION 에 묶여 있으니 단일 op 버전으로 새로 작성한다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import api_client, repository
from app.collector import _halt_message, _OK_CODES, classify_result_code, fmt_dt, total_pages
from app.db import SessionLocal, init_db
from app.models import CollectionRun
from app.transform import item_to_pre_spec_values
from app.window_split import collect_in_chunks

logger = logging.getLogger(__name__)

# 15번 사전규격(용역) 검색조건 조회.
OPERATION = "getPublicPrcureThngInfoServcPPSSrch"

# §6 확정 수집필터 — SW 용역만 적재(요청에 고정).
SW_BIZ_OBJ_YN = "Y"
# 조회구분 — 접수일시 기준(plan §2 고정).
INQRY_DIV = "1"

# 일시 장애 재시도 백오프(초). 입찰과 동일 — 짧은 선형 백오프.
_RETRY_BACKOFF_SECONDS = 2.0

# 페이징 안전장치(무한 루프 방지). 초과 시 로그로 알린다. 입찰과 동일 값.
_MAX_PAGES_SAFETY = 1000


@dataclass(frozen=True)
class _PreSpecConfigSnapshot:
    """수집에 쓰는 설정값 스냅샷(입찰 패턴과 형태 통일)."""

    num_of_rows: int
    max_retries: int


def _call_with_retry(
    raw_params: dict[str, str], max_retries: int
) -> tuple[api_client.ApiResult, int, str]:
    """페이지 1건 호출 + 재시도. 반환: (마지막 ApiResult, 사용한 재시도 수, outcome).

    outcome ∈ {"ok","failed","halt"}. failed = 재시도 소진. 선형 백오프.
    입찰 동명 함수와 동일 로직이되 사전규격 OPERATION(op15)을 호출한다.
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


def _count_unique_items(items: list[dict]) -> int:
    """사전규격 등록번호로 중복을 세며 번호가 없는 행은 각각 수신 건수에 포함한다."""
    keys = set()
    unkeyed = 0
    for item in items:
        key = item.get("bfSpecRgstNo") if isinstance(item, dict) else None
        key = str(key).strip() if key is not None else ""
        if key:
            keys.add(key)
        else:
            unkeyed += 1
    return len(keys) + unkeyed


def _fetch_pre_spec(
    window_bgn: datetime,
    window_end: datetime,
    snapshot: _PreSpecConfigSnapshot,
) -> dict[str, Any]:
    """사전규격 op15 를 **단일 스트림으로 끝까지 페이징**해 item 을 모은다. DB 비접근.

    base_params 는 `swBizObjYn=Y`·`inqryDiv=1` 고정 + 페이지크기·조회기간.
    페이지 1부터 `pageNo` 증가, `total_pages` 로 상한 계산, `resultCode=03`(No Data) 종료,
    `_MAX_PAGES_SAFETY` 가드. `api_client.ApiClientError` 는 failed 처리.

    반환 dict: items, pages, last_code, retry_count, outcome, halt_code, error_msg.
      outcome ∈ {"ok","failed","halt"}.
    """
    page_size = max(1, min(snapshot.num_of_rows, 999))
    base_params: dict[str, str] = {
        "inqryDiv": INQRY_DIV,
        "swBizObjYn": SW_BIZ_OBJ_YN,  # §6 고정 필터
        "numOfRows": str(page_size),
        "inqryBgnDt": fmt_dt(window_bgn),
        "inqryEndDt": fmt_dt(window_end),
    }

    items: list[dict] = []
    retry_count = 0
    pages_fetched = 0
    last_code: str | None = None
    max_page: int | None = None
    page = 1
    total_count = None
    paging_flags = {}

    def summary(outcome, error_msg=None, halt_code=None, **flags):
        return {"items": items, "pages": pages_fetched, "last_code": last_code,
                "retry_count": retry_count, "outcome": outcome, "halt_code": halt_code,
                "error_msg": error_msg, "total_count": total_count,
                "unique_items": _count_unique_items(items), "page_size": page_size,
                **paging_flags, **flags}

    try:
        while True:
            params = {**base_params, "pageNo": str(page)}
            result, used_retries, outcome = _call_with_retry(params, snapshot.max_retries)
            retry_count += used_retries
            last_code = result.result_code

            if outcome == "halt":
                return summary("halt", _halt_message(result), result.result_code)
            if outcome == "failed":
                return summary("failed", result.error
                               or f"일시 장애 재시도 소진(resultCode={last_code})")

            # outcome == "ok"
            pages_fetched += 1
            if result.result_code == "03":  # No Data → 종료
                try:
                    response_total_count = int(str(result.total_count).strip())
                    if (total_count is not None and response_total_count >= 0
                            and response_total_count != total_count):
                        paging_flags["total_count_changed"] = True
                except ValueError:
                    pass  # 입찰과 동일하게 03은 totalCount 없이도 정상 종료한다.
                if total_count is None:
                    total_count = 0
                break
            items.extend(result.items)

            try:
                response_total_count = int(str(result.total_count).strip())
                if response_total_count < 0:
                    raise ValueError
            except ValueError:
                return summary("failed", "totalCount 누락/오류")
            if max_page is None:
                total_count = response_total_count
                max_page = total_pages(total_count, page_size)
            elif response_total_count != total_count:
                paging_flags["total_count_changed"] = True

            if page >= max_page:
                break
            if page >= _MAX_PAGES_SAFETY:
                logger.warning(
                    "사전규격: 안전 상한(%d페이지) 도달 — 페이징 중단(total_count=%s)",
                    _MAX_PAGES_SAFETY,
                    result.total_count,
                )
                return summary("failed", f"안전 상한({_MAX_PAGES_SAFETY}페이지) 도달 — 잘린 수집",
                               truncated=True)
            page += 1
    except api_client.ApiClientError as exc:
        return summary("failed", f"ApiClientError: {exc}")

    unique = _count_unique_items(items)
    shortfall = unique < total_count
    message = f"건수 미달: 고유 {unique} / 전체 {total_count}" if shortfall else None
    duplicates = len(items) - unique
    if shortfall and duplicates:
        message += f" (중복 {duplicates})"
    return summary("failed" if shortfall else "ok", message)


def _detach_run(session, run: CollectionRun) -> CollectionRun:
    """commit 으로 만료된 run 속성을 로드하고 세션에서 분리한다(입찰과 동일).

    collect_pre_spec_window 는 내부 세션을 finally 에서 닫으므로, 닫기 전에 refresh 로
    속성을 채우고 expunge 로 분리해 두면 세션 종료 후에도 읽기 전용으로 안전하게 접근한다.
    """
    session.refresh(run)
    session.expunge(run)
    return run


def _build_detail_json(r: dict[str, Any]) -> str:
    """단일 스트림 결과를 detail_json(문자열)으로 직렬화."""
    detail = {
        "outcome": r["outcome"],
        "pages": r["pages"],
        "last_result_code": r["last_code"],
        "items": len(r["items"]),
        "retry_count": r["retry_count"],
        "error_msg": r.get("error_msg"),
        "total_count": r.get("total_count"),
        "unique_items": r.get("unique_items"),
        "page_size": r.get("page_size"),
        **({"truncated": True} if r.get("truncated") else {}),
        **({"total_count_changed": True} if r.get("total_count_changed") else {}),
    }
    return json.dumps(detail, ensure_ascii=False)


def collect_pre_spec_window(
    window_bgn: datetime,
    window_end: datetime,
    trigger: str = "manual",
) -> CollectionRun | None:
    """주어진 윈도우로 사전규격(SW 용역)을 수집·저장하고 CollectionRun(최종 상태)을 반환.

    auto_halted 게이트는 **검사하지 않는다**(§2 — 전역 halt 는 입찰 스케줄러 개념).
    `set_halt`·`update_last_success_dt` 를 호출하지 않는다.
    """
    session = SessionLocal()
    run_id = None
    retry_count = 0
    try:
        config = repository.get_config(session)

        snapshot = _PreSpecConfigSnapshot(
            num_of_rows=config.num_of_rows,
            max_retries=config.max_retries,
        )

        # collected_at/updated_at 은 run 시작 시각 한 값으로 일관되게 채운다.
        meta_ts = datetime.now()
        run = repository.create_run(
            session, trigger, window_bgn, window_end, source="pre_spec"
        )
        run_id = run.id

        logger.info(
            "사전규격 수집 시작: trigger=%s window=%s~%s swBizObjYn=%s",
            trigger,
            fmt_dt(window_bgn),
            fmt_dt(window_end),
            SW_BIZ_OBJ_YN,
        )

        r = _fetch_pre_spec(window_bgn, window_end, snapshot)
        retry_count = r["retry_count"]
        detail_json = _build_detail_json(r)

        # --- halt 발생: 저장하지 않고 중단(failed). set_halt 호출 안 함(§2). ---
        if r["outcome"] == "halt":
            halt_code = r["halt_code"]
            halt_reason = (
                f"사전규격 수집 중 비재시도 에러 {r['error_msg']} — run 실패(저장 안 함)."
            )
            logger.error("halt: %s", halt_reason)
            repository.finish_run(
                session,
                run,
                status="failed",
                total_fetched=0,
                total_new=0,
                total_updated=0,
                retry_count=r["retry_count"],
                error_code=halt_code,
                error_msg=halt_reason,
                detail_json=detail_json,
            )
            return _detach_run(session, run)

        # --- 받은 item 변환·메타 부여·upsert(ok/failed 공통) ---
        values_list: list[dict[str, Any]] = []
        skipped = 0
        for item in r["items"]:
            try:
                values = item_to_pre_spec_values(item)
            except ValueError as exc:
                skipped += 1
                logger.warning("transform 건너뜀(PK 누락 등): %s", exc)
                continue
            values["collected_at"] = meta_ts
            values["updated_at"] = meta_ts
            values_list.append(values)

        new_count, updated_count = repository.upsert_pre_specs(session, values_list)
        total_fetched = len(r["items"])

        # --- 상태 판정: failed(재시도 소진) → partial, 그 외 → success ---
        if r["outcome"] == "failed":
            status = "partial"  # 일시 장애로 일부만 수집 → last_success_dt 미갱신(5.4)
            error_code = r["last_code"] if r["last_code"] not in _OK_CODES else None
            error_msg = r["error_msg"]
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
            retry_count=r["retry_count"],
            error_code=error_code,
            error_msg=error_msg,
            detail_json=detail_json,
        )
        # 전체 성공(success) 시에만 사전규격 last_success 를 윈도우 종료로 갱신한다(Phase 5.4).
        # partial/failed/halt 는 미갱신 → 다음 tick 이 같은 윈도우를 재수집(누락 방지).
        # 입찰 update_last_success_dt 는 호출하지 않는다(입찰 last_success_dt 와 분리).
        if status == "success":
            repository.update_pre_spec_last_success_dt(session, window_end)

        logger.info(
            "사전규격 수집 종료: status=%s fetched=%d new=%d updated=%d skipped=%d retry=%d",
            status,
            total_fetched,
            new_count,
            updated_count,
            skipped,
            r["retry_count"],
        )
        return _detach_run(session, run)
    except Exception as exc:
        repository.fail_running_run(
            session, run_id, exc, retry_count=retry_count, label="사전규격",
        )
        raise
    finally:
        session.close()


# --- 수동 백필 진입점 ----------------------------------------------------
def _run_backfill() -> None:
    """최근 backfill_days 백필 1회 — 검증용. 주기 실행은 5.4."""
    init_db()  # 테이블(pre_spec 포함)·시드 보장
    with SessionLocal() as session:
        config = repository.get_config(session)
        backfill_days = config.backfill_days

    window_end = datetime.now()
    window_bgn = window_end - timedelta(days=backfill_days)

    run = collect_in_chunks(collect_pre_spec_window, window_bgn, window_end, trigger="backfill")
    if run is None:
        print("사전규격 수집이 실행되지 않았습니다(로그 확인).")
        return

    summary = (
        f"[pre_spec backfill] status={run.status} "
        f"fetched={run.total_fetched} new={run.total_new} updated={run.total_updated} "
        f"retry={run.retry_count}"
    )
    if run.error_code:
        summary += f" error_code={run.error_code}"
    print(summary)


if __name__ == "__main__":
    from app.logging_config import setup_logging  # noqa: PLC0415
    setup_logging()
    _run_backfill()
