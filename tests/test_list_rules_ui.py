"""Phase 13.4a — 목록 표시 근거·규칙 편집과 전체 재분류를 임시 DB로 검증."""

import html
import re
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import api_client, classifier, collector, main, repository, rule_test
from app.db import Base, _register_sqlite_pragmas
from app.models import AppConfig, BidNotice, CollectionRun, ListRule


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rules.db'}", connect_args={"check_same_thread": False},
    )
    _register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False)
    now = datetime(2026, 10, 7)
    with local() as session:
        session.add(AppConfig(id=1, enabled=False, pre_spec_enabled=False,
                              indstryty_cds="1426,1468", updated_at=now))
        session.add_all([
            BidNotice(bid_ntce_no=no, bid_ntce_nm=title, indstryty_lmt_yn=limit,
                      matched_indstryty_cds=codes, pub_prcrmnt_clsfc_nm=clsfc,
                      bid_ntce_dt=now, list_visible=False, collected_at=now, updated_at=now)
            for no, title, limit, codes, clsfc in (
                ("I", "업종 공고", "Y", "1468,9999", None),
                ("C", "일반 용역", "N", None, '<분류&"명>'),
                ("K", "새플랫폼 개발", "N", None, None),
                ("N", "새플랫폼 전산화단층", "N", None, None),
            )
        ])
        classifier.reclassify_notices(session)
    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main.scheduler, "start_scheduler", lambda **kwargs: None)
    monkeypatch.setattr(main.scheduler, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(main.scheduler, "is_running", lambda: False)
    monkeypatch.setattr(main.scheduler, "get_next_run_time", lambda *args: None)
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        engine.dispose()


def _state(no):
    with main.SessionLocal() as session:
        notice = session.get(BidNotice, no)
        return notice.list_visible, notice.match_basis, notice.match_detail


def _post(client, data):
    references = {r["id"]: _rule_ref(r) for r in _rules()}
    data = {"rule_ids": list(references.values()) or "", **data}
    # 기존 CRUD 테스트의 정수 id를 현재 화면에서 제출할 참조로 바꾼다.
    def reference(value):
        return references.get(value, f"{value}:000000000000") if isinstance(value, int) else value

    for field in ("rule_ids", "enabled_ids", "delete_id"):
        if field in data:
            value = data[field]
            data[field] = [reference(item) for item in value] if isinstance(value, list) else reference(value)
    return client.post("/config/rules", data=data, follow_redirects=False)


def _rule_ref(row):
    return f'{row["id"]}:{repository.list_rule_fingerprint(row)}'


def _rules():
    with main.SessionLocal() as session:
        return repository.list_list_rules(session)


@pytest.mark.parametrize("csv,expected", [
    ("9999,1468,1426", "[1468,1426]"),
    ("9999", "-"), (None, "-"),
])
def test_industry_intersection(csv, expected):
    cell = main._render_list_match(
        {"match_basis": "업종", "matched_indstryty_cds": csv}, frozenset({"1426", "1468"}),
    )
    visible = re.sub(r'<span class="sr-only">.*?</span>', "", cell)
    assert re.sub(r"<[^>]+>", "", visible) == expected
    assert "9999" not in cell
    if expected != "-":
        assert 'data-tip="소프트웨어사업자' in cell


@pytest.mark.parametrize("basis,label", [("분류", "공공조달분류"), ("키워드", "공고명 키워드")])
@pytest.mark.parametrize("detail", [None, "", '<규칙&"값\'>'])
def test_unrestricted_badge_and_escaped_tip(basis, label, detail):
    cell = main._render_list_match(
        {"match_basis": basis, "match_detail": detail, "matched_indstryty_cds": "1468"},
        frozenset({"1468"}),
    )
    tip = f"업종 제한 없음 · 표시 근거: {label}" + (f" '{detail}'" if detail else "")
    assert f'class="indcode nolimit" data-tip="{html.escape(tip)}" tabindex="0"' in cell
    assert f'제한없음<span class="sr-only">({html.escape(tip)})</span>' in cell
    assert "1468" not in cell
    badge = re.search(r'<span class="([^"]*)" data-tip="([^"]*)"', cell)
    assert "indcode" in badge[1].split()
    assert html.unescape(badge[2]) == tip


@pytest.mark.parametrize("basis", [None, "", "기타"])
def test_unknown_basis_is_dash(basis):
    assert main._render_list_match(
        {"match_basis": basis, "matched_indstryty_cds": "1468"}, frozenset({"1468"}),
    ) == '<td class="matchind">-</td>'


def test_list_route_loads_basis_detail_and_current_codes_once(client, monkeypatch):
    _post(client, {"action": "add_clsfc", "new_clsfc": '<분류&"명>'})
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼",
                   "enabled_ids": [r["id"] for r in _rules()]})
    original = classifier.load_match_codes
    calls = []

    def load(session):
        calls.append(session)
        return original(session)

    monkeypatch.setattr(classifier, "load_match_codes", load)
    page = client.get("/list?dt_from=2000-01-01&dt_to=2099-12-31").text
    assert len(calls) == 1
    assert "9999" not in page
    assert 'tabindex="0">1468' in page
    assert "표시 근거: 공공조달분류 &#x27;&lt;분류&amp;&quot;명&gt;&#x27;" in page
    assert "표시 근거: 공고명 키워드 &#x27;새 플랫폼&#x27;" in page
    assert '.indcode.nolimit' in page and 'white-space: nowrap;' in page


def test_repository_rules_crud_normalization_and_types(client):
    with main.SessionLocal() as session:
        assert repository.add_list_rule(session, "keyword", "  시스템\t  개발\n ")
        assert not repository.add_list_rule(session, "keyword", "시스템 개발")
        assert repository.add_list_rule(session, "negative", "시스템 개발")
        assert repository.add_list_rule(session, "clsfc", "가" * 100)
        rows = repository.list_list_rules(session)
        assert [r["rule_type"] for r in rows] == ["clsfc", "keyword", "negative"]
        assert rows[1]["value"] == "시스템 개발"
        repository.set_list_rules_enabled(session, [r["id"] for r in rows], [rows[1]["id"]])
        assert [r["enabled"] for r in repository.list_list_rules(session)] == [False, True, False]
        assert repository.delete_list_rule(session, rows[1]["id"])
        assert not repository.delete_list_rule(session, rows[1]["id"])
        assert len(repository.list_list_rules(session)) == 2


@pytest.mark.parametrize("rule_type,value,message", [
    ("invalid", "값", "알 수 없는"), ("keyword", "\t  ", "입력"),
    ("clsfc", "가" * 101, "100자"),
])
def test_repository_rejects_invalid_rules(client, rule_type, value, message):
    with main.SessionLocal() as session, pytest.raises(ValueError, match=message):
        repository.add_list_rule(session, rule_type, value)


def test_config_rule_tables_check_state_datalist_and_position(client):
    with main.SessionLocal() as session:
        for rule_type, value in [("clsfc", '<분류&"명>'), ("keyword", "새 플랫폼"), ("negative", "전산화단층")]:
            repository.add_list_rule(session, rule_type, value)
        rules = repository.list_list_rules(session)
        repository.set_list_rules_enabled(
            session, [r["id"] for r in rules], [rules[0]["id"], rules[2]["id"]],
        )
        session.get(BidNotice, "K").pub_prcrmnt_clsfc_nm = "빈도 높은 분류"
        session.get(BidNotice, "N").pub_prcrmnt_clsfc_nm = "빈도 높은 분류"
        session.commit()
        rules = repository.list_list_rules(session)
    page = client.get("/config").text
    assert page.index('action="/config"') < page.index('id="rules"') < page.index('id="keywords"')
    card = page.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    assert card.count('<thead><tr><th>사용</th><th>값</th><th>삭제</th></tr>') == 3
    for title in ["IT 공공조달분류", "SW 키워드", "판별 제외어"]:
        assert title in card
    for rule in rules:
        reference = _rule_ref(rule)
        assert re.fullmatch(r"[0-9]+:[0-9a-f]{12}", reference)
        assert f'<input type="hidden" name="rule_ids" value="{reference}">' in card
        assert f'name="delete_id" value="{reference}"' in card
        field = f'name="enabled_ids" value="{reference}"'
        assert field + (" checked>" if rule["rule_type"] != "keyword" else ">") in card
    assert 'name="new_clsfc" maxlength="100" list="clsfc-names"' in card
    assert card.index('<option value="빈도 높은 분류">') < card.index('<option value="&lt;분류&amp;&quot;명&gt;">')
    assert "저장된 전체 공고를 다시 분류" in card
    assert "정식 분류명과 정확히 같을 때" in card
    assert "앞뒤가 영문·숫자가 아닐 때" in card
    assert "값에 공백을 넣으면 공고명의 공백 유무와 관계없이 일치" in card


