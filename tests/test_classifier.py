"""Phase 13.2 표시 분류·배치 재분류·규칙 시드 검증 — 임시 DB 만 사용."""

from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import db
from app.classifier import (
    DEFAULT_LIST_RULES, Classification, RuleSet, _compile_terms,
    classify_notice, iter_classifications, load_rule_set, match_terms, reclassify_notices,
)
from app.models import AppConfig, BidLicenseLimit, BidNotice, ListRule, SchemaMigration


NOW = datetime(2026, 10, 7, 12)
SEED_NAME = "phase13_list_rule_seed"
DEFAULT_RULES = RuleSet(*(
    tuple(value for kind, value in DEFAULT_LIST_RULES if kind == rule_type)
    for rule_type in ("clsfc", "keyword", "negative")
))


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'classifier13.db'}", future=True)
    local = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", local)
    db.Base.metadata.create_all(engine)
    yield engine, local
    engine.dispose()


def _classify(**changes):
    values = dict(
        indstryty_lmt_yn="N", pub_prcrmnt_clsfc_nm=None, bid_ntce_nm=None,
        license_codes=(), match_codes=("1468", "1470"), rules=DEFAULT_RULES,
    )
    values.update(changes)
    return classify_notice(**values)


def _notice(no, **changes):
    values = dict(bid_ntce_no=no, bid_ntce_ord="001", indstryty_lmt_yn="N",
                  list_visible=False, collected_at=NOW, updated_at=NOW)
    values.update(changes)
    return BidNotice(**values)


def _license(no, code, *, order="001", serial="1", permsn=None):
    return BidLicenseLimit(
        bid_ntce_no=no, bid_ntce_ord=order, lmt_grp_no="1", lmt_sno=serial,
        lcns_cd=code, permsn_cds=permsn, collected_at=NOW,
    )


def _snapshot(session):
    return session.execute(select(
        BidNotice.bid_ntce_no, BidNotice.list_visible, BidNotice.match_basis,
        BidNotice.match_detail, BidNotice.matched_indstryty_cds,
        BidNotice.collected_at, BidNotice.updated_at,
    ).order_by(BidNotice.bid_ntce_no)).all()


def test_value_objects_are_frozen():
    with pytest.raises(FrozenInstanceError):
        DEFAULT_RULES.keywords = ()
    with pytest.raises(FrozenInstanceError):
        _classify().list_visible = True


def test_industry_precedes_classification_and_keywords():
    result = _classify(
        license_codes=("1470", "9999", "1468", "1468"),
        pub_prcrmnt_clsfc_nm="정보시스템개발서비스", bid_ntce_nm="홈페이지",
    )
    assert result == Classification(True, "업종", None, "1468,1470")
    assert _classify(indstryty_lmt_yn="Y", license_codes=("1468",)).match_basis == "업종"


def test_industry_restriction_blocks_classification_and_keywords():
    assert _classify(
        indstryty_lmt_yn="Y", license_codes=("9999",),
        pub_prcrmnt_clsfc_nm="정보시스템개발서비스", bid_ntce_nm="홈페이지",
    ) == Classification(False, None, None, None)


@pytest.mark.parametrize("restriction", ["N", "", None])
@pytest.mark.parametrize("clsfc,basis", [("정보시스템개발서비스", "분류"), (None, "키워드")])
def test_unrestricted_notices_allow_classification_and_keywords(restriction, clsfc, basis):
    assert _classify(indstryty_lmt_yn=restriction, pub_prcrmnt_clsfc_nm=clsfc,
                     bid_ntce_nm="홈페이지").match_basis == basis


@pytest.mark.parametrize("clsfc", DEFAULT_RULES.clsfc)
def test_official_classification_exact_match_and_trimming(clsfc):
    assert _classify(pub_prcrmnt_clsfc_nm=f"  {clsfc}\t") == Classification(True, "분류", clsfc, None)


@pytest.mark.parametrize("clsfc", ["정보시스템개발", "정보시스템개발서비스 추가", "기타정보시스템개발서비스", "", None])
def test_classification_does_not_match_short_or_partial_names(clsfc):
    assert _classify(pub_prcrmnt_clsfc_nm=clsfc) == Classification(False, None, None, None)


@pytest.mark.parametrize("title,term,matched", [
    ("우리홈페이지개편", "홈페이지", True),
    ("SW고성장클럽", "SW", True), ("SW 유지보수", "SW", True),
    ("SWOT 분석", "SW", False), ("ASW 개발", "SW", False),
    ("DB구축", "DB", True), ("db 구축", "DB", True), ("MDB", "DB", False),
    ("AI개발", "AI", True), ("AIR", "AI", False),
    ("시스템구축", "시스템 구축", True), ("시스템  구축", "시스템 구축", True),
    ("시스템\t구축", "시스템 구축", True), ("안전 산업", "전산", False),
    ("2026사업", "2026", True), ("12026사업", "2026", False),
    ("20260", "2026", False), ("C++ 개발", "C++", True),
    ("CC 개발", "C++", False), (None, "홈페이지", False), ("홈페이지", "", False),
])
def test_match_terms_boundaries_spacing_and_regex_escaping(title, term, matched):
    assert match_terms(title, [term]) == ([term] if matched else [])


