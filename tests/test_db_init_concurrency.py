"""Phase 13.6 초기화 경쟁·재시도 검증 — 임시 파일 DB 만 사용."""

import logging
import multiprocessing
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Index, MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SAWarning
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
STAGE_LABELS = {
    "create_all": "모델 테이블 생성",
    "_migrate_add_columns": "컬럼 추가",
    "_migrate_match_basis_backfill": "기존 분류값 보정",
    "_migrate_bid_notice_indexes": "모델 인덱스 확인",
    "_migrate_bid_notice_raw": "원문 이관",
    "_seed_app_config": "설정 기본값 시드",
    "_seed_list_rules": "표시 규칙 시드",
    "_validate_model_indexes": "모델 인덱스 최종 검증",
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
    indexes = {i["name"].lower(): i["column_names"] for i in inspector.get_indexes("bid_notice")}
    for name, columns in INDEXES.items():
        assert indexes[name] == columns
    for table in db.Base.metadata.sorted_tables:
        actual = {i["name"].lower(): i for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            assert actual[index.name]["column_names"] == [c.name for c in index.columns]
            assert bool(actual[index.name]["unique"]) == bool(index.unique)
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


@pytest.mark.parametrize("stage", ["create_all", "alter", "index", "raw", "config", "sequence"])
def test_other_connection_wins_after_check_before_creation(database, monkeypatch, stage):
    engine = database
    if stage != "create_all":
        _old_notice_table(engine)
    attempts, sleeps, connections, errors = [], [], [], []
    create_all = db.Base.metadata.create_all
    create_tables = db._create_model_tables
    competed = []

    def count_attempt():
        attempts.append(True)
        return create_tables()

    monkeypatch.setattr(db, "_create_model_tables", count_attempt)
    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))

    def compete(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip()
        targets = {
            "create_all": sql.startswith("CREATE TABLE"),
            "alter": sql.startswith("ALTER TABLE bid_notice ADD COLUMN"),
            "index": any(sql.startswith(f"CREATE INDEX {name} ") for name in INDEXES),
            "raw": sql.startswith("INSERT INTO bid_notice_raw "),
            "config": sql.startswith("INSERT INTO app_config "),
        }
        candidates = ("create_all", "alter", "index") if stage == "sequence" else (stage,)
        target = next((name for name in candidates if targets[name] and name not in competed), None)
        if target is None:
            return
        # 존재 확인 직후·생성 직전에 별도 연결이 같은 객체를 먼저 만든다.
        competed.append(target)
        connections.append(conn)
        with engine.begin() as other:
            assert other is not conn
            connections.append(other)
            if target == "create_all":
                create_all(bind=other)
            elif target in {"alter", "index"}:
                other.exec_driver_sql(statement, parameters)
            elif target == "raw":
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
    assert competed == (["create_all", "alter", "index"] if stage == "sequence" else [stage])
    assert len(connections) == (6 if stage == "sequence" else 2)
    assert len(errors) == (3 if stage == "sequence" else 1)
    assert all(isinstance(error, IntegrityError if stage in {"raw", "config"}
                          else OperationalError) for error in errors)
    assert len(attempts) == 1
    assert sleeps == []
    _assert_initialized(engine, legacy=stage != "create_all")
    db.init_db()
    _assert_initialized(engine, legacy=stage != "create_all")


@pytest.mark.parametrize("error_type", [OperationalError, IntegrityError, ProgrammingError])
@pytest.mark.parametrize("stage", [
    "create_all", "_migrate_add_columns", "_migrate_match_basis_backfill",
    "_migrate_bid_notice_indexes", "_migrate_bid_notice_raw", "_seed_app_config",
    "_seed_list_rules", "_validate_model_indexes",
])
def test_init_retries_entire_sequence_three_times_then_raises(
    database, monkeypatch, caplog, stage, error_type,
):
    stages = [
        "create_all", "_migrate_add_columns", "_migrate_match_basis_backfill",
        "_migrate_bid_notice_indexes", "_migrate_bid_notice_raw", "_seed_app_config",
        "_seed_list_rules", "_validate_model_indexes",
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

    monkeypatch.setattr(db, "_create_model_tables", step("create_all"))
    for name in stages[1:]:
        monkeypatch.setattr(db, name, step(name))
    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    with caplog.at_level(logging.WARNING, logger="app.db"):
        with pytest.raises(error_type) as raised:
            db.init_db()
    assert len(errors) == 3 and raised.value is errors[-1]
    assert calls == stages[:stages.index(stage) + 1] * 3
    assert sleeps == [0.2, 0.5]
    warnings = [r.getMessage() for r in caplog.records if r.name == "app.db"]
    assert len(warnings) == 2
    assert "2/3" in warnings[0] and "3/3" in warnings[1]
    assert all(f"단계={STAGE_LABELS[stage]}" in warning for warning in warnings)
    assert "대기=0.2초" in warnings[0] and "대기=0.5초" in warnings[1]
    assert all(error_type.__name__ in w and "반복 실패 주입" in w and "\n" not in w
               for w in warnings)
    assert all("SQL" not in w and "{}" not in w for w in warnings)


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


@pytest.mark.parametrize("index_name", ["ix_bid_notice_bid_ntce_dt", "ix_pre_spec_rcpt_dt"])
def test_create_all_partial_index_failure_recovers_all_model_indexes(
    database, monkeypatch, index_name,
):
    failures, sleeps = [], []

    def fail_index_once(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(f"CREATE INDEX {index_name} ") and not failures:
            failures.append(True)
            raise OperationalError(statement, parameters, Exception("인덱스 생성 실패 주입"))

    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    event.listen(database, "before_cursor_execute", fail_index_once)
    try:
        db.init_db()
    finally:
        event.remove(database, "before_cursor_execute", fail_index_once)
    assert failures == [True]
    assert sleeps == []
    _assert_initialized(database)


@pytest.mark.parametrize("index_name", ["ix_bid_notice_dminstt_cd", "ix_pre_spec_rcpt_dt"])
def test_validate_model_indexes_rejects_missing_index(database, index_name):
    db.init_db()
    index = next(index for table in db.Base.metadata.sorted_tables
                 for index in table.indexes if index.name == index_name)
    index.drop(bind=database)
    with pytest.raises(OperationalError, match=index_name):
        db._validate_model_indexes()
    db.init_db()
    _assert_initialized(database)


def test_init_retries_when_final_index_validation_finds_missing_index(
    database, monkeypatch, caplog,
):
    seed_rules = db._seed_list_rules
    calls, sleeps = [], []
    index = next(i for i in BidNotice.__table__.indexes if i.name == "ix_bid_notice_openg_dt")

    def drop_after_index_migration():
        seed_rules()
        calls.append(True)
        if len(calls) == 1:
            index.drop(bind=database)

    monkeypatch.setattr(db, "_seed_list_rules", drop_after_index_migration)
    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    with caplog.at_level(logging.WARNING, logger="app.db"):
        db.init_db()
    assert calls == [True, True] and sleeps == [0.2]
    assert "단계=모델 인덱스 최종 검증" in caplog.text
    _assert_initialized(database)


@pytest.mark.parametrize("uppercase", [False, True], ids=["lowercase", "uppercase"])
@pytest.mark.parametrize("mismatch", ["columns", "column-order", "unique", "partial"])
@pytest.mark.parametrize("phase", ["existing", "race", "final-validation"])
def test_index_definition_mismatch_stops_init_without_retry(
    database, monkeypatch, caplog, mismatch, phase, uppercase,
):
    db.init_db()
    index = next(i for i in BidNotice.__table__.indexes
                 if i.name == "ix_bid_notice_visible_ntce_dt")
    columns = [c.name for c in index.columns]
    if mismatch == "columns":
        columns = ["bid_ntce_no"]
    elif mismatch == "column-order":
        columns.reverse()

    def create_wrong_index():
        metadata = MetaData()
        table = Table("bid_notice", metadata, *[
            Column(c.name, c.type) for c in index.columns
        ], Column("bid_ntce_no", BidNotice.__table__.c.bid_ntce_no.type))
        options = {"sqlite_where": table.c.list_visible.is_(True)} if mismatch == "partial" else {}
        Index(index.name.upper() if uppercase else index.name, *(table.c[c] for c in columns),
              unique=mismatch == "unique", **options).create(bind=database)

    attempts, sleeps, competed = [], [], []
    create_tables = db._create_model_tables

    def count_attempt():
        attempts.append(True)
        create_tables()

    monkeypatch.setattr(db, "_create_model_tables", count_attempt)
    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    if phase == "existing":
        index.drop(bind=database)
        create_wrong_index()
    elif phase == "race":
        index.drop(bind=database)
    else:
        seed_rules = db._seed_list_rules

        def replace_before_validation():
            seed_rules()
            index.drop(bind=database)
            create_wrong_index()

        monkeypatch.setattr(db, "_seed_list_rules", replace_before_validation)

    def compete(conn, cursor, statement, parameters, context, executemany):
        if phase == "race" and statement.lstrip().startswith(
            f"CREATE INDEX {index.name} "
        ) and not competed:
            competed.append(True)
            create_wrong_index()

    event.listen(database, "before_cursor_execute", compete)
    try:
        with caplog.at_level(logging.WARNING, logger="app.db"):
            with pytest.raises(db.IndexDefinitionMismatchError) as raised:
                db.init_db()
    finally:
        event.remove(database, "before_cursor_execute", compete)
    expected = {"column_names": [c.name for c in index.columns], "unique": False}
    actual = {"column_names": columns, "unique": mismatch == "unique"}
    message = str(raised.value)
    assert "bid_notice" in message and index.name in message
    assert f"기대 정의={expected!r}" in message
    if mismatch == "partial":
        assert "실제 정의=" in message and "sqlite_where" in message
    else:
        assert f"실제 정의={actual!r}" in message
    assert message.endswith("해당 인덱스를 DROP 한 뒤 재기동하면 모델 정의로 다시 생성됩니다")
    assert attempts == [True] and sleeps == []
    assert competed == ([True] if phase == "race" else [])
    assert not [r for r in caplog.records if r.name == "app.db" and r.levelno >= logging.WARNING]


def test_uppercase_index_name_with_same_definition_is_accepted(database, monkeypatch):
    db.init_db()
    index = next(i for i in BidNotice.__table__.indexes
                 if i.name == "ix_bid_notice_visible_ntce_dt")
    index.drop(bind=database)
    table = Table("bid_notice", MetaData(), *[
        Column(c.name, c.type) for c in index.columns
    ])
    Index(index.name.upper(), *table.c).create(bind=database)
    sleeps, creates = [], []

    def record_create(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("CREATE INDEX"):
            creates.append(statement)

    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    event.listen(database, "before_cursor_execute", record_create)
    try:
        db.init_db()
    finally:
        event.remove(database, "before_cursor_execute", record_create)
    assert sleeps == [] and creates == []
    _assert_initialized(database)


@pytest.mark.parametrize("uppercase", [False, True], ids=["lowercase", "uppercase"])
@pytest.mark.parametrize("phase", ["existing", "race"])
def test_unreflectable_expression_index_stops_init_without_retry(
    database, monkeypatch, caplog, phase, uppercase,
):
    db.init_db()
    index = next(i for i in BidNotice.__table__.indexes
                 if i.name == "ix_bid_notice_visible_ntce_dt")
    index.drop(bind=database)
    attempts, sleeps, creates = [], [], []
    create_tables = db._create_model_tables

    def count_attempt():
        attempts.append(True)
        create_tables()

    def create_expression_index():
        with database.begin() as conn:
            name = index.name.upper() if uppercase else index.name
            conn.exec_driver_sql(f"CREATE INDEX {name} ON bid_notice (lower(bid_ntce_no))")

    def compete(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(f"CREATE INDEX {index.name} ") and (
            "lower(bid_ntce_no)" not in statement
        ):
            creates.append(True)
            if phase == "race" and len(creates) == 1:
                create_expression_index()

    if phase == "existing":
        create_expression_index()
    monkeypatch.setattr(db, "_create_model_tables", count_attempt)
    monkeypatch.setattr(db, "time", SimpleNamespace(sleep=sleeps.append))
    event.listen(database, "before_cursor_execute", compete)
    try:
        with caplog.at_level(logging.WARNING, logger="app.db"):
            with pytest.warns(SAWarning, match="Skipped unsupported reflection of expression-based index"):
                with pytest.raises(db.IndexDefinitionMismatchError) as raised:
                    db.init_db()
    finally:
        event.remove(database, "before_cursor_execute", compete)
    message = str(raised.value)
    assert index.name in message and "bid_notice" in message
    assert "같은 이름의 인덱스가 있으나 정의를 확인할 수 없음" in message
    assert "DROP 한 뒤 재기동" in message
    assert attempts == [True] and sleeps == [] and creates == [True]
    assert not [r for r in caplog.records if r.name == "app.db" and r.levelno >= logging.WARNING]


@pytest.mark.parametrize("extra", [
    {"dialect_options": {"sqlite_where": "list_visible = 1"}},
    {"dialect_options": {"postgresql_where": "list_visible = true"}},
    {"expressions": ["lower(bid_ntce_no)"]},
    {"column_names": [None, "bid_ntce_dt"]},
])
def test_reflected_condition_or_expression_is_definition_mismatch(monkeypatch, extra):
    index = next(i for i in BidNotice.__table__.indexes
                 if i.name == "ix_bid_notice_visible_ntce_dt")
    reflected = {
        "name": index.name.upper(), "column_names": [c.name for c in index.columns],
        "unique": False, **extra,
    }
    monkeypatch.setattr(db, "inspect", lambda engine: SimpleNamespace(
        get_indexes=lambda *args, **kwargs: [reflected],
    ))
    definitions = db._index_definitions(index.table)
    with pytest.raises(db.IndexDefinitionMismatchError, match="모델 인덱스 정의 불일치"):
        db._model_index_exists(index, definitions)


@pytest.mark.parametrize("attribute,code", [("sqlstate", "42P07"), ("pgcode", "42710")])
def test_unreflectable_index_with_dialect_duplicate_error_is_mismatch(
    monkeypatch, attribute, code,
):
    index = next(i for i in BidNotice.__table__.indexes
                 if i.name == "ix_bid_notice_visible_ntce_dt")
    original = Exception("중복 인덱스 오류")
    setattr(original, attribute, code)
    error = ProgrammingError(None, None, original)

    def fail_create(*args, **kwargs):
        raise error

    monkeypatch.setattr(index, "create", fail_create)
    monkeypatch.setattr(db, "_index_definitions", lambda table: {})
    with pytest.raises(db.IndexDefinitionMismatchError,
                       match="같은 이름의 인덱스가 있으나 정의를 확인할 수 없음") as raised:
        db._create_model_index(index, {})
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("legacy", [False, True], ids=["new", "old-schema"])
def test_valid_index_definitions_and_one_inspection_per_table_per_stage(
    database, monkeypatch, legacy,
):
    if legacy:
        _old_notice_table(database)
    db.init_db()
    _assert_initialized(database, legacy=legacy)
    inspections = Counter()

    def track_inspect(*args, **kwargs):
        inspector = inspect(*args, **kwargs)
        get_indexes = inspector.get_indexes

        def track_get_indexes(table_name, **kwargs):
            inspections[table_name] += 1
            return get_indexes(table_name, **kwargs)

        inspector.get_indexes = track_get_indexes
        return inspector

    monkeypatch.setattr(db, "inspect", track_inspect)
    tables = {table.name for table in db.Base.metadata.sorted_tables if table.indexes}
    for stage in (db._migrate_bid_notice_indexes, db._validate_model_indexes):
        inspections.clear()
        stage()
        assert inspections == Counter({name: 1 for name in tables})
    _assert_initialized(database, legacy=legacy)


@pytest.mark.parametrize("failures", [1, 2])
def test_raw_integrity_error_retries_batch_once_in_new_transaction(
    database, failures,
):
    db.init_db()
    with database.begin() as conn:
        conn.execute(BidNotice.__table__.insert(), {
            "bid_ntce_no": "LEGACY", "raw_json": RAW_JSON,
            "collected_at": NOW, "updated_at": NOW,
        })
    transactions, errors = [], []

    def fail_after_update(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("UPDATE bid_notice SET"):
            return
        transactions.append(conn.get_transaction())
        if len(errors) < failures:
            error = IntegrityError(statement, parameters, Exception("배치 충돌 주입"))
            errors.append(error)
            raise error

    event.listen(database, "after_cursor_execute", fail_after_update)
    try:
        if failures == 1:
            db._migrate_bid_notice_raw()
        else:
            with pytest.raises(IntegrityError) as raised:
                db._migrate_bid_notice_raw()
            assert raised.value is errors[-1]
    finally:
        event.remove(database, "after_cursor_execute", fail_after_update)
    assert len(transactions) == 2 and transactions[0] is not transactions[1]
    assert all(not transaction.is_active for transaction in transactions)
    with db.SessionLocal() as session:
        notice = session.get(BidNotice, "LEGACY")
        raw = session.get(BidNoticeRaw, "LEGACY")
        assert notice.raw_json == (None if failures == 1 else RAW_JSON)
        assert (raw.raw_json if raw else None) == (RAW_JSON if failures == 1 else None)


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


def _init_worker(database_url, barrier, stderr_path):
    from contextlib import redirect_stderr
    import traceback

    with open(stderr_path, "w") as stderr, redirect_stderr(stderr):
        try:
            _run_init_worker(database_url, barrier)
        except BaseException:
            traceback.print_exc(file=stderr)
            raise


def _run_init_worker(database_url, barrier):
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
def test_four_processes_init_together(database, monkeypatch, tmp_path, legacy):
    engine = database
    if legacy:
        _old_notice_table(engine)
    database_url = str(engine.url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    stderr_paths = [tmp_path / f"worker-{i}.stderr" for i in range(4)]
    processes = [context.Process(target=_init_worker, args=(database_url, barrier, stderr_path))
                 for stderr_path in stderr_paths]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=90)
        diagnostics = "\n".join(
            f"pid={process.pid}, exitcode={process.exitcode}, stderr="
            f"{stderr_path.read_text() if stderr_path.exists() else '<미생성>'}"
            for process, stderr_path in zip(processes, stderr_paths)
        )
        assert [p.exitcode for p in processes] == [0] * 4, diagnostics
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
    _assert_initialized(engine, legacy=legacy)
