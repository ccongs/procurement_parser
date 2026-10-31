"""HT-F 첨부 오류 응답과 화면 유지 — 가짜 HTTP·임시 DB만 사용한다."""

import asyncio
import logging
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
    assert "showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000, 'error')" in cart
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
    assert "showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000, 'error')" in after_loop
    toast = common.split("function showCartToast(msg, ms, kind)", 1)[1].split(
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
    blocked, allowed = request.split("      var timer = setTimeout(", 1)[0].split("      zipAllMsg.textContent = '다운로드를 요청했습니다.", 1)
    assert "if (zipDownloads[url]) {" in blocked
    assert "이미 요청한 다운로드입니다. 잠시 뒤(최대 1분) 다시 시도해 주세요." in blocked
    assert "zipAllMsg.classList.add('info')" in blocked and "zipAllMsg.hidden = false" in blocked
    assert "return;" in blocked.split("if (zipDownloads[url]) {", 1)[1]
    assert "zipAllMsg.classList.add('info')" in allowed and "zipAllMsg.hidden = false" in allowed
    assert "setTimeout(function () { delete zipDownloads[url]; }, 60 * 1000)" in request
    assert "zipDownloads[url] = timer" in request
    assert "if (zipDownloads[url] === timer)" in on_error
    assert "clearTimeout(timer)" in on_error and "delete zipDownloads[url]" in on_error
    guard = on_error.index("if (zipAll.getAttribute('href') !== url) return")
    assert on_error.index("delete zipDownloads[url]") < guard
    assert guard < on_error.index("zipAllMsg.classList.remove('info')") < on_error.index("zipAllMsg.textContent = message")
    assert "zipAllMsg.hidden = false" in on_error
    assert "zipAllMsg.textContent = ''" in drawer and "zipAllMsg.hidden = true" in drawer


@pytest.fixture
def file_buffer(monkeypatch):
    monkeypatch.setattr(main, "_file_buffer_state", None)
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 2)
    monkeypatch.setattr(main, "_FILE_BUFFER_WAIT_SECONDS", 0.01)


_BUFFER_ROUTES = [
    ("/list", "/files/single", main.list_file_single),
    ("/list", "/files.zip", main.list_files_zip),
    ("/pre-spec", "/files/single", main.pre_spec_file_single),
    ("/pre-spec", "/files.zip", main.pre_spec_files_zip),
]
_BUSY_MESSAGE = "다른 첨부 다운로드가 많아 지금은 받을 수 없습니다. 잠시 뒤 다시 시도해 주세요."


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
def test_full_file_buffer_returns_503_without_http(client, file_buffer, monkeypatch, caplog,
                                                  base, suffix, route):
    def unexpected_http(**kwargs):
        pytest.fail("자리를 얻기 전에 외부 첨부를 요청했습니다.")

    async def run():
        semaphore = main._file_buffer_semaphore()
        for _ in range(main._FILE_BUFFER_CONCURRENCY):
            await semaphore.acquire()
        try:
            response = await route("FILE")
            assert response.status_code == 503
            assert response.headers["content-type"] == "text/plain; charset=utf-8"
            assert response.body.decode() == _BUSY_MESSAGE
            assert semaphore.locked()
        finally:
            for _ in range(main._FILE_BUFFER_CONCURRENCY):
                semaphore.release()

    monkeypatch.setattr(main.httpx, "AsyncClient", unexpected_http)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(run())
    assert [r.getMessage() for r in caplog.records if r.name == "app.main"] == [
        f"첨부 다운로드 자리 대기 시간 초과: {base}/FILE{suffix}"]


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
@pytest.mark.parametrize("item", ["MISSING", "EMPTY"])
def test_attachment_404_does_not_acquire_full_buffer(client, file_buffer, monkeypatch,
                                                  base, suffix, route, item):
    class NoAcquire(asyncio.Semaphore):
        async def acquire(self):
            pytest.fail("404 경로가 첨부 버퍼 자리를 기다렸습니다.")

    async def run():
        semaphore = NoAcquire(0)
        monkeypatch.setattr(main, "_file_buffer_state", (asyncio.get_running_loop(), semaphore))
        response = await route(item)
        assert response.status_code == 404
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        missing = "공고를 찾을 수 없습니다." if base == "/list" else "사전규격을 찾을 수 없습니다."
        assert response.body.decode() == (missing if item == "MISSING" else "첨부가 없습니다.")

    asyncio.run(run())


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
@pytest.mark.parametrize("outcome", ["success", "network", "header_limit", "stream_limit"])
def test_file_buffer_released_after_every_response(client, file_buffer, monkeypatch,
                                                 base, suffix, route, outcome):
    calls = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            yield b"78901"

    def respond(request):
        calls.append(request)
        if outcome == "network":
            raise httpx.ConnectError("가짜 연결 실패", request=request)
        if outcome == "header_limit":
            return httpx.Response(200, headers={"content-length": "11"}, content=b"file")
        if outcome == "stream_limit":
            return httpx.Response(200, stream=Chunks())
        return httpx.Response(200, content=b"file")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(main, "_ZIP_TOTAL_LIMIT_BYTES", 10)

    async def run():
        for _ in range(main._FILE_BUFFER_CONCURRENCY + 1):
            response = await route("FILE")
            assert response.status_code == (200 if outcome == "success" else 502)
            if outcome == "success":
                if suffix == "/files.zip":
                    import io
                    import zipfile
                    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                        assert archive.read(archive.namelist()[0]) == b"file"
                else:
                    assert response.body == b"file"
        semaphore = main._file_buffer_semaphore()
        for _ in range(main._FILE_BUFFER_CONCURRENCY):
            await asyncio.wait_for(semaphore.acquire(), 1)
        assert semaphore.locked()
        for _ in range(main._FILE_BUFFER_CONCURRENCY):
            semaphore.release()

    asyncio.run(run())
    assert len(calls) == main._FILE_BUFFER_CONCURRENCY + 1


