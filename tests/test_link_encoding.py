"""정렬·일시 정렬·페이저의 검색 조건 URL 인코딩 및 화면 왕복 검증."""

from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import AppConfig, BidNotice, PreSpec


_META = datetime(2026, 6, 1, 9)
_QUERY = "R&D #1 a+b 100% 소프트웨어"
_EXCL_QUERY = "제외 검증 소프트웨어"
_KEYWORDS = ["R&D", "a+b", "100%", "#1", "한글 공백"]
_INSTT = "R&D #1 a+b 100% 기관"


class _Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """고정 날짜·임시 DB로 두 화면의 실제 조회를 검증한다."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'links.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False)
    with local() as session:
        session.add(AppConfig(id=1, enabled=False, pre_spec_enabled=False, updated_at=_META))
        names = [
            (f"{prefix}-{i}", f"{query} 공고 {i}")
            for prefix, query in (("Q", _QUERY), ("E", _EXCL_QUERY))
            for i in range(11)
        ]
        # 검색어가 잘리거나 제외 필터가 소실되면 잘못 노출될 행도 함께 둔다.
        names += [("DECOY-Q", "R 다른 공고"), ("DECOY-Q-EXCL", f"{_QUERY} 감리")]
        names += [(f"DECOY-E-{i}", f"{_EXCL_QUERY} {kw}") for i, kw in enumerate(_KEYWORDS)]
        for no, name in names:
            session.add(BidNotice(
                bid_ntce_no=no, bid_ntce_nm=name, bid_ntce_dt=_META,
                collected_at=_META, updated_at=_META,
            ))
            session.add(PreSpec(
                bf_spec_rgst_no=no, prdct_clsfc_no_nm=name,
                order_instt_nm=_INSTT, rcpt_dt=_META,
                collected_at=_META, updated_at=_META,
            ))
        session.commit()

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 1)

    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(main, "date", FixedDate)
    test_client = TestClient(main.app)
    try:
        yield test_client
    finally:
        test_client.close()
        engine.dispose()


def _link_groups(text, path):
    """HTML 속성 원문을 추출하고 모든 종류의 링크가 존재하는지도 검증한다."""
    groups = {
        "sort": re.findall(r'<a class="sortcol" href="([^"]+)"', text),
        "pager": re.findall(r'<a class="pager-btn[^"]*" href="([^"]+)"', text),
    }
    assert len(groups["sort"]) == (1 if path == "/list" else 3)
    assert len(groups["pager"]) == 3
    if path == "/list":
        groups["time-option"] = re.findall(r'<option value="(/list\?[^"]+)"', text)
        groups["time-direction"] = re.findall(
            r'<a class="sortcol time-dir" href="([^"]+)"', text,
        )
        assert len(groups["time-option"]) == 4
        assert len(groups["time-direction"]) == 2
    return groups


def _assert_screen(response, q, keywords, prefix, page=1):
    assert response.status_code == 200
    tags = _Tags(response.text).tags
    assert [
        attrs["value"] for tag, attrs in tags
        if tag == "input" and attrs.get("name") == "q"
    ] == [q]
    assert [
        attrs["data-kw"] for tag, attrs in tags
        if tag == "span" and attrs.get("class") == "kw-chip"
    ] == keywords
    row_ids = [
        attrs["data-row-id"] for tag, attrs in tags
        if tag == "tr" and "data-row-id" in attrs
    ]
    assert len(row_ids) == (1 if page == 2 else 10)
    assert set(row_ids) <= {f"{prefix}-{i}" for i in range(11)}
    assert "전체 11건" in response.text


def _assert_round_trips(client, path, q, keywords, prefix):
    params = {
        "q": q, "excl_kw": ",".join(keywords), "excl_kw_set": "1",
        "dt_from": "2026-01-01", "dt_to": "2026-12-31",
        "include_past": "1", "page_size": "10",
    }
    if path == "/pre-spec":
        params["instt"] = _INSTT
    response = client.get(path, params=params)
    _assert_screen(response, q, keywords, prefix)
    for links in _link_groups(response.text, path).values():
        for raw_url in links:
            # HTML 속성에는 구분자를 한 번만 이스케이프하고 URL 자체는 인코딩한다.
            assert "&amp;" in raw_url
            assert "&" not in raw_url.replace("&amp;", "")
            assert "&amp;amp;" not in raw_url
            url = unescape(raw_url)
            assert url.isascii()
            assert " " not in url
            parts = urlsplit(url)
            assert parts.path == path
            assert parts.fragment == ""
            query = parse_qs(parts.query)
            assert query["q"] == [q]
            assert query["excl_kw"] == [",".join(keywords)]
            assert query["excl_kw_set"] == ["1"]
            assert query["page_size"] == ["10"]
            if path == "/pre-spec":
                assert query["instt"] == [_INSTT]
            _assert_screen(
                client.get(url), q, keywords, prefix, page=max(1, int(query["page"][0])),
            )


@pytest.mark.parametrize("path", ["/list", "/pre-spec"])
def test_special_query_round_trips_in_all_links(client, path):
    _assert_round_trips(client, path, _QUERY, ["감리"], "Q")


@pytest.mark.parametrize("path", ["/list", "/pre-spec"])
def test_special_exclude_keywords_round_trip_in_all_links(client, path):
    _assert_round_trips(client, path, _EXCL_QUERY, _KEYWORDS, "E")
