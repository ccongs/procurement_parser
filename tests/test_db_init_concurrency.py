"""Phase 13.6 초기화 경쟁·재시도 검증 — 임시 파일 DB 만 사용."""

import logging
import multiprocessing
from datetime import datetime

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from app import db
from app.classifier import DEFAULT_LIST_RULES
from app.models import AppConfig, BidNotice, BidNoticeRaw, ListRule, SchemaMigration


NOW = datetime(2026, 10, 14, 12)
NEW_COLUMNS = {
    "list_visible", "match_basis", "match_detail",
    "pub_prcrmnt_clsfc_nm", "bef_bid_bbanc_no",
}
INDEXES = {
    "ix_bid_notice_visible_ntce_dt": ["list_visible", "bid_ntce_dt"],
    "ix_bid_notice_visible_openg_dt": ["list_visible", "openg_dt"],
    "ix_bid_notice_indstryty_lmt_yn": ["indstryty_lmt_yn"],
}
RAW_JSON = '{"befBidBbancNo":"OLD", "pubPrcrmntClsfcNm":"정보시스템개발"}'


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'init.db'}", future=True)
    db._register_sqlite_pragmas(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    yield engine
    engine.dispose()


def _old_notice_table(engine):
    """13.1 테스트와 동일하게 신규 컬럼·인덱스가 없는 공고 테이블을 구성."""
    metadata = MetaData()
    table = Table("bid_notice", metadata, *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in BidNotice.__table__.columns if c.name not in NEW_COLUMNS
    ])
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), {
            "bid_ntce_no": "LEGACY", "raw_json": RAW_JSON,
            "collected_at": NOW, "updated_at": NOW,
        })


def _assert_initialized(engine, *, legacy=False):
    inspector = inspect(engine)
    assert set(db.Base.metadata.tables) <= set(inspector.get_table_names())
    assert NEW_COLUMNS <= {c["name"] for c in inspector.get_columns("bid_notice")}
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("bid_notice")}
    for name, columns in INDEXES.items():
        assert indexes[name] == columns
    with sessionmaker(bind=engine)() as session:
        config = session.scalars(select(AppConfig)).one()
        assert config.id == 1 and config.enabled and config.pre_spec_enabled
        assert config.interval_minutes == 60 and config.backfill_days == 30
        assert config.indstryty_cds == "1426,1468,1469,1470"
        assert config.prtcpt_lmt_rgn_cd == "00" and config.presmpt_prce_end == "100000000"
        rules = session.scalars(select(ListRule)).all()
        assert len(rules) == len(DEFAULT_LIST_RULES) == 40
        assert {(r.rule_type, r.value) for r in rules} == set(DEFAULT_LIST_RULES)
        assert all(r.enabled for r in rules)
        markers = session.scalars(select(SchemaMigration)).all()
        assert {m.name for m in markers} == {
            "phase13_match_basis_backfill", "phase13_list_rule_seed",
        }
        assert len(markers) == 2 and all(m.applied_at is not None for m in markers)
        if legacy:
            notice = session.get(BidNotice, "LEGACY")
            raw = session.get(BidNoticeRaw, "LEGACY")
            assert notice.raw_json is None and raw.raw_json == RAW_JSON
            assert raw.updated_at == NOW
            assert notice.list_visible and notice.match_basis == "업종"
            assert notice.bef_bid_bbanc_no == "OLD"
            assert notice.pub_prcrmnt_clsfc_nm == "정보시스템개발"


