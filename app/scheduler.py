"""스케줄러 — Phase 3.4 (입찰) + Phase 5.4 (사전규격 통합 잡).

APScheduler BackgroundScheduler 로 수집을 주기 실행한다. **두 개의 통합 잡**을 같은
스케줄러·같은 interval 로 운영한다:
- 입찰 잡(`collect`): tick → 게이트(enabled/auto_halted) → 윈도우(last_success_dt) →
  collector.collect_window(trigger="scheduled").
- 사전규격 잡(`collect_pre_spec`, 5.4): tick_pre_spec → 게이트(**pre_spec_enabled 단독**,
  입찰 auto_halted 와 무관) → 윈도우(pre_spec_last_success_dt) →
  pre_spec_collector.collect_pre_spec_window(trigger="scheduled").

수집/페이징/dedup/upsert/재시도/halt/collection_run 기록/last_success 갱신은 전부
각 collect_*_window 안에 있다. 스케줄러는 **"언제·어떤 윈도우로 collect 를 부를지"만**
책임진다(수집 로직을 재구현하지 않는다). 두 잡은 같은 순수 헬퍼(compute_window)를 공유한다.

화면(/list·/config·/api-test)·FastAPI 통합, interval 동적 재스케줄은 3.5.
사전규격 게이트 토글 UI·/pre-spec 화면·실행이력 source 표시는 5.5.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import collector, pre_spec_collector, repository
from app.db import SessionLocal, init_db

logger = logging.getLogger(__name__)

_JOB_ID = "collect"
_PRE_SPEC_JOB_ID = "collect_pre_spec"  # 사전규격 통합 잡(Phase 5.4)

# 모듈 전역 단일 스케줄러(중복 start 방지). 3.5 에서 FastAPI lifespan 이 재사용.
_scheduler: BackgroundScheduler | None = None


# --- 순수 헬퍼(네트워크/DB 비의존) --------------------------------------
def compute_window(
    last_success_dt: datetime | None,
    now: datetime,
    overlap_minutes: int,
    backfill_days: int,
) -> tuple[datetime, datetime]:
    """수집 윈도우(시작, 종료)를 산정한다(계획서 §2.1).

    - last_success_dt 있으면: window_bgn = last_success_dt - overlap_minutes (겹침 재수집)
    - 없으면(최초): window_bgn = now - backfill_days (백필)
    - window_end = now
    - 방어: bgn 이 end 보다 뒤면(설정 이상·미래 last_success 등) bgn 을 end 로 클램프.
    """
    if last_success_dt is not None:
        window_bgn = last_success_dt - timedelta(minutes=overlap_minutes)
    else:
        window_bgn = now - timedelta(days=backfill_days)
    window_end = now
    if window_bgn > window_end:
        window_bgn = window_end
    return window_bgn, window_end


def should_run(enabled: bool, auto_halted: bool) -> bool:
    """스케줄 실행 가능 여부. enabled=True AND auto_halted=False 일 때만 True."""
    return bool(enabled) and not bool(auto_halted)


# --- 주기 잡 ------------------------------------------------------------
def tick(now: datetime | None = None) -> None:
    """주기마다 1회: 게이트 확인 → 윈도우 산정 → collect_window(trigger='scheduled').

    - 매 호출마다 최신 설정을 다시 읽는다(/config 편집 즉시 반영). 짧은 세션으로 읽고
      필요한 스칼라 값만 들고 나간다(ORM 객체를 잡 바깥으로 들고 다니지 않음).
    - 게이트 False 면 수집하지 않고 로그만 남기고 반환.
    - collect_window 예외는 잡아서 로그만(잡 1회 실패가 스케줄러를 죽이지 않도록).
    - `now`: 기본 None → datetime.now(). 테스트에서 시간을 고정해 윈도우를 결정적으로
      검증하기 위해 주입 가능하게 둔다(APScheduler 는 인자 없이 호출 → None).
    """
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        enabled = cfg.enabled
        auto_halted = cfg.auto_halted
        halt_code = cfg.halt_code
        overlap_minutes = cfg.window_overlap_minutes
        backfill_days = cfg.backfill_days
        last_success_dt = cfg.last_success_dt

    if not should_run(enabled, auto_halted):
        logger.info(
            "tick 건너뜀(게이트): enabled=%s auto_halted=%s halt_code=%s",
            enabled,
            auto_halted,
            halt_code,
        )
        return

    if now is None:
        now = datetime.now()
    window_bgn, window_end = compute_window(
        last_success_dt, now, overlap_minutes, backfill_days
    )

    if last_success_dt is None:
        logger.info("최초 실행 — 최근 %d일 백필 윈도우로 수집합니다.", backfill_days)

    logger.info(
        "tick 수집 시작: window=%s~%s",
        collector.fmt_dt(window_bgn),
        collector.fmt_dt(window_end),
    )
    try:
        collector.collect_window(window_bgn, window_end, trigger="scheduled")
    except Exception:  # noqa: BLE001 — 잡 1회 실패가 스케줄러 자체를 죽이지 않게.
        logger.exception("collect_window 실행 중 예외 — 이번 tick 실패(스케줄러는 계속).")


def tick_pre_spec(now: datetime | None = None) -> None:
    """사전규격 주기 잡(Phase 5.4): 게이트(pre_spec_enabled) → 윈도우 → collect_pre_spec_window.

    입찰 `tick` 과 동형이되 **게이트가 `pre_spec_enabled` 단독**이다. 입찰 `auto_halted` 를
    쓰지 않으므로(사전규격은 입찰 중단과 무관) `should_run` 을 호출하지 않는다.

    - 매 호출마다 최신 설정을 다시 읽고 필요한 스칼라만 들고 나간다(짧은 세션).
      입찰 주기/overlap/백필 설정을 재사용하되 윈도우 기준은 `pre_spec_last_success_dt`.
    - pre_spec_enabled=False 면 수집하지 않고 로그만 남기고 반환.
    - collect_pre_spec_window 예외는 잡아서 로그만(잡 1회 실패가 스케줄러를 죽이지 않도록).
    - `now`: 기본 None → datetime.now()(테스트에서 윈도우 결정성을 위해 주입 가능).
    """
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        pre_spec_enabled = cfg.pre_spec_enabled
        overlap_minutes = cfg.window_overlap_minutes
        backfill_days = cfg.backfill_days
        pre_spec_last_success_dt = cfg.pre_spec_last_success_dt

    if not pre_spec_enabled:
        logger.info("tick_pre_spec 건너뜀(게이트): pre_spec_enabled=%s", pre_spec_enabled)
        return

    if now is None:
        now = datetime.now()
    window_bgn, window_end = compute_window(
        pre_spec_last_success_dt, now, overlap_minutes, backfill_days
    )

    if pre_spec_last_success_dt is None:
        logger.info("사전규격 최초 실행 — 최근 %d일 백필 윈도우로 수집합니다.", backfill_days)

    logger.info(
        "tick_pre_spec 수집 시작: window=%s~%s",
        collector.fmt_dt(window_bgn),
        collector.fmt_dt(window_end),
    )
    try:
        pre_spec_collector.collect_pre_spec_window(
            window_bgn, window_end, trigger="scheduled"
        )
    except Exception:  # noqa: BLE001 — 잡 1회 실패가 스케줄러 자체를 죽이지 않게.
        logger.exception(
            "collect_pre_spec_window 실행 중 예외 — 이번 tick 실패(스케줄러는 계속)."
        )


# --- 구동 ---------------------------------------------------------------
def start_scheduler(run_now: bool = False) -> BackgroundScheduler:
    """init_db 보장 후 BackgroundScheduler 생성·잡 등록·start. 단일 인스턴스 관리.

    run_now=True 면 시작 직후 1회 즉시 실행(이후 interval 주기).
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        logger.warning("스케줄러가 이미 실행 중입니다 — 중복 start 무시.")
        return _scheduler

    init_db()

    with SessionLocal() as session:
        cfg = repository.get_config(session)
        interval_minutes = cfg.interval_minutes

    scheduler = BackgroundScheduler()
    job_kwargs: dict = {}
    if run_now:
        job_kwargs["next_run_time"] = datetime.now()  # 시작 직후 1회 즉시 실행
    # 입찰 잡(기존, 무변경)
    scheduler.add_job(
        tick,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=_JOB_ID,
        max_instances=1,  # 직전 실행이 안 끝났으면 겹쳐 돌지 않음
        coalesce=True,  # 밀린 실행은 1회로 합침
        **job_kwargs,
    )
    # 사전규격 잡(Phase 5.4) — 같은 interval·같은 옵션으로 추가 등록.
    scheduler.add_job(
        tick_pre_spec,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=_PRE_SPEC_JOB_ID,
        max_instances=1,
        coalesce=True,
        **job_kwargs,
    )
    scheduler.start()
    _scheduler = scheduler

    job = scheduler.get_job(_JOB_ID)
    ps_job = scheduler.get_job(_PRE_SPEC_JOB_ID)
    logger.info(
        "스케줄러 시작: interval=%d분 run_now=%s bid_next_run=%s pre_spec_next_run=%s",
        interval_minutes,
        run_now,
        getattr(job, "next_run_time", None),
        getattr(ps_job, "next_run_time", None),
    )
    return scheduler


