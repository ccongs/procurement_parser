"""사전규격 스케줄러 통합 단위/실구동 테스트 — Phase 5.4.

`tests/test_scheduler.py` 스타일(`now`·설정 주입, monkeypatch). 네트워크/실 DB
(procurement.db) 비의존·결정적. 커버:
- 게이트: pre_spec_enabled on/off → collect 호출 여부(윈도우 인자 캡처).
- auto_halted 무관: auto_halted=True 여도 pre_spec_enabled=True 면 수집 호출(입찰과 분리).
- 윈도우: pre_spec_last_success_dt None=백필 / 값=overlap (compute_window 재사용).
- source 태깅: create_run(source="pre_spec") vs 기본 "bid".
- pre_spec last_success 갱신: success 시만 갱신, partial/failed/halt 미갱신,
  입찰 last_success_dt 불변(분리).
- 자동 마이그레이션 멱등: 구 스키마 모사 → init_db() 후 컬럼 존재·기존행 무손상·2회 무에러.
- 두 잡 실구동 등록(BackgroundScheduler): collect·collect_pre_spec 둘 다·옵션·reschedule·shutdown.
- 중복 미발생: scheduled 2회(겹치는 윈도우) → pre_spec PK 중복 0.
실행: `pytest tests/test_scheduler_prespec.py`
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import api_client, pre_spec_collector, repository, scheduler
from app.db import Base
from app.models import AppConfig, CollectionRun, PreSpec
from app.pre_spec_collector import OPERATION


# =====================================================================
# 1. tick_pre_spec 게이트·윈도우·위임 (monkeypatch, DB 비접근)
# =====================================================================
class _FakeConfig:
    """tick_pre_spec 이 읽는 설정 필드만 가진 가짜 config."""

    def __init__(
        self,
        *,
        pre_spec_enabled=True,
        auto_halted=False,
        window_overlap_minutes=90,
        backfill_days=30,
        pre_spec_last_success_dt=None,
    ):
        self.pre_spec_enabled = pre_spec_enabled
        self.auto_halted = auto_halted  # 사전규격 게이트엔 안 쓰이지만 분리 실증용으로 둔다.
        self.window_overlap_minutes = window_overlap_minutes
        self.backfill_days = backfill_days
        self.pre_spec_last_success_dt = pre_spec_last_success_dt


class _DummySession:
    """SessionLocal() 대체 — with 컨텍스트만 만족하면 된다(DB 비접근)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_tick_pre_spec(monkeypatch, config, collect_fn=None):
    """tick_pre_spec 의 외부 의존(SessionLocal·get_config·collect_pre_spec_window)을 가짜로 교체.

    반환: collect_pre_spec_window 호출 인자를 모으는 리스트(calls).
    """
    calls: list[tuple] = []

    def default_collect(window_bgn, window_end, trigger="manual"):
        calls.append((window_bgn, window_end, trigger))

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(scheduler.repository, "get_config", lambda session: config)
    monkeypatch.setattr(
        scheduler.pre_spec_collector,
        "collect_pre_spec_window",
        collect_fn or default_collect,
    )
    return calls


def test_tick_pre_spec_skips_when_disabled(monkeypatch):
    calls = _patch_tick_pre_spec(monkeypatch, _FakeConfig(pre_spec_enabled=False))
    scheduler.tick_pre_spec()
    assert calls == []  # collect_pre_spec_window 미호출


def test_tick_pre_spec_runs_when_enabled_and_passes_window(monkeypatch):
    last_success = datetime(2026, 6, 1, 11, 0)
    now = datetime(2026, 6, 1, 12, 0)  # now 주입 → 윈도우 결정적(flaky 방지)
    config = _FakeConfig(
        pre_spec_enabled=True,
        window_overlap_minutes=90,
        backfill_days=30,
        pre_spec_last_success_dt=last_success,
    )
    calls = _patch_tick_pre_spec(monkeypatch, config)

    scheduler.tick_pre_spec(now=now)

    assert len(calls) == 1
    window_bgn, window_end, trigger = calls[0]
    assert trigger == "scheduled"
    assert window_bgn == last_success - timedelta(minutes=90)  # 09:30
    assert window_end == now