def test_empty_rule_tables(client):
    page = client.get("/config").text
    assert page.count("등록된 규칙이 없습니다") == 3
    assert '<input type="hidden" name="rule_ids" value="">' in page


def test_clsfc_suggestions_count_order_blank_exclusion_and_limit(client):
    with main.SessionLocal() as session:
        now = datetime(2026, 10, 7)
        for i in range(205):
            session.add(BidNotice(bid_ntce_no=f"CLASS-{i}", pub_prcrmnt_clsfc_nm=f"분류{i:03}",
                                  collected_at=now, updated_at=now))
        session.get(BidNotice, "I").pub_prcrmnt_clsfc_nm = " "
        session.get(BidNotice, "K").pub_prcrmnt_clsfc_nm = "분류204"
        session.commit()
        names = repository.list_clsfc_names(session)
        assert len(names) == 200
        assert names[0] == "분류204"
        assert " " not in names
        assert repository.list_clsfc_names(session, limit=1) == ["분류204"]


@pytest.mark.parametrize("rule_type,value,no,basis", [
    ("clsfc", '<분류&"명>', "C", "분류"), ("keyword", "  새\t 플랫폼  ", "K", "키워드"),
])
def test_add_delete_and_checkbox_save_reclassify(client, rule_type, value, no, basis):
    assert _state(no)[:2] == (False, None)
    response = _post(client, {"action": f"add_{rule_type}", f"new_{rule_type}": value})
    assert response.status_code == 303
    location = response.headers["location"]
    counts = parse_qs(urlsplit(location).query)
    expected = {"rules": ["1"], "vis": ["2" if basis == "분류" else "3"],
                "u": ["1"], "c": ["1" if basis == "분류" else "0"],
                "k": ["2" if basis == "키워드" else "0"]}
    assert counts == expected
    assert location.endswith("#rules")
    assert _state(no) == (True, basis, " ".join(value.split()))
    message = client.get(location).text
    assert "표시 규칙을 저장했습니다 — 표시" in message
    assert f'업종 1 · 분류 {counts["c"][0]} · 키워드 {counts["k"][0]}' in message
    rule_id = _rules()[0]["id"]
    _post(client, {"action": "save"})
    assert _state(no)[:2] == (False, None)
    _post(client, {"enabled_ids": [rule_id]})
    assert _state(no)[:2] == (True, basis)
    _post(client, {"delete_id": rule_id, "enabled_ids": [rule_id]})
    assert _state(no)[:2] == (False, None)
    assert _rules() == []


def test_negative_add_delete_and_checkbox_save_reclassify(client):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    kw_id = _rules()[0]["id"]
    assert _state("N")[:2] == (True, "키워드")
    response = _post(client, {"action": "add_negative", "new_negative": "  전산화단층  ",
                             "enabled_ids": [kw_id]})
    assert _state("K")[:2] == (True, "키워드")
    assert _state("N")[:2] == (False, None)
    assert "vis=2&u=1&c=0&k=1" in response.headers["location"]
    negative_id = next(r["id"] for r in _rules() if r["rule_type"] == "negative")
    _post(client, {"enabled_ids": [kw_id]})
    assert _state("N")[:2] == (True, "키워드")
    _post(client, {"enabled_ids": [kw_id, negative_id]})
    assert _state("N")[:2] == (False, None)
    _post(client, {"delete_id": negative_id, "enabled_ids": [kw_id, negative_id]})
    assert _state("N")[:2] == (True, "키워드")


@pytest.mark.parametrize("value,error", [("\t ", "입력"), ("가" * 101, "100자"), ("새\t 플랫폼", "이미 등록")])
def test_add_error_keeps_checks_and_reclassifies(client, value, error):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    assert _state("K")[:2] == (True, "키워드")
    response = _post(client, {"action": "add_keyword", "new_keyword": value})
    location = response.headers["location"]
    assert location.endswith("#rules")
    assert error in parse_qs(urlsplit(location).query)["rules_err"][0]
    assert len(_rules()) == 1 and not _rules()[0]["enabled"]
    assert _state("K")[:2] == (False, None)


def test_add_100_char_rule_and_preserve_checked_rule(client):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    kw_id = _rules()[0]["id"]
    response = _post(client, {"action": "add_clsfc", "new_clsfc": "가" * 100,
                             "enabled_ids": [kw_id]})
    assert "err=" not in response.headers["location"]
    assert all(r["enabled"] for r in _rules())
    assert _state("K")[:2] == (True, "키워드")


def _config_data(**updates):
    return {"interval_minutes": "60", "window_overlap_minutes": "90", "backfill_days": "30",
            "num_of_rows": "20", "max_retries": "2", "inqry_div": "1", "intrntnl_div_cd": "1",
            "indstryty_cds": "1426,1468", **updates}


def test_config_code_change_reclassifies_and_reports_counts(client, monkeypatch):
    original = classifier.reclassify_notices
    calls = []

    def reclassify(session, **kwargs):
        calls.append(session)
        return original(session, **kwargs)

    monkeypatch.setattr(classifier, "reclassify_notices", reclassify)
    assert _state("I")[:2] == (True, "업종")
    unchanged = client.post("/config", data=_config_data(), follow_redirects=False)
    assert unchanged.headers["location"] == "/config?saved=1"
    assert calls == []
    changed = client.post("/config", data=_config_data(indstryty_cds="1426"), follow_redirects=False)
    assert len(calls) == 1
    assert _state("I")[:2] == (False, None)
    assert "vis=0&u=0&c=0&k=0" in changed.headers["location"]
    assert "설정을 저장했습니다 — 표시 0건(업종 0 · 분류 0 · 키워드 0)" in client.get(changed.headers["location"]).text


def test_config_page_size_bounds_labels_and_api_hint(client):
    response = client.post("/config", data=_config_data(num_of_rows="1000"))
    assert response.status_code == 400
    assert "사전규격 수집·면허 누락 보정 조회 페이지 크기: 1~999" in response.text
    assert 'id="rules"' in response.text
    with main.SessionLocal() as session:
        assert repository.get_config(session).num_of_rows == 20
    response = client.post("/config", data=_config_data(num_of_rows="999"))
    assert response.status_code == 200
    with main.SessionLocal() as session:
        assert repository.get_config(session).num_of_rows == 999
    assert "매칭 업종코드 CSV" in response.text
    assert "사전규격 수집·면허 누락 보정 조회 페이지 크기" in response.text
    assert "목록 표시 판정 기준(면허제한 코드와 비교) — 수집 조건이 아닙니다." in response.text
    assert "입찰공고·면허 일괄 조회는 999행 고정. API 상한 999 — 1000 이상이면 오류 없이 10행만 응답합니다." in response.text
    assert 'name="num_of_rows" value="999" min="1" max="999"' in response.text
    assert "최대 999 — 1000 이상이면 API 가 기본 10행만 돌려줍니다" in client.get("/api-test").text


def _database_state():
    """실패 전후 모든 테이블의 저장 값(설정 시각·분류 상세 포함)을 비교한다."""
    with main.SessionLocal() as session:
        return {
            table.name: session.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in Base.metadata.sorted_tables
        }


@pytest.mark.parametrize("operation", ["add", "delete", "save"])
def test_reclassification_failure_rolls_back_rules_and_notices(client, monkeypatch, operation):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    rule_id = _rules()[0]["id"]
    before = _database_state()
    original = classifier.reclassify_notices

    def fail(session, **kwargs):
        # 규칙·표시 상태가 실제 flush된 뒤 실패해도 전체 트랜잭션을 취소해야 한다.
        original(session, commit=False, **kwargs)
        session.flush()
        raise RuntimeError("재분류 예외 주입")

    monkeypatch.setattr(classifier, "reclassify_notices", fail)
    data = {
        "add": {"action": "add_negative", "new_negative": "전산화단층"},
        "delete": {"delete_id": rule_id, "enabled_ids": [rule_id]},
        "save": {"action": "save"},
    }[operation]
    response = _post(client, data)
    assert response.status_code == 303
    assert _database_state() == before
    location = response.headers["location"]
    assert location.endswith("#rules")
    assert "변경을 취소" in parse_qs(urlsplit(location).query)["rules_err"][0]
    card = client.get(location).text.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    assert '<div class="msg err">표시 규칙 저장에 실패' in card


