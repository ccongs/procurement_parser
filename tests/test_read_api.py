"""읽기 전용 조회 API의 접근 제어·조회·직렬화 계약 시험."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, read_api, repository
from app.db import Base
from app.models import BidLicenseLimit, BidNotice, PreSpec


NOW = datetime(2026, 11, 18, 10, 0)
KINDS = ("bids", "pre-specs")
PATHS = ("/api/v1/bids", "/api/v1/bids/TEST",
         "/api/v1/pre-specs", "/api/v1/pre-specs/TEST")

# 기대 키는 구현 상수와 독립적으로 고정한다.
BID_FIELDS = set("""
bid_ntce_no bid_ntce_ord unty_ntce_no bid_ntce_nm ntce_kind_nm re_ntce_yn
chg_ntce_rsn srvce_div_nm info_biz_yn ntce_instt_cd ntce_instt_nm dminstt_cd
dminstt_nm ntce_instt_ofcl_nm ntce_instt_ofcl_tel_no ntce_instt_ofcl_email
bid_ntce_dt rgst_dt bid_qlfct_rgst_dt bid_begin_dt bid_clse_dt openg_dt chg_dt
presmpt_prce asign_bdgt_amt vat bid_methd_nm cntrct_cncls_mthd_nm sucsfbid_mthd_nm
indstryty_lmt_yn matched_indstryty_cds list_visible match_basis match_detail
pub_prcrmnt_lrgclsfc_nm pub_prcrmnt_midclsfc_nm pub_prcrmnt_clsfc_no
pub_prcrmnt_clsfc_nm bef_bid_bbanc_no bid_ntce_url bid_ntce_dtl_url collected_at updated_at
""".split())
BID_EXTRA = set("""
std_ntce_doc_url purchs_obj_prdct_list sucsfbid_lwlt_rate tech_ablt_evl_rt bid_prce_evl_rt
""".split())
PRE_FIELDS = set("""
bf_spec_rgst_no ref_no bsns_div_nm prdct_clsfc_no_nm order_instt_nm rl_dminstt_nm
ofcl_nm ofcl_tel_no asign_bdgt_amt rcpt_dt opnin_rgst_clse_dt dlvr_tmlmt_dt
dlvr_daynum sw_biz_obj_yn bid_ntce_no_list rgst_dt chg_dt collected_at updated_at
""".split())
PRE_EXTRA = {"prdct_dtl_list"}


def _row(kind, number="TEST", **values):
    common = {"collected_at": NOW, "updated_at": NOW}
    if kind == "bids":
        common.update(bid_ntce_no=number, bid_ntce_dt=NOW,
                      bid_clse_dt=NOW + timedelta(days=1), list_visible=True)
        model = BidNotice
    else:
        common.update(bf_spec_rgst_no=number, rcpt_dt=NOW,
                      opnin_rgst_clse_dt=NOW + timedelta(days=1), sw_biz_obj_yn="Y")
        model = PreSpec
    common.update(values)
    return model(**common)


def _add(db, *rows):
    with db() as session:
        session.add_all(rows)
        session.commit()


def _number(kind):
    return "bid_ntce_no" if kind == "bids" else "bf_spec_rgst_no"


@pytest.fixture
def token():
    return "".join(chr(97 + i % 26) for i in range(32))


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def db(monkeypatch, token):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    try:
        Base.metadata.create_all(engine)
        local = sessionmaker(bind=engine, autoflush=False)
        monkeypatch.setattr(read_api, "SessionLocal", local)
        monkeypatch.setattr(read_api, "_now", lambda: NOW)
        monkeypatch.setenv("NOTICE_API_TOKEN", token)
        yield local
    finally:
        engine.dispose()


@pytest.fixture
def client(db):
    client = TestClient(main.app)
    try:
        yield client
    finally:
        client.close()


def _check(response, status=200):
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def test_route_contract():
    routes = {route.path: route.methods for route in main.app.routes
              if route.path.startswith("/api/v1/")}
    assert routes == {
        "/api/v1/bids": {"GET"}, "/api/v1/bids/{bid_ntce_no}": {"GET"},
        "/api/v1/pre-specs": {"GET"}, "/api/v1/pre-specs/{bf_spec_rgst_no}": {"GET"},
    }


@pytest.mark.parametrize("value,warning", [
    (None, None), ("", None), ("   ", None),
    ("x" * 15, "NOTICE_API_TOKEN이 16자 미만이라 조회 API를 끕니다"),
    ("가" * 16, "NOTICE_API_TOKEN에 ASCII 보이는 글자가 아닌 글자가 있어 조회 API를 끕니다"),
    ("x" * 9 + " " + "x" * 10, "NOTICE_API_TOKEN에 ASCII 보이는 글자가 아닌 글자가 있어 조회 API를 끕니다"),
    ("x" * 9 + "\t" + "x" * 10, "NOTICE_API_TOKEN에 ASCII 보이는 글자가 아닌 글자가 있어 조회 API를 끕니다"),
])
def test_disabled_all_routes(client, monkeypatch, value, warning, caplog):
    caplog.set_level(logging.DEBUG)
    if value is None:
        monkeypatch.delenv("NOTICE_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("NOTICE_API_TOKEN", value)
    monkeypatch.setattr(read_api, "SessionLocal", lambda: pytest.fail("꺼진 API의 DB 접근"))
    missing = client.get("/api/v1/없음").json()
    assert missing == {"detail": "Not Found"}
    for path in PATHS:
        assert _check(client.get(path, params={"limit": "0"}), 404) == missing
    records = [record for record in caplog.records if record.name == read_api.__name__]
    assert [(record.levelno, record.getMessage()) for record in records] == (
        [(logging.WARNING, warning)] * len(PATHS) if warning else []
    )
    if value and value.strip():
        assert value not in caplog.text


@pytest.mark.parametrize("mode", ["missing", "wrong", "basic", "scheme_only", "two_spaces", "tab"])
def test_unauthorized_all_routes(client, token, mode, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    headers = {
        "missing": {}, "wrong": {"Authorization": "Bearer " + token[::-1]},
        "basic": {"Authorization": "Basic " + token},
        "scheme_only": {"Authorization": "Bearer"},
        "two_spaces": {"Authorization": "Bearer  " + token},
        "tab": {"Authorization": "Bearer\t" + token},
    }[mode]
    monkeypatch.setattr(read_api, "SessionLocal", lambda: pytest.fail("인증 전 DB 접근"))
    monkeypatch.setattr(read_api, "_parse_params", lambda *a, **k: pytest.fail("인증 전 검증"))
    for path in PATHS:
        response = client.get(path, headers=headers, params={"limit": "0"})
        assert _check(response, 401) == {"error": "unauthorized"}
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert token not in response.text
    assert token not in caplog.text


def test_token_is_read_each_request_and_trimmed(client, db, token, monkeypatch):
    _add(db, *[_row(kind) for kind in KINDS])
    monkeypatch.setenv("NOTICE_API_TOKEN", " \t" + token + " \n")
    for path in PATHS:
        _check(client.get(path, headers={"Authorization": "bEaReR " + token}))
    changed = token[::-1]
    monkeypatch.setenv("NOTICE_API_TOKEN", changed)
    for path in PATHS:
        _check(client.get(path, headers={"Authorization": "Bearer " + token}), 401)
        _check(client.get(path, headers={"Authorization": "bearer " + changed}))
    monkeypatch.setenv("NOTICE_API_TOKEN", "")
    for path in PATHS:
        _check(client.get(path), 404)


def test_exact_minimum_token_length(client, monkeypatch):
    token = "".join(chr(97 + i) for i in range(16))
    monkeypatch.setenv("NOTICE_API_TOKEN", token)
    _check(client.get("/api/v1/bids", headers={"Authorization": "bearer " + token}))


def test_latin1_non_ascii_authorization(client, db, token):
    _add(db, *[_row(kind) for kind in KINDS])
    for path in PATHS:
        wrong = b"Bearer " + ("é" + token[1:]).encode("latin-1")
        response = client.get(path, headers={b"Authorization": wrong})
        assert _check(response, 401) == {"error": "unauthorized"}
        assert response.headers["WWW-Authenticate"] == "Bearer"
        _check(client.get(path, headers={b"Authorization": b"Bearer " + token.encode("ascii")}))


@pytest.mark.parametrize("kind", KINDS)
def test_open_and_candidate_filters(client, db, auth, kind):
    date = "bid_ntce_dt" if kind == "bids" else "rcpt_dt"
    close = "bid_clse_dt" if kind == "bids" else "opnin_rgst_clse_dt"
    candidate = "list_visible" if kind == "bids" else "sw_biz_obj_yn"
    false = False if kind == "bids" else "N"
    rows = [
        _row(kind, "future"), _row(kind, "equal", **{close: NOW}),
        _row(kind, "past", **{close: NOW - timedelta(seconds=1)}),
        _row(kind, "fallback_equal", **{close: None, date: NOW - timedelta(days=30)}),
        _row(kind, "fallback_old", **{close: None, date: NOW - timedelta(days=30, minutes=1)}),
        _row(kind, "missing_dates", **{close: None, date: None}),
        _row(kind, "excluded", **{candidate: false}),
    ]
    if kind == "pre-specs":
        rows.append(_row(kind, "empty_sw", sw_biz_obj_yn=None))
    _add(db, *rows)
    url = f"/api/v1/{kind}"

    def numbers(params=None):
        return {item[_number(kind)] for item in _check(client.get(url, headers=auth, params=params))["items"]}

    expected = {"future", "equal", "fallback_equal"}
    assert numbers() == expected
    special = "visible" if kind == "bids" else "sw"
    assert numbers({special: "all"}) == expected | {"excluded"} | ({"empty_sw"} if kind == "pre-specs" else set())
    assert numbers({"open": "false"}) == expected | {"past", "fallback_old", "missing_dates"}
    assert numbers({special: "all", "open": "false"}) == (
        expected | {"past", "fallback_old", "missing_dates", "excluded"}
        | ({"empty_sw"} if kind == "pre-specs" else set())
    )
    # 목록 조건과 관계없이 저장된 행은 한 건 경로에서 조회할 수 있다.
    for number in ("past", "excluded", "missing_dates"):
        _check(client.get(f"{url}/{number}", headers=auth))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("params", [
    {"limit": "0"}, {"limit": "501"}, {"limit": "abc"}, {"limit": "1.0"},
    {"limit": " 1"}, {"limit": "9" * 5000},
    {"ntce_from": "20261118100"}, {"ntce_to": "202613011200"},
    {"updated_since": "202602301200"}, {"ntce_from": "２０２６１１１８１０００"},
    {"ntce_from": "202611181001", "ntce_to": "202611181000"},
    {"open": "x"}, {"open": "True"},
])
def test_invalid_params(client, auth, token, caplog, kind, params):
    caplog.set_level(logging.DEBUG)
    body = _check(client.get(f"/api/v1/{kind}", params=params, headers=auth), 422)
    assert set(body) == {"error"}
    assert isinstance(body["error"], str) and "\n" not in body["error"]
    assert any("가" <= ch <= "힣" for ch in body["error"])
    assert token not in body["error"]
    assert token not in caplog.text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", ["limit", "cursor", "ntce_from", "ntce_to", "updated_since", "open", "special"])
def test_empty_and_duplicate_params(client, auth, kind, name):
    if name == "special":
        name = "visible" if kind == "bids" else "sw"
    url = f"/api/v1/{kind}"
    body = _check(client.get(url, params={name: ""}, headers=auth), 422)
    assert set(body) == {"error"} and isinstance(body["error"], str)
    body = _check(client.get(url, params=[(name, "1"), (name, "2")], headers=auth), 422)
    assert set(body) == {"error"} and isinstance(body["error"], str)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", ["x", "ALL", "true"])
def test_invalid_candidate_param(client, auth, kind, value):
    name = "visible" if kind == "bids" else "sw"
    _check(client.get(f"/api/v1/{kind}", params={name: value}, headers=auth), 422)


@pytest.mark.parametrize("kind,max_length", [("bids", 40), ("pre-specs", 30)])
def test_cursor_length_and_unknown_params(client, auth, kind, max_length):
    url = f"/api/v1/{kind}"
    _check(client.get(url, params={"cursor": "z" * max_length}, headers=auth))
    _check(client.get(url, params={"cursor": "z" * (max_length + 1)}, headers=auth), 422)
    # 다른 목록의 전용 매개변수도 모르는 이름으로 취급한다.
    unknown = "sw" if kind == "bids" else "visible"
    _check(client.get(url, params=[("unknown", ""), ("unknown", "x"), (unknown, "x")], headers=auth))


@pytest.mark.parametrize("kind", KINDS)
def test_date_and_updated_boundaries(client, db, auth, kind):
    date = "bid_ntce_dt" if kind == "bids" else "rcpt_dt"
    _add(db, *[_row(kind, name, **{date: value}) for name, value in (
        ("before", NOW - timedelta(microseconds=1)), ("from", NOW),
        ("to", NOW + timedelta(minutes=1, seconds=59, microseconds=999999)),
        ("after", NOW + timedelta(minutes=2)),
    )])
    url = f"/api/v1/{kind}"
    params = {"ntce_from": "202611181000", "ntce_to": "202611181001"}
    items = _check(client.get(url, params=params, headers=auth))["items"]
    assert {item[_number(kind)] for item in items} == {"from", "to"}
    for name, delta in (("updated_before", -1), ("updated_equal", 0), ("updated_after", 1)):
        _add(db, _row(kind, name, updated_at=NOW + timedelta(seconds=delta)))
    items = _check(client.get(url, params={"updated_since": "202611181000"}, headers=auth))["items"]
    numbers = {item[_number(kind)] for item in items}
    assert "updated_before" not in numbers
    assert {"updated_equal", "updated_after"} <= numbers
    _check(client.get(url, params={"ntce_to": "999912312359"}, headers=auth))
    _check(client.get(url, params={"ntce_from": "000101010000"}, headers=auth))


@pytest.mark.parametrize("kind", KINDS)
def test_limit_default_and_maximum(client, db, auth, kind):
    _add(db, *[_row(kind, f"N{i:04}") for i in range(501)])
    url = f"/api/v1/{kind}"
    assert _check(client.get(url, headers=auth))["count"] == 100
    page = _check(client.get(url, params={"limit": "500"}, headers=auth))
    assert page["count"] == 500 and page["next_cursor"] == "N0001"
    assert _check(client.get(url, params={"limit": "1"}, headers=auth))["count"] == 1


@pytest.mark.parametrize("kind", KINDS)
def test_cursor_pagination(client, db, auth, kind):
    _add(db, *[_row(kind, f"N{i}") for i in (2, 5, 1, 6, 3, 0, 4)])
    params = {"limit": "3"}
    pages, numbers = [], []
    for _ in range(3):
        page = _check(client.get(f"/api/v1/{kind}", params=params, headers=auth))
        pages.append(page)
        numbers.extend(item[_number(kind)] for item in page["items"])
        params["cursor"] = page["next_cursor"]
    assert [page["count"] for page in pages] == [3, 3, 1]
    assert [page["next_cursor"] for page in pages] == ["N4", "N1", None]
    assert numbers == [f"N{i}" for i in range(6, -1, -1)]
    assert len(set(numbers)) == 7
    empty = _check(client.get(f"/api/v1/{kind}", params={"cursor": "N0"}, headers=auth))
    assert empty["items"] == [] and empty["count"] == 0 and empty["next_cursor"] is None


@pytest.mark.parametrize("kind", KINDS)
def test_field_and_envelope_contract(client, db, auth, kind):
    _add(db, _row(kind, raw_json="원문은 내보내지 않음"))
    url = f"/api/v1/{kind}"
    page = _check(client.get(url, headers=auth))
    detail = _check(client.get(url + "/TEST", headers=auth))
    assert set(page) == {"api_version", "generated_at", "count", "next_cursor", "items"}
    assert set(detail) == {"api_version", "generated_at", "item"}
    for envelope in (page, detail):
        assert envelope["api_version"] == "1"
        assert envelope["generated_at"] == NOW.astimezone().isoformat(timespec="seconds")
    assert page["count"] == 1 and page["next_cursor"] is None
    if kind == "bids":
        fields, extra = BID_FIELDS, BID_EXTRA
        assert isinstance(read_api.BID_LIST_FIELDS, tuple)
        assert set(read_api.BID_LIST_FIELDS) == fields
        assert set(read_api.BID_DETAIL_EXTRA_FIELDS) == extra
        assert len(read_api.BID_LIST_FIELDS) == len(fields)
        assert len(read_api.BID_DETAIL_EXTRA_FIELDS) == len(extra)
        detail_extra = {"license_limits"}
        assert detail["item"]["license_limits"] == []
    else:
        fields, extra = PRE_FIELDS, PRE_EXTRA
        assert isinstance(read_api.PRE_SPEC_LIST_FIELDS, tuple)
        assert set(read_api.PRE_SPEC_LIST_FIELDS) == fields
        assert set(read_api.PRE_SPEC_DETAIL_EXTRA_FIELDS) == extra
        assert len(read_api.PRE_SPEC_LIST_FIELDS) == len(fields)
        assert len(read_api.PRE_SPEC_DETAIL_EXTRA_FIELDS) == len(extra)
        detail_extra = set()
    assert set(page["items"][0]) == fields | {"files"}
    assert set(detail["item"]) == fields | extra | {"files"} | detail_extra
    for item in (page["items"][0], detail["item"]):
        assert not {"raw_json", "ntce_spec_doc_url1", "spec_doc_file_url1", "intrbid_yn"} & item.keys()
        assert item["files"] == []
    assert _check(client.get(url + "/MISSING", headers=auth), 404) == {"error": "not found"}


def test_bid_value_types_files_and_license_limits(client, db, auth):
    date_fields = ("bid_ntce_dt", "rgst_dt", "bid_qlfct_rgst_dt", "bid_begin_dt", "bid_clse_dt",
                   "openg_dt", "chg_dt", "collected_at", "updated_at")
    _add(db, _row("bids", **{name: NOW for name in date_fields},
                 presmpt_prce=Decimal("12345678901234567890"), asign_bdgt_amt=123, vat=0,
                 sucsfbid_lwlt_rate=Decimal("87.995"), tech_ablt_evl_rt=80, bid_prce_evl_rt=20,
                 bid_ntce_nm="한글 공고", bid_ntce_ord="", unty_ntce_no="   ",
                 ntce_spec_doc_url1="https://example.test/one", ntce_spec_file_nm1="제안요청서",
                 ntce_spec_doc_url3="https://example.test/three",
                 ntce_spec_doc_url10="https://example.test/ten"))
    for group, serial, superseded, codes in (
        ("2", "1", None, ""), ("1", "2", None, " 1426, ,1468, "),
        ("1", "1", None, None), ("0", "1", NOW, "old"),
    ):
        _add(db, BidLicenseLimit(bid_ntce_no="TEST", bid_ntce_ord="000", lmt_grp_no=group,
                                 lmt_sno=serial, lcns_cd="코드", lcns_nm="면허", permsn_cds=codes,
                                 superseded_at=superseded, collected_at=NOW))
    detail = _check(client.get("/api/v1/bids/TEST", headers=auth))["item"]
    for name in date_fields:
        assert detail[name] == NOW.astimezone().isoformat(timespec="seconds")
    for name in ("presmpt_prce", "asign_bdgt_amt", "vat"):
        assert type(detail[name]) is int
    assert detail["asign_bdgt_amt"] == 123 and detail["vat"] == 0
    for name, expected in (("sucsfbid_lwlt_rate", 87.995), ("tech_ablt_evl_rt", 80.0), ("bid_prce_evl_rt", 20.0)):
        assert type(detail[name]) is float and detail[name] == expected
    assert type(detail["list_visible"]) is bool and detail["list_visible"] is True
    assert detail["bid_ntce_nm"] == "한글 공고"
    assert detail["bid_ntce_ord"] is None and detail["unty_ntce_no"] is None
    assert detail["std_ntce_doc_url"] is None
    files = [{"name": "제안요청서", "url": "https://example.test/one"},
             {"name": "첨부3", "url": "https://example.test/three"},
             {"name": "첨부10", "url": "https://example.test/ten"}]
    assert detail["files"] == files
    assert _check(client.get("/api/v1/bids", headers=auth))["items"][0]["files"] == files
    assert detail["license_limits"] == [
        {"lmt_grp_no": "1", "lmt_sno": "1", "lcns_cd": "코드", "lcns_nm": "면허", "permsn_cds": []},
        {"lmt_grp_no": "1", "lmt_sno": "2", "lcns_cd": "코드", "lcns_nm": "면허", "permsn_cds": ["1426", "1468"]},
        {"lmt_grp_no": "2", "lmt_sno": "1", "lcns_cd": "코드", "lcns_nm": "면허", "permsn_cds": []},
    ]


@pytest.mark.parametrize("pairs,expected", [
    ([("10", "1"), ("2", "A"), ("1", "10"), ("2", "1"), ("1", "2")],
     [("1", "2"), ("1", "10"), ("2", "1"), ("2", "A"), ("10", "1")]),
    ([(" B ", "2"), ("A", "10"), (" 2 ", " 10 "), ("2", "1"), (" A ", "2")],
     [("2", "1"), (" 2 ", " 10 "), (" A ", "2"), ("A", "10"), (" B ", "2")]),
    ([("1", "1"), ("1", "²"), ("1", "１２"), ("1", "10"), ("1", "①")],
     [("1", "1"), ("1", "10"), ("1", "²"), ("1", "①"), ("1", "１２")]),
    ([("1", "1"), ("²", "1"), ("１２", "1"), ("10", "1"), ("①", "1")],
     [("1", "1"), ("10", "1"), ("²", "1"), ("①", "1"), ("１２", "1")]),
])
def test_license_limits_numeric_order(client, db, auth, pairs, expected):
    _add(db, _row("bids"))
    _add(db, *[BidLicenseLimit(
        bid_ntce_no="TEST", bid_ntce_ord="000", lmt_grp_no=group, lmt_sno=serial,
        collected_at=NOW,
    ) for group, serial in pairs])
    item = _check(client.get("/api/v1/bids/TEST", headers=auth))["item"]
    assert [(limit["lmt_grp_no"], limit["lmt_sno"]) for limit in item["license_limits"]] == expected


def test_pre_spec_value_types_and_files(client, db, auth):
    dates = ("rcpt_dt", "opnin_rgst_clse_dt", "dlvr_tmlmt_dt", "rgst_dt", "chg_dt", "collected_at", "updated_at")
    _add(db, _row("pre-specs", **{name: NOW for name in dates}, asign_bdgt_amt=Decimal("321"),
                 dlvr_daynum=0, ref_no="", prdct_dtl_list="물품상세 원문",
                 spec_doc_file_url1="https://example.test/one", spec_doc_file_url5="https://example.test/five"))
    item = _check(client.get("/api/v1/pre-specs/TEST", headers=auth))["item"]
    for name in dates:
        assert item[name] == NOW.astimezone().isoformat(timespec="seconds")
    assert type(item["asign_bdgt_amt"]) is int and item["asign_bdgt_amt"] == 321
    assert type(item["dlvr_daynum"]) is int and item["dlvr_daynum"] == 0
    assert item["ref_no"] is None and item["ofcl_nm"] is None
    assert item["prdct_dtl_list"] == "물품상세 원문"
    files = [{"name": "첨부1", "url": "https://example.test/one"},
             {"name": "첨부5", "url": "https://example.test/five"}]
    assert item["files"] == files
    assert _check(client.get("/api/v1/pre-specs", headers=auth))["items"][0]["files"] == files


@pytest.mark.parametrize("raw,expected", [
    (" A, B, ,C, ", ["A", "B", "C"]), (' [" A ", "", 12, " B "] ', ["A", "12", "B"]),
    (None, []), ("", []), ("  ", []), ("[]", []),
    (' ["broken" ', [' ["broken" ']), ("[1] trailing", ["[1] trailing"]),
])
def test_bid_number_lists(client, db, auth, raw, expected):
    _add(db, _row("pre-specs", bid_ntce_no_list=raw))
    for url in ("/api/v1/pre-specs", "/api/v1/pre-specs/TEST"):
        body = _check(client.get(url, headers=auth))
        item = body["item"] if "item" in body else body["items"][0]
        assert item["bid_ntce_no_list"] == expected


@pytest.mark.parametrize("kind", KINDS)
def test_now_once_and_repeatable_read(client, db, auth, kind, monkeypatch):
    _add(db, _row(kind))
    calls = []

    def now():
        calls.append(True)
        return NOW

    monkeypatch.setattr(read_api, "_now", now)
    for suffix in ("", "/TEST"):
        url = f"/api/v1/{kind}{suffix}"
        first = _check(client.get(url, headers=auth))
        assert len(calls) == 1
        calls.clear()
        assert _check(client.get(url, headers=auth)) == first
        assert len(calls) == 1
        calls.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_list_query_count_and_same_session(client, db, auth, kind, monkeypatch):
    file_column = "ntce_spec_doc_url1" if kind == "bids" else "spec_doc_file_url1"
    _add(db, *[_row(kind, f"N{i:02}", raw_json="원문", **{file_column: "https://example.test/file"})
               for i in range(20)])
    statements = []
    sessions = []
    engine = db.kw["bind"]

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    name = "get_notice_files" if kind == "bids" else "get_pre_spec_files"
    original = getattr(repository, name)

    def files(session, number):
        sessions.append(session)
        # 같은 세션에 이미 읽은 행이 유지되는지도 확인한다.
        assert any(getattr(row, _number(kind), None) == number for row in session.identity_map.values())
        return original(session, number)

    monkeypatch.setattr(repository, name, files)
    event.listen(engine, "before_cursor_execute", record)
    try:
        counts = []
        for limit in (1, 20):
            statements.clear()
            sessions.clear()
            body = _check(client.get(f"/api/v1/{kind}", params={"limit": str(limit)}, headers=auth))
            assert body["count"] == limit
            counts.append(len(statements))
            assert len(sessions) == limit and all(s is sessions[0] for s in sessions)
            assert all("bid_notice_raw" not in sql.lower() and "bid_license_limit" not in sql.lower()
                       for sql in statements)
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert all("raw_json" not in sql.lower() for sql in statements)
        assert counts == [1, 1]
    finally:
        event.remove(engine, "before_cursor_execute", record)