def test_tick_pre_spec_runs_even_when_auto_halted(monkeypatch):
    """auto_halted=True 여도 pre_spec_enabled=True 면 사전규격 수집(입찰과 분리)."""
    now = datetime(2026, 6, 1, 12, 0)
    config = _FakeConfig(pre_spec_enabled=True, auto_halted=True)
    calls = _patch_tick_pre_spec(monkeypatch, config)

    scheduler.tick_pre_spec(now=now)

    assert len(calls) == 1  # auto_halted 무관 — 호출됨


def test_tick_pre_spec_backfill_window_when_no_last_success(monkeypatch):
    """pre_spec_last_success_dt=None → 백필(now-backfill_days)."""
    now = datetime(2026, 6, 1, 12, 0)
    config = _FakeConfig(
        pre_spec_enabled=True,
        backfill_days=30,
        pre_spec_last_success_dt=None,
    )
    calls = _patch_tick_pre_spec(monkeypatch, config)

    scheduler.tick_pre_spec(now=now)

    window_bgn, window_end, _ = calls[0]
    assert window_bgn == now - timedelta(days=30)
    assert window_end == now


def test_tick_pre_spec_swallows_collect_exception(monkeypatch):
    def boom(window_bgn, window_end, trigger="manual"):
        raise RuntimeError("collect 실패")

    _patch_tick_pre_spec(
        monkeypatch, _FakeConfig(pre_spec_enabled=True), collect_fn=boom
    )
    # 예외가 tick_pre_spec 밖으로 전파되지 않아야 한다(로그만).
    scheduler.tick_pre_spec(now=datetime(2026, 6, 1, 12, 0))


# =====================================================================
# 2. create_run source 태깅 (인메모리 SQLite)
# =====================================================================
@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_create_run_default_source_is_bid(session):
    run = repository.create_run(
        session, "scheduled", datetime(2026, 5, 1), datetime(2026, 6, 1)
    )
    assert run.source == "bid"


def test_create_run_pre_spec_source(session):
    run = repository.create_run(
        session,
        "scheduled",
        datetime(2026, 5, 1),
        datetime(2026, 6, 1),
        source="pre_spec",
    )
    assert run.source == "pre_spec"


# =====================================================================
# 3. collect_pre_spec_window: source 태깅 + last_success 갱신 (임시 engine)
# =====================================================================
def _result(result_code, items=None, total_count="0", error=None):
    return api_client.ApiResult(
        operation=OPERATION,
        request_url="",
        sent_params={},
        response_type="json",
        status_code=200,
        raw_text="",
        parsed=None,
        result_code=result_code,
        result_msg="",
        items=items or [],
        total_count=total_count,
        error=error,
    )


def _item(rgst_no, **extra):
    base = {
        "bfSpecRgstNo": rgst_no,
        "prdctClsfcNoNm": f"사업명-{rgst_no}",
        "swBizObjYn": "Y",
    }
    base.update(extra)
    return base


@pytest.fixture
def db_engine(monkeypatch):
    """인메모리 SQLite engine 으로 SessionLocal 을 바꿔치기 + app_config 시드(pre_spec 필드 포함)."""
    import app.db as dbmod

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    seed = Session()
    seed.add(
        AppConfig(
            id=1,
            backfill_days=30,
            num_of_rows=2,
            max_retries=2,
            pre_spec_enabled=True,
            last_success_dt=None,
            pre_spec_last_success_dt=None,
        )
    )
    seed.commit()
    seed.close()

    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(pre_spec_collector, "SessionLocal", Session)
    monkeypatch.setattr(repository, "SessionLocal", Session, raising=False)
    return Session


WIN_BGN = datetime(2026, 5, 1, 0, 0)
WIN_END = datetime(2026, 6, 1, 0, 0)


def test_collect_pre_spec_tags_run_source_pre_spec(monkeypatch, db_engine):
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("00", items=[_item("S1")], total_count="1")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END, trigger="manual")

    s = db_engine()
    try:
        assert s.get(CollectionRun, run.id).source == "pre_spec"
    finally:
        s.close()


