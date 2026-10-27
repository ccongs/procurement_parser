"""HT-B 화면·ZIP·숫자 검증 회귀 시험. 임시 DB와 가짜 HTTP만 사용한다."""

import asyncio
import html
import io
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import BackgroundTasks, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, repository
from app.db import Base
from app.models import AppConfig, BidNotice, ExcludeKeyword, PreSpec


_NOW = datetime(2026, 6, 1, 12)
_WIDE = {"dt_from": "2000-01-01", "dt_to": "2099-12-31", "include_past": "1"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ht_b.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False)
    with local() as session:
        session.add(AppConfig(
            id=1, enabled=False, pre_spec_enabled=False, updated_at=_NOW,
            presmpt_prce_bgn="100", presmpt_prce_end="200",
            pre_spec_amt_bgn="100", pre_spec_amt_end="200", indstryty_cds="1426",
        ))
        for i in range(12):
            amount = [None, 50, 150, 300][i % 4]
            session.add(BidNotice(
                bid_ntce_no=f"B{i}", bid_ntce_nm=f"입찰검증{i}",
                ntce_kind_nm="등록공고", bid_ntce_dt=_NOW, asign_bdgt_amt=amount,
                collected_at=_NOW, updated_at=_NOW,
                ntce_spec_doc_url1="https://files.test/1",
                ntce_spec_doc_url2="https://files.test/2",
                ntce_spec_doc_url3="https://files.test/3",
            ))
            session.add(PreSpec(
                bf_spec_rgst_no=f"P{i}", prdct_clsfc_no_nm=f"규격검증{i}",
                rcpt_dt=_NOW, asign_bdgt_amt=amount,
                collected_at=_NOW, updated_at=_NOW,
                spec_doc_file_url1="https://files.test/1",
                spec_doc_file_url2="https://files.test/2",
                spec_doc_file_url3="https://files.test/3",
            ))
        session.add(ExcludeKeyword(id=1, keyword="기본제외", is_default=False))
        session.commit()
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 1)
    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(main, "date", FixedDate)
    monkeypatch.setattr(main, "analysis_enabled", lambda: True)
    try:
        yield local
    finally:
        engine.dispose()


@pytest.fixture
def client(db):
    test_client = TestClient(main.app)
    try:
        yield test_client
    finally:
        test_client.close()


class _StreamResponse:
    def __init__(self, chunks, *, length=None, fail=False):
        self.chunks = chunks
        self.headers = {"content-disposition": 'attachment; filename="서버파일.pdf"'}
        if length is not None:
            self.headers["content-length"] = str(length)
        self.fail = fail
        self.read_count = 0

    def raise_for_status(self):
        if self.fail:
            raise httpx.HTTPError("가짜 다운로드 실패")

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


def _fake_downloads(monkeypatch, responses):
    opened = []
    closed = []
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @asynccontextmanager
        async def stream(self, method, url):
            assert method == "GET"
            index = int(url.rsplit("/", 1)[1]) - 1
            opened.append(index)
            try:
                yield responses[index]
            finally:
                closed.append(index)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(main, "_ZIP_TOTAL_LIMIT_BYTES", 10)
    return opened, closed


@pytest.mark.parametrize("path", ["/list/B0", "/pre-spec/P0"])
@pytest.mark.parametrize("case", ["within", "declared", "chunked", "failure", "all_fail", "first_big"])
def test_zip_stream_limits_and_failures(client, monkeypatch, path, case):
    responses = [_StreamResponse([b"abc"]), _StreamResponse([b"de"]), _StreamResponse([b"f"])]
    expected = [0, 1, 2]
    opened_expected = [0, 1, 2]
    if case == "declared":
        responses[1] = _StreamResponse([b"too large"], length=8)
        expected, opened_expected = [0], [0, 1]
    elif case == "chunked":
        responses[1] = _StreamResponse([b"1234", b"5678", b"never read"])
        expected, opened_expected = [0], [0, 1]
    elif case == "failure":
        responses[1].fail = True
        expected = [0, 2]
    elif case == "all_fail":
        for response in responses:
            response.fail = True
        expected = []
    elif case == "first_big":
        responses[0] = _StreamResponse([b"never read"], length=11)
        expected, opened_expected = [], [0]
    opened, closed = _fake_downloads(monkeypatch, responses)
    response = client.get(f"{path}/files.zip")
    assert opened == opened_expected
    assert closed == opened_expected
    assert response.status_code == (200 if expected else 502)
    if expected:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            assert [int(name.split("_", 1)[0]) - 1 for name in names] == expected
            for name, index in zip(names, expected):
                assert archive.read(name) == b"".join(responses[index].chunks)
                if path.startswith("/pre-spec"):
                    assert name == f"{index + 1}_서버파일.pdf"
    if case == "declared":
        assert responses[1].read_count == 0
    if case == "chunked":
        assert responses[1].read_count == 2
    if case == "first_big":
        assert responses[0].read_count == 0


