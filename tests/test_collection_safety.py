"""실 네트워크·실 DB 없이 오류 봉투, 분할 수집, 예외 정리를 통합 검증한다."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import api_client, classifier, collector, pre_spec_collector, repository, scheduler
from app.db import Base
from app.models import AppConfig, BidNotice, BidNoticeRaw, CollectionRun, PreSpec
from app.window_split import split_window

NOW = datetime(2026, 10, 27, 12)
BGN = NOW - timedelta(days=60)
ERROR_JSON = ('{"nkoneps.com.response.ResponseError": {   "header": {     '
              '"resultCode": "07",     "resultMsg": "입력범위값 초과 에러"   } }}')
ERROR_XML = ("<nkoneps.com.response.ResponseError>   <header>     <resultCode>07</resultCode>"
             "     <resultMsg>입력범위값 초과 에러</resultMsg>   </header> "
             "</nkoneps.com.response.ResponseError>")


@pytest.fixture
def local_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine)
    with local() as session:
        session.add(AppConfig(
            id=1, enabled=True, auto_halted=False, pre_spec_enabled=True,
            backfill_days=30, window_overlap_minutes=0, num_of_rows=999, max_retries=0,
            inqry_div="1", indstryty_cds="1468",
            last_success_dt=BGN, pre_spec_last_success_dt=BGN,
        ))
        session.commit()
    for module in (collector, pre_spec_collector, scheduler):
        monkeypatch.setattr(module, "SessionLocal", local)
    monkeypatch.setattr(api_client, "SERVICE_KEY", "test-key")

    def unexpected_network(*args, **kwargs):
        pytest.fail("실 네트워크 호출은 허용하지 않습니다.")

    monkeypatch.setattr(api_client.httpx, "get", unexpected_network)
    try:
        yield local
    finally:
        engine.dispose()


@pytest.mark.parametrize("raw", [ERROR_JSON, ERROR_XML])
@pytest.mark.parametrize("response_type", ["json", "xml"])
def test_error_envelope_parses_actual_raw_response(monkeypatch, raw, response_type):
    monkeypatch.setattr(api_client, "SERVICE_KEY", "test-key")
    monkeypatch.setattr(api_client.httpx, "get", lambda *a, **k: httpx.Response(200, text=raw))
    result = api_client.call_endpoint(collector.OPERATION, {}, response_type=response_type)
    assert result.result_code == "07"
    assert result.result_msg == "입력범위값 초과 에러"
    assert result.items == [] and result.total_count is None and result.error is None


@pytest.mark.parametrize("envelope", [True, False])
def test_regular_response_and_unwrapped_response_unchanged(envelope):
    response = {"header": {"resultCode": "00", "resultMsg": "정상"},
                "body": {"items": [{"id": "A"}], "totalCount": 1}}
    parsed = {"response": response, **json.loads(ERROR_JSON)} if envelope else response
    assert api_client._extract_from_parsed(parsed) == ("00", "정상", [{"id": "A"}], "1")


@pytest.mark.parametrize("raw", [ERROR_JSON, ERROR_XML])
@pytest.mark.parametrize("source", ["bid", "pre_spec"])
def test_error_envelope_halts_with_api_message(monkeypatch, local_db, source, raw):
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs["params"])
        return httpx.Response(200, text=raw)

    monkeypatch.setattr(api_client.httpx, "get", get)
    collect = collector.collect_window if source == "bid" else pre_spec_collector.collect_pre_spec_window
    run = collect(BGN, NOW)
    assert run.status == "failed" and run.error_code == "07"
    assert "입력범위값 초과 에러" in run.error_msg
    assert len(calls) == 1 and run.retry_count == 0
    with local_db() as session:
        cfg = session.get(AppConfig, 1)
        assert cfg.auto_halted is (source == "bid")
        assert cfg.last_success_dt == cfg.pre_spec_last_success_dt == BGN


def _result(operation, code="00", items=None):
    return api_client.ApiResult(
        operation=operation, request_url="", sent_params={}, response_type="json",
        status_code=200, raw_text="", parsed=None, result_code=code, result_msg="",
        items=items or [], total_count=str(len(items or [])),
    )


@pytest.mark.parametrize("source", ["bid", "pre_spec"])
@pytest.mark.parametrize("partial_second", [False, True])
def test_tick_collects_chunks_and_advances_only_successes(monkeypatch, local_db, source, partial_second):
    windows = split_window(BGN, NOW)
    operation = collector.OPERATION if source == "bid" else pre_spec_collector.OPERATION
    calls = []

    def call(op, params, response_type="json"):
        if op == operation:
            window = (datetime.strptime(params["inqryBgnDt"], "%Y%m%d%H%M"),
                      datetime.strptime(params["inqryEndDt"], "%Y%m%d%H%M"))
            calls.append(window)
            code = "01" if partial_second and window == windows[1] else "00"
            return _result(op, code)
        assert op == collector.LICENSE_OPERATION
        return _result(op)

    monkeypatch.setattr(api_client, "call_endpoint", call)
    tick = scheduler.tick if source == "bid" else scheduler.tick_pre_spec
    tick(now=NOW)
    expected = windows[:2] if partial_second else windows
    assert calls == expected
    field = "last_success_dt" if source == "bid" else "pre_spec_last_success_dt"
    other_field = "pre_spec_last_success_dt" if source == "bid" else "last_success_dt"
    with local_db() as session:
        cfg = session.get(AppConfig, 1)
        assert getattr(cfg, field) == (windows[0][1] if partial_second else NOW)
        assert getattr(cfg, other_field) == BGN
        runs = session.scalars(select(CollectionRun).order_by(CollectionRun.id)).all()
        assert [(r.window_bgn_dt, r.window_end_dt) for r in runs] == expected
        assert [r.status for r in runs] == (["success", "partial"] if partial_second else ["success"] * 3)
        assert all(r.source == source and r.trigger == "scheduled" for r in runs)

    if partial_second:
        partial_second = False
        calls.clear()
        tick(now=NOW)
        assert calls == windows[1:]  # 성공한 첫 조각 끝부터 다시 이어서 수집한다.
        with local_db() as session:
            assert getattr(session.get(AppConfig, 1), field) == NOW


@pytest.mark.parametrize("module,collect_name", [
    (collector, "collect_window"), (pre_spec_collector, "collect_pre_spec_window"),
])
def test_backfill_30_days_calls_two_chunks(monkeypatch, local_db, module, collect_name, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return NOW

    calls = []

    def collect(bgn, end, *, trigger):
        calls.append((bgn, end, trigger))
        return SimpleNamespace(status="success", total_fetched=len(calls), total_new=0,
                               total_updated=0, retry_count=0, error_code=None)

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setattr(module, "init_db", lambda: None)
    monkeypatch.setattr(module, collect_name, collect)
    module._run_backfill()
    assert calls == [(b, e, "backfill") for b, e in split_window(NOW - timedelta(days=30), NOW)]
    assert len(calls) == 2
    assert "status=success fetched=2" in capsys.readouterr().out


@pytest.mark.parametrize("source,point", [
    ("bid", "upsert"), ("bid", "classification"), ("pre_spec", "upsert"),
])
def test_exception_rolls_back_data_and_closes_run(monkeypatch, local_db, source, point):
    error = RuntimeError("저장 단계 실패" + "가" * 600)
    retried = False

    def call(op, params, response_type="json"):
        nonlocal retried
        if op == collector.LICENSE_OPERATION:
            return _result(op)
        if not retried:
            retried = True
            return _result(op, "01")
        items = ([{"bidNtceNo": "NEW", "bidNtceOrd": "000", "bidNtceNm": "사업"}]
                 if source == "bid" else [{"bfSpecRgstNo": "NEW", "prdctClsfcNoNm": "사업"}])
        return _result(op, items=items)

    with local_db() as session:
        session.get(AppConfig, 1).max_retries = 1
        session.commit()
    monkeypatch.setattr(api_client, "call_endpoint", call)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)
    target = classifier if point == "classification" else repository
    name = ("reclassify_notices" if point == "classification" else
            "upsert_bid_notices" if source == "bid" else "upsert_pre_specs")
    original = getattr(target, name)

    def fail(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        model = BidNotice if source == "bid" else PreSpec
        assert session.get(model, "NEW") is not None
        raise error

    monkeypatch.setattr(target, name, fail)
    collect = collector.collect_window if source == "bid" else pre_spec_collector.collect_pre_spec_window
    with pytest.raises(RuntimeError) as raised:
        collect(BGN, NOW)
    assert raised.value is error
    with local_db() as session:
        run = session.scalars(select(CollectionRun)).one()
        assert run.status == "failed" and run.run_finished_at is not None
        assert run.error_msg.startswith("처리 중 예외로 중단: RuntimeError: 저장 단계 실패")
        assert len(run.error_msg) == 500 and run.error_code is None
        assert run.total_fetched == run.total_new == run.total_updated == 0
        assert run.retry_count == 1
        assert session.get(AppConfig, 1).last_success_dt == BGN
        assert session.get(AppConfig, 1).pre_spec_last_success_dt == BGN
        for model in (BidNotice, BidNoticeRaw, PreSpec):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("source", ["bid", "pre_spec"])
def test_cleanup_failure_preserves_original_exception(monkeypatch, local_db, source, caplog):
    error = RuntimeError("원래 저장 실패")
    monkeypatch.setattr(api_client, "call_endpoint", lambda op, *a, **k: _result(op))
    name = "upsert_bid_notices" if source == "bid" else "upsert_pre_specs"

    def fail(*args, **kwargs):
        raise error

    def fail_cleanup(*args, **kwargs):
        raise ValueError("정리 커밋 실패")

    monkeypatch.setattr(repository, name, fail)
    monkeypatch.setattr(repository, "finish_run", fail_cleanup)
    collect = collector.collect_window if source == "bid" else pre_spec_collector.collect_pre_spec_window
    with pytest.raises(RuntimeError) as raised:
        collect(BGN, NOW)
    assert raised.value is error
    assert "실행 기록 정리 실패" in caplog.text and "ValueError: 정리 커밋 실패" in caplog.text
    with local_db() as session:
        assert session.scalars(select(CollectionRun)).one().status == "running"


@pytest.mark.parametrize("source", ["bid", "pre_spec"])
@pytest.mark.parametrize("halted", [False, True])
def test_already_finished_run_is_not_finished_again(monkeypatch, local_db, source, halted):
    monkeypatch.setattr(api_client, "call_endpoint",
                        lambda op, *a, **k: _result(op, "07" if halted else "00"))
    module = collector if source == "bid" else pre_spec_collector
    original_finish = repository.finish_run
    finishes = []
    error = RuntimeError("종료 기록 이후 실패")

    def finish(*args, **kwargs):
        finishes.append(kwargs["status"])
        original_finish(*args, **kwargs)

    def fail_detach(*args, **kwargs):
        raise error

    monkeypatch.setattr(repository, "finish_run", finish)
    monkeypatch.setattr(module, "_detach_run", fail_detach)
    collect = module.collect_window if source == "bid" else module.collect_pre_spec_window
    with pytest.raises(RuntimeError) as raised:
        collect(BGN, NOW)
    assert raised.value is error
    assert finishes == (["failed"] if halted else ["success"])
    with local_db() as session:
        assert session.scalars(select(CollectionRun)).one().status == finishes[0]


@pytest.mark.parametrize("source", ["bid", "pre_spec"])
@pytest.mark.parametrize("api_message", ["SERVICE_KEY_IS_NOT_REGISTERED_ERROR", ""])
def test_halt_reason_keeps_internal_hint_and_api_message(monkeypatch, local_db, source, api_message):
    def call(op, params, **kwargs):
        return api_client.ApiResult(operation=op, request_url="", sent_params={}, response_type="json",
            status_code=200, raw_text="", parsed=None, result_code="30", result_msg=api_message,
            items=[], total_count=None)

    monkeypatch.setattr(api_client, "call_endpoint", call)
    collect = collector.collect_window if source == "bid" else pre_spec_collector.collect_pre_spec_window
    run = collect(BGN, NOW)
    assert run.status == "failed" and run.error_code == "30"
    assert "등록되지 않은 서비스 키 (키 오류 또는 URL 인코딩 문제)" in run.error_msg
    assert (" — API: " in run.error_msg) == bool(api_message)
    if api_message:
        assert api_message in run.error_msg
