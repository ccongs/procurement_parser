"""HT-F 첨부 오류 응답과 화면 유지 — 가짜 HTTP·임시 DB만 사용한다."""

import asyncio
import io
import logging
import socket
import zipfile
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote

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


@pytest.fixture
def uvicorn_logging(monkeypatch):
    @contextmanager
    def preserve():
        loggers = [logging.getLogger(name) for name in
                   ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")]
        handlers = [list(logger.handlers) for logger in loggers]
        try:
            with monkeypatch.context() as patch:
                for logger in loggers:
                    for attr in ("level", "propagate", "handlers", "disabled"):
                        patch.setattr(logger, attr, getattr(logger, attr))
                yield patch
        finally:
            # 수준 복원 뒤에도 시험 중 계산한 수준 판단 캐시를 남기지 않는다.
            for logger, original_handlers in zip(loggers, handlers):
                logger.handlers[:] = original_handlers
                logger._cache.clear()

    return preserve


_BUFFER_ROUTES = [
    ("/list", "/files/single", main.list_file_single),
    ("/list", "/files.zip", main.list_files_zip),
    ("/pre-spec", "/files/single", main.pre_spec_file_single),
    ("/pre-spec", "/files.zip", main.pre_spec_files_zip),
]
_BUSY_MESSAGE = "다른 첨부 다운로드가 많아 지금은 받을 수 없습니다. 잠시 뒤 다시 시도해 주세요."


async def _send_response(response, send=None):
    async def receive():
        pytest.fail("일반 첨부 응답이 receive를 호출했습니다.")

    async def discard(message):
        pass

    await response({"type": "http"}, receive, send or discard)


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
            assert bytes(response.body).decode() == _BUSY_MESSAGE
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
        assert bytes(response.body).decode() == (missing if item == "MISSING" else "첨부가 없습니다.")

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
            semaphore = main._file_buffer_semaphore()
            assert semaphore._value == main._FILE_BUFFER_CONCURRENCY - (outcome == "success")
            if outcome == "success":
                if suffix == "/files.zip":
                    with zipfile.ZipFile(io.BytesIO(bytes(response.body))) as archive:
                        assert archive.read(archive.namelist()[0]) == b"file"
                else:
                    assert bytes(response.body) == b"file"
            await _send_response(response)
            assert semaphore._value == main._FILE_BUFFER_CONCURRENCY
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

        async def download(route):
            response = await route("FILE")
            await _send_response(response)
            return response

        async with asyncio.TaskGroup() as group:
            first_task = group.create_task(download(first[2]))
            await started.wait()
            second_task = group.create_task(download(second[2]))
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


@pytest.fixture
def attachment_http(monkeypatch):
    calls = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first\x00"
            yield b"second\xff"

    def respond(request):
        assert str(request.url) == "https://attachment.invalid/file"
        calls.append(request)
        return httpx.Response(200, headers={
            "content-type": "application/pdf",
            "content-disposition": "attachment; filename*=UTF-8''" + quote("원본.pdf"),
        }, stream=Chunks())

    original_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    return calls


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
@pytest.mark.parametrize("ending", ["complete", "send_error", "cancel"])
def test_attachment_slot_held_through_body_send(client, file_buffer, attachment_http,
                                               monkeypatch, base, suffix, route, ending):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_CHUNK_BYTES", 4)

    async def run():
        response = await route("FILE")
        assert response.status_code == 200
        expected_body = bytes(response.body)
        semaphore = main._file_buffer_semaphore()
        assert semaphore._value == 0
        sending = asyncio.Event()
        finish = asyncio.Event()
        messages = []

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message["more_body"]:
                sending.set()
                await finish.wait()
                if ending == "send_error":
                    raise OSError("가짜 클라이언트 끊김")

        task = asyncio.create_task(_send_response(response, send))
        try:
            await sending.wait()
            assert not task.done()
            async with main._file_buffer_slot("전송 중 자리 확인") as acquired:
                assert not acquired
            busy = await route("FILE")
            assert busy.status_code == 503
            assert bytes(busy.body).decode() == _BUSY_MESSAGE
            assert len(attachment_http) == 1
            assert semaphore._value == 0
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                finish.set()
                if ending == "send_error":
                    with pytest.raises(OSError, match="가짜 클라이언트 끊김"):
                        await task
                else:
                    await task
            assert messages[0]["type"] == "http.response.start"
            chunks = messages[1:]
            assert len(chunks) > 1
            assert all(m["type"] == "http.response.body" for m in chunks)
            assert [m["more_body"] for m in chunks] == [True] * (len(chunks) - 1) + [False]
            assert all(isinstance(m["body"], bytes) and len(m["body"]) <= 4 for m in chunks)
            assert b"".join(m["body"] for m in chunks) == expected_body
            assert response.body == b""
            assert semaphore._value == 1
            response._release()
            response._release()
            assert semaphore._value == 1
            async with main._file_buffer_slot("전송 종료 뒤 자리 확인") as acquired:
                assert acquired
                assert semaphore._value == 0
            assert semaphore._value == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
def test_attachment_response_preserves_bytes_and_headers(client, file_buffer, attachment_http,
                                                       monkeypatch, base, suffix, route):
    monkeypatch.setattr(main, "_FILE_SEND_CHUNK_BYTES", 4)
    # ZIP 시각을 고정해 기존 BytesIO/getvalue 방식과 본문 전체를 바이트 단위로 비교한다.
    monkeypatch.setattr(zipfile.time, "localtime", lambda *args: (2026, 6, 1, 12, 0, 0))
    content = b"first\x00second\xff"
    buffers = []
    if suffix == "/files.zip":
        buf = io.BytesIO()
        name = "첨부.pdf" if base == "/list" else "원본.pdf"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("1_" + name, content)
        expected = buf.getvalue()
        buf.close()
        media_type = "application/zip"
        filename = "FILE.zip"

        class ObservedBuffer(io.BytesIO):
            def __init__(self):
                super().__init__()
                buffers.append(self)

        monkeypatch.setattr(main, "io", SimpleNamespace(BytesIO=ObservedBuffer))
    else:
        expected = content
        media_type = "application/pdf"
        filename = "원본.pdf"

    async def run():
        response = await route("FILE")
        assert response.status_code == 200
        if suffix == "/files/single":
            assert isinstance(response.body, memoryview)
            assert isinstance(response.body.obj, bytearray)
        else:
            assert len(buffers) == 1 and buffers[0].closed
        assert bytes(response.body) == expected
        assert dict(response.headers) == {
            "content-type": media_type,
            "content-disposition": "attachment; filename*=UTF-8''" + quote(filename),
            "content-length": str(len(expected)),
        }
        sent = []

        async def send(message):
            sent.append(message)

        await _send_response(response, send)
        assert response.body == b""
        assert b"".join(m["body"] for m in sent[1:]) == expected
        assert dict(sent[0]["headers"]) == dict(response.raw_headers)

    asyncio.run(run())


@pytest.mark.parametrize("content", [b"", b"abcdefghij"])
def test_attachment_chunk_send_runs_background_after_body(file_buffer, monkeypatch, content):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_CHUNK_BYTES", 4)

    async def run():
        release = await main._acquire_file_buffer_slot("본문 시험")
        semaphore = main._file_buffer_semaphore()
        messages = []
        background_called = False

        async def background():
            nonlocal background_called
            assert messages[-1]["more_body"] is False
            assert semaphore._value == 0
            background_called = True

        inner = main.Response(content, background=background)
        response = main._SlotReleasingResponse(inner, release)

        async def send(message):
            assert not background_called
            assert semaphore._value == 0
            messages.append(message)

        await _send_response(response, send)
        assert background_called
        assert response.body == b""
        assert semaphore._value == 1
        assert messages[0] == {"type": "http.response.start", "status": 200,
                               "headers": inner.raw_headers}
        chunks = messages[1:]
        assert len(chunks) == max(1, (len(content) + 3) // 4)
        assert [m["more_body"] for m in chunks] == [True] * (len(chunks) - 1) + [False]
        assert b"".join(m["body"] for m in chunks) == content
        if not content:
            assert chunks == [{"type": "http.response.body", "body": b"", "more_body": False}]

    asyncio.run(run())


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
def test_attachment_stalled_chunk_times_out_and_releases_slot(
        client, file_buffer, attachment_http, monkeypatch, caplog, base, suffix, route):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_CHUNK_BYTES", 4)
    monkeypatch.setattr(main, "_FILE_SEND_STALL_SECONDS", 0.05)
    path = base + "/FILE" + suffix

    async def run():
        response = await route("FILE")
        expected_length = len(response.body)
        semaphore = main._file_buffer_semaphore()
        sent = 0

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.body":
                assert semaphore._value == 0
                if sent:
                    await asyncio.Future()
                sent += len(message["body"])

        with pytest.raises(TimeoutError):
            await response({"type": "http", "path": path}, None, send)
        assert response.body == b""
        assert sent == 4
        assert semaphore._value == 1
        response._release()
        assert semaphore._value == 1
        async with main._file_buffer_slot("전송 시간 초과 뒤 자리 확인") as acquired:
            assert acquired
        assert [r.getMessage() for r in caplog.records if r.name == "app.main"] == [
            f"첨부 본문 전송 대기 시간 초과: 경로={path}, 보낸 바이트=4, 전체 바이트={expected_length}"]

    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(asyncio.wait_for(run(), 3))


def test_attachment_send_timeout_error_is_not_stall_timeout(file_buffer, caplog):
    async def run():
        release = await main._acquire_file_buffer_slot("send 예외 시험")
        response = main._SlotReleasingResponse(main.Response(b"file"), release)

        async def send(message):
            if message["type"] == "http.response.body":
                raise TimeoutError("send 자체 오류")

        with pytest.raises(TimeoutError, match="send 자체 오류"):
            await _send_response(response, send)
        assert response.body == b""
        assert main._file_buffer_semaphore()._value == main._FILE_BUFFER_CONCURRENCY

    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(run())
    assert not [r for r in caplog.records if r.name == "app.main"]


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
@pytest.mark.parametrize("ending", ["timeout", "cancel", "send_timeout_error"])
def test_attachment_start_stall_releases_slot(
        client, file_buffer, attachment_http, monkeypatch, caplog, base, suffix, route, ending):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_STALL_SECONDS", 0.05)
    path = base + "/FILE" + suffix

    async def run():
        response = await route("FILE")
        expected_length = len(response.body)
        semaphore = main._file_buffer_semaphore()
        sending = asyncio.Event()
        messages = []

        async def send(message):
            messages.append(message)
            assert semaphore._value == 0
            sending.set()
            if ending == "send_timeout_error":
                raise TimeoutError("머리 send 자체 오류")
            await asyncio.Future()

        task = asyncio.create_task(response({"type": "http", "path": path}, None, send))
        try:
            await sending.wait()
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif ending == "send_timeout_error":
                with pytest.raises(TimeoutError, match="머리 send 자체 오류"):
                    await task
            else:
                with pytest.raises(TimeoutError):
                    await task
            assert [m["type"] for m in messages] == ["http.response.start"]
            assert response.body == b""
            assert semaphore._value == 1
            response._release()
            assert semaphore._value == 1
            async with main._file_buffer_slot("머리 전송 종료 뒤 자리 확인") as acquired:
                assert acquired
            warnings = [r for r in caplog.records if r.name == "app.main"]
            if ending == "timeout":
                assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
                assert warnings[0].getMessage() == (
                    f"첨부 본문 전송 대기 시간 초과: 경로={path}, 보낸 바이트=0, "
                    f"전체 바이트={expected_length}")
            else:
                assert not warnings
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(asyncio.wait_for(run(), 3))


def test_attachment_stall_deadline_restarts_for_each_chunk(file_buffer, monkeypatch):
    monkeypatch.setattr(main, "_FILE_SEND_CHUNK_BYTES", 4)
    monkeypatch.setattr(main, "_FILE_SEND_STALL_SECONDS", 60)

    async def run():
        release = await main._acquire_file_buffer_slot("느린 전송 시험")
        response = main._SlotReleasingResponse(main.Response(b"abcdefghijkl"), release)
        loop = asyncio.get_running_loop()
        started = clock = loop.time()
        sent = []

        async def send(message):
            nonlocal clock
            if message["type"] == "http.response.body":
                # 실제 대기 없이 조각마다 40초가 흐르는 계속 진행 중인 전송을 재현한다.
                clock += 40
                await asyncio.sleep(0)
                sent.append(message["body"])

        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: clock)
            await _send_response(response, send)
        assert clock - started == 120
        assert b"".join(sent) == b"abcdefghijkl"
        assert main._file_buffer_semaphore()._value == main._FILE_BUFFER_CONCURRENCY

    asyncio.run(run())


@pytest.mark.parametrize("http", ["httptools", "h11"])
def test_attachment_slot_held_with_uvicorn_socketpair(
        file_buffer, monkeypatch, uvicorn_logging, http):
    from uvicorn.config import Config
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    from uvicorn.server import ServerState

    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    content = b"x" * (8 * 1024 * 1024)

    async def run():
        progress = asyncio.Event()
        completed = asyncio.Event()
        released = asyncio.Event()
        body_calls = 0
        errors = []

        async def app(scope, receive, send):
            nonlocal body_calls
            release = await main._acquire_file_buffer_slot("socketpair 시험")

            def observed_release():
                release()
                released.set()

            async def observed_send(message):
                nonlocal body_calls
                if message["type"] == "http.response.body":
                    body_calls += 1
                    if body_calls >= 2:
                        progress.set()
                await send(message)

            try:
                response = main._SlotReleasingResponse(main.Response(content), observed_release)
                await response(scope, receive, observed_send)
            except BaseException as exc:
                errors.append(exc)
                raise
            finally:
                completed.set()
                # 단일 메시지 방식도 완료 사건으로 깨워, 시간 경과 없이 회귀를 판정한다.
                progress.set()

        config = Config(app=app, http=http, loop="asyncio", lifespan="off",
                        log_level="warning", access_log=False, log_config=None)
        config.load()
        state = ServerState()
        protocol = HttpToolsProtocol if http == "httptools" else H11Protocol
        server, client_socket = socket.socketpair()
        transport = None
        loop = asyncio.get_running_loop()
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client_socket.setblocking(False)
            transport, _ = await loop.connect_accepted_socket(
                lambda: protocol(config=config, server_state=state, app_state={}, _loop=loop),
                server)
            await loop.sock_sendall(client_socket, b"GET /file HTTP/1.1\r\nHost: test\r\n\r\n")
            await progress.wait()
            assert not released.is_set()
            assert body_calls >= 2
            assert not completed.is_set()
            assert main._file_buffer_semaphore()._value == 0

            headers = bytearray()
            while b"\r\n\r\n" not in headers:
                data = await loop.sock_recv(client_socket, 65536)
                assert data
                headers.extend(data)
            head, first_body = bytes(headers).split(b"\r\n\r\n", 1)
            assert f"content-length: {len(content)}".encode() in head.lower()
            assert b"transfer-encoding" not in head.lower()
            received = bytearray(first_body)
            while len(received) < len(content):
                data = await loop.sock_recv(client_socket, 65536)
                assert data
                received.extend(data)
            await completed.wait()
            assert not errors
            assert released.is_set()
            assert main._file_buffer_semaphore()._value == 1
            assert received == content
        finally:
            client_socket.close()
            if transport is not None:
                transport.close()
            else:
                server.close()
            tasks = list(state.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with uvicorn_logging():
        asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("http", ["httptools", "h11"])
def test_attachment_stall_releases_original_body_with_uvicorn_socketpair(
        file_buffer, monkeypatch, uvicorn_logging, http):
    from uvicorn.config import Config
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    from uvicorn.server import ServerState

    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_STALL_SECONDS", 0.2)
    content = bytearray(8 * 1024 * 1024)

    async def run():
        completed = asyncio.Event()
        released = asyncio.Event()
        timed_out = asyncio.Event()

        async def app(scope, receive, send):
            release = await main._acquire_file_buffer_slot("멈춘 socketpair 시험")

            def observed_release():
                release()
                released.set()

            try:
                response = main._SlotReleasingResponse(
                    main.Response(memoryview(content)), observed_release)
                await response(scope, receive, send)
            except TimeoutError:
                timed_out.set()
                raise
            finally:
                completed.set()

        config = Config(app=app, http=http, loop="asyncio", lifespan="off",
                        log_level="warning", access_log=False, log_config=None)
        config.load()
        state = ServerState()
        protocol = HttpToolsProtocol if http == "httptools" else H11Protocol
        server, client_socket = socket.socketpair()
        transport = None
        loop = asyncio.get_running_loop()
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client_socket.setblocking(False)
            transport, _ = await loop.connect_accepted_socket(
                lambda: protocol(config=config, server_state=state, app_state={}, _loop=loop),
                server)
            await loop.sock_sendall(client_socket, b"GET /file HTTP/1.1\r\nHost: test\r\n\r\n")
            await completed.wait()
            await asyncio.gather(*list(state.tasks))
            assert timed_out.is_set()
            assert released.is_set()
            assert main._file_buffer_semaphore()._value == 1
            assert transport.is_closing()
            assert transport.get_write_buffer_size() > 0
            # 클라이언트는 읽지 않았고 버퍼가 남아 있어도 원본을 내보내는 참조는 없어야 한다.
            content.append(0)
            assert len(content) == 8 * 1024 * 1024 + 1
        finally:
            client_socket.close()
            if transport is not None:
                transport.close()
            else:
                server.close()
            tasks = list(state.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with uvicorn_logging():
        asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("http", ["httptools", "h11"])
def test_attachment_pipeline_start_stall_releases_original_body_with_uvicorn_socketpair(
        file_buffer, monkeypatch, uvicorn_logging, http):
    from uvicorn.config import Config
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    from uvicorn.server import ServerState

    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    monkeypatch.setattr(main, "_FILE_SEND_STALL_SECONDS", 0.2)
    content = bytearray(8 * 1024 * 1024)

    async def run():
        completed = asyncio.Event()
        timed_out = asyncio.Event()
        handling_500 = asyncio.Event()
        released = []
        second_messages = []
        original_drain = FlowControl.drain

        async def observed_drain(flow):
            if timed_out.is_set():
                handling_500.set()
            await original_drain(flow)

        monkeypatch.setattr(FlowControl, "drain", observed_drain)

        async def app(scope, receive, send):
            path = scope["path"]
            release = await main._acquire_file_buffer_slot(path)

            def observed_release():
                release()
                released.append(path)

            async def observed_send(message):
                if path == "/second":
                    second_messages.append(message["type"])
                await send(message)

            response = main._SlotReleasingResponse(
                main.Response(b"x" * (1024 * 1024) if path == "/first"
                              else memoryview(content)), observed_release)
            try:
                await response(scope, receive, observed_send)
            except TimeoutError:
                assert path == "/second"
                timed_out.set()
                raise
            finally:
                if path == "/second":
                    completed.set()

        config = Config(app=app, http=http, loop="asyncio", lifespan="off",
                        log_level="warning", access_log=False, log_config=None)
        config.load()
        state = ServerState()
        protocol = HttpToolsProtocol if http == "httptools" else H11Protocol
        server, client_socket = socket.socketpair()
        transport = None
        loop = asyncio.get_running_loop()
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client_socket.setblocking(False)
            transport, _ = await loop.connect_accepted_socket(
                lambda: protocol(config=config, server_state=state, app_state={}, _loop=loop),
                server)
            await loop.sock_sendall(client_socket,
                                    b"GET /first HTTP/1.1\r\nHost: test\r\n\r\n"
                                    b"GET /second HTTP/1.1\r\nHost: test\r\n\r\n")
            await completed.wait()
            await handling_500.wait()
            assert timed_out.is_set()
            assert released == ["/first", "/second"]
            assert main._file_buffer_semaphore()._value == 1
            assert second_messages == ["http.response.start"]
            pending = [task for task in state.tasks if not task.done()]
            assert len(pending) == 1
            coroutine = pending[0].get_coro()
            chain = []
            while hasattr(coroutine, "cr_code"):
                chain.append(coroutine.cr_code.co_name)
                coroutine = coroutine.cr_await
            assert "send_500_response" in chain and "drain" in chain
            assert transport.get_write_buffer_size() > 0
            # 500 전송이 기다리는 동안 traceback이 남아도 원본 참조는 없어야 한다.
            content.append(0)
            assert len(content) == 8 * 1024 * 1024 + 1
        finally:
            client_socket.close()
            if transport is not None:
                transport.close()
            else:
                server.close()
            tasks = list(state.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with uvicorn_logging():
        asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("http", ["httptools", "h11"])
@pytest.mark.parametrize("socketpair_test", [
    test_attachment_slot_held_with_uvicorn_socketpair,
    test_attachment_stall_releases_original_body_with_uvicorn_socketpair,
    test_attachment_pipeline_start_stall_releases_original_body_with_uvicorn_socketpair,
])
def test_uvicorn_socketpair_restores_logger_state(
        file_buffer, monkeypatch, uvicorn_logging, http, socketpair_test):
    loggers = [logging.getLogger(name) for name in
               ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")]
    try:
        with monkeypatch.context() as patch:
            for logger in loggers:
                patch.setattr(logger, "level", logging.INFO)
                patch.setattr(logger, "propagate", True)
                patch.setattr(logger, "handlers", [logging.NullHandler()])
                patch.setattr(logger, "disabled", False)
                logger._cache.clear()
            before = [(logger.level, logger.propagate, logger.disabled,
                       list(logger.handlers), id(logger.handlers))
                      for logger in loggers]
            socketpair_test(file_buffer, patch, uvicorn_logging, http)
            for logger, expected in zip(loggers, before):
                assert (logger.level, logger.propagate, logger.disabled) == expected[:3]
                assert list(logger.handlers) == expected[3]
                assert id(logger.handlers) == expected[4]
                assert not logger._cache
                assert logger.isEnabledFor(logging.INFO)
    finally:
        # 이 검증 시험이 설정한 수준도 복원한 뒤 캐시를 비운다.
        for logger in loggers:
            logger._cache.clear()


def test_uvicorn_logging_restores_mutated_handler_contents(uvicorn_logging):
    loggers = [logging.getLogger(name) for name in
               ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")]
    before = [(list(logger.handlers), id(logger.handlers)) for logger in loggers]
    with uvicorn_logging():
        for logger, (handlers, handlers_id) in zip(loggers, before):
            handler = logging.NullHandler()
            logger.handlers.append(handler)
            assert list(logger.handlers) == handlers + [handler]
            assert id(logger.handlers) == handlers_id
    for logger, (handlers, handlers_id) in zip(loggers, before):
        assert list(logger.handlers) == handlers
        assert id(logger.handlers) == handlers_id


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
@pytest.mark.parametrize("failure", ["client", "build", "wrapper"])
def test_attachment_slot_released_before_response_on_failure(client, file_buffer, attachment_http,
                                                           monkeypatch, base, suffix, route, failure):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)

    def fail(*args, **kwargs):
        raise RuntimeError("가짜 응답 생성 실패")

    async def fail_single(*args, **kwargs):
        fail()

    if failure == "client":
        monkeypatch.setattr(main.httpx, "AsyncClient", fail)
    elif failure == "wrapper":
        monkeypatch.setattr(main, "_SlotReleasingResponse", fail)
    elif suffix == "/files.zip":
        monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    else:
        monkeypatch.setattr(main, "_single_file_response", fail_single)

    async def run():
        for _ in range(3):
            if failure == "client" and suffix == "/files/single":
                response = await route("FILE")
                assert response.status_code == 502
            else:
                with pytest.raises(RuntimeError, match="가짜 응답 생성 실패"):
                    await route("FILE")
            semaphore = main._file_buffer_semaphore()
            assert semaphore._value == 1
            async with main._file_buffer_slot("생성 실패 뒤 자리 확인") as acquired:
                assert acquired
                assert semaphore._value == 0
            assert semaphore._value == 1

    asyncio.run(run())


@pytest.mark.parametrize("base,suffix,route", _BUFFER_ROUTES)
def test_attachment_slot_released_when_download_cancelled(client, file_buffer, monkeypatch,
                                                         base, suffix, route):
    monkeypatch.setattr(main, "_FILE_BUFFER_CONCURRENCY", 1)
    original_client = httpx.AsyncClient

    async def run():
        downloading = asyncio.Event()

        async def respond(request):
            downloading.set()
            await asyncio.Future()

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs))
        task = asyncio.create_task(route("FILE"))
        try:
            await downloading.wait()
            semaphore = main._file_buffer_semaphore()
            assert semaphore._value == 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert semaphore._value == 1
            async with main._file_buffer_slot("다운로드 취소 뒤 자리 확인") as acquired:
                assert acquired
            assert semaphore._value == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))


def test_file_buffer_wait_is_shorter_than_drawer_retry_window():
    assert main._FILE_BUFFER_WAIT_SECONDS == 55


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