@pytest.mark.parametrize("path", ["/list/B0", "/pre-spec/P0"])
@pytest.mark.parametrize("case", [
    "partial_declared", "partial_chunked", "first_declared", "first_chunked",
    "failure_then_limit", "all_fail",
])
def test_zip_limit_warning_and_empty_result_message(client, monkeypatch, caplog, path, case):
    responses = [_StreamResponse([b"abc"]) for _ in range(3)]
    partial = case.startswith("partial_")
    limit_index = 1 if partial or case == "failure_then_limit" else 0
    if case == "all_fail":
        for response in responses:
            response.fail = True
    else:
        responses[limit_index] = (
            _StreamResponse([b"never read"], length=11)
            if case.endswith("declared") or case == "failure_then_limit"
            else _StreamResponse([b"12345678", b"901", b"never read"])
        )
        if case == "failure_then_limit":
            responses[0].fail = True
    opened, closed = _fake_downloads(monkeypatch, responses)
    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        response = client.get(f"{path}/files.zip")
    assert closed == opened
    warnings = [record for record in caplog.records
                if record.getMessage().startswith("zip 누적 총량 상한 초과")]
    if case == "all_fail":
        assert response.status_code == 502
        assert response.text == "첨부 다운로드에 모두 실패했습니다."
        assert not warnings
        assert opened == [0, 1, 2]
    else:
        assert opened == list(range(limit_index + 1))
        assert len(warnings) == 1
        assert warnings[0].args == (path.rsplit("/", 1)[1], limit_index + 1, 3 if partial else 0)
        if partial:
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert len(archive.namelist()) == 1
                assert archive.read(archive.namelist()[0]) == b"abc"
        else:
            assert response.status_code == 502
            assert response.text == "첨부 크기가 제한을 초과했습니다."


def _rows(text):
    return set(re.findall(r'<tr data-row-id="([^"]+)">', text))


@pytest.mark.parametrize(("path", "prefix"), [("/list", "B"), ("/pre-spec", "P")])
@pytest.mark.parametrize("price_set", [None, "0", "1", "on", "true", "Y", "y"])
def test_explicit_empty_prices_override_defaults(client, path, prefix, price_set):
    params = dict(_WIDE)
    if price_set is not None:
        params["price_set"] = price_set
    response = client.get(path, params=params)
    assert response.status_code == 200
    indices = range(12) if price_set in main._EXCL_KW_SET_TRUE else (2, 6, 10)
    assert _rows(response.text) == {f"{prefix}{i}" for i in indices}
    assert '<input type="hidden" name="price_set" value="1">' in response.text


@pytest.mark.parametrize(("path", "prefix"), [("/list", "B"), ("/pre-spec", "P")])
@pytest.mark.parametrize(("prices", "indices"), [
    ({"price_min": "100", "price_max": ""}, (2, 3, 6, 7, 10, 11)),
    ({"price_min": "", "price_max": "200"}, (1, 2, 5, 6, 9, 10)),
    ({"price_min": "abc", "price_max": ""}, range(12)),
    ({"price_min": "1,00", "price_max": "200"}, (2, 6, 10)),
])
def test_explicit_prices_keep_one_sided_limits_and_parser(client, path, prefix, prices, indices):
    response = client.get(path, params={**_WIDE, "price_set": "1", **prices})
    assert response.status_code == 200
    assert _rows(response.text) == {f"{prefix}{i}" for i in indices}


@pytest.mark.parametrize(("path", "prefix"), [("/list", "B"), ("/pre-spec", "P")])
def test_explicit_empty_prices_survive_pagination_and_sort(client, path, prefix):
    first = client.get(path, params={**_WIDE, "price_set": "1", "page_size": "10"})
    links = [html.unescape(url) for url in re.findall(r'href="([^"]+)"', first.text)]
    second_url = next(url for url in links if url.startswith(path + "?")
                      and parse_qs(urlsplit(url).query).get("page") == ["2"])
    query = parse_qs(urlsplit(second_url).query)
    assert query["price_set"] == ["1"]
    assert "price_min" not in query and "price_max" not in query
    second = client.get(second_url)
    assert len(_rows(second.text)) == 2
    assert len(_rows(first.text) | _rows(second.text)) == 12
    sort_urls = [url for url in links if url.startswith(path + "?") and "sort=" in url]
    assert sort_urls
    assert all(parse_qs(urlsplit(url).query)["price_set"] == ["1"] for url in sort_urls)


@pytest.mark.parametrize(("item_type", "prefix", "title"), [
    ("bid", "B", "입찰검증"), ("pre_spec", "P", "규격검증"),
])
@pytest.mark.parametrize("index", [0, 1, 3])
def test_cart_link_shows_null_and_outside_default_prices(client, item_type, prefix, title, index):
    url = main._cart_search_url(item_type, f"{title}{index}")
    query = parse_qs(urlsplit(url).query)
    assert query["price_set"] == ["1"]
    assert "price_min" not in query and "price_max" not in query
    response = client.get(url)
    assert response.status_code == 200
    assert f"{prefix}{index}" in _rows(response.text)


