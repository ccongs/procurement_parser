"""collect_window 통합 단위 테스트 — Phase 4.7 확정(회귀 고정).

네트워크/실 DB 비의존:
- api_client.call_endpoint / collector._call_with_retry 를 monkeypatch 로 교체.
- DB 는 인메모리 SQLite(SessionLocal 을 테스트 엔진으로 바꿔치기).
- 확정 동작: partial 판정·last_success 미갱신·재시도 백오프 상수·detail_json 구조.

실행: `pytest tests/test_collector_window.py`
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import collector, repository
from app.collector import _RETRY_BACKOFF_SECONDS
from app.db import Base
from app.models import AppConfig, BidNotice, BidNoticeRaw, CollectionRun

WIN_BGN = datetime(2026, 5, 1, 0, 0)
WIN_END = datetime(2026, 6, 1, 0, 0)


# --- 픽스처: 인메모리 DB + SessionLocal 바꿔치기 -------------------------
@pytest.fixture
def db_engine(monkeypatch):
    """인메모리 SQLite 엔진으로 collector.SessionLocal 을 교체."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)

    # AppConfig 시드(id=1)
    with TestSession() as s:
        cfg = AppConfig(
            id=1,
            enabled=True,
            auto_halted=False,
            interval_minutes=60,
            window_overlap_minutes=90,
            backfill_days=30,
            num_of_rows=20,
            max_retries=2,
            inqry_div="1",
            intrntnl_div_cd="1",
            indstryty_cds="1468",
            last_success_dt=None,
        )
        s.add(cfg)
        s.commit()

    monkeypatch.setattr(collector, "SessionLocal", TestSession)
    return engine, TestSession


def _ok_result(items=None, total_count="0"):
    from app import api_client
    return api_client.ApiResult(
        operation=collector.OPERATION,
        request_url="",
        sent_params={},
        response_type="json",
        status_code=200,
        raw_text="",
        parsed=None,
        result_code="00" if items else "03",
        result_msg="",
        items=items or [],
        total_count=str(total_count),
    )


def _failed_result():
    from app import api_client
    return api_client.ApiResult(
        operation=collector.OPERATION,
        request_url="",
        sent_params={},
        response_type="json",
        status_code=200,
        raw_text="",
        parsed=None,
        result_code="01",
        result_msg="일시 장애",
        items=[],
        total_count="0",
    )


# --- 1. partial 판정: API 일시장애 재시도 소진이 1건 → status='partial' ---
def test_partial_when_call_fails(monkeypatch, db_engine):
    """outcome='failed' 단계가 1개 이상 있으면 status='partial'.

    이 상태에서 last_success_dt 는 갱신하지 않는다(다음 회차 재수집).
    """
    engine, TestSession = db_engine

    call_count = [0]

    def fake_call_with_retry(operation, params, max_retries):
        call_count[0] += 1
        # 모든 호출에서 재시도 소진 → outcome='failed'
        return _failed_result(), max_retries, "failed"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.status == "partial"

    # last_success_dt 미갱신
    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        assert cfg.last_success_dt is None


# --- 2. transform-skip 만 있고 API 정상 → status='success' ---------------
def test_success_when_transform_skip_only(monkeypatch, db_engine):
    """transform-skip(PK 누락 등) 만 있고 API 응답이 ok → status='success'.

    transform-skip 은 partial 판정에 영향 없음(로그만).
    """
    engine, TestSession = db_engine

    # PK(bidNtceNo) 없는 item 1개 → transform에서 skip
    bad_item = {"bidNtceNm": "이름만 있는 공고"}

    def fake_call_with_retry(operation, params, max_retries):
        # 1페이지: bad_item 반환 / 2페이지 이후: No Data
        if params.get("pageNo") == "1":
            r = _ok_result(items=[bad_item], total_count="1")
        else:
            r = _ok_result()  # 03 No Data
        return r, 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.status == "success"

    # success 이므로 last_success_dt 갱신됨
    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        assert cfg.last_success_dt == WIN_END


# --- 3. 전부 ok → status='success', last_success_dt 갱신 ----------------
def test_success_updates_last_success_dt(monkeypatch, db_engine):
    """모든 단계가 ok → status='success' + last_success_dt=window_end."""
    engine, TestSession = db_engine

    def fake_call_with_retry(operation, params, max_retries):
        return _ok_result(), 0, "ok"  # No Data(03)

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.status == "success"
    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        assert cfg.last_success_dt == WIN_END