def test_keyword_order_duplicates_and_detail_limit():
    rules = RuleSet((), ("플랫폼", "프로그램 개발", "플랫폼"), ())
    title = "프로그램개발 및 플랫폼 플랫폼"
    assert match_terms(title, rules.keywords) == ["플랫폼", "프로그램 개발"]
    assert _classify(bid_ntce_nm=title, rules=rules).match_detail == "플랫폼, 프로그램 개발"
    terms = tuple("가" * length for length in range(20, 40))
    result = _classify(bid_ntce_nm="가" * 40, rules=RuleSet((), terms, ()))
    assert result.match_detail == ", ".join(terms)[:200]
    assert len(result.match_detail) == 200


def test_rule_patterns_are_cached_across_notices():
    rules = RuleSet((), ("캐시검증 SW",), ("캐시제외",))
    _compile_terms.cache_clear()
    for _ in range(10):
        _classify(bid_ntce_nm="캐시검증SW", rules=rules)
    info = _compile_terms.cache_info()
    assert info.misses == 2 and info.hits == 18


def test_negatives_only_block_keyword_stage():
    title = "전산화단층촬영 장비 유지보수"
    assert _classify(bid_ntce_nm=title) == Classification(False, None, None, None)
    assert _classify(bid_ntce_nm=title, pub_prcrmnt_clsfc_nm="정보시스템유지관리서비스").match_basis == "분류"
    assert _classify(bid_ntce_nm=title, license_codes=("1468",)).match_basis == "업종"


def test_empty_match_codes_have_no_industry_basis():
    assert _classify(license_codes=("1468",), match_codes=()) == Classification(False, None, None, None)


def test_reported_notice_is_visible_by_homepage_keyword():
    result = _classify(
        bid_ntce_nm="(긴급)2026년 소상공인연합회 소상공인교육원(소상공인 디지털교육센터) 홈페이지 리뉴얼 및 고도화 사업",
        pub_prcrmnt_clsfc_nm="기타사업지원서비스",
    )
    assert result == Classification(True, "키워드", "홈페이지", None)


def test_load_rule_set_filters_and_orders_by_id(database):
    _, local = database
    with local() as session:
        session.add_all([
            ListRule(id=9, rule_type="keyword", value="후순위"),
            ListRule(id=2, rule_type="keyword", value="선순위"),
            ListRule(id=3, rule_type="keyword", value="꺼짐", enabled=False),
            ListRule(id=4, rule_type="unknown", value="무시"),
            ListRule(id=5, rule_type="clsfc", value="분류"),
            ListRule(id=6, rule_type="negative", value="제외"),
        ])
        session.commit()
        assert load_rule_set(session) == RuleSet(("분류",), ("선순위", "후순위"), ("제외",))


@pytest.mark.parametrize("fallback", [None, "분류", "키워드"])
def test_legacy_csv_survives_removal_and_restores_industry_basis(database, fallback):
    _, local = database
    with local() as session:
        session.add_all([
            AppConfig(id=1, indstryty_cds=" ,1468, 1470, ,"),
            _notice("LEGACY", matched_indstryty_cds="1426,1468",
                    pub_prcrmnt_clsfc_nm="정보시스템개발서비스" if fallback == "분류" else None,
                    bid_ntce_nm="홈페이지" if fallback == "키워드" else None),
        ])
        session.commit()
        db._seed_list_rules()
        for codes, basis in [("1468,1470", "업종"), ("1470", fallback), ("1426,1468", "업종")]:
            session.get(AppConfig, 1).indstryty_cds = codes
            result = reclassify_notices(session)
            notice = session.get(BidNotice, "LEGACY")
            assert notice.match_basis == basis
            assert notice.list_visible is (basis is not None)
            assert notice.matched_indstryty_cds == "1426,1468"
            assert result["changed"] == 1
        session.add(_license("LEGACY", "9999"))
        session.commit()
        reclassify_notices(session)
        notice = session.get(BidNotice, "LEGACY")
        assert notice.match_basis == fallback and notice.matched_indstryty_cds is None


def test_legacy_original_csv_is_preserved_verbatim(database):
    _, local = database
    with local() as session:
        session.add(_notice("LEGACY", matched_indstryty_cds=" 1468,1426,1468 "))
        session.commit()
        _, result = next(iter_classifications(session, rules=DEFAULT_RULES, match_codes=("1468",)))
        assert result == Classification(True, "업종", None, " 1468,1426,1468 ")