def test_reclassification_failure_rolls_back_config_and_allows_retry(client, monkeypatch):
    before = _database_state()
    original = classifier.reclassify_notices
    rescheduled = []
    monkeypatch.setattr(main.scheduler, "is_running", lambda: True)
    monkeypatch.setattr(main.scheduler, "reschedule", rescheduled.append)

    def fail(session, **kwargs):
        original(session, commit=False, **kwargs)
        session.flush()
        raise RuntimeError("재분류 예외 주입")

    monkeypatch.setattr(classifier, "reclassify_notices", fail)
    data = _config_data(indstryty_cds="1426", num_of_rows="99")
    response = client.post("/config", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert "변경을 취소" in parse_qs(urlsplit(response.headers["location"]).query)["err"][0]
    assert _database_state() == before
    assert rescheduled == []
    monkeypatch.setattr(classifier, "reclassify_notices", original)
    response = client.post("/config", data=data, follow_redirects=False)
    assert "vis=0" in response.headers["location"]
    assert _state("I")[:2] == (False, None)
    assert rescheduled == [60]


@pytest.mark.parametrize("data", [
    {"delete_id": "abc"}, {"delete_id": "²"}, {"delete_id": ""},
    {"delete_id": "١"}, {"delete_id": " 1"},
    {"enabled_ids": ["1", "x"]}, {"enabled_ids": ["²"]},
    {"action": "unknown"}, {"action": ""}, {"action": "unknown", "delete_id": "1"},
])
def test_invalid_ids_and_actions_never_change_state(client, monkeypatch, data):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    before = _database_state()

    def unexpected(session, **kwargs):
        pytest.fail("잘못된 ID·action에서 재분류하면 안 됩니다")

    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    response = _post(client, data)
    assert response.status_code == 303
    location = response.headers["location"]
    assert "rules_err=" in location and location.endswith("#rules")
    assert _database_state() == before


@pytest.mark.parametrize("rule_type,existing,duplicate", [
    (rule_type, existing, duplicate)
    for rule_type in ("keyword", "negative")
    for existing, duplicate in (("SW", "sw"), ("시스템 구축", "시스템  구축"))
])
def test_duplicate_terms_use_existing_value_in_error(client, rule_type, existing, duplicate):
    _post(client, {"action": f"add_{rule_type}", f"new_{rule_type}": existing})
    rule_id = _rules()[0]["id"]
    with main.SessionLocal() as session:
        assert not repository.add_list_rule(session, rule_type, duplicate)
    response = _post(client, {"action": f"add_{rule_type}", f"new_{rule_type}": duplicate,
                             "enabled_ids": [rule_id]})
    error = parse_qs(urlsplit(response.headers["location"]).query)["rules_err"][0]
    assert error == f"이미 등록된 규칙입니다('{existing}')."
    assert len(_rules()) == 1


@pytest.mark.parametrize("rule_type", ["keyword", "negative"])
@pytest.mark.parametrize("existing,new", [("홈페이지", "홈 페이지"), ("SW", "S W"),
                                         ("시스템 구축", "시스템구축"), ("Straße", "STRASSE")])
def test_rule_card_allows_distinct_values(client, rule_type, existing, new):
    _post(client, {"action": f"add_{rule_type}", f"new_{rule_type}": existing})
    response = _post(client, {"action": f"add_{rule_type}", f"new_{rule_type}": new,
                             "enabled_ids": [_rules()[0]["id"]]})
    assert response.status_code == 303 and "rules=1" in response.headers["location"]
    assert [(r["value"], r["enabled"]) for r in _rules()] == [(existing, True), (new, True)]


def test_clsfc_duplicate_comparison_keeps_case_and_spaces(client):
    with main.SessionLocal() as session:
        assert repository.normalize_rule_value("  시스템\t 구축\n") == "시스템 구축"
        assert repository.add_list_rule(session, "clsfc", "  시스템\t 구축\n")
        assert not repository.add_list_rule(session, "clsfc", "시스템 구축")
        assert repository.add_list_rule(session, "clsfc", "시스템구축")
        assert repository.add_list_rule(session, "clsfc", "SW")
        assert repository.add_list_rule(session, "clsfc", "sw")


def test_rule_enter_markup_and_keyword_submit_ignore_clsfc_input(client):
    card = client.get("/config").text.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    for kind in ("clsfc", "keyword", "negative"):
        assert re.search(rf'<input[^>]*name="new_{kind}"[^>]*data-add-btn="add-{kind}"', card)
        assert re.search(rf'<button[^>]*id="add-{kind}"[^>]*value="add_{kind}"', card)
    assert "'#rules input[data-add-btn]'" in card
    assert "event.key !== 'Enter' || event.isComposing" in card
    assert "event.preventDefault()" in card
    assert "document.getElementById(input.dataset.addBtn)" in card
    assert "input.form.requestSubmit(button)" in card
    assert "else button.click()" in card
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼", "new_clsfc": "다른 분류"})
    assert [(r["rule_type"], r["value"]) for r in _rules()] == [("keyword", "새 플랫폼")]
    assert _state("K")[:2] == (True, "키워드")


def test_rule_card_messages_are_separate_from_other_config_errors(client):
    page = client.get("/config?rules=1&vis=3&u=1&c=0&k=2").text
    card = page.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    assert '<div class="msg ok">표시 규칙을 저장했습니다 — 표시 3건' in card
    assert page.count('<div class="msg ok">표시 규칙을 저장했습니다') == 1
    page = client.get("/config", params={"rules_err": '<규칙&"오류>'}).text
    card = page.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    assert '<div class="msg err">&lt;규칙&amp;&quot;오류&gt;</div>' in card
    assert page.count('<div class="msg err">&lt;규칙&amp;&quot;오류&gt;</div>') == 1
    for query in ("err=다른오류", "saved=1"):
        card = client.get(f"/config?{query}").text.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
        assert '<div class="msg ' not in card


@pytest.mark.parametrize("key", ["vis", "u", "c", "k"])
@pytest.mark.parametrize("value", ["abc", "²"])
def test_invalid_count_query_is_ignored(client, key, value):
    query = {"rules": "1", "vis": "3", "u": "1", "c": "0", "k": "2", key: value}
    response = client.get("/config", params=query)
    assert response.status_code == 200
    assert '<div class="msg ok">표시 규칙을 저장했습니다</div>' in response.text
    assert "— 표시" not in response.text


def test_unrestricted_badge_css_has_no_underline(client):
    css = re.search(r"\.indcode\.nolimit\s*\{([^}]+)\}", client.get("/list").text)[1]
    assert "text-decoration: none" in css


@pytest.mark.parametrize("path", ["/config", "/config/rules"])
def test_config_writes_and_classification_run_in_worker_thread(client, monkeypatch, path):
    original_pool = main.run_in_threadpool
    original_local = main.SessionLocal
    original_classify = classifier.reclassify_notices
    loop_threads, session_threads, classify_threads = [], [], []

    async def pool(func, *args, **kwargs):
        loop_threads.append(threading.get_ident())
        return await original_pool(func, *args, **kwargs)

    def local():
        session_threads.append(threading.get_ident())
        return original_local()

    def classify(session, **kwargs):
        classify_threads.append(threading.get_ident())
        return original_classify(session, **kwargs)

    monkeypatch.setattr(main, "run_in_threadpool", pool)
    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(classifier, "reclassify_notices", classify)
    data = (_config_data(indstryty_cds="1426") if path == "/config" else
            {"action": "add_keyword", "new_keyword": "새 플랫폼", "rule_ids": ""})
    response = client.post(path, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert len(loop_threads) == len(session_threads) == len(classify_threads) == 1
    assert session_threads == classify_threads
    assert classify_threads[0] != loop_threads[0]


def test_rule_save_serializes_collector_storage_in_file_sqlite(client, monkeypatch):
    """조회 중인 수집이 규칙 저장의 잠금을 기다린 뒤 새 규칙으로 저장·분류한다."""
    monkeypatch.setattr(collector, "SessionLocal", main.SessionLocal)
    fetched, continue_fetch, saving, release_save = [threading.Event() for _ in range(4)]
    collector_waiting, collector_acquired, upserted = [threading.Event() for _ in range(3)]
    lock = threading.RLock()
    results, errors = {}, []

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "test-collector":
                collector_waiting.set()
            lock.acquire()
            if threading.current_thread().name == "test-collector":
                collector_acquired.set()
            return self

        def __exit__(self, *args):
            lock.release()

    monkeypatch.setattr(classifier, "CLASSIFY_WRITE_LOCK", ObservedLock())
    original_classify = classifier.reclassify_notices
    original_upsert = repository.upsert_bid_notices

    def classify(session, **kwargs):
        if "rules" in kwargs:  # 초안으로 재분류하는 규칙 저장 경로.
            saving.set()
            assert release_save.wait(5), "규칙 저장 해제 신호 없음"
        return original_classify(session, **kwargs)

    def upsert(session, values):
        assert collector_acquired.is_set()
        upserted.set()
        return original_upsert(session, values)

    def fake_call(operation, params, max_retries):
        if operation == collector.OPERATION:
            items = [{"bidNtceNo": "AFTER", "bidNtceOrd": "000", "bidNtceNm": "새플랫폼 개발",
                      "indstrytyLmtYn": "N"}]
        else:
            fetched.set()
            assert continue_fetch.wait(5), "수집 조회 해제 신호 없음"
            items = []
        return api_client.ApiResult(
            operation=operation, request_url="", sent_params={}, response_type="json",
            status_code=200, raw_text="", parsed=None, result_code="00" if items else "03",
            result_msg="", items=items, total_count=str(len(items)),
        ), 0, "ok"

    monkeypatch.setattr(classifier, "reclassify_notices", classify)
    monkeypatch.setattr(repository, "upsert_bid_notices", upsert)
    monkeypatch.setattr(collector, "_call_with_retry", fake_call)

    def collect():
        try:
            results["run"] = collector.collect_window(datetime(2026, 10, 1), datetime(2026, 10, 7))
        except Exception as exc:
            errors.append(exc)

    def save():
        try:
            results["response"] = _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
        except Exception as exc:
            errors.append(exc)

    collect_thread = threading.Thread(target=collect, name="test-collector")
    save_thread = threading.Thread(target=save, name="test-rule-save")
    collect_thread.start()
    try:
        assert fetched.wait(5)
        save_thread.start()
        assert saving.wait(5)
        # 저장이 막혀 있어도 이벤트 루프에서 다른 요청을 처리할 수 있다.
        assert client.get("/config").status_code == 200
        continue_fetch.set()
        assert collector_waiting.wait(5)
        assert not collector_acquired.is_set()
        assert not upserted.is_set()
    finally:
        release_save.set()
        continue_fetch.set()
        collect_thread.join(10)
        if save_thread.ident is not None:
            save_thread.join(10)
    assert not collect_thread.is_alive() and not save_thread.is_alive()
    assert errors == []
    assert results["response"].status_code == 303
    assert "rules=1" in results["response"].headers["location"]
    assert results["run"].status == "success"
    assert collector_acquired.is_set() and upserted.is_set()
    assert _state("AFTER") == (True, "키워드", "새 플랫폼")
    assert _state("K") == (True, "키워드", "새 플랫폼")


@pytest.mark.parametrize("data", [
    {"delete_id": "999999:000000000000"}, {"enabled_ids": ["999999:000000000000"]},
    {"rule_ids": ["999999:000000000000"]}, {"rule_ids": ["²"]}, {"rule_ids": ["1", "x"]},
])
def test_nonexistent_or_invalid_rule_ids_do_not_change_any_table(client, monkeypatch, data):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    before = _database_state()

    def unexpected(*args, **kwargs):
        pytest.fail("유효하지 않은 규칙 목록에서 재분류하면 안 됩니다")

    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    response = _post(client, data)
    assert response.status_code == 303
    assert "rules_err=" in response.headers["location"]
    assert _database_state() == before


def test_missing_rule_ids_is_rejected_without_changes(client):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    before = _database_state()
    response = client.post("/config/rules", data={"action": "save"}, follow_redirects=False)
    assert response.status_code == 303
    error = parse_qs(urlsplit(response.headers["location"]).query)["rules_err"][0]
    assert error == "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요."
    assert _database_state() == before


@pytest.mark.parametrize("new_enabled", [True, False])
def test_old_form_only_updates_rendered_rules(client, new_enabled):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    page_a = client.get("/config").text
    rendered = re.findall(r'<input type="hidden" name="rule_ids" value="([0-9]+:[0-9a-f]{12})">', page_a)
    assert len(rendered) == 1
    _post(client, {"action": "add_negative", "new_negative": "전산화단층",
                   "enabled_ids": rendered})
    negative_id = next(r["id"] for r in _rules() if r["rule_type"] == "negative")
    if not new_enabled:
        _post(client, {"enabled_ids": rendered})
    response = _post(client, {"action": "save", "rule_ids": rendered})
    assert "rules=1" in response.headers["location"]
    rules = {r["id"]: r for r in _rules()}
    assert not rules[int(rendered[0].split(":")[0])]["enabled"]
    assert rules[negative_id]["enabled"] is new_enabled
    with main.SessionLocal() as session:
        assert classifier.reclassify_notices(session)["changed"] == 0


@pytest.mark.parametrize("operation", ["delete", "save_off", "save_on"])
def test_other_tab_enabled_change_rejects_old_form_without_changes(client, monkeypatch, operation):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    _post(client, {"action": "add_negative", "new_negative": "전산화단층",
                   "enabled_ids": [r["id"] for r in _rules()]})
    page_a = client.get("/config").text
    rendered = re.findall(r'name="rule_ids" value="([0-9]+:[0-9a-f]{12})"', page_a)
    assert len(rendered) == 2
    keyword = next(r for r in _rules() if r["rule_type"] == "keyword")
    negative = next(r for r in _rules() if r["rule_type"] == "negative")
    negative_ref = _rule_ref(negative)
    assert _state("K") == (True, "키워드", "새 플랫폼")

    # 탭 B에서 키워드만 끈다. 체크 편집 시에도 폼의 지문은 렌더 당시 값이다.
    response_b = client.post("/config/rules", data={
        "rule_ids": rendered, "enabled_ids": [negative_ref], "action": "save",
    }, follow_redirects=False)
    assert "rules=1" in response_b.headers["location"]
    assert not next(r for r in _rules() if r["id"] == keyword["id"])["enabled"]
    assert _state("K")[:2] == (False, None)
    before = _database_state()
    monkeypatch.setattr(classifier, "reclassify_notices", lambda *a, **kw: pytest.fail("재분류 금지"))

    data = {"rule_ids": rendered, "action": "save"}
    if operation == "delete":
        data.update(delete_id=negative_ref, enabled_ids=rendered)
    elif operation == "save_on":
        data["enabled_ids"] = rendered
    response_a = client.post("/config/rules", data=data, follow_redirects=False)
    assert response_a.status_code == 303
    location = response_a.headers["location"]
    assert location.endswith("#rules")
    assert parse_qs(urlsplit(location).query)["rules_err"] == [
        "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.",
    ]
    assert _database_state() == before


@pytest.mark.parametrize("operation", ["delete", "save_off", "save_on"])
def test_reused_max_id_rejects_old_rendered_form_without_changes(client, monkeypatch, operation):
    _post(client, {"action": "add_negative", "new_negative": "전산화단층"})
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼",
                   "enabled_ids": [r["id"] for r in _rules()]})
    page_a = client.get("/config").text
    rendered = re.findall(r'<input type="hidden" name="rule_ids" value="([0-9]+:[0-9a-f]{12})">', page_a)
    old = max(_rules(), key=lambda row: row["id"])
    old_ref = _rule_ref(old)
    assert old_ref in rendered
    _post(client, {"delete_id": old["id"], "enabled_ids": [r["id"] for r in _rules()]})
    _post(client, {"action": "add_keyword", "new_keyword": "개발",
                   "enabled_ids": [r["id"] for r in _rules()]})
    new = max(_rules(), key=lambda row: row["id"])
    assert new["id"] == old["id"]  # SQLite의 최대 id 재사용을 실제 파일 DB에서 확인.
    assert _rule_ref(new) != old_ref
    before = _database_state()

    def unexpected(*args, **kwargs):
        pytest.fail("오래된 규칙 참조는 재분류 전에 거부해야 합니다")

    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    data = {"rule_ids": rendered, "action": "save"}
    if operation == "delete":
        data["delete_id"] = old_ref
    elif operation == "save_on":
        data["enabled_ids"] = rendered
    response = client.post("/config/rules", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["rules_err"] == [
        "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.",
    ]
    assert _database_state() == before


@pytest.mark.parametrize("field", ["rule_ids", "enabled_ids", "delete_id"])
@pytest.mark.parametrize("missing", [False, True])
def test_stale_reference_in_each_field_rejected_before_changes(client, monkeypatch, field, missing):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    old = _rules()[0]
    old_ref = _rule_ref(old)
    with main.SessionLocal() as session:
        if missing:
            repository.delete_list_rule(session, old["id"])
        else:
            row = session.get(ListRule, old["id"])
            row.created_at += timedelta(seconds=1)
            session.commit()
    before = _database_state()
    monkeypatch.setattr(classifier, "reclassify_notices", lambda *a, **kw: pytest.fail("재분류 금지"))
    data = {field: old_ref if field == "delete_id" else [old_ref]}
    response = _post(client, data)
    assert parse_qs(urlsplit(response.headers["location"]).query)["rules_err"] == [
        "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.",
    ]
    assert _database_state() == before


@pytest.mark.parametrize("field", ["rule_ids", "enabled_ids", "delete_id"])
@pytest.mark.parametrize("reference", [
    "1", "1:", "1:abcdef01234", "1:abcdef0123456", "1:abcdef01234g",
    "１:abcdef012345", "²:abcdef012345", " 1:abcdef012345", "+1:abcdef012345",
    "1:abcdef012345\n", "1:abcdef012345:0", "1:ａｂｃｄｅｆ012345",
])
def test_invalid_fingerprint_format_rejected_without_changes(client, monkeypatch, field, reference):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    before = _database_state()
    monkeypatch.setattr(classifier, "reclassify_notices", lambda *a, **kw: pytest.fail("재분류 금지"))
    response = _post(client, {field: reference if field == "delete_id" else [reference]})
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["rules_err"] == [
        "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.",
    ]
    assert _database_state() == before


def test_fresh_rendered_references_save_and_delete_successfully(client):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    for checked in (False, True):
        page = client.get("/config").text
        references = re.findall(r'name="rule_ids" value="([0-9]+:[0-9a-f]{12})"', page)
        assert len(references) == 1
        data = {"rule_ids": references, "enabled_ids": references if checked else [], "action": "save"}
        response = client.post("/config/rules", data=data, follow_redirects=False)
        assert "rules=1" in response.headers["location"]
        assert _rules()[0]["enabled"] is checked
        assert _state("K")[0] is checked
    page = client.get("/config").text
    references = re.findall(r'name="rule_ids" value="([0-9]+:[0-9a-f]{12})"', page)
    delete_ref = re.search(r'name="delete_id" value="([0-9]+:[0-9a-f]{12})"', page)[1]
    response = client.post("/config/rules", data={
        "rule_ids": references, "enabled_ids": references, "delete_id": delete_ref,
    }, follow_redirects=False)
    assert "rules=1" in response.headers["location"]
    assert _rules() == [] and _state("K")[0] is False


def test_rule_fingerprint_uses_identity_enabled_and_iso_timestamp(client):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    row = _rules()[0]
    fingerprint = repository.list_rule_fingerprint(row)
    assert re.fullmatch(r"[0-9a-f]{12}", fingerprint)
    with main.SessionLocal() as session:
        model = session.get(ListRule, row["id"])
        assert row["created_at"] == model.created_at.isoformat()
        assert repository.list_rule_fingerprint(model) == fingerprint
    for field, value in (("id", row["id"] + 1), ("rule_type", "negative"),
                         ("value", "다른 값"), ("enabled", False),
                         ("created_at", "2026-10-08T00:00:00")):
        assert repository.list_rule_fingerprint({**row, field: value}) != fingerprint


@pytest.mark.parametrize("action", ["add_clsfc", "add_keyword", "add_negative", "delete", "save"])
def test_draft_rule_order_and_results_equal_committed_rules(client, action):
    # 여러 일치 후보를 두고 id 순에 따른 match_detail 선택도 비교한다.
    for kind, value in (("keyword", "새 플랫폼"), ("negative", "전산화단층"),
                        ("keyword", "플랫폼"), ("clsfc", '<분류&"명>')):
        _post(client, {"action": f"add_{kind}", f"new_{kind}": value,
                       "enabled_ids": [r["id"] for r in _rules()]})
    ids = [r["id"] for r in _rules()]
    data = {"enabled_ids": ids}
    if action.startswith("add_"):
        kind = action.removeprefix("add_")
        data.update(action=action, **{f"new_{kind}": {
            "clsfc": "다른 분류", "keyword": "개발", "negative": "개발",
        }[kind]})
    elif action == "delete":
        data["delete_id"] = next(r["id"] for r in _rules() if r["value"] == "새 플랫폼")
    else:
        data.update(action="save", enabled_ids=[r["id"] for r in _rules() if r["value"] != "새 플랫폼"])
    response = _post(client, data)
    assert "rules=1" in response.headers["location"]
    with main.SessionLocal() as session:
        stats = classifier.reclassify_notices(session)
        assert stats["changed"] == 0
        assert _classification_counts(response) == main._classification_params(stats)


def _classification_counts(response):
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {key: int(query[key][0]) for key in ("vis", "u", "c", "k")}


@pytest.mark.parametrize("path", ["/config/rules", "/config"])
def test_classification_does_not_hold_sqlite_write_lock(client, monkeypatch, path):
    """파일 DB의 판정을 멈추고 별도 연결이 대기 없이 쓰는지 검증한다."""
    original_local = main.SessionLocal
    # autoflush=True여도 판정 중 조회가 쓰기를 유발하면 안 된다.
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(
        bind=original_local.kw["bind"], autoflush=True,
    ))
    paused, resume = threading.Event(), threading.Event()
    original_classify = classifier.classify_notice
    results, errors = {}, []

    def classify(**kwargs):
        if kwargs["bid_ntce_nm"] == "새플랫폼 전산화단층":
            # K(규칙 변경) 또는 I(설정 변경)의 메모리 갱신 뒤 멈춘다.
            paused.set()
            assert resume.wait(5), "재분류 해제 신호 없음"
        return original_classify(**kwargs)

    monkeypatch.setattr(classifier, "classify_notice", classify)

    def save():
        try:
            data = ({"action": "add_keyword", "new_keyword": "새 플랫폼", "rule_ids": ""}
                    if path == "/config/rules" else _config_data(indstryty_cds="1426"))
            results["response"] = client.post(path, data=data, follow_redirects=False)
        except Exception as exc:
            errors.append(exc)

    independent = create_engine(original_local.kw["bind"].url, connect_args={"timeout": 0})
    thread = threading.Thread(target=save)
    thread.start()
    try:
        assert paused.wait(5)
        # busy_timeout=0이므로 쓰기 잠금이 있으면 즉시 실패한다.
        with independent.begin() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=0")
            connection.execute(CollectionRun.__table__.insert().values(trigger="test", status="running"))
        assert _rules() == []
        assert _state("K") == (False, None, None)
        assert _state("I")[:2] == (True, "업종")
        with original_local() as session:
            assert repository.get_config(session).indstryty_cds == "1426,1468"
            assert session.scalar(select(CollectionRun.id)) is not None
    finally:
        resume.set()
        thread.join(10)
        independent.dispose()
    assert not thread.is_alive()
    assert errors == []
    response = results["response"]
    assert response.status_code == 303
    if path == "/config/rules":
        assert _rules()[0]["value"] == "새 플랫폼"
        assert _state("K") == (True, "키워드", "새 플랫폼")
    else:
        assert _state("I")[:2] == (False, None)
        with original_local() as session:
            assert repository.get_config(session).indstryty_cds == "1426"
    with original_local() as session:
        stats = classifier.reclassify_notices(session)
        assert stats["changed"] == 0
        assert _classification_counts(response) == main._classification_params(stats)


