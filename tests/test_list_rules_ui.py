"""Phase 13.4a — 목록 표시 근거·규칙 편집과 전체 재분류를 임시 DB로 검증."""

import html
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import classifier, main, repository
from app.db import Base
from app.models import AppConfig, BidNotice


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rules.db'}", connect_args={"check_same_thread": False},
    )
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
    assert ".indcode[data-tip]" in main._LIST_MATCHIND_TIP_SCRIPT


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
        repository.set_list_rules_enabled(session, [rows[1]["id"]])
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
        repository.set_list_rules_enabled(session, [rules[0]["id"], rules[2]["id"]])
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
    assert error in parse_qs(urlsplit(location).query)["err"][0]
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

    def reclassify(session):
        calls.append(session)
        return original(session)

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
    assert "누락 보정 조회 페이지 크기: 1~999" in response.text
    assert 'id="rules"' in response.text
    with main.SessionLocal() as session:
        assert repository.get_config(session).num_of_rows == 20
    response = client.post("/config", data=_config_data(num_of_rows="999"))
    assert response.status_code == 200
    with main.SessionLocal() as session:
        assert repository.get_config(session).num_of_rows == 999
    assert "매칭 업종코드 CSV" in response.text
    assert "목록 표시 판정 기준(면허제한 코드와 비교) — 수집 조건이 아닙니다." in response.text
    assert "공고·면허 일괄 조회는 999행 고정, 이 값은 면허 누락 보정 조회에만 쓰입니다." in response.text
    assert 'name="num_of_rows" value="999" min="1" max="999"' in response.text
    assert "최대 999 — 1000 이상이면 API 가 기본 10행만 돌려줍니다" in client.get("/api-test").text
