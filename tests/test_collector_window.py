"""collect_window 통합 단위 테스트 — Phase 4.7 확정(회귀 고정).

네트워크/실 DB 비의존:
- api_client.call_endpoint / collector._call_with_retry 를 monkeypatch 로 교체.
- DB 는 인메모리 SQLite(SessionLocal 을 테스트 엔진으로 바꿔치기).
- 확정 동작: partial 판정·last_success 미갱신·재시도 백오프 상수·detail_json 구조.

실행: `pytest tests/test_collector_window.py`
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

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
    assert all(detail["total_count"] == 0 for detail in data["by_call"].values())
    assert all(detail["unique_items"] == 0 for detail in data["by_call"].values())
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
        rows = notices if operation == collector.OPERATION else licenses
        return _ok_result(rows, str(len(rows)))

    calls = _fake_api(monkeypatch, responder)
    original = classifier.reclassify_notices

    def classify(session, **kwargs):
        assert session.get(BidNotice, "IND").list_visible is False
        assert session.get(BidLicenseLimit, ("IND", "001", "", "hca738a74f")) is not None
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
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 2
        assert session.get(BidLicenseLimit, ("OLD", "000", "", "hca738a74f")).lcns_cd == "1468"
    data = json.loads(run.detail_json)
    assert data["by_call"]["license"]["kept"] == 2 and data["classify"]["total"] == 1


@pytest.mark.parametrize("stage", ["license", "license_fix"])
@pytest.mark.parametrize("fields, codes, expected_keys", [
    ({"lmtGrpNo": "", "lmtSno": ""}, ("1468", "9999"),
     {("", "hca738a74f"), ("", "hab5757036")}),
    ({"lmtGrpNo": "", "lmtSno": ""}, ("1468", "1468"), {("", "hca738a74f")}),
    ({"lmtGrpNo": "1", "lmtSno": ""}, ("1468", "9999"), None),
    ({"lmtGrpNo": "", "lmtSno": "2"}, ("1468", "9999"), None),
    ({"lmtGrpNo": "1", "lmtSno": "2"}, ("1468",), {("1", "2")}),
])
def test_optional_license_keys_preserved_and_updated_on_recollection(
    monkeypatch, db_engine, stage, fields, codes, expected_keys,
):
    _, Local = db_engine
    rows = [_license("NEW", code=code, **fields, rgstDt="2026-05-01 09:00:00")
            for code in codes]
    replay = False

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("NEW")], "1")
        if params["inqryDiv"] == "1" and stage == "license_fix" and not replay:
            return _ok_result()
        # 저장 중복 검증에 맞춰 전체 조회의 총건수는 실제 고유 면허 수를 반환한다.
        count = len({collector._license_key(collector.license_values(row)) for row in rows})
        return _ok_result(rows, str(count if params["inqryDiv"] == "2" else len(rows)))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success" and run.error_msg is None
    detail = json.loads(run.detail_json)["by_call"][stage]
    assert detail["total_count"] == (len(set(codes)) if stage == "license_fix" else len(rows))
    assert detail["unique_items"] == len(rows)
    assert sum(params["inqryDiv"] == "2" for _, params in calls) == (stage == "license_fix")
    with Local() as session:
        stored = session.scalars(select(BidLicenseLimit)).all()
        keys = {(row.lmt_grp_no, row.lmt_sno) for row in stored}
        timestamps = {(row.lmt_grp_no, row.lmt_sno): row.collected_at for row in stored}
        assert len(stored) == len(set(codes))
        assert {row.lcns_cd for row in stored} == set(codes)
        if expected_keys is not None:
            assert keys == expected_keys
        else:
            assert all(group == fields["lmtGrpNo"] and sequence.startswith("h")
                       and len(sequence) == 10 for group, sequence in keys)
        notice = session.get(BidNotice, "NEW")
        assert (notice.list_visible, notice.match_basis, notice.matched_indstryty_cds) == (
            True, "업종", "1468")
        assert session.get(AppConfig, 1).last_success_dt == WIN_END

    # 다음 실행의 일괄 응답으로 같은 면허를 재수신하고, 등록일시만 갱신한다.
    replay = True
    rows = [{**row, "rgstDt": "2026-06-01 10:00:00"} for row in rows]
    next_end = WIN_END + timedelta(days=1)
    rerun = collector.collect_window(WIN_END, next_end)
    assert rerun.status == "success" and rerun.error_msg is None
    with Local() as session:
        stored = session.scalars(select(BidLicenseLimit)).all()
        assert len(stored) == len(keys)
        assert {(row.lmt_grp_no, row.lmt_sno) for row in stored} == keys
        assert all(row.rgst_dt == datetime(2026, 6, 1, 10) for row in stored)
        assert {(row.lmt_grp_no, row.lmt_sno): row.collected_at for row in stored} == timestamps
        assert session.get(AppConfig, 1).last_success_dt == next_end


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
                   "failed": [], "retry_count": 0, "total_count": 1, "unique_items": 1,
                   "verify_targets": 0, "superseded": 0, "restored": 0,
                   "superseded_notices": [], "superseded_notices_count": 0}
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


@pytest.mark.parametrize("count", [199, 200, 205])
def test_missing_license_fix_queries_all_targets(monkeypatch, db_engine, count):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 300
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()
    notices = [_notice(f"A-{i}") for i in range(count)]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result(notices, str(count))
        if params["inqryDiv"] == "2" and int(params["bidNtceNo"].split("-")[1]) % 2 == 0:
            return _ok_result([_license(params["bidNtceNo"])], "1")
        return _ok_result()

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix == {"outcome": "ok", "targets": count, "called": count,
                   "found": (count + 1) // 2, "failed": [], "retry_count": 0,
                   "total_count": (count + 1) // 2, "unique_items": (count + 1) // 2,
                   "verify_targets": 0, "superseded": 0, "restored": 0,
                   "superseded_notices": [], "superseded_notices_count": 0}
    fixes = [params for op, params in calls
             if op == collector.LICENSE_OPERATION and params["inqryDiv"] == "2"]
    assert [params["bidNtceNo"] for params in fixes] == [item["bidNtceNo"] for item in notices]
    assert len(calls) == count + 2
    assert run.status == "success" and run.error_msg is None
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_END


@pytest.mark.parametrize("count", [49, 50])
def test_many_missing_license_targets_warns_without_changing_status(monkeypatch, db_engine, caplog, count):
    _, Local = db_engine
    notices = [_notice(f"MISSING-{i}") for i in range(count)] + [
        _notice("BULK"), _notice("UNLIMITED", limited="N"),
    ]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result(notices, str(len(notices)))
        if params["inqryDiv"] == "1":
            return _ok_result([_license("BULK")], "1")
        return _ok_result()

    calls = _fake_api(monkeypatch, responder)
    with caplog.at_level(logging.WARNING, logger="app.collector"):
        run = collector.collect_window(WIN_BGN, WIN_END)
    warnings = [record for record in caplog.records
                if record.name == "app.collector" and record.levelno == logging.WARNING]
    if count >= 50:
        assert len(warnings) == 1
        assert warnings[0].getMessage() == (f"면허 보정·확인 대상 {count}건(확인 0건) "
                                            f"— 이번 창 공고 {len(notices)}건")
    else:
        assert warnings == []
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["targets"] == fix["called"] == count
    assert len(calls) == count + 2
    assert run.status == "success" and run.error_msg is None
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_END


@pytest.mark.parametrize("code", ["03", "00"])
def test_missing_license_fix_no_rows_advances_successive_windows(monkeypatch, db_engine, code):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 300
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()
    notices = [_notice(f"MISSING-{i}") for i in range(205)]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result(notices, str(len(notices)))
        result = _ok_result()
        if params["inqryDiv"] == "2":
            result.result_code = code
        return result

    calls = _fake_api(monkeypatch, responder)
    for window_bgn, window_end in [(WIN_BGN, WIN_END), (WIN_END, datetime(2026, 7, 1))]:
        calls.clear()
        run = collector.collect_window(window_bgn, window_end)
        assert run.status == "success" and run.error_msg is None
        fix = json.loads(run.detail_json)["by_call"]["license_fix"]
        assert fix == {"outcome": "ok", "targets": 205, "called": 205,
                       "found": 0, "failed": [], "retry_count": 0, "total_count": 0,
                       "unique_items": 0, "verify_targets": 0, "superseded": 0, "restored": 0,
                       "superseded_notices": [], "superseded_notices_count": 0}
        assert len(calls) == 207
        with Local() as session:
            assert session.get(AppConfig, 1).last_success_dt == window_end
            assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 0
            assert session.scalar(select(func.count()).select_from(BidNotice).where(
                BidNotice.list_visible.is_(True)
            )) == 0


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
        total = int(params["numOfRows"]) + 1 if current == stage else 1
        return _ok_result(rows, str(total))

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
        assert session.get(BidLicenseLimit, ("NEW", "001", "", "hca738a74f")) is not None
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
            rows = ([_notice("FIX")] + [_notice(f"EXTRA-{i}", limited="N") for i in range(998)]
                    if page == 1 else [_notice("BULK")])
            return _ok_result(rows, "1000")
        if stage == "license":
            offset = (page - 1) * 999
            return _ok_result([_license("BULK", lmtGrpNo="1", lmtSno=str(i))
                               for i in range(offset, min(offset + 999, 1000))], "1000")
        return _ok_result([_license("FIX", lmtGrpNo="1", lmtSno=str(page))], "2")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success" and run.retry_count == 3
    servc_calls = [params for op, params in calls if op == collector.OPERATION]
    assert [params["pageNo"] for params in servc_calls] == ["1", "1", "2"]
    for params in servc_calls:
        assert "indstrytyCd" not in params
        assert params["inqryDiv"] == "2" and params["intrntnlDivCd"] == "2"
        assert params["prtcptLmtRgnCd"] == "28" and params["numOfRows"] == "999"
        assert params["inqryBgnDt"] == "202605010000" and params["inqryEndDt"] == "202606010000"
    data = json.loads(run.detail_json)
    assert data["by_call"]["servc"]["pages"] == data["by_call"]["license"]["pages"] == 2
    assert data["by_call"]["servc"]["page_size"] == 999
    assert data["by_call"]["license"]["page_size"] == 999
    license_calls = [params for op, params in calls if op == collector.LICENSE_OPERATION]
    assert all(params["numOfRows"] == ("999" if params["inqryDiv"] == "1" else "1")
               for params in license_calls)
    assert data["by_call"]["license_fix"]["retry_count"] == 1
    with Local() as session:
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 1002
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


@pytest.mark.parametrize("stage", ["license", "license_fix"])
def test_client_error_is_partial_and_halt_gate_still_skips(monkeypatch, db_engine, stage):
    _, Local = db_engine

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("NEW")], "1")
        if params["inqryDiv"] == ("1" if stage == "license" else "2"):
            raise api_client.ApiClientError("가짜 클라이언트 오류")
        return _ok_result()

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "partial" and stage in run.error_msg
    if stage == "license_fix":
        fix = json.loads(run.detail_json)["by_call"]["license_fix"]
        assert fix["outcome"] == "failed" and fix["failed"] == ["NEW"]
    with Local() as session:
        session.get(AppConfig, 1).auto_halted = True
        session.commit()
    assert collector.collect_window(WIN_BGN, WIN_END) is None
    assert len(calls) == (2 if stage == "license" else 3)


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
        total = int(params["numOfRows"]) + 1 if current == stage else 1
        return _ok_result(rows, str(total))

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
        assert session.get(BidLicenseLimit, ("NEW", "000", "", "hca738a74f")) is not None
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


def test_previous_license_with_no_current_fix_is_hidden(monkeypatch, db_engine):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", indstryty_lmt_yn="Y", list_visible=True,
                                 match_basis="업종", matched_indstryty_cds="1468"),
          BidLicenseLimit(bid_ntce_no="OLD", bid_ntce_ord="000", lmt_grp_no="", lmt_sno="",
                          lcns_cd="1468", collected_at=WIN_BGN))
    calls = _fake_api(monkeypatch, lambda op, params: _ok_result([
        _notice("OLD", "001", bidNtceNm="홈페이지"),
    ], "1") if op == collector.OPERATION else _ok_result())
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success"
    fixes = [params for op, params in calls
             if op == collector.LICENSE_OPERATION and params["inqryDiv"] == "2"]
    assert len(fixes) == 1 and fixes[0]["bidNtceOrd"] == "001"
    assert fixes[0]["numOfRows"] == "20"
    servc = next(params for op, params in calls if op == collector.OPERATION)
    assert servc["numOfRows"] == "999"
    bulk = next(params for op, params in calls if op == collector.LICENSE_OPERATION)
    assert bulk["numOfRows"] == "999"
    detail = json.loads(run.detail_json)
    assert detail["by_call"]["servc"]["page_size"] == 999
    assert detail["by_call"]["license"]["page_size"] == 999
    assert detail["by_call"]["license_fix"]["found"] == 0
    with Local() as session:
        row = session.get(BidNotice, "OLD")
        assert row.list_visible is False and row.match_basis is None
        assert row.matched_indstryty_cds is None
        assert session.get(BidLicenseLimit, ("OLD", "000", "", "")) is not None


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_safety_limit_is_partial_and_preserves_received_data(monkeypatch, db_engine, stage):
    _, Local = db_engine
    monkeypatch.setattr(collector, "_MAX_PAGES_SAFETY", 2)
    with Local() as session:
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == "servc":
            rows = [_notice("NEW", "001")]
        elif stage == "license_fix" and current == "license":
            return _ok_result()
        else:
            rows = [_license("NEW", "000" if stage == "license" else "001",
                             lmtSno=params["pageNo"])]
        total = int(params["numOfRows"]) * 2 + 1 if current == stage else 1
        return _ok_result(rows, str(total))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    detail = json.loads(run.detail_json)
    assert run.status == "partial" and stage in run.error_msg and "안전 상한" in run.error_msg
    assert detail["by_call"][stage]["outcome"] == "failed"
    assert detail["by_call"][stage]["truncated"] is True
    if stage in ("servc", "license"):
        assert detail["by_call"][stage]["pages"] == 2
        assert detail["by_call"]["license_fix"]["outcome"] == "skipped"
        assert all(params["inqryDiv"] != "2" for _, params in calls)
        assert any(op == collector.LICENSE_OPERATION for op, _ in calls)
    assert all(int(params["pageNo"]) <= 2 for _, params in calls)
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_BGN
        assert session.get(AppConfig, 1).auto_halted is False
        notice = session.get(BidNotice, "NEW")
        assert notice.list_visible is (stage == "servc")
        assert session.get(BidNoticeRaw, "NEW") is not None
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == (
            1 if stage == "servc" else 2 if stage == "license" else 0
        )


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_complete_last_page_at_safety_limit_is_not_truncated(monkeypatch, stage):
    monkeypatch.setattr(collector, "_MAX_PAGES_SAFETY", 2)
    operation = collector.OPERATION if stage == "servc" else collector.LICENSE_OPERATION
    params = {"inqryDiv": "2" if stage == "license_fix" else "1"}
    page_size = 20 if stage == "license_fix" else 999
    calls = _fake_api(monkeypatch, lambda op, params: _ok_result([
        {"page": params["pageNo"]},
    ] * page_size, str(page_size * 2)))
    snapshot = collector._ConfigSnapshot("1", "1", None, 20, 0)
    result = collector._fetch_pages(operation, params, snapshot)
    assert result["outcome"] == "ok" and result["pages"] == 2
    assert not result.get("truncated")
    assert len(result["items"]) == page_size * 2
    assert len(calls) == 2
    assert all(params["numOfRows"] == str(page_size) for _, params in calls)


@pytest.mark.parametrize("stage,total,expected_pages", [
    ("servc", 2417, 3), ("license", 2417, 3), ("license", 6939, 7),
    ("license_fix", 41, 3),
])
def test_short_api_pages_are_partial_and_keep_received_data(
    monkeypatch, db_engine, stage, total, expected_pages,
):
    """요청 크기와 달리 매 페이지 10행만 오면 조용한 누락을 실패로 기록한다."""
    _, Local = db_engine
    _seed(Local, *(ListRule(rule_type=kind, value=value, enabled=True)
                   for kind, value in classifier.DEFAULT_LIST_RULES))
    with Local() as session:
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == stage:
            offset = (int(params["pageNo"]) - 1) * 10
            rows = ([_notice(f"NEW-{i}", limited="N", bidNtceNm="홈페이지")
                     for i in range(offset, offset + 10)] if stage == "servc" else
                    [_license("NEW", lmtSno=str(i)) for i in range(offset, offset + 10)])
            return _ok_result(rows, str(total))
        if current == "servc":
            return _ok_result([_notice("NEW")], "1")
        return _ok_result()

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    received = expected_pages * 10
    reason = f"건수 미달: 고유 {received} / 전체 {total}"
    detail = json.loads(run.detail_json)
    result = detail["by_call"][stage]
    assert run.status == "partial" and f"{stage}: {reason}" in run.error_msg
    assert result["outcome"] == "failed" and result["total_count"] == total
    assert result["unique_items"] == received
    assert result["error_msg"] == reason and not result.get("truncated")
    stage_calls = [params for op, params in calls if (
        "servc" if op == collector.OPERATION else
        "license" if params["inqryDiv"] == "1" else "license_fix") == stage]
    assert len(stage_calls) == expected_pages
    assert all(params["numOfRows"] == ("20" if stage == "license_fix" else "999")
               for params in stage_calls)
    if stage == "license_fix":
        assert result["failed"] == ["NEW"]
    else:
        assert result["pages"] == expected_pages and result["items"] == received
        assert detail["by_call"]["license_fix"]["outcome"] == "skipped"
        assert all(params["inqryDiv"] != "2" for _, params in calls)
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_BGN
        assert session.get(AppConfig, 1).auto_halted is False
        assert session.scalar(select(func.count()).select_from(BidNotice)) == (
            received if stage == "servc" else 1)
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == (
            received if stage == "license" else 0)
        notice = session.get(BidNotice, "NEW-0" if stage == "servc" else "NEW")
        assert notice.list_visible is (stage != "license_fix")
        assert detail["classify"]["visible"] == (received if stage == "servc" else 1 if stage == "license" else 0)


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
@pytest.mark.parametrize("total_count", [None, "", "abc", "1.5", "-1"])
def test_invalid_total_count_is_partial(monkeypatch, db_engine, stage, total_count):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == "license" and stage == "license_fix":
            return _ok_result()
        rows = [_notice("NEW")] if current == "servc" else [_license("NEW")]
        result = _ok_result(rows, "1")
        if current == stage:
            result.total_count = total_count
        return result

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    detail = json.loads(run.detail_json)["by_call"]
    assert run.status == "partial" and f"{stage}: totalCount 누락/오류" in run.error_msg
    assert detail[stage]["outcome"] == "failed" and detail[stage]["total_count"] is None
    assert detail[stage]["error_msg"] == "totalCount 누락/오류"
    assert all(params["pageNo"] == "1" for _, params in calls)
    if stage != "license_fix":
        assert detail["license_fix"]["outcome"] == "skipped"
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_BGN
        assert session.get(BidNotice, "NEW") is not None


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
@pytest.mark.parametrize("received", [1, 2])
def test_received_count_at_least_total_is_success(monkeypatch, db_engine, stage, received):
    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == "license" and stage == "license_fix":
            return _ok_result()
        rows = ([_notice(f"NEW-{i}") for i in range(received if stage == current else 1)]
                if current == "servc" else
                [_license(params.get("bidNtceNo", "NEW-0"), lmtSno=str(i))
                 for i in range(received if stage == current else 1)])
        return _ok_result(rows, "1")

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    detail = json.loads(run.detail_json)["by_call"][stage]
    assert run.status == "success" and run.error_msg is None
    assert detail["outcome"] == "ok" and detail["total_count"] == 1
    assert detail["unique_items"] == received
    if stage != "license_fix":
        assert detail["items"] == received
    _, Local = db_engine
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_END


@pytest.mark.parametrize("operation,total,expected_pages", [
    (collector.OPERATION, 2417, 3), (collector.LICENSE_OPERATION, 6939, 7),
])
def test_bulk_999_pages_receive_full_seven_day_counts(monkeypatch, operation, total, expected_pages):
    def responder(op, params):
        assert params["numOfRows"] == "999"
        start = (int(params["pageNo"]) - 1) * 999
        return _ok_result([{"index": i} for i in range(start, min(start + 999, total))], str(total))

    calls = _fake_api(monkeypatch, responder)
    result = collector._fetch_pages(operation, {"inqryDiv": "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["outcome"] == "ok" and result["total_count"] == total
    assert result["pages"] == len(calls) == expected_pages
    assert [row["index"] for row in result["items"]] == list(range(total))


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
@pytest.mark.parametrize("second_code", ["00", "03", "03_total"])
def test_first_total_count_is_preserved_when_later_page_changes(monkeypatch, stage, second_code):
    operation = collector.OPERATION if stage == "servc" else collector.LICENSE_OPERATION
    page_size = 20 if stage == "license_fix" else 999
    total = page_size + 1

    def responder(op, params):
        if params["pageNo"] == "1":
            return _ok_result([{"page": 1}] * page_size, str(total))
        result = _ok_result([{"page": 2}], "999999") if second_code == "00" else _ok_result()
        if second_code == "03":
            result.total_count = None
        return result

    calls = _fake_api(monkeypatch, responder)
    result = collector._fetch_pages(operation, {"inqryDiv": "2" if stage == "license_fix" else "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["total_count"] == total and len(calls) == 2
    assert result.get("total_count_changed", False) is (second_code != "03")
    assert result["outcome"] == ("ok" if second_code == "00" else "failed")
    if second_code != "00":
        assert result["error_msg"] == f"건수 미달: 고유 {page_size} / 전체 {total}"


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
@pytest.mark.parametrize("invalid_total", [None, "abc"])
def test_later_invalid_total_count_fails_and_preserves_first_total(monkeypatch, stage, invalid_total):
    operation = collector.OPERATION if stage == "servc" else collector.LICENSE_OPERATION
    page_size = 20 if stage == "license_fix" else 999

    def responder(op, params):
        result = _ok_result([{"page": params["pageNo"]}], str(page_size + 1))
        if params["pageNo"] == "2":
            result.total_count = invalid_total
        return result

    _fake_api(monkeypatch, responder)
    result = collector._fetch_pages(operation, {"inqryDiv": "2" if stage == "license_fix" else "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["outcome"] == "failed" and result["error_msg"] == "totalCount 누락/오류"
    assert result["total_count"] == page_size + 1
    assert result["pages"] == len(result["items"]) == 2


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_no_data_without_total_count_is_ok(monkeypatch, stage):
    result = _ok_result()
    result.total_count = None
    _fake_api(monkeypatch, lambda op, params: result)
    operation = collector.OPERATION if stage == "servc" else collector.LICENSE_OPERATION
    fetched = collector._fetch_pages(operation, {"inqryDiv": "2" if stage == "license_fix" else "1"},
                                     collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert fetched["outcome"] == "ok" and fetched["total_count"] == 0
    assert fetched["unique_items"] == 0


def test_license_fix_total_count_sums_all_targets(monkeypatch, db_engine):
    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("A"), _notice("B")], "2")
        if params["inqryDiv"] == "1":
            return _ok_result()
        count = 1 if params["bidNtceNo"] == "A" else 2
        return _ok_result([_license(params["bidNtceNo"], lmtSno=str(i))
                           for i in range(count)], str(count))

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "success"
    assert fix["called"] == fix["found"] == 2 and fix["total_count"] == 3
    assert fix["unique_items"] == 3


@pytest.mark.parametrize("configured_size,expected_size", [(1000, 999), (20, 20)])
def test_license_fix_caps_page_size_and_receives_all_rows(
    monkeypatch, db_engine, configured_size, expected_size,
):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = configured_size
        session.commit()
    rows = [_license("FIX", lmtGrpNo="1", lmtSno=str(i)) for i in range(17)]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("FIX")], "1")
        if params["inqryDiv"] == "1":
            return _ok_result()
        # 실측 장애 재현: 1000 요청은 오류 없이 기본 10행만 응답한다.
        size = int(params["numOfRows"])
        actual_size = 10 if size >= 1000 else size
        offset = (int(params["pageNo"]) - 1) * actual_size
        return _ok_result(rows[offset:offset + actual_size], "17")

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fixes = [params for _, params in calls if params["inqryDiv"] == "2"]
    assert len(fixes) == 1 and fixes[0]["numOfRows"] == str(expected_size)
    assert run.status == "success" and run.error_msg is None
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["outcome"] == "ok" and fix["total_count"] == 17
    assert fix["unique_items"] == 17
    with Local() as session:
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 17
        assert session.get(AppConfig, 1).last_success_dt == WIN_END


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
def test_repeated_page_key_cannot_hide_missing_row(monkeypatch, db_engine, stage):
    _, Local = db_engine
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 999
        session.get(AppConfig, 1).last_success_dt = WIN_BGN
        session.commit()
    rows = ([_notice(f"NEW-{i}", limited="N") for i in range(999)] if stage == "servc"
            else [_license("NEW", lmtGrpNo="1", lmtSno=str(i)) for i in range(999)])

    def responder(op, params):
        current = "servc" if op == collector.OPERATION else (
            "license" if params["inqryDiv"] == "1" else "license_fix")
        if current == stage:
            return _ok_result(rows if params["pageNo"] == "1" else [rows[0]], "1000")
        if current == "servc":
            return _ok_result([_notice("NEW")], "1")
        return _ok_result()

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    detail = json.loads(run.detail_json)["by_call"]
    result = detail[stage]
    reason = "건수 미달: 고유 999 / 전체 1000 (중복 1)"
    assert run.status == "partial" and f"{stage}: {reason}" in run.error_msg
    assert result["outcome"] == "failed" and result["error_msg"] == reason
    assert result["total_count"] == 1000 and result["unique_items"] == 999
    if stage == "license_fix":
        assert result["failed"] == ["NEW"]
    else:
        assert result["items"] == 1000 and result["pages"] == 2
        assert detail["license_fix"]["outcome"] == "skipped"
        assert all(params["inqryDiv"] != "2" for _, params in calls)
    with Local() as session:
        assert session.get(AppConfig, 1).last_success_dt == WIN_BGN
        assert session.scalar(select(func.count()).select_from(BidNotice)) == (
            999 if stage == "servc" else 1)
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == (
            999 if stage == "license" else 0)


@pytest.mark.parametrize("stage", ["servc", "license", "license_fix"])
@pytest.mark.parametrize("orders,unique", [
    (("000", "001"), 2), (("0001", "1", "001"), 1), (("A", "00A"), 1),
])
def test_unique_keys_normalize_orders_and_keep_distinct_orders(monkeypatch, stage, orders, unique):
    operation = collector.OPERATION if stage == "servc" else collector.LICENSE_OPERATION
    rows = ([_notice("SAME", order) for order in orders] if stage == "servc" else
            [_license("SAME", order, lmtGrpNo="1", lmtSno="1") for order in orders])
    _fake_api(monkeypatch, lambda op, params: _ok_result(rows, "2"))
    result = collector._fetch_pages(operation, {"inqryDiv": "2" if stage == "license_fix" else "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["unique_items"] == unique and len(result["items"]) == len(orders)
    assert result["outcome"] == ("ok" if unique == 2 else "failed")
    if unique == 1:
        assert result["error_msg"] == f"건수 미달: 고유 1 / 전체 2 (중복 {len(orders) - 1})"


@pytest.mark.parametrize("operation,missing_field", [
    (collector.OPERATION, "bidNtceNo"), (collector.OPERATION, "bidNtceOrd"),
    (collector.LICENSE_OPERATION, "bidNtceNo"), (collector.LICENSE_OPERATION, "bidNtceOrd"),
    (collector.LICENSE_OPERATION, "lmtGrpNo"), (collector.LICENSE_OPERATION, "lmtSno"),
])
@pytest.mark.parametrize("blank", [None, "  ", "missing"])
def test_incomplete_key_rows_each_count_once(monkeypatch, operation, missing_field, blank):
    complete = _license("SAME", lmtGrpNo="1", lmtSno="1")
    incomplete = {**complete, missing_field: blank}
    if blank == "missing":
        incomplete.pop(missing_field)
    # 같은 불완전 행도 각각 1건으로 세고, 완전한 키 중복만 제거한다.
    rows = [complete, complete, incomplete, incomplete]
    _fake_api(monkeypatch, lambda op, params: _ok_result(rows, "3"))
    result = collector._fetch_pages(operation, {"inqryDiv": "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["outcome"] == "ok" and result["unique_items"] == 3
    assert len(result["items"]) == 4 and result["error_msg"] is None


@pytest.mark.parametrize("field,value", [
    ("bidNtceNo", "OTHER"), ("bidNtceOrd", "001"), ("lmtGrpNo", "2"), ("lmtSno", "2"),
])
def test_license_unique_key_uses_every_pk_field(monkeypatch, field, value):
    first = _license("SAME", lmtGrpNo="1", lmtSno="1")
    _fake_api(monkeypatch, lambda op, params: _ok_result([first, {**first, field: value}], "2"))
    result = collector._fetch_pages(collector.LICENSE_OPERATION, {"inqryDiv": "1"},
                                    collector._ConfigSnapshot("1", "1", None, 20, 0))
    assert result["outcome"] == "ok" and result["unique_items"] == 2


def test_finish_run_commit_failure_rolls_back_notices_licenses_and_classification(monkeypatch, db_engine):
    _, Local = db_engine
    _seed(Local, _existing_notice("OLD", bid_ntce_nm="기존", list_visible=False,
                                 matched_indstryty_cds="9999"))
    _fake_api(monkeypatch, lambda op, params: _ok_result([
        _notice("OLD", bidNtceNm="변경"), _notice("NEW"),
    ], "2") if op == collector.OPERATION else _ok_result([_license("OLD"), _license("NEW")], "2"))
    original_finish = repository.finish_run

    def finish_with_failure(session, run, **kwargs):
        def fail_before_commit(session):
            session.flush()
            assert session.get(BidNotice, "OLD").bid_ntce_nm == "변경"
            assert session.get(BidNotice, "OLD").matched_indstryty_cds == "1468"
            assert session.get(BidNotice, "NEW").list_visible is True
            assert session.get(BidLicenseLimit, ("NEW", "000", "", "hca738a74f")) is not None
            assert run.status == "success" and run.run_finished_at is not None
            raise RuntimeError("가짜 finish_run 커밋 직전 실패")

        event.listen(session, "before_commit", fail_before_commit, once=True)
        original_finish(session, run, **kwargs)

    monkeypatch.setattr(repository, "finish_run", finish_with_failure)
    with pytest.raises(RuntimeError, match="가짜 finish_run 커밋 직전 실패"):
        collector.collect_window(WIN_BGN, WIN_END)
    with Local() as session:
        old = session.get(BidNotice, "OLD")
        assert old.bid_ntce_nm == "기존" and old.list_visible is False
        assert old.matched_indstryty_cds == "9999" and old.match_basis is None
        assert session.get(BidNotice, "NEW") is None
        for model in (BidLicenseLimit, BidNoticeRaw):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        run = session.scalar(select(CollectionRun))
        assert run.status == "running" and run.run_finished_at is None
        assert session.get(AppConfig, 1).last_success_dt is None


# --- Phase 13.7 대체 표시·전체 조회 확인 --------------------------------
def _stored_license(raw, *, superseded_at=None):
    return BidLicenseLimit(**collector.license_values(raw), collected_at=WIN_BGN,
                           superseded_at=superseded_at)


@pytest.mark.parametrize("key_fields", [{}, {"lmtGrpNo": "1", "lmtSno": "2"}])
@pytest.mark.parametrize("license_only", [False, True])
def test_new_license_pk_verified_and_old_row_superseded_without_deletion(
    monkeypatch, db_engine, key_fields, license_only,
):
    _, Local = db_engine
    old = _license("CHANGE")
    new = _license("CHANGE", code="9999", **key_fields)
    _seed(Local, _existing_notice("CHANGE", indstryty_lmt_yn="Y", list_visible=True,
                                 match_basis="업종", matched_indstryty_cds="1468"),
          _stored_license(old), _stored_license(_license("CHANGE", "001")),
          _existing_notice("OTHER"), _stored_license(_license("OTHER")))

    def responder(op, params):
        rows = ([] if license_only else [_notice("CHANGE")]) if op == collector.OPERATION else [new]
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success"
    assert [params["bidNtceNo"] for _, params in calls if params["inqryDiv"] == "2"] == ["CHANGE"]
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert (fix["targets"], fix["verify_targets"], fix["called"], fix["superseded"], fix["restored"]) == (1, 1, 1, 1, 0)
    with Local() as session:
        old_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(old)))
        new_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(new)))
        assert old_row.superseded_at == new_row.collected_at
        assert old_row.collected_at == WIN_BGN and old_row.lcns_cd == "1468"
        assert new_row.superseded_at is None
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == 4
        assert all(row.superseded_at is None for row in session.scalars(select(BidLicenseLimit).where(
            (BidLicenseLimit.bid_ntce_no == "OTHER") | (BidLicenseLimit.bid_ntce_ord == "001")
        )))
        notice = session.get(BidNotice, "CHANGE")
        assert notice.list_visible is False and notice.match_basis is None
        assert notice.matched_indstryty_cds is None


def test_verify_normal_addition_keeps_existing_and_new_rows(monkeypatch, db_engine):
    _, Local = db_engine
    old, new = _license("ADD", lmtSno="1"), _license("ADD", code="9999", lmtSno="2")
    _seed(Local, _existing_notice("ADD", indstryty_lmt_yn="Y"), _stored_license(old))

    def responder(op, params):
        rows = [_notice("ADD")] if op == collector.OPERATION else (
            [new] if params["inqryDiv"] == "1" else [old, new])
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    assert run.status == "success" and len(calls) == 3
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["verify_targets"] == 1 and fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit)).all()
        assert len(rows) == 2 and all(row.superseded_at is None for row in rows)
        assert session.get(BidNotice, "ADD").match_basis == "업종"


@pytest.mark.parametrize("outcome", ["empty00", "empty03", "failed", "shortfall", "halt", "late_halt"])
def test_verify_empty_failed_or_halted_never_supersedes(monkeypatch, db_engine, outcome):
    _, Local = db_engine
    old, new = _license("SAFE"), _license("SAFE", code="9999")
    _seed(Local, _existing_notice("SAFE", indstryty_lmt_yn="Y", list_visible=True,
                                 match_basis="업종", matched_indstryty_cds="1468"),
          _stored_license(old))
    with Local() as session:
        cfg = session.get(AppConfig, 1)
        cfg.max_retries = 0
        cfg.num_of_rows = 1 if outcome == "late_halt" else 20
        cfg.last_success_dt = WIN_BGN
        session.commit()

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("SAFE")], 1)
        if params["inqryDiv"] == "1":
            return _ok_result([new], 1)
        if outcome.startswith("empty"):
            result = _ok_result()
            result.result_code = outcome[-2:]
            return result
        if outcome == "shortfall" or (outcome == "late_halt" and params["pageNo"] == "1"):
            return _ok_result([new], 2)
        result = _failed_result()
        if outcome in {"halt", "late_halt"}:
            result.result_code = "22"
        return result

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    halted = outcome in {"halt", "late_halt"}
    assert run.status == ("failed" if halted else "partial" if outcome in {"failed", "shortfall"} else "success")
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["verify_targets"] == 1 and fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit)).all()
        assert len(rows) == (1 if halted else 2)
        assert all(row.superseded_at is None for row in rows)
        assert session.get(BidNotice, "SAFE").match_basis == "업종"
        cfg = session.get(AppConfig, 1)
        assert cfg.auto_halted is halted
        assert cfg.last_success_dt == (WIN_END if run.status == "success" else WIN_BGN)


def test_superseded_license_restored_by_later_full_response(monkeypatch, db_engine):
    _, Local = db_engine
    old, new, third = (_license("RETURN", code=code) for code in ("1468", "9999", "8888"))
    _seed(Local, _existing_notice("RETURN", indstryty_lmt_yn="Y"), _stored_license(old))
    replay = False

    def responder(op, params):
        rows = [_notice("RETURN")] if op == collector.OPERATION else (
            [third if replay else new] if params["inqryDiv"] == "1" else
            [old, new, third] if replay else [new])
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    first = collector.collect_window(WIN_BGN, WIN_END)
    assert json.loads(first.detail_json)["by_call"]["license_fix"]["superseded"] == 1
    with Local() as session:
        assert session.get(BidNotice, "RETURN").list_visible is False
    replay = True
    second = collector.collect_window(WIN_END, WIN_END + timedelta(days=1))
    fix = json.loads(second.detail_json)["by_call"]["license_fix"]
    assert fix["verify_targets"] == fix["restored"] == 1 and fix["superseded"] == 0
    assert sum(params["inqryDiv"] == "2" for _, params in calls) == 2
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit)).all()
        assert len(rows) == 3 and all(row.superseded_at is None for row in rows)
        old_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(old)))
        assert old_row.collected_at == WIN_BGN
        assert session.get(BidNotice, "RETURN").matched_indstryty_cds == "1468"
        assert session.get(BidNotice, "RETURN").match_basis == "업종"


def test_missing_fix_restores_superseded_rows(monkeypatch, db_engine):
    _, Local = db_engine
    old = _license("MISSING", lmtSno="1")
    _seed(Local, _existing_notice("MISSING", indstryty_lmt_yn="Y"),
          _stored_license(old, superseded_at=WIN_BGN),
          _stored_license(_license("MISSING", lmtSno="2"), superseded_at=WIN_BGN))

    def responder(op, params):
        rows = [_notice("MISSING")] if op == collector.OPERATION else (
            [] if params["inqryDiv"] == "1" else [old])
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert len(calls) == 3 and fix["targets"] == 1 and fix["verify_targets"] == 0
    assert fix["superseded"] == 0 and fix["restored"] == 1
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit).order_by(BidLicenseLimit.lmt_sno)).all()
        assert rows[0].superseded_at is None and rows[1].superseded_at == WIN_BGN
        assert session.get(BidNotice, "MISSING").match_basis == "업종"


@pytest.mark.parametrize("bulk_codes", [("9999",), ("1468", "8888")])
def test_verify_requires_new_pk_and_missing_active_row(monkeypatch, db_engine, bulk_codes):
    _, Local = db_engine
    _seed(Local, _existing_notice("NO-VERIFY", indstryty_lmt_yn="Y"),
          _stored_license(_license("NO-VERIFY", code="1468")),
          _stored_license(_license("NO-VERIFY", code="9999")),
          _stored_license(_license("NO-VERIFY", code="7777"), superseded_at=WIN_BGN))
    # 기존 유효 PK 만 수신하거나 모든 유효 행+새 PK 수신이면 확인 대상 제외.
    rows = [_license("NO-VERIFY", code=code) for code in bulk_codes]
    if "8888" in bulk_codes:
        rows.append(_license("NO-VERIFY", code="9999"))

    def responder(op, params):
        assert params["inqryDiv"] == "1"
        items = [_notice("NO-VERIFY")] if op == collector.OPERATION else rows
        return _ok_result(items, len(items))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert len(calls) == 2 and fix["targets"] == fix["verify_targets"] == 0
    assert fix["superseded"] == fix["restored"] == 0


def test_verify_and_missing_targets_deduplicated_by_normalized_pair(monkeypatch, db_engine):
    _, Local = db_engine
    old, new = _license("OVERLAP"), _license("OVERLAP", code="9999")
    _seed(Local, _existing_notice("OVERLAP", indstryty_lmt_yn="Y"), _stored_license(old))
    monkeypatch.setattr(collector, "_missing_license_targets",
                        lambda session, notices, licenses: [_notice("OVERLAP", "0")])

    def responder(op, params):
        rows = [_notice("OVERLAP")] if op == collector.OPERATION else [new]
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert len(calls) == 3 and fix["targets"] == fix["called"] == fix["verify_targets"] == 1
    assert fix["superseded"] == 1


@pytest.mark.parametrize("failed_stage", ["servc", "license"])
def test_verify_skipped_unless_both_bulk_stages_ok(monkeypatch, db_engine, failed_stage):
    _, Local = db_engine
    _seed(Local, _existing_notice("GATE", indstryty_lmt_yn="Y"),
          _stored_license(_license("GATE")))

    def responder(op, params):
        assert params["inqryDiv"] == "1"
        stage = "servc" if op == collector.OPERATION else "license"
        rows = [_notice("GATE")] if stage == "servc" else [_license("GATE", code="9999")]
        return _ok_result(rows, 2 if stage == failed_stage else 1)

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "partial" and len(calls) == 2
    assert fix["outcome"] == "skipped"
    assert fix["verify_targets"] == fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        assert all(row.superseded_at is None for row in session.scalars(select(BidLicenseLimit)))


@pytest.mark.parametrize("count", [50, 51, 205, 501])
def test_verify_queries_all_targets_without_cap(monkeypatch, db_engine, caplog, count):
    _, Local = db_engine
    _seed(Local, *[_existing_notice(f"VERIFY-{i}", indstryty_lmt_yn="Y") for i in range(count)],
          *[_stored_license(_license(f"VERIFY-{i}")) for i in range(count)])
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 999
        session.commit()
    bulk = [_license(f"VERIFY-{i}", code="9999") for i in range(count)]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result()
        rows = bulk if params["inqryDiv"] == "1" else [_license(params["bidNtceNo"], code="9999")]
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    with caplog.at_level(logging.INFO, logger="app.collector"):
        run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "success"
    assert fix["verify_targets"] == fix["targets"] == fix["called"] == fix["superseded"] == count
    assert len(calls) == count + 2
    assert fix["superseded_notices_count"] == count
    if count <= 50:
        assert set(fix["superseded_notices"]) == {f"VERIFY-{i}-000" for i in range(count)}
    else:
        assert "superseded_notices" not in fix
    assert f"면허 보정·확인 대상 {count}건(확인 {count}건)" in caplog.text
    ending = next(record.getMessage() for record in caplog.records
                  if "수집 종료:" in record.getMessage())
    assert f"verify_targets={count} superseded={count} restored=0" in ending
    with Local() as session:
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit)) == count * 2
        assert session.scalar(select(func.count()).select_from(BidLicenseLimit).where(
            BidLicenseLimit.superseded_at.is_not(None))) == count


@pytest.mark.parametrize("old_order,new_order", [("0", "000"), ("001", "1"), ("A", "00A")])
def test_verify_and_supersede_normalize_order_notation(monkeypatch, db_engine, old_order, new_order):
    _, Local = db_engine
    old = _license("ORDER", old_order)
    new = _license("ORDER", new_order, code="9999")
    notice = _existing_notice("ORDER", indstryty_lmt_yn="Y")
    notice.bid_ntce_ord = new_order
    _seed(Local, notice, _stored_license(old))

    def responder(op, params):
        rows = [_notice("ORDER", new_order)] if op == collector.OPERATION else [new]
        return _ok_result(rows, len(rows))

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["verify_targets"] == fix["superseded"] == 1
    with Local() as session:
        assert session.get(BidLicenseLimit, collector._license_key(collector.license_values(old))).superseded_at is not None
        assert session.get(BidNotice, "ORDER").list_visible is False


@pytest.mark.parametrize("second_outcome", ["failed", "halt"])
def test_full_snapshots_saved_on_partial_but_discarded_on_later_halt(
    monkeypatch, db_engine, second_outcome,
):
    _, Local = db_engine
    _seed(Local, *[_existing_notice(no, indstryty_lmt_yn="Y") for no in ("A", "B")],
          *[_stored_license(_license(no)) for no in ("A", "B")])
    with Local() as session:
        session.get(AppConfig, 1).max_retries = 0
        session.commit()

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result()
        if params["inqryDiv"] == "1":
            return _ok_result([_license(no, code="9999") for no in ("A", "B")], 2)
        if params["bidNtceNo"] == "B":
            result = _failed_result()
            if second_outcome == "halt":
                result.result_code = "22"
            return result
        return _ok_result([_license("A", code="9999")], 1)

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    halted = second_outcome == "halt"
    assert run.status == ("failed" if halted else "partial")
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["called"] == 2 and fix["failed"] == ["B"]
    assert fix["superseded"] == (0 if halted else 1)
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit)).all()
        assert len(rows) == (2 if halted else 4)
        superseded = [row for row in rows if row.superseded_at is not None]
        assert [row.bid_ntce_no for row in superseded] == ([] if halted else ["A"])


@pytest.mark.parametrize("failure_point", ["classification", "finish_commit"])
def test_superseded_and_restored_roll_back_with_classification_and_run(
    monkeypatch, db_engine, failure_point,
):
    _, Local = db_engine
    old, restored, new = (_license("ATOMIC", code=code) for code in ("1470", "1468", "9999"))
    _seed(Local, _existing_notice("ATOMIC", indstryty_lmt_yn="Y", list_visible=False),
          _stored_license(old), _stored_license(restored, superseded_at=WIN_BGN))
    original_classify = classifier.reclassify_notices
    original_finish = repository.finish_run

    def responder(op, params):
        rows = [_notice("ATOMIC", bidNtceNm="변경")] if op == collector.OPERATION else (
            [new] if params["inqryDiv"] == "1" else [new, restored])
        return _ok_result(rows, len(rows))

    def check_then_fail(session):
        session.flush()
        old_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(old)))
        restored_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(restored)))
        assert old_row.superseded_at is not None and restored_row.superseded_at is None
        assert session.get(BidNotice, "ATOMIC").match_basis == "업종"
        raise RuntimeError("가짜 대체 표시 커밋 실패")

    def classify_then_fail(session, **kwargs):
        original_classify(session, **kwargs)
        check_then_fail(session)

    def finish_then_fail(session, run, **kwargs):
        detail = json.loads(kwargs["detail_json"])["by_call"]["license_fix"]
        assert detail["superseded"] == detail["restored"] == 1
        event.listen(session, "before_commit", check_then_fail, once=True)
        original_finish(session, run, **kwargs)

    _fake_api(monkeypatch, responder)
    if failure_point == "classification":
        monkeypatch.setattr(classifier, "reclassify_notices", classify_then_fail)
    else:
        monkeypatch.setattr(repository, "finish_run", finish_then_fail)
    with pytest.raises(RuntimeError, match="가짜 대체 표시 커밋 실패"):
        collector.collect_window(WIN_BGN, WIN_END)
    with Local() as session:
        rows = {row.lcns_cd: row for row in session.scalars(select(BidLicenseLimit))}
        assert set(rows) == {"1470", "1468"}
        assert rows["1470"].superseded_at is None and rows["1468"].superseded_at == WIN_BGN
        notice = session.get(BidNotice, "ATOMIC")
        assert notice.list_visible is False and notice.match_basis is None and notice.bid_ntce_nm is None
        run = session.scalars(select(CollectionRun)).one()
        assert run.status == "running" and run.run_finished_at is None and run.detail_json is None
        assert session.get(AppConfig, 1).last_success_dt is None


@pytest.mark.parametrize("full_outcome", [
    "includes", "excludes", "failed", "shortfall", "empty", "halt", "late_halt",
])
def test_bulk_reappearing_superseded_pk_requires_valid_full_snapshot(
    monkeypatch, db_engine, caplog, full_outcome,
):
    _, Local = db_engine
    old = _license("REAPPEAR", lmtGrpNo="1", lmtSno="1")
    active = _license("REAPPEAR", code="9999", lmtGrpNo="1", lmtSno="2")
    _seed(Local, _existing_notice("REAPPEAR", indstryty_lmt_yn="Y", list_visible=False),
          _stored_license(old, superseded_at=WIN_BGN), _stored_license(active))
    with Local() as session:
        cfg = session.get(AppConfig, 1)
        cfg.max_retries = 0
        cfg.num_of_rows = 1 if full_outcome == "late_halt" else 20
        session.commit()

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("REAPPEAR")], 1)
        if params["inqryDiv"] == "1":
            return _ok_result([old], 1)
        if full_outcome == "empty":
            return _ok_result()
        if full_outcome == "shortfall" or (full_outcome == "late_halt" and params["pageNo"] == "1"):
            return _ok_result([old], 2)
        if full_outcome in {"failed", "halt", "late_halt"}:
            result = _failed_result()
            if full_outcome != "failed":
                result.result_code = "22"
            return result
        rows = [active, old] if full_outcome == "includes" else [active]
        return _ok_result(rows, len(rows))

    calls = _fake_api(monkeypatch, responder)
    with caplog.at_level(logging.INFO, logger="app.collector"):
        run = collector.collect_window(WIN_BGN, WIN_END)
    halted = full_outcome in {"halt", "late_halt"}
    assert run.status == ("failed" if halted else "partial" if full_outcome in {"failed", "shortfall"}
                          else "success")
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert fix["verify_targets"] == fix["targets"] == fix["called"] == 1
    assert fix["restored"] == int(full_outcome == "includes") and fix["superseded"] == 0
    assert fix["superseded_notices"] == (["REAPPEAR-000"] if full_outcome == "includes" else [])
    assert f"verify_targets=1 superseded=0 restored={fix['restored']}" in caplog.text
    assert any(params["inqryDiv"] == "2" for _, params in calls)
    with Local() as session:
        rows = {row.lmt_sno: row for row in session.scalars(select(BidLicenseLimit))}
        assert rows["1"].superseded_at == (None if full_outcome == "includes" else WIN_BGN)
        assert rows["2"].superseded_at is None
        assert session.get(BidNotice, "REAPPEAR").list_visible is (full_outcome == "includes")


@pytest.mark.parametrize("failed_stage", ["servc", "license"])
def test_bulk_alone_never_restores_when_full_query_gate_fails(monkeypatch, db_engine, failed_stage):
    _, Local = db_engine
    old = _license("BULK-ONLY")
    _seed(Local, _existing_notice("BULK-ONLY", indstryty_lmt_yn="Y"),
          _stored_license(old, superseded_at=WIN_BGN))

    def responder(op, params):
        assert params["inqryDiv"] == "1"
        stage = "servc" if op == collector.OPERATION else "license"
        rows = [_notice("BULK-ONLY")] if stage == "servc" else [old]
        return _ok_result(rows, 2 if stage == failed_stage else 1)

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "partial" and len(calls) == 2
    assert fix["outcome"] == "skipped" and fix["restored"] == fix["superseded"] == 0
    with Local() as session:
        assert session.scalars(select(BidLicenseLimit)).one().superseded_at == WIN_BGN


@pytest.mark.parametrize("bad_row,reason", [
    ({"bidNtceOrd": "000"}, "변환 불가"),
    ({"bidNtceNo": "INVALID"}, "변환 불가"),
    (None, "변환 불가"),
    (_license("OTHER", lmtSno="3"), "다른 공고·차수"),
    (_license("INVALID", "001", lmtSno="3"), "다른 공고·차수"),
])
def test_full_snapshot_rejects_any_unconvertible_or_wrong_pair_row(
    monkeypatch, db_engine, bad_row, reason,
):
    _, Local = db_engine
    active = _license("INVALID", lmtSno="1")
    old = _license("INVALID", code="1470", lmtSno="2")
    new = _license("INVALID", code="9999", lmtSno="4")
    _seed(Local, _existing_notice("INVALID", indstryty_lmt_yn="Y"),
          _stored_license(active), _stored_license(old, superseded_at=WIN_BGN))

    def responder(op, params):
        rows = [_notice("INVALID")] if op == collector.OPERATION else (
            [new] if params["inqryDiv"] == "1" else [new, old, bad_row])
        return _ok_result(rows, len(rows))

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "partial" and fix["outcome"] == "failed"
    # 기존 13.3 수신 건수 검증은 통과해도 대체·복원용 검증에서 거절한다.
    assert fix["unique_items"] == fix["total_count"] == 3
    assert reason in run.error_msg and reason in fix["error_msg"]
    assert fix["failed"] == ["INVALID"] and fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        assert session.get(BidLicenseLimit, collector._license_key(collector.license_values(active))).superseded_at is None
        assert session.get(BidLicenseLimit, collector._license_key(collector.license_values(old))).superseded_at == WIN_BGN
        assert session.get(BidNotice, "INVALID").list_visible is True
        assert session.get(AppConfig, 1).last_success_dt is None


@pytest.mark.parametrize("stored_ord,response_ord", [("001", "1"), ("A", "00A"), ("00A", "A")])
def test_full_snapshot_same_pk_with_unpadded_order_neither_supersedes_nor_duplicates(
    monkeypatch, db_engine, stored_ord, response_ord,
):
    _, Local = db_engine
    old = _license("SAME-PK", stored_ord, lmtGrpNo="1", lmtSno="1")
    new = _license("SAME-PK", stored_ord, code="9999", lmtGrpNo="1", lmtSno="2")
    notice = _existing_notice("SAME-PK", indstryty_lmt_yn="Y")
    notice.bid_ntce_ord = stored_ord
    old_row = _stored_license(old)
    old_row.bid_ntce_ord = stored_ord  # 기존 비정규 표기의 물리 PK 도 보존한다.
    _seed(Local, notice, old_row)

    def responder(op, params):
        rows = [_notice("SAME-PK", stored_ord)] if op == collector.OPERATION else (
            [new] if params["inqryDiv"] == "1" else [{**old, "bidNtceOrd": response_ord}, new])
        return _ok_result(rows, len(rows))

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "success" and fix["called"] == 1
    assert fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        rows = session.scalars(select(BidLicenseLimit)).all()
        assert len(rows) == 2 and all(row.superseded_at is None for row in rows)
        assert {row.bid_ntce_ord for row in rows} == {stored_ord, new["bidNtceOrd"].zfill(3)}
        assert session.get(BidNotice, "SAME-PK").match_basis == "업종"


@pytest.mark.parametrize("duplicate_hash", [True, False])
def test_full_snapshot_hash_duplicates_cannot_hide_missing_rows(monkeypatch, db_engine, duplicate_hash):
    _, Local = db_engine
    active = _license("HASH-COUNT", lmtGrpNo="1", lmtSno="1")
    hashed = _license("HASH-COUNT", code="9999")
    new = _license("HASH-COUNT", code="9999", lmtGrpNo="1", lmtSno="2")
    third = _license("HASH-COUNT", code="9999", lmtGrpNo="1", lmtSno="3")
    _seed(Local, _existing_notice("HASH-COUNT", indstryty_lmt_yn="Y"),
          _stored_license(active), _stored_license(hashed))
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 1
        session.commit()
    pages = [hashed, hashed if duplicate_hash else third, new]

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("HASH-COUNT")], 1)
        if params["inqryDiv"] == "1":
            return _ok_result([new], 1)
        return _ok_result([pages[int(params["pageNo"]) - 1]], 3)

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == ("partial" if duplicate_hash else "success")
    assert fix["called"] == 1 and len(calls) == 5
    assert fix["unique_items"] == fix["total_count"] == 3
    assert fix["superseded"] == int(not duplicate_hash) and fix["restored"] == 0
    if duplicate_hash:
        assert "변환 후 고유 PK 미달: 고유 2 / 전체 3" in run.error_msg
    with Local() as session:
        row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(active)))
        assert (row.superseded_at is None) is duplicate_hash
        assert session.get(BidNotice, "HASH-COUNT").list_visible is duplicate_hash


@pytest.mark.parametrize("second_total", [2, 3, 1])
def test_full_snapshot_count_change_neither_supersedes_restores_nor_saves(
    monkeypatch, db_engine, second_total,
):
    _, Local = db_engine
    active = _license("COUNT-CHANGE", lmtGrpNo="1", lmtSno="1")
    restored = _license("COUNT-CHANGE", code="1470", lmtGrpNo="1", lmtSno="2")
    new = _license("COUNT-CHANGE", code="9999", lmtGrpNo="1", lmtSno="3")
    _seed(Local, _existing_notice("COUNT-CHANGE", indstryty_lmt_yn="Y",
                                 list_visible=True, match_basis="업종", matched_indstryty_cds="1468"),
          _stored_license(active), _stored_license(restored, superseded_at=WIN_BGN))
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 1
        session.commit()

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result()
        if params["inqryDiv"] == "1":
            return _ok_result([new], 1)
        if params["pageNo"] == "1":
            return _ok_result([{**restored, "lcnsLmtNm": "변경/1470"}], 2)
        return _ok_result([{**new, "lcnsLmtNm": "전체 조회만/9999"}], second_total)

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    changed = second_total != 2
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert len(calls) == 4 and fix["total_count"] == fix["unique_items"] == 2
    assert run.status == ("partial" if changed else "success")
    assert fix.get("total_count_changed", False) is changed
    assert fix["superseded"] == fix["restored"] == int(not changed)
    assert fix["found"] == int(not changed)
    if changed:
        assert "totalCount 변화" in run.error_msg and "totalCount 변화" in fix["error_msg"]
    with Local() as session:
        active_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(active)))
        restored_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(restored)))
        new_row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(new)))
        assert (active_row.superseded_at is None) is changed
        assert restored_row.superseded_at == (WIN_BGN if changed else None)
        assert restored_row.lcns_nm == (collector.license_values(restored)["lcns_nm"] if changed else "변경")
        assert new_row.lcns_nm == (collector.license_values(new)["lcns_nm"] if changed else "전체 조회만")
        notice = session.get(BidNotice, "COUNT-CHANGE")
        assert notice.list_visible is changed
        assert notice.match_basis == ("업종" if changed else None)
        assert session.get(AppConfig, 1).last_success_dt == (None if changed else WIN_END)


@pytest.mark.parametrize("bad_row,reason", [
    (_license("OTHER", lmtSno="3"), "다른 공고·차수"),
    (_license("REJECT", "001", lmtSno="3"), "다른 공고·차수"),
    ({"bidNtceOrd": "000"}, "변환 불가"),
])
def test_rejected_full_response_saves_no_rows_and_keeps_classification(
    monkeypatch, db_engine, bad_row, reason,
):
    _, Local = db_engine
    old = _license("REJECT", code="9999", lmtSno="1")
    new = _license("REJECT", lmtSno="2")
    _seed(Local, _existing_notice("REJECT", indstryty_lmt_yn="Y", list_visible=False),
          _existing_notice("OTHER", indstryty_lmt_yn="Y", list_visible=False),
          _stored_license(old, superseded_at=WIN_BGN))

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("REJECT")], 1)
        if params["inqryDiv"] == "1":
            return _ok_result()
        return _ok_result([{**old, "lcnsLmtNm": "거절된 변경/1468"}, new, bad_row], 3)

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == "partial" and reason in run.error_msg
    assert fix["found"] == fix["superseded"] == fix["restored"] == 0
    with Local() as session:
        row = session.scalars(select(BidLicenseLimit)).one()
        assert row.superseded_at == WIN_BGN and row.lcns_cd == "9999"
        assert row.lcns_nm == collector.license_values(old)["lcns_nm"]
        for no in ("REJECT", "OTHER"):
            notice = session.get(BidNotice, no)
            assert notice.list_visible is False and notice.match_basis is None
            assert notice.matched_indstryty_cds is None
        assert session.get(AppConfig, 1).last_success_dt is None


@pytest.mark.parametrize("existing", ["none", "other_order", "active", "superseded"])
@pytest.mark.parametrize("invalid", [None, "count_change", "wrong_pair", "unconvertible"])
def test_duplicate_hash_snapshot_skips_comparison_only_without_existing_pair(
    monkeypatch, db_engine, existing, invalid,
):
    _, Local = db_engine
    hashed = _license("DUPLICATE")
    old = _license("DUPLICATE", "001" if existing == "other_order" else "000",
                   code="9999", lmtSno="1")
    _seed(Local, _existing_notice("DUPLICATE", indstryty_lmt_yn="Y"))
    if existing != "none":
        _seed(Local, _stored_license(old, superseded_at=WIN_BGN if existing == "superseded" else None))
    with Local() as session:
        session.get(AppConfig, 1).num_of_rows = 1
        session.commit()

    def responder(op, params):
        if op == collector.OPERATION:
            return _ok_result([_notice("DUPLICATE")], 1)
        if params["inqryDiv"] == "1":
            # 유효 행이 있으면 새 PK 로 확인 조회를 유도한다.
            return _ok_result([_license("DUPLICATE", code="9999", lmtSno="2")], 1) if existing == "active" else _ok_result()
        row = hashed
        total = 2
        if params["pageNo"] == "2":
            if invalid == "count_change":
                total = 3
            elif invalid == "wrong_pair":
                row = {**hashed, "bidNtceNo": "OTHER"}
            elif invalid == "unconvertible":
                row = {"bidNtceOrd": "000"}
        return _ok_result([row], total)

    _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    accepted = invalid is None and existing in {"none", "other_order"}
    fix = json.loads(run.detail_json)["by_call"]["license_fix"]
    assert run.status == ("success" if accepted else "partial")
    assert fix["called"] == 1 and fix["superseded"] == fix["restored"] == 0
    assert fix["found"] == int(accepted)
    if invalid is None and not accepted:
        assert "고유 PK 미달" in run.error_msg
    with Local() as session:
        row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(hashed)))
        assert (row is not None) is accepted
        if row is not None:
            assert row.superseded_at is None
        if existing != "none":
            row = session.get(BidLicenseLimit, collector._license_key(collector.license_values(old)))
            assert row.superseded_at == (WIN_BGN if existing == "superseded" else None)
        assert session.get(AppConfig, 1).last_success_dt == (WIN_END if accepted else None)


@pytest.mark.parametrize("stage", ["servc", "license"])
def test_bulk_count_change_only_records_history_without_changing_outcome(monkeypatch, db_engine, stage):
    def responder(op, params):
        current = "servc" if op == collector.OPERATION else "license"
        if current != stage:
            return _ok_result()
        if params["pageNo"] == "1":
            return _ok_result([{"page": 1}] * 999, 1000)
        return _ok_result([{"page": 2}], 1001)

    calls = _fake_api(monkeypatch, responder)
    run = collector.collect_window(WIN_BGN, WIN_END)
    detail = json.loads(run.detail_json)["by_call"][stage]
    assert run.status == "success" and detail["outcome"] == "ok"
    assert len(calls) == 3 and detail["pages"] == 2
    assert detail["total_count"] == detail["unique_items"] == 1000
    assert detail["total_count_changed"] is True and detail["error_msg"] is None