# --- Phase 13.4b: 새 창 초안 미리보기·적용 ---

def _rule_test_page_form(page):
    data = {kind: html.unescape(re.search(
        rf'<textarea name="{kind}"[^>]*>(.*?)</textarea>', page, re.S)[1])
        for kind in ("clsfc", "keyword", "negative")}
    data.update(fingerprint=re.search(r'name="fingerprint" value="([^"]+)"', page)[1],
                **{field: html.unescape(re.search(rf'name="{field}" value="([^"]*)"', page)[1])
                   for field in ("dt_from", "dt_to", "search")})
    return data


def _test_form(client, **updates):
    data = _rule_test_page_form(client.get("/config/rules/test").text)
    data.update(dt_from="", dt_to="", search="")
    return {**data, **updates}


def _preview(client, data):
    return client.post("/config/rules/test", data=data)


def _apply(client, data):
    return client.post("/config/rules/test/apply", data=data, follow_redirects=False)


def _assert_rule_test_error_at_top(page):
    assert 'id="preview"' not in page
    assert 'window.location.hash' not in page
    assert page.index("초안 편집") < page.index('<div class="msg err">') < page.index('<textarea')


def test_rule_test_link_defaults_enabled_only_and_escaping(client):
    with main.SessionLocal() as session:
        repository.add_list_rule(session, "clsfc", '<분류&"명>')
        repository.add_list_rule(session, "keyword", '</textarea><script>alert(1)</script>')
        repository.add_list_rule(session, "negative", "끈 제외어")
        rows = repository.list_list_rules(session)
        repository.set_list_rules_enabled(session, [r["id"] for r in rows],
                                          [r["id"] for r in rows if r["rule_type"] != "negative"])
    config = client.get("/config").text
    assert '<a href="/config/rules/test" target="_blank" rel="noopener">규칙 테스트 (새 창)</a>' in config
    page = client.get("/config/rules/test").text
    data = _test_form(client)
    assert data["clsfc"] == '<분류&"명>'
    assert data["keyword"] == '</textarea><script>alert(1)</script>'
    assert data["negative"] == ""
    assert "끈 제외어" not in page
    assert '<script>alert(1)</script>' not in page
    assert "&lt;/textarea&gt;&lt;script&gt;" in page
    assert "1426, 1468" in page and "읽기 전용" in page
    assert "정식 분류명과 정확히 같을 때" in page
    assert "앞뒤가 영문·숫자가 아닐 때" in page
    today = datetime.now().date()
    assert f'name="dt_from" value="{(today - timedelta(days=29)).isoformat()}"' in page
    assert f'name="dt_to" value="{today.isoformat()}"' in page
    assert "전체 공고를 다시 분류" in page


