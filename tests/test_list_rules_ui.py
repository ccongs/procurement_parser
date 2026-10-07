"""Phase 13.4a — 목록 표시 근거·규칙 편집과 전체 재분류를 임시 DB로 검증."""

import html
import re
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import api_client, classifier, collector, main, repository
from app.db import Base, _register_sqlite_pragmas
from app.models import AppConfig, BidNotice, CollectionRun


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
    monkeypatch.setattr(main.scheduler, "is_running", lambda: False)
    monkeypatch.setattr(main.scheduler, "get_next_run_time", lambda *args: None)
    with TestClient(main.app) as test_client:
        yield test_client
    engine.dispose()


def _state(no):
    with main.SessionLocal() as session:
        notice = session.get(BidNotice, no)
        return notice.list_visible, notice.match_basis, notice.match_detail


def _post(client, data):
    data = {"rule_ids": [r["id"] for r in _rules()] or "", **data}
    return client.post("/config/rules", data=data, follow_redirects=False)


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
    page = client.get("/config").text
    assert page.index('action="/config"') < page.index('id="rules"') < page.index('id="keywords"')
    card = page.split('id="rules"', 1)[1].split('id="keywords"', 1)[0]
    assert card.count('<thead><tr><th>사용</th><th>값</th><th>삭제</th></tr>') == 3
    for title in ["IT 공공조달분류", "SW 키워드", "판별 제외어"]:
        assert title in card
    for rule in rules:
        assert f'<input type="hidden" name="rule_ids" value="{rule["id"]}">' in card
        field = f'name="enabled_ids" value="{rule["id"]}"'
        assert field + (" checked>" if rule["rule_type"] != "keyword" else ">") in card
    assert 'name="new_clsfc" maxlength="100" list="clsfc-names"' in card
    assert card.index('<option value="빈도 높은 분류">') < card.index('<option value="&lt;분류&amp;&quot;명&gt;">')
    assert "저장된 전체 공고를 다시 분류" in card
    assert "정식 분류명과 정확히 같을 때" in card
    assert "앞뒤가 영문·숫자가 아닐 때" in card
    assert "공백은 있어도 없어도 일치" in card


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
    for existing, duplicate in (("SW", "sw"), ("SW", "S W"), ("시스템 구축", "시스템구축"))
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
    {"delete_id": "999999"}, {"enabled_ids": ["999999"]},
    {"rule_ids": ["999999"]}, {"rule_ids": ["²"]}, {"rule_ids": ["1", "x"]},
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
    rendered = re.findall(r'<input type="hidden" name="rule_ids" value="(\d+)">', page_a)
    _post(client, {"action": "add_negative", "new_negative": "전산화단층",
                   "enabled_ids": rendered})
    negative_id = next(r["id"] for r in _rules() if r["rule_type"] == "negative")
    if not new_enabled:
        _post(client, {"enabled_ids": rendered})
    response = _post(client, {"action": "save", "rule_ids": rendered})
    assert "rules=1" in response.headers["location"]
    rules = {r["id"]: r for r in _rules()}
    assert not rules[int(rendered[0])]["enabled"]
    assert rules[negative_id]["enabled"] is new_enabled
    with main.SessionLocal() as session:
        assert classifier.reclassify_notices(session)["changed"] == 0


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