def shutdown_scheduler() -> None:
    """실행 중이면 스케줄러 종료(wait=False)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료.")
    _scheduler = None


# --- 화면(/config) 연동용 추가 헬퍼 — Phase 3.5 -------------------------
def reschedule(interval_minutes: int) -> None:
    """스케줄러가 실행 중이면 **두 잡 모두**(입찰+사전규격) 트리거를 새 interval 로 교체. 아니면 무시.

    /config 에서 interval_minutes 를 바꿨을 때 실행 중인 스케줄러에 즉시 반영한다.
    스케줄러가 떠 있지 않으면 다음 start_scheduler 가 최신 설정을 읽으므로 아무것도 안 한다.
    """
    global _scheduler
    if _scheduler is None or not _scheduler.running:
        logger.info("reschedule 무시: 스케줄러가 실행 중이 아닙니다(interval=%s).", interval_minutes)
        return
    for job_id in (_JOB_ID, _PRE_SPEC_JOB_ID):
        _scheduler.reschedule_job(
            job_id, trigger=IntervalTrigger(minutes=interval_minutes)
        )
    job = _scheduler.get_job(_JOB_ID)
    ps_job = _scheduler.get_job(_PRE_SPEC_JOB_ID)
    logger.info(
        "스케줄러 재스케줄: interval=%d분 bid_next_run=%s pre_spec_next_run=%s",
        interval_minutes,
        getattr(job, "next_run_time", None),
        getattr(ps_job, "next_run_time", None),
    )


def is_running() -> bool:
    """스케줄러가 현재 구동 중인지 여부(화면 표시용)."""
    return _scheduler is not None and _scheduler.running


def get_next_run_time(job_id: str = _JOB_ID) -> datetime | None:
    """다음 실행 예정 시각(화면 표시용). 실행 중이 아니거나 잡이 없으면 None.

    job_id 기본=입찰 잡(`collect`) → main.py 의 무인자 호출 하위호환. 5.5 화면은
    `_PRE_SPEC_JOB_ID`("collect_pre_spec")를 넘겨 사전규격 next_run 도 조회한다.
    """
    if _scheduler is None or not _scheduler.running:
        return None
    job = _scheduler.get_job(job_id)
    return getattr(job, "next_run_time", None) if job is not None else None


# --- 독립 실행 진입점(검증용) -------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_scheduler(run_now=True)
    # BackgroundScheduler 는 데몬 스레드이므로 메인 스레드를 살려 둔다.
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        shutdown_scheduler()