def test_collection_stores_industry_classification_and_separate_raw(monkeypatch, db_engine):
    """면허 업종으로 분류하고 원문은 별도 테이블에 저장."""
    _engine, TestSession = db_engine
    item = {"bidNtceNo": "COLLECT-13", "bidNtceOrd": "000", "bidNtceNm": "수집 분류 검증"}
    calls = []

    def fake_call_with_retry(operation, params, max_retries):
        calls.append(params.copy())
        rows = [item] if operation == collector.OPERATION else [{
            "bidNtceNo": "COLLECT-13", "bidNtceOrd": "000", "lcnsLmtNm": "소프트웨어사업자/1468",
        }]
        return _ok_result(items=rows, total_count="1"), 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)
    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")
    assert run.status == "success"
    assert run.total_new == 1
    assert len(calls) == 2 and "indstrytyCd" not in calls[0]
    with TestSession() as session:
        row = session.get(BidNotice, "COLLECT-13")
        assert row.list_visible is True
        assert row.match_basis == "업종"
        assert row.match_detail is None
        assert row.matched_indstryty_cds == "1468"
        assert row.raw_json is None
        raw = session.get(BidNoticeRaw, "COLLECT-13")
        assert json.loads(raw.raw_json) == item
        assert raw.updated_at == row.updated_at


# --- 4. detail_json 구조 확인 — by_call / ord_changes / ord_changed_count --
def test_detail_json_structure(monkeypatch, db_engine):
    """collect_window 완료 후 run.detail_json 이 4.7 구조를 가진다."""
    engine, TestSession = db_engine

    def fake_call_with_retry(operation, params, max_retries):
        return _ok_result(), 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.detail_json is not None
    data = json.loads(run.detail_json)
    assert "by_call" in data
    assert "ord_changes" in data
    assert "ord_changed_count" in data
    assert set(data["by_call"]) == {"servc", "license", "license_fix"}
    assert data["classify"]["total"] == 0
    assert isinstance(data["ord_changes"], list)
    assert data["ord_changed_count"] == len(data["ord_changes"])


# --- 5. partial → last_success_dt 미갱신(명시 재확인) --------------------
def test_partial_does_not_update_last_success(monkeypatch, db_engine):
    """partial 상태에서 last_success_dt 가 변하지 않아야 한다.

    §3.3 확정: partial/failed 이면 last_success_dt 미갱신.
    """
    engine, TestSession = db_engine

    # 미리 last_success_dt 를 특정 값으로 설정
    preset_dt = datetime(2026, 5, 30, 10, 0)
    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        cfg.last_success_dt = preset_dt
        s.commit()

    def fake_call_with_retry(operation, params, max_retries):
        return _failed_result(), max_retries, "failed"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")
    assert run.status == "partial"

    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        # preset 값 그대로 보존(갱신 안 됨)
        assert cfg.last_success_dt == preset_dt


# --- 6. 재시도 백오프 상수 확정 ------------------------------------------
def test_retry_backoff_constant():
    """_RETRY_BACKOFF_SECONDS = 2.0 — 4.7에서 상수·식 변경 금지."""
    assert _RETRY_BACKOFF_SECONDS == 2.0


# --- Phase 13.3 전체 수집 흐름 -----------------------------------------
from sqlalchemy import event, func, select
from app import api_client, classifier
from app.models import BidLicenseLimit, ListRule


def _notice(no, ord_="000", limited="Y", **fields):
    return {"bidNtceNo": no, "bidNtceOrd": ord_, "indstrytyLmtYn": limited, **fields}


def _license(no, ord_="000", code="1468", **fields):
    return {"bidNtceNo": no, "bidNtceOrd": ord_, "lcnsLmtNm": f"면허/{code}", **fields}


def _seed(TestSession, *rows):
    with TestSession() as session:
        session.add_all(rows)
        session.commit()


def _existing_notice(no, **values):
    return BidNotice(bid_ntce_no=no, bid_ntce_ord="000", collected_at=WIN_BGN,
                     updated_at=WIN_BGN, **values)


def _fake_api(monkeypatch, responder):
    calls = []

    def call(operation, params, **kwargs):
        calls.append((operation, params.copy()))
        return responder(operation, params)

    monkeypatch.setattr(api_client, "call_endpoint", call)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)
    return calls


