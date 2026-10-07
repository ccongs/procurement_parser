"""Phase 13.2 표시 분류·배치 재분류·규칙 시드 검증 — 임시 DB 만 사용."""

from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app import db
from app.classifier import (
    DEFAULT_LIST_RULES, Classification, RuleSet, _compile_terms,
    classify_notice, iter_classifications, load_match_codes, load_rule_set, match_terms,
    reclassify_notices,
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


@pytest.mark.parametrize("title,terms,expected", [
    ("ASW 개발", [" SW"], []),
    ("SW 개발", [" SW "], ["SW"]),
    ("SWOT 분석", ["SW "], []),
    ("시스템구축 및 SW 개발", [" 시스템\t 구축 ", "시스템 구축", " SW ", "SW"],
     ["시스템 구축", "SW"]),
    ("SW 개발", [" ", "\t\n", ""], []),
])
def test_match_terms_normalizes_rule_values(title, terms, expected):
    assert match_terms(title, terms) == expected


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


@pytest.mark.parametrize("codes,expected", [
    (" ,1468, 1470, ,1468,", frozenset({"1468", "1470"})),
    (" , \t,", frozenset()),
    ("", frozenset()),
])
def test_load_match_codes_normalizes_config(database, codes, expected):
    _, local = database
    with local() as session:
        session.add(AppConfig(id=1, indstryty_cds=codes))
        session.commit()
        result = load_match_codes(session)
        assert isinstance(result, frozenset)
        assert result == expected


@pytest.mark.parametrize("action", ["add", "disable", "delete"])
def test_reclassification_flushes_pending_rule_changes(database, action):
    _, local = database
    with local() as session:
        assert session.autoflush is False
        visible_before = action != "add"
        rule = ListRule(rule_type="keyword", value="홈페이지")
        session.add_all([
            AppConfig(id=1, indstryty_cds=""),
            _notice("RULE", bid_ntce_nm="홈페이지", list_visible=visible_before,
                    match_basis="키워드" if visible_before else None,
                    match_detail="홈페이지" if visible_before else None),
        ])
        if visible_before:
            session.add(rule)
        session.commit()
        if action == "add":
            session.add(rule)
        elif action == "disable":
            rule.enabled = False
        else:
            session.delete(rule)

        stats = reclassify_notices(session)
        assert stats["total"] == 1 and stats["changed"] == 1
        assert stats["visible"] == int(action == "add")
        assert stats["hidden"] == int(action != "add")
    with local() as verifier:
        rules = load_rule_set(verifier)
        assert rules.keywords == (("홈페이지",) if action == "add" else ())
        notice = verifier.get(BidNotice, "RULE")
        result = classify_notice(
            indstryty_lmt_yn=notice.indstryty_lmt_yn, pub_prcrmnt_clsfc_nm=None,
            bid_ntce_nm=notice.bid_ntce_nm, license_codes=(), match_codes=(), rules=rules,
        )
        assert notice.list_visible is result.list_visible
        assert notice.match_basis == result.match_basis
        assert notice.match_detail == result.match_detail


def test_reclassification_flushes_pending_notice_and_license(database):
    _, local = database
    with local() as session:
        assert session.autoflush is False
        session.add(AppConfig(id=1, indstryty_cds="1468"))
        session.commit()
        session.add_all([
            BidNotice(bid_ntce_no="NEW", bid_ntce_ord="001", indstryty_lmt_yn="Y",
                      collected_at=NOW, updated_at=NOW),
            _license("NEW", "1468"),
        ])
        assert reclassify_notices(session, bid_ntce_nos=["NEW"]) == {
            "total": 1, "changed": 1, "visible": 1, "hidden": 0,
            "by_basis": {"업종": 1, "분류": 0, "키워드": 0},
        }
    with local() as verifier:
        notice = verifier.get(BidNotice, "NEW")
        assert notice.list_visible is True and notice.match_basis == "업종"
        assert notice.match_detail is None and notice.matched_indstryty_cds == "1468"
        assert verifier.get(BidLicenseLimit, ("NEW", "001", "1", "1")) is not None


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


def test_target_batches_bound_parameters_and_only_process_existing_notices(database):
    engine, local = database
    targets = [f"TARGET-{index:04d}" for index in range(1005)]
    existing = targets[::50]
    batch_size = 10
    with local() as session:
        for no in [*existing, "OUTSIDE"]:
            session.add_all([_notice(no), _license(no, "1468")])
        session.commit()
        before = _snapshot(session)
        queries = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            queries.append((statement, parameters))

        # 역순·공백·중복·빈값을 포함한 단발성 iterable 도 정규화한다.
        supplied = (no for no in reversed([*targets, f" {existing[0]} ", "", " \t "]))
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            results = list(iter_classifications(
                session, rules=DEFAULT_RULES, match_codes=("1468",),
                bid_ntce_nos=supplied, batch_size=batch_size,
            ))
        finally:
            event.remove(engine, "before_cursor_execute", record_query)

        assert [row.bid_ntce_no for row, _ in results] == existing
        assert all(result == Classification(True, "업종", None, "1468") for _, result in results)
        assert all(len(parameters) <= batch_size for _, parameters in queries)
        batch_count = (len(targets) + batch_size - 1) // batch_size
        existing_batch_count = len({
            index // batch_size for index, no in enumerate(targets) if no in existing
        })
        assert sum("FROM bid_notice \n" in query for query, _ in queries) == batch_count
        assert sum("FROM bid_license_limit" in query for query, _ in queries) == existing_batch_count
        assert all(query.startswith("SELECT") for query, _ in queries)
        assert not session.dirty and _snapshot(session) == before


@pytest.mark.parametrize("targets", [[], ["", " ", "\t\n"]])
def test_empty_normalized_targets_do_not_query(database, targets):
    engine, local = database
    queries = []

    def record_query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    with local() as session:
        session.add(_notice("OUTSIDE"))
        session.commit()
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            assert list(iter_classifications(
                session, rules=DEFAULT_RULES, match_codes=(), bid_ntce_nos=targets,
            )) == []
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert queries == []


def test_preview_does_not_autoflush_pending_changes(database):
    engine, _ = database
    with sessionmaker(bind=engine, autoflush=True)() as session:
        session.add(_notice("PENDING"))
        assert list(iter_classifications(session, rules=DEFAULT_RULES, match_codes=())) == []
        with engine.connect() as conn:
            assert conn.execute(select(BidNotice)).all() == []


@pytest.mark.parametrize("argument", ["bid_ntce_nos", "match_codes"])
def test_preview_does_not_autoflush_while_consuming_lazy_arguments(database, argument):
    engine, _ = database
    with sessionmaker(bind=engine, autoflush=True)() as session:
        session.add(_notice("EXISTING", matched_indstryty_cds="1468"))
        session.commit()
        pending = _notice("PENDING")
        session.add(pending)
        queries = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        def delayed_values():
            column = (BidNotice.bid_ntce_no if argument == "bid_ntce_nos"
                      else BidNotice.matched_indstryty_cds)
            yield from session.scalars(select(column).where(BidNotice.bid_ntce_no == "EXISTING"))

        arguments = {"match_codes": ("1468",), argument: delayed_values()}
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            preview = iter_classifications(session, rules=DEFAULT_RULES, **arguments)
            assert queries == []
            results = list(preview)
        finally:
            event.remove(engine, "before_cursor_execute", record_query)

        assert not any(query.startswith("INSERT") for query in queries)
        assert all(query.startswith("SELECT") for query in queries)
        assert pending in session.new
        assert [row.bid_ntce_no for row, _ in results] == ["EXISTING"]
        assert results[0][1] == Classification(True, "업종", None, "1468")


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


ONCE_JOBS = [
    ("_seed_list_rules", SEED_NAME, "SELECT list_rule.rule_type"),
    ("_migrate_match_basis_backfill", "phase13_match_basis_backfill", "UPDATE bid_notice SET match_basis="),
]


def _prepare_once_notices(local):
    with local() as session:
        session.add_all([
            _notice("VISIBLE", list_visible=True),
            _notice("HIDDEN"),
            _notice("FILLED", list_visible=True, match_basis="분류", match_detail="기존 근거"),
        ])
        session.commit()


def _assert_once_result(local, name, *, completed=True):
    with local() as session:
        markers = session.scalars(select(SchemaMigration)).all()
        assert [row.name for row in markers] == ([name] if completed else [])
        assert all(row.applied_at is not None for row in markers)
        rules = session.scalars(select(ListRule).order_by(ListRule.id)).all()
        expected_rules = DEFAULT_LIST_RULES if completed and name == SEED_NAME else ()
        assert tuple((row.rule_type, row.value) for row in rules) == expected_rules
        assert all(row.enabled for row in rules)
        basis = "업종" if completed and name != SEED_NAME else None
        assert session.get(BidNotice, "VISIBLE").match_basis == basis
        assert session.get(BidNotice, "HIDDEN").match_basis is None
        filled = session.get(BidNotice, "FILLED")
        assert filled.match_basis == "분류" and filled.match_detail == "기존 근거"
        assert all(row.collected_at == NOW and row.updated_at == NOW
                   for row in session.scalars(select(BidNotice)))


@pytest.mark.parametrize("function_name,name,work_start", ONCE_JOBS)
@pytest.mark.parametrize("race_error", ["integrity", "operational"])
def test_once_other_connection_commits_after_missing_marker_check(
    database, function_name, name, work_start, race_error,
):
    engine, local = database
    _prepare_once_notices(local)
    run = getattr(db, function_name)
    connections = []
    errors = []

    def finish_on_other_connection(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith(work_start):
            return
        connections.append(conn)
        if len(connections) == 1:
            # 외부 호출이 완료 기록 부재를 읽은 직후·작업 SQL 실행 전에 경쟁자를 완료시킨다.
            run()
            if race_error == "operational":
                raise OperationalError(statement, parameters, Exception("경쟁 잠금 오류 주입"))

    def record_error(context):
        errors.append(context.sqlalchemy_exception)

    event.listen(engine, "before_cursor_execute", finish_on_other_connection)
    event.listen(engine, "handle_error", record_error)
    try:
        run()
    finally:
        event.remove(engine, "before_cursor_execute", finish_on_other_connection)
        event.remove(engine, "handle_error", record_error)
    assert len(connections) == 2 and connections[0] is not connections[1]
    if race_error == "integrity":
        assert len(errors) == 1 and isinstance(errors[0], IntegrityError)
    _assert_once_result(local, name)


@pytest.mark.parametrize("function_name,name,work_start", ONCE_JOBS)
@pytest.mark.parametrize("failure_point", ["work", "marker_insert"])
def test_once_operational_error_without_marker_retries_once(
    database, function_name, name, work_start, failure_point,
):
    engine, local = database
    _prepare_once_notices(local)
    statements = []
    failures = []
    target = ("INSERT INTO schema_migration " if failure_point == "marker_insert" else
              "INSERT INTO list_rule " if name == SEED_NAME else work_start)

    def fail_first_attempt(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.startswith(target) and not failures:
            failures.append(True)
            # SQL 실행 뒤 실패시켜 작업·완료 기록의 실제 롤백까지 검증한다.
            raise OperationalError(statement, parameters, Exception("첫 시도 실패 주입"))

    event.listen(engine, "after_cursor_execute", fail_first_attempt)
    try:
        getattr(db, function_name)()
    finally:
        event.remove(engine, "after_cursor_execute", fail_first_attempt)
    assert failures == [True]
    assert sum(statement.startswith(work_start) for statement in statements) == 2
    # 최초 확인 → 롤백 뒤 새 트랜잭션 확인 → 재시도 확인.
    assert sum("FROM schema_migration" in statement for statement in statements) == 3
    _assert_once_result(local, name)


@pytest.mark.parametrize("function_name,name,work_start", ONCE_JOBS)
@pytest.mark.parametrize("error_type", [IntegrityError, OperationalError])
def test_once_two_database_failures_without_marker_raise_last_error(
    database, function_name, name, work_start, error_type,
):
    engine, local = database
    _prepare_once_notices(local)
    failures = []
    checks = []

    def fail_each_marker_insert(conn, cursor, statement, parameters, context, executemany):
        if "FROM schema_migration" in statement:
            checks.append(conn)
        if statement.startswith("INSERT INTO schema_migration "):
            error = error_type(statement, parameters, Exception("연속 실패 주입"))
            failures.append(error)
            raise error

    event.listen(engine, "after_cursor_execute", fail_each_marker_insert)
    try:
        with pytest.raises(error_type) as raised:
            getattr(db, function_name)()
    finally:
        event.remove(engine, "after_cursor_execute", fail_each_marker_insert)
    assert len(failures) == 2 and raised.value is failures[-1]
    assert len(checks) == 4  # 두 시도 모두 롤백 후 완료 여부를 다시 확인한다.
    assert checks[0] is not checks[1] and checks[2] is not checks[3]
    _assert_once_result(local, name, completed=False)