def test_license_order_and_permission_codes(database):
    _, local = database
    with local() as session:
        session.add_all([
            _notice("CURRENT"), _license("CURRENT", "1468", order="000"),
            _license("CURRENT", "9999", order="001"),
            _notice("LATEST", bid_ntce_ord="002"),
            _license("LATEST", "9999", order="000"), _license("LATEST", "1468", order="001"),
            _notice("PERMISSION"),
            _license("PERMISSION", "9999", permsn=" ,1470, 1468, ,1470,"),
            _notice("UNPADDED", bid_ntce_ord="2"),
            _license("UNPADDED", "9999", order="2"), _license("UNPADDED", "1468", order="010"),
            _notice("FALLBACK", bid_ntce_ord="011"),
            _license("FALLBACK", "9999", order="2"), _license("FALLBACK", "1468", order="010"),
            _notice("UNION"), _license("UNION", " 1468 "),
            _license("UNION", "1470", serial="2"),
        ])
        session.commit()
        results = {row.bid_ntce_no: result for row, result in iter_classifications(
            session, rules=DEFAULT_RULES, match_codes=("1468", "1470"),
        )}
        assert results["CURRENT"].match_basis is None
        assert results["LATEST"].matched_indstryty_cds == "1468"
        assert results["PERMISSION"].matched_indstryty_cds == "1468,1470"
        assert results["UNPADDED"].match_basis is None
        assert results["FALLBACK"].matched_indstryty_cds == "1468"
        assert results["UNION"].matched_indstryty_cds == "1468,1470"


@pytest.mark.parametrize("order", [None, ""])
def test_missing_notice_order_uses_latest_license_order(database, order):
    _, local = database
    with local() as session:
        session.add_all([
            _notice("NO-ORDER", bid_ntce_ord=order),
            _license("NO-ORDER", "9999", order="000"),
            _license("NO-ORDER", "1468", order="001"),
        ])
        session.commit()
        _, result = next(iter_classifications(session, rules=DEFAULT_RULES, match_codes=("1468",)))
        assert result == Classification(True, "업종", None, "1468")


def test_preview_batches_no_n_plus_one_and_reclassification_statistics(database):
    engine, local = database
    db.init_db()
    with local() as session:
        session.get(AppConfig, 1).indstryty_cds = " ,1468, 1470, ,"
        session.add_all([
            _notice("A", indstryty_lmt_yn="Y"), _license("A", "1468"),
            _notice("B", pub_prcrmnt_clsfc_nm="정보시스템개발서비스"),
            _notice("C", bid_ntce_nm="홈페이지"),
            _notice("D", indstryty_lmt_yn="Y", bid_ntce_nm="홈페이지", list_visible=True),
            _notice("E", bid_ntce_nm="일반 용역"),
            _notice("F"), _license("F", "9999", permsn="1470"),
        ])
        session.commit()
        before = _snapshot(session)
        queries = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        event.listen(engine, "before_cursor_execute", record_query)
        try:
            preview = list(iter_classifications(
                session, rules=DEFAULT_RULES, match_codes=("1468", "1470"), batch_size=2,
            ))
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert [row.bid_ntce_no for row, _ in preview] == list("ABCDEF")
        assert sum("FROM bid_license_limit" in query for query in queries) == 3
        assert all(query.startswith("SELECT") for query in queries)
        assert not session.dirty and _snapshot(session) == before
        with local() as verifier:
            assert _snapshot(verifier) == before

        commits = []
        event.listen(session, "after_commit", lambda session: commits.append(True))
        stats = reclassify_notices(session)
        assert commits == [True]
        assert stats == {"total": 6, "changed": 5, "visible": 4, "hidden": 2,
                         "by_basis": {"업종": 2, "분류": 1, "키워드": 1}}
        after = _snapshot(session)
        assert all(row.collected_at == NOW and row.updated_at == NOW for row in after)
        queries.clear()
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            assert reclassify_notices(session) == dict(stats, changed=0)
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert not any(query.startswith("UPDATE") for query in queries)
        assert _snapshot(session) == after

        # 초안 규칙은 실제 저장 규칙·공고에 반영되지 않는다.
        draft = list(iter_classifications(session, rules=RuleSet((), (), ()), match_codes=()))
        assert all(not result.list_visible for _, result in draft)
        assert _snapshot(session) == after

        session.get(BidNotice, "B").pub_prcrmnt_clsfc_nm = None
        session.get(BidNotice, "C").bid_ntce_nm = None
        session.commit()
        subset = reclassify_notices(session, bid_ntce_nos=iter(["B", "B", "MISSING"]))
        assert subset == {"total": 1, "changed": 1, "visible": 0, "hidden": 1,
                          "by_basis": {"업종": 0, "분류": 0, "키워드": 0}}
        assert session.get(BidNotice, "C").match_basis == "키워드"
        assert list(iter_classifications(session, rules=DEFAULT_RULES, match_codes=(), bid_ntce_nos=[])) == []
        empty = reclassify_notices(session, bid_ntce_nos=[])
        assert empty == dict(subset, total=0, changed=0, hidden=0)