def test_collect_latest_order_and_reclassify_all_bases(monkeypatch, db_engine):
    engine, Local = db_engine
    _seed(Local, *(ListRule(rule_type=kind, value=value, enabled=True)
                   for kind, value in classifier.DEFAULT_LIST_RULES))
    notices = [_notice("IND", "000"), _notice("IND", "001"),
               _notice("CLS", limited="N", pubPrcrmntClsfcNm="정보시스템개발서비스"),
               _notice("KEY", limited="N", bidNtceNm="홈페이지 개발"),
               _notice("HIDDEN", bidNtceNm="홈페이지 개발")]
    licenses = [_license("IND", "001"), _license("HIDDEN", code="9999")]
    writes_during_api = []
    in_api = [False]

    def capture(conn, cursor, statement, parameters, context, executemany):
        if in_api[0] and statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes_during_api.append(statement)

    event.listen(engine, "before_cursor_execute", capture)

    def responder(operation, params):
        in_api[0] = True
        # 조회 중 DB 에는 아직 공고·면허가 없다.
        with Local() as session:
            assert session.scalar(select(func.count()).select_from(BidNotice)) == 0
            assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 0
        in_api[0] = False
        return _ok_result(notices if operation == collector.OPERATION else licenses, "5")

    calls = _fake_api(monkeypatch, responder)
    original = classifier.reclassify_notices

    def classify(session, **kwargs):
        assert session.get(BidNotice, "IND").list_visible is False
        assert session.get(BidLicenseLimit, ("IND", "001", "", "")) is not None
        return original(session, **kwargs)

    monkeypatch.setattr(classifier, "reclassify_notices", classify)
    try:
        run = collector.collect_window(WIN_BGN, WIN_END)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert writes_during_api == [] and len(calls) == 2
    assert run.status == "success" and run.total_new == 4
    with Local() as session:
        for no, basis in (("IND", "업종"), ("CLS", "분류"), ("KEY", "키워드"), ("HIDDEN", None)):
            row = session.get(BidNotice, no)
            assert row.list_visible is (basis is not None) and row.match_basis == basis
        assert session.get(BidNotice, "IND").bid_ntce_ord == "001"
    detail = json.loads(run.detail_json)
    assert detail["classify"]["by_basis"] == {"업종": 1, "분류": 1, "키워드": 1}
    assert detail["by_call"]["servc"]["items"] == 5
    assert detail["by_call"]["servc"]["notices"] == 4


def test_license_only_existing_notice_is_kept_and_reclassified(monkeypatch, db_engine):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", list_visible=False, indstryty_lmt_yn="Y"))
    rows = [_license("OLD", code="9999"), _license("FOREIGN"), _license("OLD")]
    _fake_api(monkeypatch, lambda op, params: _ok_result() if op == collector.OPERATION
              else _ok_result(rows, "3"))
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.total_fetched == 0
    with Local() as session:
        assert session.get(BidNotice, "OLD").list_visible is True
        assert session.get(BidNotice, "OLD").match_basis == "업종"
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 1
        assert session.get(BidLicenseLimit, ("OLD", "000", "", "")).lcns_cd == "1468"
    data = json.loads(run.detail_json)
    assert data["by_call"]["license"]["kept"] == 1 and data["classify"]["total"] == 1


def test_missing_license_only_current_limited_notices_are_called(monkeypatch, db_engine):
    _, Local = db_engine
    _seed(Local, _existing_notice("DB"), BidLicenseLimit(
        bid_ntce_no="DB", bid_ntce_ord="001", lmt_grp_no="1", lmt_sno="1",
        lcns_cd="1468", collected_at=WIN_BGN,
    ))
    notices = [_notice("FIX", "001"), _notice("BULK"), _notice("DB", "001"),
               _notice("UNLIMITED", limited="N"), _notice("MISS")]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result(notices, "5")
        if params["inqryDiv"] == "1":
            return _ok_result([_license("BULK"), _license("FIX", "000")], "2")
        if params["bidNtceNo"] == "MISS":
            return _ok_result()
        assert params["bidNtceNo"] == "FIX" and params["bidNtceOrd"] == "001"
        return _ok_result([_license("FIX", "001")], "1")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fixes = [params for _, params in calls if params["inqryDiv"] == "2"]
    assert {params["bidNtceNo"] for params in fixes} == {"FIX", "MISS"}
    assert all("inqryBgnDt" not in params for params in fixes)
    assert run.status == "success"
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix == {"outcome": "ok", "targets": 2, "called": 2, "found": 1,
                   "skipped": 0, "failed": [], "retry_count": 0}
    with Local() as session:
        assert session.get(BidNotice, "MISS").list_visible is False
        assert session.get(BidNotice, "FIX").match_basis == "업종"


