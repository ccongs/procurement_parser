"""Phase 12.2 — /list 전용 검색 필터·날짜 기간·장바구니 검색 회귀 테스트."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import AppConfig, BidNotice


_TODAY = date(2026, 9, 23)
_META = datetime(2026, 9, 10, 9)
_WIDE = {"dt_from": "2000-01-01", "dt_to": "2099-12-31"}
_KINDS = {"reg": "등록공고", "re": "재공고", "chg": "변경공고", "cncl": "취소공고"}


class _Tags(HTMLParser):
    """속성 순서·공백과 무관하게 폼 상태와 링크를 검사한다."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _attrs(html, tag, **attrs):
    return [
        values for name, values in _Tags(html).tags
        if name == tag and all(values.get(key) == value for key, value in attrs.items())
    ]


def _form(html, action="/list"):
    match = re.search(rf'<form\b[^>]*action="{action}"[^>]*>(.*?)</form>', html, re.S)
    assert match is not None
    return match.group(1)


def _rows(html):
    return set(re.findall(r'<tr data-row-id="([^"]+)">', html))


def _checked_kinds(html):
    return {
        attrs["value"] for attrs in _attrs(_form(html), "input", name="kind")
        if "checked" in attrs
    }


def _dates(html):
    return tuple(_attrs(_form(html), "input", name=name)[0]["value"] for name in ("dt_from", "dt_to"))


def _range_state(html):
    form = _form(html)
    select = re.search(r'<select\b[^>]*name="dt_range"[^>]*>(.*?)</select>', form, re.S)
    assert select is not None
    selected = [attrs["value"] for attrs in _attrs(select.group(1), "option") if "selected" in attrs]
    assert len(selected) == 1
    custom = next(attrs for _tag, attrs in _Tags(form).tags if attrs.get("id") == "list-custom-dates")
    return selected[0], "hidden" in custom


