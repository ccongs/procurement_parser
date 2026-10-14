"""Phase 13.1 스키마·원문 이관 검증 — 임시 DB 만 사용."""

from datetime import datetime
import json

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import db
from app.models import BidLicenseLimit, BidNotice, BidNoticeRaw, LicenseVerification, ListRule, SchemaMigration


NEW_COLUMNS = {
    "list_visible", "match_basis", "match_detail",
    "pub_prcrmnt_clsfc_nm", "bef_bid_bbanc_no",
}
INDEXES = {
    "ix_bid_notice_visible_ntce_dt": ["list_visible", "bid_ntce_dt"],
    "ix_bid_notice_visible_openg_dt": ["list_visible", "openg_dt"],
    "ix_bid_notice_indstryty_lmt_yn": ["indstryty_lmt_yn"],
}
NOW = datetime(2026, 10, 7, 12)
BACKFILL_NAME = "phase13_match_basis_backfill"


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema13.db'}", future=True)
    local = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", local)
    # Phase 13.1 스키마 검증은 이후 추가된 기본 규칙 시드와 격리한다.
    monkeypatch.setattr(db, "_seed_list_rules", lambda: None)
    yield engine, local
    engine.dispose()


def _assert_notice_schema(engine):
    inspector = inspect(engine)
    assert {"bid_notice_raw", "bid_license_limit", "list_rule", "schema_migration"} <= set(inspector.get_table_names())
    columns = {c["name"]: c for c in inspector.get_columns("bid_notice")}
    assert NEW_COLUMNS <= columns.keys()
    assert columns["list_visible"]["nullable"] is False
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("bid_notice")}
    for name, fields in INDEXES.items():
        assert indexes[name] == fields


def test_new_schema_tables_columns_indexes_and_rule_unique(database):
    engine, local = database
    db.init_db()
    _assert_notice_schema(engine)
    inspector = inspect(engine)
    assert inspector.get_pk_constraint("schema_migration")["constrained_columns"] == ["name"]
    migration_columns = {c["name"]: c for c in inspector.get_columns("schema_migration")}
    assert migration_columns["name"]["type"].length == 100
    assert isinstance(migration_columns["applied_at"]["type"], type(SchemaMigration.applied_at.type))
    assert migration_columns["applied_at"]["nullable"] is False
    assert inspector.get_pk_constraint("bid_notice_raw")["constrained_columns"] == ["bid_ntce_no"]
    assert inspector.get_foreign_keys("bid_notice_raw") == []
    assert inspector.get_pk_constraint("bid_license_limit")["constrained_columns"] == [
        "bid_ntce_no", "bid_ntce_ord", "lmt_grp_no", "lmt_sno",
    ]
    assert {tuple(i["column_names"]) for i in inspector.get_indexes("bid_license_limit")} == {
        ("lcns_cd",),
    }
    for table, fields in (
        ("bid_notice_raw", ["updated_at"]),
        ("bid_license_limit", ["collected_at"]),
        ("list_rule", ["rule_type", "value", "enabled", "created_at"]),
    ):
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        assert all(cols[field]["nullable"] is False for field in fields)
    with local() as session:
        marker = session.execute(select(SchemaMigration)).scalar_one()
        assert marker.name == BACKFILL_NAME and marker.applied_at is not None
        assert session.execute(select(BidNotice)).scalars().all() == []
        assert session.execute(select(ListRule)).scalars().all() == []
        assert session.execute(select(BidLicenseLimit)).scalars().all() == []
        session.add_all([
            ListRule(rule_type="clsfc", value="정보시스템개발"),
            ListRule(rule_type="keyword", value="정보시스템개발"),
        ])
        session.commit()
        rules = session.execute(select(ListRule)).scalars().all()
        assert all(rule.enabled is True and rule.created_at is not None for rule in rules)
        session.add(ListRule(rule_type="clsfc", value="정보시스템개발"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def _old_notice_table(engine, retained_columns=()):
    """신규 5컬럼·테이블·인덱스가 없는 기존 공고 스키마를 생성."""
    metadata = MetaData()
    table = Table("bid_notice", metadata, *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in BidNotice.__table__.columns
        if c.name not in NEW_COLUMNS or c.name in retained_columns
    ])
    metadata.create_all(engine)
    return table


def test_license_superseded_column_migration_twice_preserves_old_rows(database):
    engine, local = database
    metadata = MetaData()
    table = Table("bid_license_limit", metadata, *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in BidLicenseLimit.__table__.columns if c.name != "superseded_at"
    ])
    metadata.create_all(engine)
    values = dict(bid_ntce_no="OLD-LICENSE", bid_ntce_ord="001", lmt_grp_no="1",
                  lmt_sno="1", lcns_cd="1468", collected_at=NOW)
    with engine.begin() as conn:
        conn.execute(table.insert(), values)
    alters = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("ALTER TABLE bid_license_limit"):
            alters.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        for _ in range(2):
            db.init_db()
            column = next(c for c in inspect(engine).get_columns("bid_license_limit")
                          if c["name"] == "superseded_at")
            assert column["nullable"] is True
            with local() as session:
                row = session.scalars(select(BidLicenseLimit)).one()
                assert row.superseded_at is None
                assert all(getattr(row, name) == value for name, value in values.items())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(alters) == 1