@pytest.mark.parametrize("field", ["delete_id", "default_ids"])
@pytest.mark.parametrize(("raw", "valid"), [("²", False), ("١", False), (" 1", True), ("1", True), ("1" * 13, False)])
def test_keyword_numeric_ids_ascii_and_strip(client, db, field, raw, valid):
    response = client.post("/config/keywords", data={field: raw}, follow_redirects=False)
    assert response.status_code == 303
    with db() as session:
        keyword = session.get(ExcludeKeyword, 1)
        if field == "delete_id":
            assert (keyword is None) is valid
        else:
            assert keyword.is_default is valid


@pytest.mark.parametrize("code", ["²", "１４２６", "١", "1426,١"])
def test_industry_codes_reject_unicode_digits(client, db, code):
    response = client.post("/config", data={
        "interval_minutes": "30", "window_overlap_minutes": "90", "backfill_days": "30",
        "num_of_rows": "20", "max_retries": "2", "inqry_div": "1", "intrntnl_div_cd": "1",
        "indstryty_cds": code,
    }, follow_redirects=False)
    assert response.status_code == 400
    assert "업종코드(indstryty_cds)는 숫자 코드의 콤마 구분이어야 합니다." in response.text
    with db() as session:
        assert session.get(AppConfig, 1).indstryty_cds == "1426"


@pytest.mark.parametrize("size_known", [True, False])
def test_upload_size_guard_before_read_and_unknown_size_fallback(db, monkeypatch, size_known):
    monkeypatch.setattr(main, "_ANALYSIS_MAX_BYTES", 10)
    class Upload:
        filename = "large.pdf"
        read_count = 0
        async def read(self):
            self.read_count += 1
            return b"x" * 11
    upload = Upload()
    if size_known:
        upload.size = 11
    response, background = Response(), BackgroundTasks()
    result = asyncio.run(main.analysis_upload(
        background_tasks=background, response=response, files=[upload], legacy_file=None,
        source_type="pre-spec", source_id="P0", type_alias=None, id_alias=None,
    ))
    assert response.status_code == 413
    assert result == {"status": "error", "message": "파일 크기가 50MB를 초과합니다."}
    assert upload.read_count == (0 if size_known else 1)
    assert not background.tasks
    with db() as session:
        assert repository.get_analysis(session, "pre_spec", "P0") is None


def test_pre_spec_responsive_filter_css(client):
    page = client.get("/pre-spec", params=_WIDE).text
    form = re.search(r'<form method="get" action="/pre-spec">(.*?)</form>', page, re.S).group(1)
    assert 'style="min-width:260px;"' not in form
    assert 'style="min-width:220px;"' not in form
    selector = '.filter-card form[action="/pre-spec"]'
    block = next(block for block in re.findall(r'@media \(max-width: 700px\) \{(.*?)\n  \}', main.BASE_CSS, re.S)
                 if selector in block)
    assert selector + ' .field' in block
    assert selector + ' .kw-field' in block
    assert 'min-width: 0; width: 100%' in block
    narrow_blocks = re.findall(r'@media \(max-width: 480px\) \{(.*?)\n  \}', main.BASE_CSS, re.S)
    assert all(selector not in block for block in narrow_blocks)
    assert any('.list-filter .list-group-label' in block for block in narrow_blocks)
    for element in ('.kw-box', '.kw-input', '.kw-wrap', '.quick'):
        assert selector + ' ' + element in block
    assert re.search(r'\.kw-chip\s*\{[^}]*max-width: 100%; min-width: 0;', main.BASE_CSS)
    assert re.search(r'\.kw-chip-text\s*\{[^}]*text-overflow: ellipsis;', main.BASE_CSS)
    assert 'box-sizing: border-box;' in block


def test_analysis_keyboard_and_cleanup(client):
    script = main._ANALYSIS_SCRIPT
    for key in ('ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape', 'Tab'):
        assert f"'{key}'" in script
    assert 'firstItem.focus({ preventScroll: true })' in script
    assert 'items[index].focus({ preventScroll: true })' in script
    assert "if (index < 0) index = (e.key === 'ArrowUp') ? items.length : -1;" in script
    assert '(index + 1) % items.length' in script
    assert '(index - 1 + items.length) % items.length' in script
    assert "root.querySelector('.analysis-menu-toggle').focus()" in script
    assert "if (e.key === 'Tab') closeMenu(root);" in script
    assert "var btn = e.target.closest('.btn-analyze[data-status]')" not in script
    assert 'button.btn-analyze.open' not in main.BASE_CSS
    assert 'data-colspan' not in script
    focus_rule = re.search(r'\.analysis-menu button:focus-visible\s*\{([^}]+)\}', main.BASE_CSS)[1]
    assert 'background: #f3eeff;' in focus_rule
    assert 'outline: 2px solid #2767b1;' in focus_rule
    assert 'outline-offset: -2px;' in focus_rule
    page = client.get('/pre-spec', params=_WIDE).text
    assert '<th>분석</th>' in page
    assert '<td class="analysis-col">' in page
    assert 'data-colspan' not in page
    assert 'table:not(.listtable):has(td.analysis-col) > thead > tr > th:last-child' not in main.BASE_CSS
    assert re.search(r'table:not\(\.listtable\) > tbody > tr > td.analysis-col\s*'
                     r'\{[^}]*text-align: center;', main.BASE_CSS)