def _notice(no, kind, when=_META):
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=f"필터 검증 {no}",
        ntce_kind_nm=kind,
        bid_ntce_dt=when,
        collected_at=_META,
        updated_at=_META,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """실 DB·서버 기동 없이 임시 SQLite와 고정 날짜로 라우트를 검증한다."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'list_filter.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False)
    with local() as session:
        session.add(AppConfig(id=1, enabled=False, pre_spec_enabled=False, updated_at=_META))
        session.add_all([_notice(f"K-{code}", label) for code, label in _KINDS.items()])
        session.add(_notice("K-null", None))
        session.commit()

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(_TODAY.year, _TODAY.month, _TODAY.day)

    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(main, "date", FixedDate)
    client = TestClient(main.app)
    try:
        yield client
    finally:
        client.close()
        engine.dispose()


@pytest.mark.parametrize("kind_params", [[], [("kind", "")], [("kind", "bogus")], [("kind", ",invalid,")]])
def test_list_kind_default_and_empty_invalid_fallback(client, kind_params):
    response = client.get("/list", params=[*_WIDE.items(), *kind_params])
    assert response.status_code == 200
    assert _rows(response.text) == {"K-reg", "K-re", "K-chg", "K-null"}
    assert _checked_kinds(response.text) == {"reg", "re", "chg"}
    checkboxes = _attrs(_form(response.text), "input", name="kind")
    assert [attrs["value"] for attrs in checkboxes] == ["reg", "re", "chg", "cncl"]
    assert all(attrs["type"] == "checkbox" for attrs in checkboxes)


def test_list_kind_cancel_only_keeps_unknown_kind(client):
    response = client.get("/list", params={**_WIDE, "kind": "cncl"})
    assert response.status_code == 200
    assert _rows(response.text) == {"K-cncl", "K-null"}
    assert _checked_kinds(response.text) == {"cncl"}


@pytest.mark.parametrize(
    "kind_params",
    [
        [("kind", "reg"), ("kind", "cncl")],
        [("kind", "reg,cncl")],
        [("kind", "reg,bogus"), ("kind", "cncl,reg")],
    ],
)
def test_list_kind_repeated_and_csv_values_are_merged(client, kind_params):
    response = client.get("/list", params=[*_WIDE.items(), *kind_params])
    assert response.status_code == 200
    assert _rows(response.text) == {"K-reg", "K-cncl", "K-null"}
    assert _checked_kinds(response.text) == {"reg", "cncl"}


def test_list_kind_and_date_range_preserved_in_sort_and_pager_links(client):
    with main.SessionLocal() as session:
        session.add_all(_notice(f"PAGE-{i:02d}", "등록공고") for i in range(12))
        session.commit()
    response = client.get(
        "/list", params={**_WIDE, "q": "PAGE-", "kind": "reg,cncl", "page_size": 10}
    )
    assert response.status_code == 200
    links = [
        parse_qs(urlparse(attrs["href"]).query)
        for attrs in _attrs(response.text, "a") if attrs.get("href", "").startswith("/list?")
    ]
    assert any(query.get("page") == ["2"] for query in links)
    assert any(query.get("sort") == ["asign_bdgt_amt_desc"] for query in links)
    for query in links:
        assert query["kind"] == ["reg,cncl"]
        assert query["dt_range"] == ["custom"]
        assert query["dt_from"] == [_WIDE["dt_from"]]
        assert query["dt_to"] == [_WIDE["dt_to"]]


def test_cart_search_url_includes_cancelled_notices(client):
    title = "필터 검증 K-cncl"
    url = main._cart_search_url("bid", title)
    assert parse_qs(urlparse(url).query)["kind"] == ["reg,re,chg,cncl"]
    response = client.get(url)
    assert response.status_code == 200
    assert _rows(response.text) == {"K-cncl"}
    assert _checked_kinds(response.text) == set(_KINDS)
    assert _range_state(response.text) == ("custom", False)


def test_pre_spec_cart_search_url_unchanged():
    assert main._cart_search_url("pre_spec", "test") == (
        "/pre-spec?q=test&dt_from=2000-01-01&dt_to=2099-12-31"
        "&include_past=1&excl_kw=&excl_kw_set=1"
    )


@pytest.mark.parametrize(
    ("date_field", "dt_range", "expected"),
    [
        ("bid_ntce_dt", "today", ("2026-09-23", "2026-09-23")),
        ("bid_ntce_dt", "1m", ("2026-08-23", "2026-09-23")),
        ("bid_ntce_dt", "3m", ("2026-06-23", "2026-09-23")),
        ("bid_ntce_dt", "6m", ("2026-03-23", "2026-09-23")),
        ("openg_dt", "today", ("2026-09-23", "2026-09-23")),
        ("openg_dt", "1m", ("2026-09-23", "2026-10-23")),
        ("openg_dt", "3m", ("2026-09-23", "2026-12-23")),
        ("openg_dt", "6m", ("2026-09-23", "2027-03-23")),
    ],
)
def test_list_preset_date_range(date_field, dt_range, expected):
    assert main._list_preset_date_range(date_field, dt_range, today=_TODAY) == expected


@pytest.mark.parametrize(
    ("date_field", "today", "expected"),
    [
        ("bid_ntce_dt", date(2024, 3, 31), ("2024-02-29", "2024-03-31")),
        ("openg_dt", date(2026, 1, 31), ("2026-01-31", "2026-02-28")),
    ],
)
def test_list_preset_date_range_month_end(date_field, today, expected):
    assert main._list_preset_date_range(date_field, "1m", today=today) == expected


def test_list_preset_ranges_data_matches_date_helper(client):
    group = _attrs(client.get("/list").text, "fieldset", id="list-date-group")[0]
    actual = json.loads(group["data-preset-ranges"])
    expected = {
        field: {
            preset: list(main._list_preset_date_range(field, preset, today=_TODAY))
            for preset in ("today", "1m", "3m", "6m")
        }
        for field in ("bid_ntce_dt", "openg_dt")
    }
    assert actual == expected


def test_list_preset_overrides_explicit_dates_and_filters_rows(client):
    with main.SessionLocal() as session:
        session.add(_notice("RANGE-JULY", "등록공고", datetime(2026, 7, 1)))
        session.add(_notice("RANGE-MAY", "등록공고", datetime(2026, 5, 1)))
        session.commit()
    response = client.get("/list", params={**_WIDE, "dt_range": "3m", "q": "RANGE-"})
    assert response.status_code == 200
    assert _dates(response.text) == ("2026-06-23", "2026-09-23")
    assert _range_state(response.text) == ("3m", True)
    assert _rows(response.text) == {"RANGE-JULY"}


@pytest.mark.parametrize(
    ("params", "selected", "expected"),
    [
        ({}, "1m", ("2026-08-23", "2026-09-23")),
        ({"dt_range": "1m"}, "1m", ("2026-08-23", "2026-09-23")),
        ({"dt_range": "invalid"}, "1m", ("2026-08-23", "2026-09-23")),
        (_WIDE, "custom", ("2000-01-01", "2099-12-31")),
        ({**_WIDE, "dt_range": "invalid"}, "custom", ("2000-01-01", "2099-12-31")),
        ({"dt_from": "2026-09-01"}, "custom", ("2026-09-01", "2026-09-23")),
        ({"dt_to": "2026-09-15"}, "custom", ("2026-08-23", "2026-09-15")),
        ({"dt_range": "custom"}, "custom", ("2026-08-23", "2026-09-23")),
        ({"date_field": "openg_dt", "dt_range": "custom"}, "custom", ("2026-09-23", "2026-10-23")),
    ],
)
def test_list_date_range_defaults_legacy_urls_and_invalid_fallback(client, params, selected, expected):
    response = client.get("/list", params=params)
    assert response.status_code == 200
    assert _dates(response.text) == expected
    assert _range_state(response.text) == (selected, selected != "custom")


def test_list_filter_summary_detail_order_and_single_form(client):
    html = client.get("/list", params=_WIDE).text
    form = _form(html)
    summary, separator, detail = form.partition('<div class="filter-detail">')
    assert separator
    fields = ["q", "date_field", "dt_range", "dt_from", "dt_to", "price_max", "page_size"]
    positions = [summary.index(f'name="{name}"') for name in fields]
    assert positions == sorted(positions)
    assert '<span class="flabel">공고명</span>' in summary
    assert 'class="list-date-group"' in summary
    assert 'type="submit"' in summary
    assert "filter-toggle" in summary
    assert "btn-add-to-cart" in summary
    assert _attrs(summary, "select", name="page_size")[0]["onchange"] == "this.form.submit()"
    for name in ("excl_kw", "kind", "price_min", "include_past", "excl_privt"):
        assert f'name="{name}"' in detail
        assert f'name="{name}"' not in summary
    for label in ("공고명 제외 키워드", "공고종류", "추정가격 최소", "지난 개찰 포함", "수의계약제외"):
        assert label in detail
    assert "<form" not in form
    assert html.count('action="/list"') == 1


def test_list_floating_labels_are_scoped_and_shared_field_css_unchanged(client):
    html = client.get("/list").text
    card = _attrs(html, "div", id="filterCard")[0]
    assert "list-filter" in card["class"].split()
    rule = re.search(r"\.list-filter[^{}]*\.flabel\s*\{([^}]+)\}", main.BASE_CSS)
    assert rule is not None
    assert "font-size: 12px" in rule.group(1)
    assert "position: absolute" in rule.group(1)
    assert "background" in rule.group(1)
    date_label = re.search(
        r"\.list-filter \.list-date-group \.flabel\s*\{([^}]+)\}", main.BASE_CSS,
    )
    assert date_label is not None
    assert "background: #f8fafc" in date_label.group(1)
    check_header = re.search(
        r"table\.listtable > thead > tr > th:not\(\.col-chk\)\s*\{([^}]+)\}",
        main.BASE_CSS,
    )
    assert check_header is not None
    assert "text-align: left" in check_header.group(1)
    assert "vertical-align: middle" in check_header.group(1)
    assert re.search(r"\.col-chk\s*\{[^}]*text-align: center", main.BASE_CSS)
    assert re.search(r"(?m)^\s*\.field \{([^}]+)\}", main.BASE_CSS).group(1).strip() == (
        "display: flex; flex-direction: column; font-size: 13px;"
    )
    assert re.search(r"(?m)^\s*\.flabel \{([^}]+)\}", main.BASE_CSS).group(1).strip() == "margin-bottom: 4px;"
    pre_spec = client.get("/pre-spec").text
    assert _attrs(pre_spec, "div", id="filterCard")[0]["class"] == "filter-card filter-collapsed"
    for page in (pre_spec, client.get("/config").text):
        assert not any("list-filter" in attrs.get("class", "").split() for _tag, attrs in _Tags(page).tags)
        assert not _attrs(page, "select", name="dt_range")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, "공고일 2026-08-23 ~ 2026-09-23"),
        ({"date_field": "openg_dt", "dt_range": "1m"}, "개찰일 2026-09-23 ~ 2026-10-23"),
        ({**_WIDE, "dt_range": "custom"}, "공고일 2000-01-01 ~ 2099-12-31"),
    ],
)
def test_list_search_range_is_inside_results_card(client, params, expected):
    response = client.get("/list", params=params)
    assert response.status_code == 200
    title_pos = response.text.index("<h2>수집된 공고</h2>")
    table_pos = response.text.index("<table", title_pos)
    result_heading = response.text[title_pos:table_pos]
    assert f'<span class="rangeinfo">{expected}</span>' in result_heading
    assert "rangeinfo" not in _form(response.text)