def test_collect_pre_spec_success_updates_pre_spec_last_success(monkeypatch, db_engine):
    """success → pre_spec_last_success_dt==window_end; 입찰 last_success_dt 불변(분리)."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("00", items=[_item("Z")], total_count="1")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "success"

    s = db_engine()
    try:
        cfg = s.get(AppConfig, 1)
        assert cfg.pre_spec_last_success_dt == WIN_END  # 윈도우 종료로 갱신
        assert cfg.last_success_dt is None  # 입찰 것은 불변
    finally:
        s.close()


def test_collect_pre_spec_partial_does_not_update_last_success(monkeypatch, db_engine):
    """재시도 소진(partial) → pre_spec_last_success_dt 미갱신."""
    monkeypatch.setattr(pre_spec_collector.time, "sleep", lambda s: None)

    def _fake_call(operation, raw_params, response_type="json"):
        page = int(raw_params["pageNo"])
        if page == 1:
            return _result("00", items=[_item("P1"), _item("P2")], total_count="4")
        return _result("01", error="일시 장애 지속")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "partial"

    s = db_engine()
    try:
        assert s.get(AppConfig, 1).pre_spec_last_success_dt is None  # 미갱신
    finally:
        s.close()


def test_collect_pre_spec_halt_does_not_update_last_success(monkeypatch, db_engine):
    """halt(비재시도 코드) → run failed·pre_spec_last_success_dt 미갱신·auto_halted 불변."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("30")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "failed"

    s = db_engine()
    try:
        cfg = s.get(AppConfig, 1)
        assert cfg.pre_spec_last_success_dt is None  # 미갱신
        assert cfg.auto_halted in (False, None)  # 전역 halt 미사용
    finally:
        s.close()


# =====================================================================
# 4. 자동 마이그레이션 멱등 (구 스키마 모사)
# =====================================================================
def test_auto_migration_adds_columns_idempotent(monkeypatch, tmp_path):
    """구(舊) 스키마(신규 컬럼 없는 collection_run/app_config) → init_db 후 컬럼 추가·기존행 무손상·2회 무에러."""
    import app.db as dbmod

    db_file = tmp_path / "old_schema.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Session = sessionmaker(bind=engine, future=True)

    # --- 구 스키마 테이블을 손으로 만든다(신규 컬럼 없음) ---
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE collection_run ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, trigger VARCHAR(20), status VARCHAR(12))"
            )
        )
        # 구 스키마 app_config: 5.4 이전의 모든 컬럼을 갖되 신규 3컬럼만 없음(실제 마이그레이션 시나리오).
        conn.execute(
            text(
                "CREATE TABLE app_config ("
                "id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT 1, "
                "auto_halted BOOLEAN NOT NULL DEFAULT 0, halt_code VARCHAR(2), halt_reason TEXT, "
                "interval_minutes INTEGER NOT NULL DEFAULT 60, "
                "window_overlap_minutes INTEGER NOT NULL DEFAULT 90, backfill_days INTEGER NOT NULL DEFAULT 30, "
                "num_of_rows INTEGER NOT NULL DEFAULT 20, max_retries INTEGER NOT NULL DEFAULT 2, "
                "inqry_div VARCHAR(1) NOT NULL DEFAULT '1', intrntnl_div_cd VARCHAR(1) NOT NULL DEFAULT '1', "
                "indstryty_cds VARCHAR(100) NOT NULL DEFAULT '1426', prtcpt_lmt_rgn_cd VARCHAR(2), "
                "presmpt_prce_bgn VARCHAR(25), presmpt_prce_end VARCHAR(25), "
                "last_success_dt DATETIME, updated_at DATETIME)"
            )
        )
        # 기존 행: collection_run 1건(source 컬럼 없이), app_config 1건.
        conn.execute(
            text("INSERT INTO collection_run (trigger, status) VALUES ('manual', 'success')")
        )
        conn.execute(text("INSERT INTO app_config (id, enabled) VALUES (1, 1)"))

    # init_db 가 이 engine/Session 을 쓰도록 패치.
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)

    dbmod.init_db()  # create_all(no-op for existing) → migrate(ADD COLUMN) → seed(스킵, 행 존재)

    insp = inspect(engine)
    cr_cols = {c["name"] for c in insp.get_columns("collection_run")}
    ac_cols = {c["name"] for c in insp.get_columns("app_config")}
    assert "source" in cr_cols
    assert "pre_spec_enabled" in ac_cols
    assert "pre_spec_last_success_dt" in ac_cols

    # 기존 collection_run 행 source 기본 'bid'(무손상), app_config pre_spec_enabled truthy.
    s = Session()
    try:
        row = s.execute(text("SELECT source FROM collection_run")).scalar_one()
        assert row == "bid"
        cfg = s.get(AppConfig, 1)
        assert bool(cfg.pre_spec_enabled) is True
        assert cfg.pre_spec_last_success_dt is None
    finally:
        s.close()

    # 2회 호출 무에러(멱등 — inspector 가 present 보고 ALTER 스킵).
    dbmod.init_db()
    insp2 = inspect(engine)
    assert "source" in {c["name"] for c in insp2.get_columns("collection_run")}