def test_rule_test_preview_add_remove_and_no_writes(client):
    before = _database_state()
    data = _test_form(client, keyword="\n 새\t 플랫폼\n\n", negative="전산화단층", search="새플랫폼")
    response = _preview(client, data)
    assert response.status_code == 200
    page = response.text
    assert "대상 공고 4건" in page
    assert "현재 표시 1건 → 초안 표시 2건" in page
    assert "새로 표시 1건 · 새로 숨김 0건" in page
    assert "초안: 업종 1 · 분류 0 · 키워드 1" in page
    added = page.split('<h2>새로 표시', 1)[1].split('</section>', 1)[0]
    assert '<td>K</td>' in added and '<td>N</td>' not in added
    assert '표시 · 키워드</span> 새 플랫폼' in added
    search = page.split('<h2>공고명 검색 결과', 1)[1]
    assert '(2건)' in search and '<td>N</td>' in search
    assert _database_state() == before
    assert _apply(client, data).status_code == 303
    before = _database_state()
    response = _preview(client, _test_form(client, keyword=""))
    assert "현재 표시 2건 → 초안 표시 1건" in response.text
    assert "새로 표시 0건 · 새로 숨김 1건" in response.text
    assert '<td>K</td>' in response.text.split('<h2>새로 숨김', 1)[1]
    assert _database_state() == before