@pytest.mark.parametrize("first", _BUFFER_ROUTES, ids=lambda v: str(v))
@pytest.mark.parametrize("second", _BUFFER_ROUTES, ids=lambda v: str(v))
def test_routes_share_one_buffer_slot(client, file_buffer, monkeypatch, first, second):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_BUFFER_WAIT_SECONDS", 10)
    original_client = httpx.AsyncClient

    async def run():
        started = asyncio.Event()
        finish_first = asyncio.Event()
        waiting = asyncio.Event()
        active = maximum = calls = 0

        class ObservedSemaphore(asyncio.Semaphore):
            async def acquire(self):
                if self.locked():
                    waiting.set()
                return await super().acquire()

        semaphore = ObservedSemaphore(main._FILE_BUFFER_CONCURRENCY)
        monkeypatch.setattr(main, "_file_buffer_state", (asyncio.get_running_loop(), semaphore))

        async def respond(request):
            nonlocal active, maximum, calls
            calls += 1
            active += 1
            maximum = max(maximum, active)
            try:
                if calls == 1:
                    started.set()
                    await finish_first.wait()
                return httpx.Response(200, content=b"file")
            finally:
                active -= 1

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs))
        async with asyncio.TaskGroup() as group:
            first_task = group.create_task(first[2]("FILE"))
            await started.wait()
            second_task = group.create_task(second[2]("FILE"))
            await waiting.wait()
            assert calls == 1
            assert not first_task.done() and not second_task.done()
            finish_first.set()
        assert first_task.result().status_code == second_task.result().status_code == 200
        assert calls == 2 and maximum == 1

    async def bounded_run():
        await asyncio.wait_for(run(), 3)

    asyncio.run(bounded_run())


def test_file_buffer_waiting_works_across_event_loops(file_buffer, monkeypatch):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    states = []

    async def run():
        semaphore = main._file_buffer_semaphore()
        states.append(main._file_buffer_state)
        await semaphore.acquire()
        try:
            async with main._file_buffer_slot("시험 공고") as acquired:
                assert not acquired
        finally:
            semaphore.release()
        async with main._file_buffer_slot("시험 공고") as acquired:
            assert acquired

    asyncio.run(run())
    asyncio.run(run())
    assert states[0][0] is not states[1][0]
    assert states[0][1] is not states[1][1]


def test_file_buffer_released_on_exception_and_cancellation(file_buffer, monkeypatch):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)

    async def run():
        with pytest.raises(ValueError, match="가짜 오류"):
            async with main._file_buffer_slot("오류 시험") as acquired:
                assert acquired
                raise ValueError("가짜 오류")
        entered = asyncio.Event()

        async def cancelled_download():
            async with main._file_buffer_slot("취소 시험") as acquired:
                assert acquired
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(cancelled_download())
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with main._file_buffer_slot("반환 확인") as acquired:
                assert acquired
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cart_failure_toast_uses_error_color_and_resets_for_other_messages(client):
    html = client.get("/list").text
    assert "#cartToast.error { background: #b02a25; }" in html
    common = main._CART_SCRIPT
    toast = common.split("function showCartToast(msg, ms, kind)", 1)[1].split(
        "function updateCartBadge(", 1)[0]
    assert "t.classList.toggle('error', kind === 'error')" in toast
    calls = [line.strip() for line in common.splitlines() if "showCartToast(" in line
             and "function showCartToast(" not in line]
    errors = [line for line in calls if "'error'" in line]
    assert errors == ["showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000, 'error');"]
