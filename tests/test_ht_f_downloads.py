"""HT-F 첨부 오류 응답과 화면 유지 — 가짜 HTTP·임시 DB만 사용한다."""

from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.models import AppConfig, BidNotice, PreSpec


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine)
    now = datetime(2026, 6, 1)
    with Local() as session:
        session.add(AppConfig(id=1))
        for no in ("FILE", "EMPTY"):
            url = "https://attachment.invalid/file" if no == "FILE" else None
            session.add(BidNotice(bid_ntce_no=no, bid_ntce_ord="000", bid_ntce_nm=no,
                                  ntce_spec_doc_url1=url, ntce_spec_file_nm1="첨부.pdf",
                                  collected_at=now, updated_at=now))
            session.add(PreSpec(bf_spec_rgst_no=no, prdct_clsfc_no_nm=no,
                                spec_doc_file_url1=url, collected_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(main, "SessionLocal", Local)
    test_client = TestClient(main.app)
    try:
        yield test_client
    finally:
        test_client.close()
        engine.dispose()


@pytest.mark.parametrize("base,missing", [
    ("/list", "공고를 찾을 수 없습니다."),
    ("/pre-spec", "사전규격을 찾을 수 없습니다."),
])
@pytest.mark.parametrize("suffix", ["/files/single", "/files.zip"])
@pytest.mark.parametrize("item", ["MISSING", "EMPTY"])
def test_attachment_404_preserves_korean_plain_text(client, base, missing, suffix, item):
    response = client.get(base + "/" + item + suffix)
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == (missing if item == "MISSING" else "첨부가 없습니다.")


@pytest.mark.parametrize("base", ["/list", "/pre-spec"])
@pytest.mark.parametrize("suffix", ["/files/single", "/files.zip"])
@pytest.mark.parametrize("failure", ["network", "header_limit", "stream_limit"])
def test_attachment_502_preserves_korean_plain_text(client, monkeypatch, base, suffix, failure):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            yield b"78901"

    def respond(request):
        assert str(request.url) == "https://attachment.invalid/file"
        if failure == "network":
            raise httpx.ConnectError("가짜 연결 실패", request=request)
        if failure == "header_limit":
            return httpx.Response(200, headers={"content-length": "11"}, content=b"file")
        return httpx.Response(200, stream=Chunks())

    original_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(main, "_ZIP_TOTAL_LIMIT_BYTES", 10)
    response = client.get(base + "/FILE" + suffix)
    assert response.status_code == 502
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    expected = ("첨부 크기가 제한을 초과했습니다." if failure != "network" else
                "첨부 다운로드에 모두 실패했습니다." if suffix == "/files.zip" else
                "첨부 다운로드에 실패했습니다.")
    assert response.text == expected


@pytest.mark.parametrize("path,script,msg_id", [
    ("/list", main._LIST_SCRIPT, "zipAllMsg"),
    ("/pre-spec", main._PRE_SPEC_SCRIPT, "psZipAllMsg"),
])
def test_downloads_share_hidden_frame_and_drawer_error_line(client, path, script, msg_id):
    html = client.get(path).text
    common = main._CART_SCRIPT
    assert common.count("function downloadInFrame(") == 1
    assert html.count("function downloadInFrame(") == 1
    assert "document.createElement('iframe')" in common and "frame.hidden = true" in common
    assert "document.body.appendChild(frame)" in common and "frame.src = url" in common
    assert "frame.contentDocument.body.textContent.trim()" in common
    assert "onError(message)" in common and "frame.remove()" in common
    assert "30 * 60 * 1000" in common
    assert "zipAll.addEventListener('click'" in script and "event.preventDefault()" in script
    assert "zipAll.getAttribute('href')" in script and "url === '#'" in script
    assert "downloadInFrame(url, function (message)" in script
    assert "zipAllMsg.textContent = message" in script and "zipAllMsg.hidden = false" in script
    assert "zipAllMsg.textContent = ''" in script and "zipAllMsg.hidden = true" in script
    assert f'<p id="{msg_id}" class="zipall-msg" role="status" hidden>' in html
    cart = common.split("async function downloadCartFiles()", 1)[1].split(
        "async function deleteSelectedCartItems()", 1)[0]
    assert "downloadInFrame(url, function (message)" in cart
    assert "link.click()" not in cart
    assert "failures++" in cart and "})(item.itemId)" in cart
    assert "showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000)" in cart
    assert "setTimeout(resolve, 800)" in cart and "건 다운로드를 시작했습니다." in cart


def test_cart_start_toast_precedes_downloads_and_failure_toast_resets_timer():
    common = main._CART_SCRIPT
    cart = common.split("async function downloadCartFiles()", 1)[1].split(
        "async function deleteSelectedCartItems()", 1)[0]
    before_loop, after_loop = cart.split("for (var i = 0;", 1)
    assert "var message = targets.length + '건 다운로드를 시작했습니다.'" in before_loop
    assert "if (excluded) message += ' (첨부 없음 ' + excluded + '건 제외)'" in before_loop
    assert "showCartToast(message)" in before_loop
    assert "showCartToast(message)" not in after_loop
    assert "건 다운로드를 시작했습니다." not in after_loop
    assert "showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000)" in after_loop
    toast = common.split("function showCartToast(msg, ms)", 1)[1].split(
        "function updateCartBadge(", 1)[0]
    assert "var cartToastTimer = null" in common
    assert toast.index("clearTimeout(cartToastTimer)") < toast.index("cartToastTimer = setTimeout(")
    assert "ms === undefined ? 3000 : ms" in toast


@pytest.mark.parametrize("script,msg_id", [
    (main._LIST_SCRIPT, "zipAllMsg"),
    (main._PRE_SPEC_SCRIPT, "psZipAllMsg"),
])
def test_drawer_zip_shows_info_and_blocks_duplicate_url_until_timeout_or_error(script, msg_id):
    drawer = script.split(f"var zipAllMsg = document.getElementById('{msg_id}');", 1)[1]
    click = drawer.split("zipAll.addEventListener('click'", 1)[1].split("function closeDrawer()", 1)[0]
    assert "var zipDownloads = Object.create(null)" in drawer.split("zipAll.addEventListener", 1)[0]
    assert click.index("if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return") < click.index("event.preventDefault()")
    request, on_error = click.split("downloadInFrame(url, function (message)", 1)
    assert "zipAllMsg.textContent = '다운로드를 요청했습니다. 첨부가 많으면 시작까지 시간이 걸릴 수 있습니다.'" in request
    assert "zipAllMsg.classList.add('info')" in request and "zipAllMsg.hidden = false" in request
    assert request.index("zipAllMsg.textContent = ") < request.index("if (zipDownloads[url]) return")
    assert request.index("if (zipDownloads[url]) return") < request.index("var timer = setTimeout(")
    assert "setTimeout(function () { delete zipDownloads[url]; }, 60 * 1000)" in request
    assert "zipDownloads[url] = timer" in request
    assert "if (zipDownloads[url] === timer)" in on_error
    assert "clearTimeout(timer)" in on_error and "delete zipDownloads[url]" in on_error
    guard = on_error.index("if (zipAll.getAttribute('href') !== url) return")
    assert on_error.index("delete zipDownloads[url]") < guard
    assert guard < on_error.index("zipAllMsg.classList.remove('info')") < on_error.index("zipAllMsg.textContent = message")
    assert "zipAllMsg.hidden = false" in on_error
    assert "zipAllMsg.textContent = ''" in drawer and "zipAllMsg.hidden = true" in drawer