def test_rule_test_preview_date_boundaries_and_search_does_not_filter_summary(client):
    with main.SessionLocal() as session:
        session.get(BidNotice, "K").bid_ntce_dt = datetime(2026, 10, 7, 23, 59, 59)
        session.get(BidNotice, "N").bid_ntce_dt = datetime(2026, 10, 8)
        session.get(BidNotice, "C").bid_ntce_dt = None
        session.get(BidNotice, "I").bid_ntce_dt = datetime(2026, 10, 6, 23, 59, 59)
        session.commit()
    data = _test_form(client, keyword="새플랫폼", dt_from="2026-10-07", dt_to="2026-10-07", search="없는검색")
    before = _database_state()
    page = _preview(client, data).text
    assert "대상 공고 1건" in page
    assert "새로 표시 1건 · 새로 숨김 0건" in page
    assert "공고명 검색 결과 (0건)" in page
    assert '<td>K</td>' in page and '<td>N</td>' not in page
    for start, end, total in (("", "", 4), ("2026-10-07", "", 2), ("", "2026-10-07", 2)):
        page = _preview(client, {**data, "dt_from": start, "dt_to": end}).text
        assert f"대상 공고 {total}건" in page
    assert _database_state() == before


@pytest.mark.parametrize("dt_from,dt_to", [
    ("", ""), ("2026-10-07", ""), ("", "2026-10-07"), ("2026-10-07", "2026-10-07"),
])
def test_rule_test_preview_passes_date_bounds_without_number_targets(client, monkeypatch, dt_from, dt_to):
    data = _test_form(client, keyword="새플랫폼", dt_from=dt_from, dt_to=dt_to)
    original = classifier.iter_classifications
    calls = []

    def classify(*args, **kwargs):
        assert "bid_ntce_nos" not in kwargs
        calls.append((kwargs["bid_ntce_dt_from"], kwargs["bid_ntce_dt_to"]))
        yield from original(*args, **kwargs)

    monkeypatch.setattr(classifier, "iter_classifications", classify)
    before = _database_state()
    response = _preview(client, data)
    assert response.status_code == 200 and "대상 공고 4건" in response.text
    assert calls == [(datetime(2026, 10, 7) if dt_from else None,
                      datetime(2026, 10, 8) if dt_to else None)]
    assert _database_state() == before


@pytest.mark.parametrize("removed", [False, True])
def test_rule_test_preview_row_caps_and_search_cap(client, removed):
    with main.SessionLocal() as session:
        now = datetime(2026, 10, 7)
        session.add_all([BidNotice(bid_ntce_no=f"CAP-{i:03}", bid_ntce_nm="경계키워드 <공고>",
                                  bid_ntce_dt=now, collected_at=now, updated_at=now)
                         for i in range(503)])
        if removed:
            repository.add_list_rule(session, "keyword", "경계키워드", commit=False)
        classifier.reclassify_notices(session)
    data = _test_form(client, keyword="" if removed else "경계키워드", search="경계키워드")
    before = _database_state()
    page = _preview(client, data).text
    section = page.split('<h2>새로 숨김' if removed else '<h2>새로 표시', 1)[1].split('</section>', 1)[0]
    assert '(503건)' in section and section.count('<tr><td>') == 200
    assert '외 303건' in section and 'CAP-200' not in section
    section = page.split('<h2>공고명 검색 결과', 1)[1]
    assert section.count('<tr><td>') == 100 and '외 403건' in section
    assert '&lt;공고&gt;' in section and '<공고>' not in section
    assert _database_state() == before


@pytest.mark.parametrize("kind,value", [
    (kind, value) for kind in ("clsfc", "keyword", "negative")
    for value in ("가" * 101, "중복\n 중복 ")
] + [(kind, value) for kind in ("keyword", "negative")
    for value in ("SW\nsw", "시스템 구축\n시스템  구축", "S W\ns  w")])
def test_rule_test_invalid_draft_never_classifies_or_writes(client, monkeypatch, kind, value):
    data = _test_form(client, **{kind: value})
    before = _database_state()
    def unexpected(*args, **kwargs):
        pytest.fail("검증 실패한 초안은 분류하지 않아야 합니다")
    monkeypatch.setattr(classifier, "iter_classifications", unexpected)
    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    for response in (_preview(client, data), _apply(client, data)):
        assert response.status_code == 400
        assert "100자" in response.text or "중복된 규칙" in response.text
        assert "미리보기 결과" not in response.text
        _assert_rule_test_error_at_top(response.text)
        assert _database_state() == before


@pytest.mark.parametrize("start,end", [("bad", ""), ("2026-10-08", "2026-10-07"), ("", "9999-12-31")])
def test_rule_test_invalid_date_never_writes(client, start, end):
    data = _test_form(client, keyword="추가", dt_from=start, dt_to=end)
    before = _database_state()
    for response in (_preview(client, data), _apply(client, data)):
        assert response.status_code == 400
        assert "날짜를 확인" in response.text
        _assert_rule_test_error_at_top(response.text)
        assert _database_state() == before


def test_rule_test_apply_enables_adds_disables_without_delete_and_matches_preview(client):
    with main.SessionLocal() as session:
        for kind, value in (("clsfc", '<분류&"명>'), ("keyword", "새 플랫폼"), ("negative", "전산화단층")):
            repository.add_list_rule(session, kind, value)
        rows = repository.list_list_rules(session)
        repository.set_list_rules_enabled(session, [r["id"] for r in rows], [rows[0]["id"]])
        classifier.reclassify_notices(session)
    data = _test_form(client, clsfc="", keyword="새 플랫폼\n경계키워드", negative="전산화단층",
                      search='<검색&"값>', dt_from="2026-10-01", dt_to="2026-10-07")
    preview = _preview(client, data).text
    assert "현재 표시 2건 → 초안 표시 2건" in preview
    assert "새로 표시 1건 · 새로 숨김 1건" in preview
    response = _apply(client, data)
    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert {key: query[key] for key in ("vis", "u", "c", "k")} == {
        "vis": ["2"], "u": ["1"], "c": ["0"], "k": ["1"]}
    assert query["dt_from"] == ["2026-10-01"] and query["q"] == [data["search"]]
    after = _rules()
    assert {r["id"] for r in rows}.issubset({r["id"] for r in after})
    assert [(r["value"], r["enabled"]) for r in after] == [
        ('<분류&"명>', False), ("새 플랫폼", True), ("경계키워드", True), ("전산화단층", True)]
    assert _state("K") == (True, "키워드", "새 플랫폼")
    assert _state("N")[:2] == (False, None) and _state("C")[:2] == (False, None)
    page = client.get(location).text
    assert "규칙을 적용했습니다 — 표시 2건(업종 1 · 분류 0 · 키워드 1) (저장된 전체 공고 기준)" in page
    assert '<textarea name="clsfc" rows="10"></textarea>' in page
    assert '<textarea name="keyword" rows="10">새 플랫폼\n경계키워드</textarea>' in page
    assert '&lt;검색&amp;&quot;값&gt;' in page
    assert data["fingerprint"] not in page