# =====================================================================
# 5. 두 잡 실구동 등록 (BackgroundScheduler) + 중복 미발생
# =====================================================================
@pytest.fixture
def sched_db(monkeypatch, tmp_path):
    """start_scheduler 가 임시 파일 DB 를 쓰도록 init_db·SessionLocal 을 패치."""
    import app.db as dbmod

    db_file = tmp_path / "sched.db"
    engine = create_engine(
        f"sqlite:///{db_file}", future=True, connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(engine)

    seed = Session()
    seed.add(
        AppConfig(
            id=1,
            enabled=True,
            auto_halted=False,
            interval_minutes=37,
            window_overlap_minutes=90,
            backfill_days=30,
            num_of_rows=2,
            max_retries=2,
            pre_spec_enabled=True,
            pre_spec_last_success_dt=None,
        )
    )
    seed.commit()
    seed.close()

    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    monkeypatch.setattr(pre_spec_collector, "SessionLocal", Session)
    monkeypatch.setattr(repository, "SessionLocal", Session, raising=False)
    # init_db 는 시드/마이그레이션만 — 이미 준비했으니 no-op 로 둔다(스케줄러 start 내 호출).
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    return Session


def test_start_scheduler_registers_both_jobs(sched_db):
    """run_now=False 로 띄워 두 잡 등록·옵션·reschedule·shutdown 확인(실구동)."""
    try:
        scheduler.start_scheduler(run_now=False)
        bid = scheduler._scheduler.get_job(scheduler._JOB_ID)
        ps = scheduler._scheduler.get_job(scheduler._PRE_SPEC_JOB_ID)
        assert bid is not None
        assert ps is not None
        for job in (bid, ps):
            assert job.max_instances == 1
            assert job.coalesce is True
            assert int(job.trigger.interval.total_seconds()) == 37 * 60

        # reschedule → 두 잡 모두 interval 갱신.
        scheduler.reschedule(5)
        bid2 = scheduler._scheduler.get_job(scheduler._JOB_ID)
        ps2 = scheduler._scheduler.get_job(scheduler._PRE_SPEC_JOB_ID)
        assert int(bid2.trigger.interval.total_seconds()) == 5 * 60
        assert int(ps2.trigger.interval.total_seconds()) == 5 * 60

        # get_next_run_time: 기본=입찰, job_id 지정=사전규격 (둘 다 시각 반환).
        assert scheduler.get_next_run_time() is not None
        assert scheduler.get_next_run_time(scheduler._PRE_SPEC_JOB_ID) is not None
    finally:
        scheduler.shutdown_scheduler()
    assert scheduler.is_running() is False


def test_tick_pre_spec_scheduled_twice_no_dup(monkeypatch, sched_db):
    """겹치는 윈도우로 scheduled 2회 → pre_spec PK 중복 0(upsert 멱등, 스케줄 경유)."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("00", items=[_item("DUP1"), _item("DUP2")], total_count="2")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)

    # tick_pre_spec 을 직접 2회(겹치는 now/윈도우)로 호출 — 스케줄 진입점 경유.
    scheduler.tick_pre_spec(now=datetime(2026, 6, 1, 12, 0))
    scheduler.tick_pre_spec(now=datetime(2026, 6, 1, 12, 30))

    s = sched_db()
    try:
        total = s.execute(select(func.count()).select_from(PreSpec)).scalar_one()
        assert total == 2  # PK 중복 0(2건만)
        pks = s.execute(select(PreSpec.bf_spec_rgst_no)).scalars().all()
        assert len(pks) == len(set(pks)) == 2
        # source 분포: pre_spec run 2건.
        ps_runs = s.execute(
            select(func.count()).select_from(CollectionRun).where(
                CollectionRun.source == "pre_spec"
            )
        ).scalar_one()
        assert ps_runs == 2
    finally:
        s.close()