def _snapshot(engine):
    with engine.connect() as conn:
        return (
            conn.execute(select(BidNotice.__table__).order_by(BidNotice.bid_ntce_no)).all(),
            conn.execute(select(BidNoticeRaw.__table__).order_by(BidNoticeRaw.bid_ntce_no)).all(),
            conn.execute(select(SchemaMigration.__table__).order_by(SchemaMigration.name)).all(),
        )


@pytest.mark.parametrize("count", [3, 501])
def test_old_schema_migration_twice_and_batch_boundaries(database, count):
    engine, local = database
    table = _old_notice_table(engine)
    originals = [
        json.dumps({"befBidBbancNo": " OLD-13 ", "pubPrcrmntClsfcNm": " 정보시스템개발 "}, ensure_ascii=False),
        json.dumps({"pubPrcrmntClsfcNm": " 일반용역 "}, ensure_ascii=False),
        "{broken json",
    ] + ["[]"] * (count - 3)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            {"bid_ntce_no": f"OLD-{i:04}", "raw_json": raw,
             "ntce_kind_nm": "재공고" if i == 0 else "등록공고",
             "collected_at": NOW, "updated_at": NOW}
            for i, raw in enumerate(originals)
        ])
    batches = []
    transaction_sizes = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO bid_notice_raw"):
            batches.append(len(parameters) if executemany else 1)

    def capture_commit(conn):
        transaction_sizes.append(sum(batches))

    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "commit", capture_commit)
    try:
        db.init_db()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "commit", capture_commit)
    assert batches == ([3] if count == 3 else [500, 1])
    if count == 501:
        assert 500 in transaction_sizes and 501 in transaction_sizes
    _assert_notice_schema(engine)
    with local() as session:
        assert session.get(SchemaMigration, BACKFILL_NAME).applied_at is not None
        for i, original in enumerate(originals):
            row = session.get(BidNotice, f"OLD-{i:04}")
            raw = session.get(BidNoticeRaw, row.bid_ntce_no)
            assert row.raw_json is None
            assert raw.raw_json == original and raw.updated_at == NOW
            assert row.match_basis == "업종" and row.list_visible is True
            assert row.match_detail is None
            assert row.bef_bid_bbanc_no == ("OLD-13" if i == 0 else None)
            assert row.pub_prcrmnt_clsfc_nm == (
                "정보시스템개발" if i == 0 else "일반용역" if i == 1 else None
            )
    first = _snapshot(engine)
    db.init_db()
    assert _snapshot(engine) == first


def test_migration_preserves_existing_raw_and_filled_columns(database):
    engine, local = database
    db.init_db()
    with local() as session:
        session.add_all([
            BidNotice(
                bid_ntce_no="PARTIAL-13", collected_at=NOW, updated_at=NOW,
                raw_json='{"befBidBbancNo":"new", "pubPrcrmntClsfcNm":"new"}',
                bef_bid_bbanc_no="kept", pub_prcrmnt_clsfc_nm="kept",
                match_basis="분류", match_detail="kept", list_visible=False,
            ),
            BidNoticeRaw(bid_ntce_no="PARTIAL-13", raw_json="kept", updated_at=NOW),
            BidNotice(
                bid_ntce_no="EMPTY-13", collected_at=NOW, updated_at=NOW,
                raw_json='{"befBidBbancNo":" trimmed ", "pubPrcrmntClsfcNm":" trimmed "}',
                bef_bid_bbanc_no="  ", pub_prcrmnt_clsfc_nm="",
            ),
        ])
        session.commit()
    db.init_db()
    with local() as session:
        row = session.get(BidNotice, "PARTIAL-13")
        assert row.raw_json is None
        assert row.bef_bid_bbanc_no == row.pub_prcrmnt_clsfc_nm == "kept"
        assert row.match_basis == "분류" and row.match_detail == "kept"
        assert row.list_visible is False
        assert session.get(BidNoticeRaw, "PARTIAL-13").raw_json == "kept"
        empty = session.get(BidNotice, "EMPTY-13")
        assert empty.bef_bid_bbanc_no == empty.pub_prcrmnt_clsfc_nm == "trimmed"
    first = _snapshot(engine)
    db.init_db()
    assert _snapshot(engine) == first