@pytest.mark.parametrize("change", ["add", "disable", "delete", "missing", "invalid", "codes"])
def test_rule_test_stale_whole_set_fingerprint_refuses_changes(client, monkeypatch, change):
    _post(client, {"action": "add_keyword", "new_keyword": "새 플랫폼"})
    data = _test_form(client, keyword="새 플랫폼\n새규칙", dt_from="2026-10-01",
                      dt_to="2026-10-07", search='<검색&"값>')
    with main.SessionLocal() as session:
        if change == "add":
            repository.add_list_rule(session, "negative", "끄거나추가한값")
        elif change == "disable":
            repository.set_list_rules_enabled(session, [_rules()[0]["id"]], [])
        elif change == "delete":
            repository.delete_list_rule(session, _rules()[0]["id"])
        elif change == "missing":
            data.pop("fingerprint")
        elif change == "codes":
            response = client.post("/config", data=_config_data(indstryty_cds="1426"),
                                   follow_redirects=False)
            assert response.status_code == 303
        else:
            data["fingerprint"] = "wrong"
    before = _database_state()
    def unexpected(*args, **kwargs):
        pytest.fail("지문 불일치 시 재분류 금지")
    original = classifier.reclassify_notices
    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    # 미리보기를 거쳐도 옛 지문을 새 지문으로 바꾸어서는 안 된다.
    preview = _preview(client, data)
    fingerprint = re.search(r'name="fingerprint" value="([^"]*)"', preview.text)[1]
    assert fingerprint == data.get("fingerprint", "")
    assert preview.status_code == 200
    assert "미리보기 결과 (대상 기간 기준)" in preview.text
    assert "현재 상태로 계산한 미리보기" in preview.text
    reload_url = html.unescape(re.search(
        r'<a href="([^"]+)">현재 규칙 다시 불러오기\(초안 버림\)</a>', preview.text)[1])
    expected_filters = {"dt_from": [data["dt_from"]], "dt_to": [data["dt_to"]], "q": [data["search"]]}
    assert parse_qs(urlsplit(reload_url).query) == expected_filters
    response = _apply(client, {**data, "fingerprint": fingerprint})
    assert response.status_code == 409
    assert "location" not in response.headers
    assert rule_test.STALE_MESSAGE in response.text
    assert 'id="preview"' not in response.text
    assert 'window.location.hash' not in response.text
    assert response.text.index("초안 편집") < response.text.index(rule_test.STALE_MESSAGE) < response.text.index('<textarea')
    preserved = {kind: html.unescape(re.search(
        rf'<textarea name="{kind}"[^>]*>(.*?)</textarea>', response.text, re.S)[1])
        for kind, _, _ in repository.RULE_FIELDS}
    preserved.update({field: html.unescape(re.search(
        rf'name="{field}" value="([^"]*)"', response.text)[1])
        for field in ("fingerprint", "dt_from", "dt_to", "search")})
    assert preserved == {**data, "fingerprint": fingerprint}
    assert '&lt;검색&amp;&quot;값&gt;' in response.text
    assert html.unescape(re.search(
        r'<a href="([^"]+)">현재 규칙 다시 불러오기\(초안 버림\)</a>', response.text)[1]) == reload_url
    assert "현재 상태로 계산한 미리보기" not in response.text
    assert _database_state() == before
    repeated = _apply(client, preserved)
    assert repeated.status_code == 409
    assert repeated.text == response.text
    assert _database_state() == before

    refreshed = client.get(reload_url)
    assert refreshed.status_code == 200
    assert "현재 상태로 계산한 미리보기" not in refreshed.text
    assert rule_test.STALE_MESSAGE not in refreshed.text
    fresh_data = _rule_test_page_form(refreshed.text)
    assert fresh_data["fingerprint"] != fingerprint
    assert fresh_data["keyword"] != data["keyword"]
    assert fresh_data["dt_from"] == data["dt_from"]
    assert fresh_data["dt_to"] == data["dt_to"]
    assert fresh_data["search"] == data["search"]
    assert client.get(reload_url).text == refreshed.text  # 새로고침은 GET만 반복한다.
    assert _database_state() == before
    monkeypatch.setattr(classifier, "reclassify_notices", original)
    applied = _apply(client, fresh_data)
    assert applied.status_code == 303 and "applied=1" in applied.headers["location"]


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_rule_test_apply_exception_rolls_back_everything(client, monkeypatch, error_type):
    data = _test_form(client, keyword="새 플랫폼")
    before = _database_state()
    original = classifier.reclassify_notices
    def fail(session, **kwargs):
        assert kwargs.pop("commit") is True
        original(session, commit=False, **kwargs)
        session.flush()
        raise error_type("규칙 테스트 재분류 예외 주입")
    monkeypatch.setattr(classifier, "reclassify_notices", fail)
    response = _apply(client, data)
    assert response.status_code == 400 and "변경을 취소" in response.text
    assert "규칙 테스트 재분류 예외 주입" not in response.text
    assert _database_state() == before
    monkeypatch.setattr(classifier, "reclassify_notices", original)
    assert _apply(client, data).status_code == 303


def test_rule_test_preview_worker_thread_without_lock_and_apply_lock(client, monkeypatch):
    data = _test_form(client, keyword="새 플랫폼")
    original_pool, original_iter = main.run_in_threadpool, classifier.iter_classifications
    loop_threads, classify_threads, locks = [], [], []
    async def pool(func, *args, **kwargs):
        loop_threads.append(threading.get_ident())
        return await original_pool(func, *args, **kwargs)
    class Lock:
        def __enter__(self):
            locks.append(True)
        def __exit__(self, *args):
            locks.append(False)
    def classify(*args, **kwargs):
        classify_threads.append(threading.get_ident())
        yield from original_iter(*args, **kwargs)
    monkeypatch.setattr(main, "run_in_threadpool", pool)
    monkeypatch.setattr(classifier, "iter_classifications", classify)
    monkeypatch.setattr(classifier, "CLASSIFY_WRITE_LOCK", Lock())
    assert _preview(client, data).status_code == 200
    assert locks == []
    assert _apply(client, data).status_code == 303
    assert locks == [True, False]
    assert len(loop_threads) == len(classify_threads) == 2
    assert all(worker != loop for worker, loop in zip(classify_threads, loop_threads))


@pytest.mark.parametrize("kind", ["clsfc", "keyword", "negative"])
@pytest.mark.parametrize("invalid", ["missing", "duplicate"])
def test_rule_test_requires_each_draft_field_exactly_once(client, monkeypatch, kind, invalid):
    assert _apply(client, _test_form(client, clsfc='<분류&"명>', keyword="새 플랫폼",
                                    negative="전산화단층")).status_code == 303
    data = _test_form(client, dt_from="2026-10-01", dt_to="2026-10-07", search='<검색&"값>')
    if invalid == "missing":
        data.pop(kind)
    else:
        data[kind] = [data[kind], data[kind]]
    before = _database_state()
    def unexpected(*args, **kwargs):
        pytest.fail("누락·중복 필드는 분류 전에 거부해야 합니다")
    for path in ("/config/rules/test", "/config/rules/test/apply"):
        with monkeypatch.context() as patches:
            patches.setattr(classifier, "iter_classifications", unexpected)
            patches.setattr(classifier, "reclassify_notices", unexpected)
            response = client.post(path, data=data, follow_redirects=False)
            assert response.status_code == 303
            location = response.headers["location"]
            assert urlsplit(location).fragment == ""
            assert parse_qs(urlsplit(location).query) == {
                "err": ["form"], "dt_from": [data["dt_from"]], "dt_to": [data["dt_to"]],
                "q": [data["search"]]}
            refreshed = client.get(location)
            assert refreshed.status_code == 200
            assert rule_test.FORM_MESSAGE in refreshed.text
            assert 'id="preview"' not in refreshed.text
            assert 'window.location.hash' not in refreshed.text
            assert refreshed.text.index("초안 편집") < refreshed.text.index(rule_test.FORM_MESSAGE) < refreshed.text.index('<textarea')
            fresh_data = _rule_test_page_form(refreshed.text)
            assert fresh_data == _test_form(client, dt_from=data["dt_from"], dt_to=data["dt_to"],
                                            search=data["search"])
            assert _database_state() == before
        applied = _apply(client, fresh_data)
        assert applied.status_code == 303 and "applied=1" in applied.headers["location"]
        assert _database_state() == before