@pytest.mark.parametrize("code", ["03", "00"])
def test_legacy_industry_preserved_when_fix_finds_no_rows(monkeypatch, db_engine, code):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", matched_indstryty_cds="1468", list_visible=True,
                                 match_basis="업종", indstryty_lmt_yn="Y"))

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("OLD")], "1")
        result = _ok_result()
        result.result_code = code
        return result

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert len(calls) == 3 and run.status == "success"
    with Local() as session:
        row = session.get(BidNotice, "OLD")
        assert row.matched_indstryty_cds == "1468" and row.list_visible is True
        assert row.match_basis == "업종"


def test_missing_license_fix_cap(monkeypatch, db_engine):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 300
        session.commit()
    notices = [_notice(f"A-{i}") for i in range(205)]
    calls = _fake_api(monkeypatch, lambda op, params: _ok_result(notices, "205")
                      if op == collector.OPERATION else _ok_result())
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert collector._MAX_LICENSE_FIX_TARGETS == 200
    assert fix["targets"] == 205 and fix["called"] == 200 and fix["skipped"] == 5
    assert len(calls) == 202 and run.status == "success"


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_halt_each_stage_does_not_store_or_classify(monkeypatch, db_engine, stage):
    _, Local = db_engine
    calls = []

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        calls.append(current)
        if current == stage:
            result = _failed_result()
            result.result_code = "22"
            return result
        return _ok_result([_notice("NEW")], "1") if current == "servc" else _ok_result()

    _fake_api(monkeypatch, responder)
    monkeypatch.setattr(classifier, "reclassify_notices", lambda *a, **kw: pytest.fail("halt 이후 분류"))
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert calls == ["servc", "license", "license_fix"][:["servc", "license", "license_fix"].index(stage) + 1]
    assert run.status == "failed" and stage in run.error_msg
    assert (run.total_new, run.total_updated, run.total_fetched) == (0, 0, 0)
    assert json.loads(run.detail_json)["classify"] is None
    with Local() as session:
        cfg = session.get(AppConfig, 1)
        assert cfg.auto_halted is True and stage in cfg.halt_reason and cfg.halt_code == "22"
        assert cfg.last_success_dt is None
        for model in (BidNotice, BidNoticeRaw, BidLicenseLimit):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_failed_stage_partial_keeps_received_rows_and_retry_sum(monkeypatch, db_engine, stage):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 1
        session.get(AppConfig, 1).max_retries = 1
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()
    failures = []

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == stage and params["pageNo"] == "2":
            failures.append(current)
            return _failed_result()
        rows = [_notice("NEW")] if current == "servc" else [_license("NEW", "001")]
        return _ok_result(rows, "2" if current == stage else "1")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    data = json.loads(run.detail_json)
    assert run.status == "partial" and stage in run.error_msg
    assert run.retry_count == 1 == sum(row["retry_count"] for row in data["by_call"].values())
    assert failures == [stage, stage]
    assert data["classify"]["total"] == 1
    if stage in ("servc", "license"):
        assert data["by_call"]["license_fix"]["outcome"] == "skipped"
        assert all(params["inqryDiv"] != "2" for _, params in calls)
    else:
        assert data["by_call"]["license_fix"]["failed"] == ["NEW"]
    with Local() as session:
        assert session.get(BidNotice, "NEW") is not None
        assert session.get(BidLicenseLimit, ("NEW", "001", "", "")) is not None
        assert session.get(AppConfig, 1).last_success_dt == WIN_BGN


def test_all_stages_page_and_retry_and_config_params(monkeypatch, db_engine):
    _, Local = db_engine
    with Local() as session:
        cfg = session.get(AppConfig, 1)
        cfg.inqry_div, cfg.intrntnl_div_cd, cfg.prtcpt_lmt_rgn_cd = "2", "2", "28"
        cfg.num_of_rows = 1
        session.commit()
    tried = set()

    def responder(op, params):
        stage = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        page = int(params["pageNo"])
        key = (stage, page)
        if page == 1 and key not in tried:
            tried.add(key)
            return _failed_result()
        if stage == "servc":
            return _ok_result([_notice("FIX" if page == 1 else "BULK")], "2")
        if stage == "license":
            return _ok_result([_license("BULK", lmtSno=str(page))], "2")
        return _ok_result([_license("FIX", lmtSno=str(page))], "2")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success" and run.retry_count == 3
    servc_calls = [params for op, params in calls if op == collector.OPERATION]
    assert [params["pageNo"] for params in servc_calls] == ["1", "1", "2"]
    for params in servc_calls:
        assert "indstrytyCd" not in params
        assert params["inqryDiv"] == "2" and params["intrntnlDivCd"] == "2"
        assert params["prtcptLmtRgnCd"] == "28" and params["numOfRows"] == "1"
        assert params["inqryBgnDt"] == "202605010000" and params["inqryEndDt"] == "202606010000"
    data = json.loads(run.detail_json)
    assert data["by_call"]["servc"]["pages"] == data["by_call"]["license"]["pages"] == 2
    assert data["by_call"]["license_fix"]["retry_count"] == 1
    with Local() as session:
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 4
        assert session.get(AppConfig, 1).last_success_dt == WIN_END