def test_classification_migrates_without_any_legacy_raw(database):
    engine, local = database
    table = _old_notice_table(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), {
            "bid_ntce_no": "NO-RAW-13", "collected_at": NOW, "updated_at": NOW,
        })
    db.init_db()
    with local() as session:
        row = session.get(BidNotice, "NO-RAW-13")
        assert row.list_visible is True and row.match_basis == "업종"
        assert session.get(BidNoticeRaw, "NO-RAW-13") is None
    first = _snapshot(engine)
    db.init_db()
    assert _snapshot(engine) == first


def test_completed_backfill_preserves_null_basis_after_two_inits(database):
    engine, local = database
    db.init_db()
    with local() as session:
        assert session.get(SchemaMigration, BACKFILL_NAME).applied_at is not None
    with local() as session:
        session.add_all([
            BidNotice(
                bid_ntce_no=f"EXISTING-{visible}", list_visible=visible, match_basis=None,
                collected_at=NOW, updated_at=NOW,
            )
            for visible in (False, True)
        ])
        session.commit()
    before = _snapshot(engine)
    db.init_db()
    db.init_db()
    assert _snapshot(engine) == before
    with local() as session:
        for visible in (False, True):
            row = session.get(BidNotice, f"EXISTING-{visible}")
            assert row.list_visible is visible and row.match_basis is None


@pytest.mark.parametrize("retained_columns", [{"list_visible"}, NEW_COLUMNS])
def test_missing_backfill_marker_only_backfills_visible_notices(database, retained_columns):
    engine, local = database
    table = _old_notice_table(engine, retained_columns=retained_columns)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            {"bid_ntce_no": f"LEGACY-{visible}", "list_visible": visible,
             "collected_at": NOW, "updated_at": NOW}
            for visible in (False, True)
        ])
    db.init_db()
    with local() as session:
        assert session.get(SchemaMigration, BACKFILL_NAME).applied_at is not None
        hidden = session.get(BidNotice, "LEGACY-False")
        visible = session.get(BidNotice, "LEGACY-True")
        assert hidden.list_visible is False and hidden.match_basis is None
        assert visible.list_visible is True and visible.match_basis == "업종"
    before = _snapshot(engine)
    db.init_db()
    assert _snapshot(engine) == before


@pytest.mark.parametrize("failure_point", ["alter", "update", "marker_insert"])
def test_backfill_failure_rolls_back_dml_and_retries(database, failure_point):
    engine, local = database
    table = _old_notice_table(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), {
            "bid_ntce_no": "RETRY-13", "collected_at": NOW, "updated_at": NOW,
        })

    def fail_after_statement(conn, cursor, statement, parameters, context, executemany):
        target = {
            "alter": "ALTER TABLE bid_notice ADD COLUMN match_basis ",
            "update": "UPDATE bid_notice SET match_basis=",
            "marker_insert": "INSERT INTO schema_migration ",
        }[failure_point]
        if statement.startswith(target):
            raise RuntimeError("분류값 보정 실패 주입")

    event.listen(engine, "after_cursor_execute", fail_after_statement)
    try:
        with pytest.raises(RuntimeError, match="분류값 보정 실패 주입"):
            db.init_db()
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_statement)

    # SQLite ALTER 는 남지만 보정 UPDATE·완료 INSERT 는 예외 시 함께 롤백된다.
    assert "match_basis" in {c["name"] for c in inspect(engine).get_columns("bid_notice")}
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT list_visible, match_basis FROM bid_notice WHERE bid_ntce_no='RETRY-13'"
        )).one() == (True, None)
        assert conn.execute(select(SchemaMigration)).all() == []

    db.init_db()
    with local() as session:
        row = session.get(BidNotice, "RETRY-13")
        assert row.list_visible is True and row.match_basis == "업종"
        marker = session.execute(select(SchemaMigration)).scalar_one()
        assert marker.name == BACKFILL_NAME and marker.applied_at is not None
    first = _snapshot(engine)
    db.init_db()
    assert _snapshot(engine) == first