@pytest.mark.parametrize("field", ["clsfc", "keyword", "negative", "dt_from", "dt_to", "search"])
def test_rule_test_file_form_values_reload_without_classification_or_error_logs(client, monkeypatch, caplog, field):
    assert _apply(client, _test_form(client, keyword="새 플랫폼")).status_code == 303
    data = _test_form(client, dt_from="2026-10-01", dt_to="2026-10-07", search="검색")
    data.pop(field)
    before = _database_state()
    def unexpected(*args, **kwargs):
        pytest.fail("파일 폼 값은 분류 전에 거부해야 합니다")
    monkeypatch.setattr(classifier, "iter_classifications", unexpected)
    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    caplog.clear()
    for path in ("/config/rules/test", "/config/rules/test/apply"):
        response = client.post(path, data=data, files={field: ("value.txt", b"value", "text/plain")},
                               follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        params = parse_qs(urlsplit(location).query, keep_blank_values=True)
        assert params == {"err": ["form"], "dt_from": [data.get("dt_from", "")],
                          "dt_to": [data.get("dt_to", "")], "q": [data.get("search", "")]}
        refreshed = client.get(location)
        assert refreshed.status_code == 200 and rule_test.FORM_MESSAGE in refreshed.text
        assert '<textarea name="keyword" rows="10">새 플랫폼</textarea>' in refreshed.text
        assert _database_state() == before
    assert not [record for record in caplog.records if record.levelno >= 40 or record.exc_info]


@pytest.mark.parametrize("field", ["clsfc", "keyword", "negative", "dt_from", "dt_to", "search"])
def test_rule_test_non_string_mapping_values_are_structure_errors(field):
    data = dict(clsfc="", keyword="", negative="", dt_from="", dt_to="", search="")
    data[field] = object()
    with pytest.raises(rule_test.FormStructureError):
        rule_test.draft_values(data)


def test_rule_test_getlist_protocol_rejects_duplicates_without_formdata_dependency():
    class MultiValues(dict):
        def getlist(self, key):
            return [self[key], self[key]] if key == "keyword" else [self[key]] if key in self else []

    with pytest.raises(rule_test.FormStructureError):
        rule_test.draft_values(MultiValues(clsfc="", keyword="SW", negative=""))


def test_rule_test_get_uses_only_q_for_search(client):
    assert _rule_test_page_form(client.get("/config/rules/test?search=ignored").text)["search"] == ""
    assert _rule_test_page_form(client.get("/config/rules/test?q=chosen&search=ignored").text)["search"] == "chosen"


def _sized_draft(client, size):
    data = _test_form(client, clsfc="분류", keyword="새 플랫폼", negative="전산화단층")
    for index, (kind, _, _) in enumerate(repository.RULE_FIELDS):
        field_size = size // 3 + (1 if index < size % 3 else 0)
        data[kind] += " " * (field_size - len(data[kind]))
    assert sum(len(data[kind]) for kind, _, _ in repository.RULE_FIELDS) == size
    return data


@pytest.mark.parametrize("kind", ["clsfc", "keyword", "negative", "chars"])
def test_rule_test_draft_limits_reject_before_classification_and_writes(client, monkeypatch, kind):
    assert _apply(client, _test_form(client, keyword="새 플랫폼")).status_code == 303
    data = (_sized_draft(client, 20_001) if kind == "chars" else
            _test_form(client, **{kind: "\n".join(f"규칙{i:03}" for i in range(301))}))
    before = _database_state()
    def unexpected(*args, **kwargs):
        pytest.fail("크기 초과 초안은 분류 전에 거부해야 합니다")
    monkeypatch.setattr(classifier, "iter_classifications", unexpected)
    monkeypatch.setattr(classifier, "reclassify_notices", unexpected)
    for response in (_preview(client, data), _apply(client, data)):
        assert response.status_code == 400
        assert ("20,000자" if kind == "chars" else "종류별 300개") in response.text
        assert "미리보기 결과" not in response.text
        _assert_rule_test_error_at_top(response.text)
        assert _database_state() == before


@pytest.mark.parametrize("kind", ["clsfc", "keyword", "negative", "chars"])
def test_rule_test_draft_limit_boundaries_allow_preview_and_apply(client, kind):
    data = (_sized_draft(client, 20_000) if kind == "chars" else
            _test_form(client, **{kind: "\n\n".join(f"규칙{i:03}" for i in range(300))}))
    before = _database_state()
    response = _preview(client, data)
    assert response.status_code == 200 and "미리보기 결과" in response.text
    assert _database_state() == before
    assert _apply(client, data).status_code == 303
    assert len(_rules()) == (3 if kind == "chars" else 300)
    assert all(r["enabled"] for r in _rules())


@pytest.mark.parametrize("kind", ["keyword", "negative"])
def test_rule_test_spacing_values_remain_distinct_in_preview_and_apply(client, kind):
    with main.SessionLocal() as session:
        repository.add_list_rule(session, kind, "홈페이지")
        repository.add_list_rule(session, kind, "시스템 구축")
    data = _test_form(client, **{kind: "홈페이지\n홈 페이지\n시스템구축"})
    before = _database_state()
    assert _preview(client, data).status_code == 200
    assert _database_state() == before
    assert _apply(client, data).status_code == 303
    assert [(r["value"], r["enabled"]) for r in _rules()] == [
        ("홈페이지", True), ("시스템 구축", False), ("홈 페이지", True), ("시스템구축", True)]


@pytest.mark.parametrize("kind", ["keyword", "negative"])
def test_rule_test_unicode_values_remain_distinct_in_preview_and_apply(client, kind):
    assert classifier.match_terms("STRASSE", ["Straße"]) == []
    assert classifier.match_terms("Straße", ["STRASSE"]) == []
    with main.SessionLocal() as session:
        assert repository.add_list_rule(session, kind, "Straße")
    data = _test_form(client, **{kind: "Straße\nSTRASSE"})
    before = _database_state()
    assert _preview(client, data).status_code == 200
    assert _database_state() == before
    assert _apply(client, data).status_code == 303
    assert [(r["value"], r["enabled"]) for r in _rules()] == [("Straße", True), ("STRASSE", True)]


@pytest.mark.parametrize("invalid", ["duplicate", "date"])
def test_rule_test_validation_error_preserves_input_and_fingerprint_for_retry(client, invalid):
    data = _test_form(client, keyword="SW\nsw" if invalid == "duplicate" else "새 플랫폼",
                      dt_from="bad" if invalid == "date" else "2026-10-01",
                      dt_to="2026-10-07", search='<검색&"값>')
    before = _database_state()
    for response in (_preview(client, data), _apply(client, data)):
        assert response.status_code == 400
        assert f'name="fingerprint" value="{data["fingerprint"]}"' in response.text
        assert f'<textarea name="keyword" rows="10">{data["keyword"]}</textarea>' in response.text
        assert f'name="dt_from" value="{data["dt_from"]}"' in response.text
        assert '&lt;검색&amp;&quot;값&gt;' in response.text
        _assert_rule_test_error_at_top(response.text)
        assert _database_state() == before
    data.update(keyword="새 플랫폼", dt_from="2026-10-01")
    assert _apply(client, data).status_code == 303


def test_rule_test_result_focus_date_format_and_apply_confirmation(client):
    with main.SessionLocal() as session:
        session.get(BidNotice, "K").bid_ntce_dt = datetime(2026, 10, 7, 12, 34, 56)
        session.get(BidNotice, "C").bid_ntce_dt = None
        session.commit()
    data = _test_form(client, keyword="새 플랫폼", search="일반")
    page = _preview(client, data).text
    assert "미리보기 결과 (대상 기간 기준)" in page
    assert '<div id="preview">' in page
    assert 'window.location.hash = "preview"' in page
    assert 'action="/config/rules/test#preview"' in page
    assert 'formaction="/config/rules/test/apply#preview"' in page
    assert "onclick=\"return confirm('규칙을 저장하고 저장된 전체 공고를 다시 분류합니다. 초안에 없는 켜진 규칙은 꺼집니다.');\"" in page
    assert '<td>2026-10-07 12:34</td>' in page
    assert "12:34:56" not in page
    assert '<td>C</td><td>일반 용역</td><td></td>' in page


def test_rule_test_fingerprint_ignores_match_code_order_and_duplicates(client):
    data = _test_form(client)
    with main.SessionLocal() as session:
        repository.update_config(session, indstryty_cds=" 1468,1426,1468 ")
    assert _test_form(client)["fingerprint"] == data["fingerprint"]
    assert _apply(client, data).status_code == 303


def test_rule_test_stale_invalid_preview_warns_without_claiming_a_result(client, monkeypatch):
    data = _test_form(client, keyword="SW\nsw")
    with main.SessionLocal() as session:
        repository.add_list_rule(session, "negative", "새 규칙")
    before = _database_state()
    monkeypatch.setattr(classifier, "iter_classifications",
                        lambda *args, **kwargs: pytest.fail("검증 오류 시 분류 금지"))
    response = _preview(client, data)
    assert response.status_code == 400
    assert "중복된 규칙" in response.text
    assert "다른 곳에서 규칙(또는 매칭 업종코드)이 바뀌었습니다." in response.text
    assert "현재 규칙 다시 불러오기(초안 버림)" in response.text
    assert "현재 상태로 계산한 미리보기" not in response.text
    assert "미리보기 결과" not in response.text
    _assert_rule_test_error_at_top(response.text)
    assert f'name="fingerprint" value="{data["fingerprint"]}"' in response.text
    assert _database_state() == before
