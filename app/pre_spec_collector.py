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
from app.collector import classify_result_code, fmt_dt, total_pages
from app.db import SessionLocal, init_db
from app.models import CollectionRun
from app.transform import item_to_pre_spec_values

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
    base_params: dict[str, str] = {
        "inqryDiv": INQRY_DIV,
        "swBizObjYn": SW_BIZ_OBJ_YN,  # §6 고정 필터
        "numOfRows": str(snapshot.num_of_rows),
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
            result, used_retries, outcome = _call_with_retry(params, snapshot.max_retries)
            retry_count += used_retries
            last_code = result.result_code

            if outcome == "halt":
                halt_code = result.result_code
                return {
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
                max_page = total_pages(result.total_count, snapshot.num_of_rows)

            if page >= max_page:
                break
            if page >= _MAX_PAGES_SAFETY:
                logger.warning(
                    "사전규격: 안전 상한(%d페이지) 도달 — 페이징 중단(total_count=%s)",
                    _MAX_PAGES_SAFETY,
                    result.total_count,
                )
                break
            page += 1
    except api_client.ApiClientError as exc:
        # 파라미터/키 등 클라이언트 측 오류 — 일시 장애로 보지 않고 실패 처리.
        return {
            "items": items,
            "pages": pages_fetched,
            "last_code": last_code,
            "retry_count": retry_count,
            "outcome": "failed",
            "halt_code": None,
            "error_msg": f"ApiClientError: {exc}",
        }

    return {
        "items": items,
        "pages": pages_fetched,
        "last_code": last_code,
        "retry_count": retry_count,
        "outcome": "ok",
        "halt_code": None,
        "error_msg": None,
    }


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

        logger.info(
            "사전규격 수집 시작: trigger=%s window=%s~%s swBizObjYn=%s",
            trigger,
            fmt_dt(window_bgn),
            fmt_dt(window_end),
            SW_BIZ_OBJ_YN,
        )

        r = _fetch_pre_spec(window_bgn, window_end, snapshot)
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
            error_code = r["last_code"]
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

    run = collect_pre_spec_window(window_bgn, window_end, trigger="backfill")
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _run_backfill()