def test_list_rule_sql_insert_uses_enabled_server_default(database):
    engine, local = database
    db.init_db()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO list_rule (rule_type, value, created_at) "
            "VALUES (:rule_type, :value, :created_at)"
        ), {"rule_type": "keyword", "value": "소프트웨어", "created_at": NOW.isoformat(sep=" ")})
    with local() as session:
        rule = session.execute(select(ListRule)).scalar_one()
        assert rule.enabled is True


@pytest.mark.parametrize("failed_batch", [1, 2])
def test_raw_migration_batch_rolls_back_and_retries(database, failed_batch):
    engine, local = database
    db.init_db()
    original = '{"befBidBbancNo":"OLD", "pubPrcrmntClsfcNm":"분류"}'
    with engine.begin() as conn:
        conn.execute(BidNotice.__table__.insert(), [
            {"bid_ntce_no": f"ROLLBACK-{i:04}", "raw_json": original,
             "collected_at": NOW, "updated_at": NOW}
            for i in range(1001)
        ])
    batches = []

    def fail_after_first_notice_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO bid_notice_raw"):
            batches.append(len(parameters) if executemany else 1)
        if len(batches) == failed_batch and statement.startswith("UPDATE bid_notice SET"):
            # 원문 INSERT 와 첫 호환 컬럼 비우기가 실행된 뒤 예외를 주입한다.
            raise RuntimeError("원문 이관 배치 실패 주입")

    event.listen(engine, "after_cursor_execute", fail_after_first_notice_update)
    try:
        with pytest.raises(RuntimeError, match="원문 이관 배치 실패 주입"):
            db.init_db()
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_first_notice_update)
    assert batches == [500] * failed_batch
    committed = (failed_batch - 1) * 500
    with local() as session:
        notices = session.execute(select(BidNotice).order_by(BidNotice.bid_ntce_no)).scalars().all()
        raw_rows = session.execute(select(BidNoticeRaw)).scalars().all()
        assert len(raw_rows) == committed
        assert all(raw.raw_json == original and raw.updated_at == NOW for raw in raw_rows)
        for i, row in enumerate(notices):
            assert row.raw_json == (None if i < committed else original)
            assert row.bef_bid_bbanc_no == ("OLD" if i < committed else None)
            assert row.pub_prcrmnt_clsfc_nm == ("분류" if i < committed else None)
    db.init_db()
    with local() as session:
        notices = session.execute(select(BidNotice)).scalars().all()
        raw_rows = session.execute(select(BidNoticeRaw)).scalars().all()
        assert len(notices) == len(raw_rows) == 1001
        assert all(row.raw_json is None and row.bef_bid_bbanc_no == "OLD"
                   and row.pub_prcrmnt_clsfc_nm == "분류" for row in notices)
        assert all(raw.raw_json == original and raw.updated_at == NOW for raw in raw_rows)


def test_license_verification_create_all_twice_schema_defaults_and_preservation(database):
    engine, local = database
    db.init_db()
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("license_verification")}
    assert inspector.get_pk_constraint("license_verification")["constrained_columns"] == [
        "bid_ntce_no", "bid_ntce_ord",
    ]
    assert columns["bid_ntce_no"]["type"].length == 40
    assert columns["bid_ntce_ord"]["type"].length == 3
    assert columns["last_error"]["type"].length == 200
    assert columns["attempts"]["nullable"] is False
    for name in ("pending_since", "last_attempt_at", "last_error", "verified_at", "gave_up_at"):
        assert columns[name]["nullable"] is True
    assert inspector.get_indexes("license_verification") == []
    with local() as session:
        session.add(LicenseVerification(bid_ntce_no="NEW", bid_ntce_ord="001"))
        session.commit()
        state = session.get(LicenseVerification, ("NEW", "001"))
        assert state.attempts == 0
        assert all(getattr(state, name) is None for name in (
            "pending_since", "last_attempt_at", "last_error", "verified_at", "gave_up_at",
        ))
        state.pending_since = NOW
        state.attempts = 2
        state.last_error = "검증 실패"
        session.commit()
    # 표준 SQL INSERT에도 DB 기본값 0이 적용된다.
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO license_verification (bid_ntce_no, bid_ntce_ord) VALUES ('SQL', '000')"))
    db.init_db()
    with local() as session:
        assert session.get(LicenseVerification, ("SQL", "000")).attempts == 0
        state = session.get(LicenseVerification, ("NEW", "001"))
        assert state.pending_since == NOW and state.attempts == 2 and state.last_error == "검증 실패"