def test_preview_does_not_autoflush_pending_changes(database):
    engine, _ = database
    with sessionmaker(bind=engine, autoflush=True)() as session:
        session.add(_notice("PENDING"))
        assert list(iter_classifications(session, rules=DEFAULT_RULES, match_codes=())) == []
        with engine.connect() as conn:
            assert conn.execute(select(BidNotice)).all() == []


@pytest.mark.parametrize("batch_size", [0, -1])
def test_invalid_batch_size(database, batch_size):
    _, local = database
    with local() as session, pytest.raises(ValueError, match="batch_size"):
        list(iter_classifications(session, rules=DEFAULT_RULES, match_codes=(), batch_size=batch_size))


def test_seed_counts_order_and_idempotence_preserve_operator_changes(database):
    _, local = database
    db.init_db()
    with local() as session:
        rules = session.scalars(select(ListRule).order_by(ListRule.id)).all()
        assert Counter(row.rule_type for row in rules) == {"clsfc": 16, "keyword": 23, "negative": 1}
        assert tuple((row.rule_type, row.value) for row in rules) == DEFAULT_LIST_RULES
        assert all(row.enabled and row.created_at is not None for row in rules)
        assert session.get(SchemaMigration, SEED_NAME).applied_at is not None
        first = [(row.id, row.rule_type, row.value, row.created_at) for row in rules]
    db.init_db()
    with local() as session:
        rules = session.scalars(select(ListRule).order_by(ListRule.id)).all()
        assert [(row.id, row.rule_type, row.value, row.created_at) for row in rules] == first
        deleted, disabled = rules[:2]
        deleted_id, disabled_id = deleted.id, disabled.id
        session.delete(deleted)
        disabled.enabled = False
        session.commit()
    db.init_db()
    with local() as session:
        assert session.get(ListRule, deleted_id) is None
        assert session.get(ListRule, disabled_id).enabled is False
        assert len(session.scalars(select(ListRule)).all()) == 39


def test_partial_seed_only_adds_missing_rules_and_never_reclassifies(database):
    _, local = database
    with local() as session:
        session.add_all([
            ListRule(rule_type="keyword", value="홈페이지", enabled=False, created_at=NOW),
            ListRule(rule_type="keyword", value="사용자 규칙"),
            SchemaMigration(name="phase13_match_basis_backfill"),
            _notice("UNCHANGED", bid_ntce_nm="홈페이지", list_visible=True,
                    match_basis=None, match_detail="기존 근거", matched_indstryty_cds="1468"),
        ])
        session.commit()
        before = _snapshot(session)
    db.init_db()
    with local() as session:
        rules = session.scalars(select(ListRule).order_by(ListRule.id)).all()
        assert len(rules) == 41
        assert rules[0].enabled is False and rules[0].created_at == NOW
        assert tuple((row.rule_type, row.value) for row in rules[2:]) == tuple(
            pair for pair in DEFAULT_LIST_RULES if pair != ("keyword", "홈페이지")
        )
        assert session.get(SchemaMigration, SEED_NAME) is not None
        assert _snapshot(session) == before


@pytest.mark.parametrize("failure_point", ["rule_insert", "marker_insert"])
def test_seed_failure_rolls_back_rules_and_marker_then_retries(database, failure_point):
    engine, local = database
    with local() as session:
        session.add(ListRule(rule_type="keyword", value="홈페이지", enabled=False))
        session.commit()

    def fail_after_statement(conn, cursor, statement, parameters, context, executemany):
        target = "INSERT INTO list_rule " if failure_point == "rule_insert" else "INSERT INTO schema_migration "
        if statement.startswith(target):
            raise RuntimeError("규칙 시드 실패 주입")

    event.listen(engine, "after_cursor_execute", fail_after_statement)
    try:
        with pytest.raises(RuntimeError, match="규칙 시드 실패 주입"):
            db._seed_list_rules()
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_statement)
    with local() as session:
        rules = session.scalars(select(ListRule)).all()
        assert len(rules) == 1 and rules[0].enabled is False
        assert session.get(SchemaMigration, SEED_NAME) is None
    db._seed_list_rules()
    with local() as session:
        assert len(session.scalars(select(ListRule)).all()) == 40
        assert session.get(SchemaMigration, SEED_NAME) is not None