def test_empty_match_codes_still_collects(monkeypatch, db_engine, caplog):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).indstryty_cds = ""
        session.commit()
    _fake_api(monkeypatch, lambda op, params: _ok_result([_notice("NEW", limited="N")], "1")
              if op == collector.OPERATION else _ok_result([_license("NEW")], "1"))
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success" and run.total_new == 1
    assert "매칭 업종코드 없음" in caplog.text
    with Local() as session:
        assert session.get(BidNotice, "NEW").list_visible is False


def test_client_error_is_partial_and_halt_gate_still_skips(monkeypatch, db_engine):
    _, Local = db_engine

    def responder(op, params):
        if op == collector.LICENSE_OPERATION:
            raise api_client.ApiClientError("가짜 클라이언트 오류")
        return _ok_result([_notice("NEW")], "1")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "partial" and "license" in run.error_msg
    with Local() as session:
        session.get(AppConfig, 1).auto_halted = True
        session.commit()
    assert collector.collect_window(WIN_BGN, WIN_END) is None and len(calls) == 2


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_halt_after_received_pages_preserves_existing_data(monkeypatch, db_engine, stage):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", list_visible=True, match_basis="업종",
                                 matched_indstryty_cds="1470"))
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 1
        session.commit()

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == stage and params["pageNo"] == "2":
            result = _failed_result()
            result.result_code = "22"
            return result
        rows = [_notice("NEW", "001")] if current == "servc" else [
            _license("NEW", "001" if current == "license_fix" else "000")]
        return _ok_result(rows, "2" if current == stage else "1")

    calls = _fake_api(monkeypatch, responder)
    monkeypatch.setattr(classifier, "reclassify_notices", lambda *a, **kw: pytest.fail("halt 이후 분류"))
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "failed" and calls[-1][1]["pageNo"] == "2"
    detail = json.loads(run.detail_json)
    assert detail["classify"] is None
    if stage == "license_fix":
        assert detail["by_call"]["license_fix"]["outcome"] == "failed"
        assert detail["by_call"]["license_fix"]["failed"] == ["NEW"]
    with Local() as session:
        assert session.get(BidNotice, "NEW") is None
        row = session.get(BidNotice, "OLD")
        assert row.matched_indstryty_cds == "1470" and row.list_visible is True
        assert row.match_basis == "업종" and row.updated_at == WIN_BGN
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 0


def test_classification_failure_rolls_back_notices_raw_and_licenses(monkeypatch, db_engine):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", bid_ntce_nm="기존", list_visible=True, match_basis="업종"))
    _fake_api(monkeypatch, lambda op, params: _ok_result([
        _notice("OLD", bidNtceNm="변경"), _notice("NEW"),
    ], "2") if op == collector.OPERATION else _ok_result([_license("OLD"), _license("NEW")], "2"))

    def fail_before_commit(session, **kwargs):
        session.flush()
        assert session.get(BidNotice, "NEW").list_visible is False
        assert session.get(BidLicenseLimit, ("NEW", "000", "", "")) is not None
        session.get(BidNotice, "OLD").list_visible = False
        session.flush()
        raise RuntimeError("가짜 분류 실패")

    monkeypatch.setattr(classifier, "reclassify_notices", fail_before_commit)
    with pytest.raises(RuntimeError, match="가짜 분류 실패"):
        collector.collect_window(WIN_BGN, WIN_END)
    with Local() as session:
        row = session.get(BidNotice, "OLD")
        assert row.bid_ntce_nm == "기존" and row.list_visible is True
        assert session.get(BidNotice, "NEW") is None
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 0
        assert session.scalar(select(func.count()).select_from(BidNoticeRaw)) == 0