@pytest.mark.parametrize("stage", ["create_all", "alter", "index", "raw", "config"])
def test_other_connection_wins_after_check_before_creation(database, monkeypatch, stage):
    engine = database
    if stage != "create_all":
        _old_notice_table(engine)
    attempts, sleeps, connections, errors = [], [], [], []
    create_all = db.Base.metadata.create_all

    def count_attempt(**kwargs):
        attempts.append(True)
        return create_all(**kwargs)

    monkeypatch.setattr(db.Base.metadata, "create_all", count_attempt)
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    def compete(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip()
        target = {
            "create_all": sql.startswith("CREATE TABLE"),
            "alter": sql.startswith("ALTER TABLE bid_notice ADD COLUMN"),
            "index": any(sql.startswith(f"CREATE INDEX {name} ") for name in INDEXES),
            "raw": sql.startswith("INSERT INTO bid_notice_raw "),
            "config": sql.startswith("INSERT INTO app_config "),
        }[stage]
        if not target or connections:
            return
        # 존재 확인 직후·생성 직전에 별도 연결이 같은 객체를 먼저 만든다.
        connections.append(conn)
        with engine.begin() as other:
            assert other is not conn
            connections.append(other)
            if stage == "create_all":
                create_all(bind=other)
            elif stage in {"alter", "index"}:
                other.exec_driver_sql(statement, parameters)
            elif stage == "raw":
                other.execute(BidNoticeRaw.__table__.insert(), {
                    "bid_ntce_no": "LEGACY", "raw_json": RAW_JSON, "updated_at": NOW,
                })
            else:
                # 경쟁자도 동일 기본값을 사용하며 별도 세션에서 커밋한다.
                db._seed_app_config()

    def record_error(context):
        errors.append(context.sqlalchemy_exception)

    event.listen(engine, "before_cursor_execute", compete)
    event.listen(engine, "handle_error", record_error)
    try:
        db.init_db()
    finally:
        event.remove(engine, "before_cursor_execute", compete)
        event.remove(engine, "handle_error", record_error)
    assert len(connections) == 2
    assert len(errors) == 1
    assert isinstance(errors[0], IntegrityError if stage in {"raw", "config"} else OperationalError)
    assert len(attempts) == (1 if stage == "config" else 2)
    assert sleeps == ([] if stage == "config" else [0.2])
    _assert_initialized(engine, legacy=stage != "create_all")
    db.init_db()
    _assert_initialized(engine, legacy=stage != "create_all")


@pytest.mark.parametrize("error_type", [OperationalError, IntegrityError, ProgrammingError])
@pytest.mark.parametrize("stage", [
    "create_all", "_migrate_add_columns", "_migrate_match_basis_backfill",
    "_migrate_bid_notice_indexes", "_migrate_bid_notice_raw", "_seed_app_config",
    "_seed_list_rules",
])
def test_init_retries_entire_sequence_three_times_then_raises(
    database, monkeypatch, caplog, stage, error_type,
):
    stages = [
        "create_all", "_migrate_add_columns", "_migrate_match_basis_backfill",
        "_migrate_bid_notice_indexes", "_migrate_bid_notice_raw", "_seed_app_config",
        "_seed_list_rules",
    ]
    calls, errors, sleeps = [], [], []

    def step(name):
        def run(*args, **kwargs):
            calls.append(name)
            if name == stage:
                error = error_type("SQL", {}, Exception("반복 실패\n주입"))
                errors.append(error)
                raise error
        return run

    monkeypatch.setattr(db.Base.metadata, "create_all", step("create_all"))
    for name in stages[1:]:
        monkeypatch.setattr(db, name, step(name))
    monkeypatch.setattr(db.time, "sleep", sleeps.append)
    with caplog.at_level(logging.WARNING, logger="app.db"):
        with pytest.raises(error_type) as raised:
            db.init_db()
    assert len(errors) == 3 and raised.value is errors[-1]
    assert calls == stages[:stages.index(stage) + 1] * 3
    assert sleeps == [0.2, 0.5]
    warnings = [r.getMessage() for r in caplog.records if r.name == "app.db"]
    assert len(warnings) == 2
    assert "2/3" in warnings[0] and "3/3" in warnings[1]
    assert all(error_type.__name__ in w and "반복 실패 주입" in w and "\n" not in w
               for w in warnings)


def test_init_does_not_retry_unrelated_error(database, monkeypatch):
    error = RuntimeError("DB 경쟁이 아닌 오류")
    calls = []

    def fail(**kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(db.Base.metadata, "create_all", fail)
    with pytest.raises(RuntimeError) as raised:
        db.init_db()
    assert raised.value is error and calls == [True]


@pytest.mark.parametrize("failures", [1, 2])
def test_config_missing_after_integrity_error_retries_once(database, monkeypatch, failures):
    engine = database
    db.Base.metadata.create_all(engine)
    sessions, inserts, errors = [], [], []
    local = db.SessionLocal

    def record_session():
        session = local()
        sessions.append(session)
        return session

    def fail_after_insert(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("INSERT INTO app_config "):
            return
        inserts.append(True)
        if len(errors) < failures:
            error = IntegrityError(statement, parameters, Exception("설정 생성 실패 주입"))
            errors.append(error)
            # 실제 INSERT 뒤 실패시켜 롤백 후 행 부재까지 확인한다.
            raise error

    monkeypatch.setattr(db, "SessionLocal", record_session)
    event.listen(engine, "after_cursor_execute", fail_after_insert)
    try:
        if failures == 1:
            db._seed_app_config()
        else:
            with pytest.raises(IntegrityError) as raised:
                db._seed_app_config()
            assert raised.value is errors[-1]
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_insert)
    assert len(inserts) == 2
    assert len(sessions) == (3 if failures == 1 else 4)
    assert len({id(s) for s in sessions}) == len(sessions)
    assert all(not s.in_transaction() for s in sessions)
    with local() as session:
        assert len(session.scalars(select(AppConfig)).all()) == (1 if failures == 1 else 0)


def _init_worker(database_url, barrier):
    """spawn 자식 프로세스마다 임시 DB 전용 engine 을 사용."""
    import os

    os.environ["DATABASE_URL"] = database_url
    from app import db as worker_db

    # pytest 수집 시 임포트에 의존하지 않고 명시적으로 임시 DB 에 연결한다.
    worker_db.engine.dispose()
    worker_db.engine = create_engine(database_url, future=True)
    worker_db._register_sqlite_pragmas(worker_db.engine)
    worker_db.SessionLocal = sessionmaker(bind=worker_db.engine, autoflush=False)
    try:
        barrier.wait(timeout=20)
        worker_db.init_db()
    finally:
        worker_db.engine.dispose()


@pytest.mark.parametrize("legacy", [False, True], ids=["new", "old-schema"])
def test_four_processes_init_together(database, monkeypatch, legacy):
    engine = database
    if legacy:
        _old_notice_table(engine)
    database_url = str(engine.url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [context.Process(target=_init_worker, args=(database_url, barrier))
                 for _ in range(4)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
        assert [p.exitcode for p in processes] == [0] * 4
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
    _assert_initialized(engine, legacy=legacy)
