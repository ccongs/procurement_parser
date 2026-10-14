"""collector 순수 헬퍼 + repository.upsert 단위테스트 — Phase 3.3.

네트워크/실 DB(procurement.db) 비의존. 순수 함수는 직접 호출하고,
upsert 는 인메모리 SQLite 로 검증한다. 실 API 백필은 통합 검증에서 수행.
실행: `pytest tests/test_collector.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import api_client, collector, repository
from app.collector import (
    classify_result_code,
    fmt_dt,
    dedup_notices,
    total_pages,
)
from app.collector import _ConfigSnapshot, _fetch_pages, _servc_params
from app.db import Base
from app.models import BidNotice, BidNoticeRaw


# --- 1. classify_result_code -------------------------------------------
def test_classify_ok():
    assert classify_result_code("00") == "ok"
    assert classify_result_code("03") == "ok"


def test_classify_retry():
    for code in ("01", "02", "04", "05"):
        assert classify_result_code(code) == "retry"
    # None(HTTP/파싱 실패)도 retry 에 준함
    assert classify_result_code(None) == "retry"


def test_classify_halt():
    for code in ("06", "07", "08", "10", "11", "12", "20", "22", "30", "31", "32"):
        assert classify_result_code(code) == "halt"


# --- 2. total_pages ----------------------------------------------------
def test_total_pages():
    assert total_pages(17, 20) == 1
    assert total_pages(20, 20) == 1
    assert total_pages(40, 20) == 2
    assert total_pages(41, 20) == 3
    assert total_pages(0, 20) == 0
    # 문자열 total_count(응답이 str) 방어
    assert total_pages("41", 20) == 3
    # 비정상 입력 → 0
    assert total_pages(None, 20) == 0
    assert total_pages("abc", 20) == 0


# --- 3. 최신 차수 중복 제거 --------------------------------------------
@pytest.mark.parametrize("orders", [("000", "001"), ("9", "10"), ("A", "00B")])
def test_dedup_notices_keeps_highest_order(orders):
    items = dedup_notices([
        {"bidNtceNo": "A", "bidNtceOrd": orders[0]},
        {"bidNtceNo": "B"},
        {"bidNtceNo": "A", "bidNtceOrd": orders[1]},
    ])
    assert len(items) == 2
    assert items[0]["bidNtceOrd"] == orders[1]


def test_dedup_notices_skips_blank_pk():
    items = dedup_notices([
        {"bidNtceNo": "  "}, {"bidNtceNo": None}, {"foo": "bar"}, {"bidNtceNo": "X"},
    ])
    assert [i["bidNtceNo"] for i in items] == ["X"]


@pytest.mark.parametrize("orders", [
    ("0001", "1", "001"), ("0001", "001", "1"),
    ("1", "0001", "001"), ("1", "001", "0001"),
    ("001", "0001", "1"), ("001", "1", "0001"),
])
def test_dedup_notices_recognizes_equivalent_order_notation(orders):
    notices = [{"bidNtceNo": "SAME", "bidNtceOrd": order} for order in orders]
    assert dedup_notices(notices) == [notices[0]]


# --- 4. fmt_dt ---------------------------------------------------------
def test_fmt_dt():
    assert fmt_dt(datetime(2025, 7, 1, 9, 0)) == "202507010900"
    assert fmt_dt(datetime(2026, 12, 31, 23, 59)) == "202612312359"


# --- 4.5 _fetch_pages base_params: 참가제한지역 prtcptLmtRgnCd (Phase 4.3) ---
def _snapshot(prtcpt_lmt_rgn_cd):
    """테스트용 _ConfigSnapshot — 참가제한지역만 가변, 나머지는 고정."""
    return _ConfigSnapshot(
        inqry_div="1",
        intrntnl_div_cd="1",
        prtcpt_lmt_rgn_cd=prtcpt_lmt_rgn_cd,
        num_of_rows=20,
        max_retries=2,
    )


def _capture_base_params(monkeypatch, snapshot):
    """_fetch_pages 가 만드는 첫 페이지 params(=base_params + pageNo)를 캡처.

    네트워크 없이: _call_with_retry 를 가짜로 바꿔 params 를 기록하고
    result_code='03'(No Data) 을 돌려 즉시 종료시킨다.
    """
    captured: dict = {}

    fake_result = api_client.ApiResult(
        operation=collector.OPERATION,
        request_url="",
        sent_params={},
        response_type="json",
        status_code=200,
        raw_text="",
        parsed=None,
        result_code="03",  # No Data → 1페이지 후 종료
        result_msg="no data",
        items=[],
        total_count="0",
    )

    def _fake_call_with_retry(operation, params, max_retries):
        captured.update(params)
        return fake_result, 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", _fake_call_with_retry)
    params = _servc_params(datetime(2026, 5, 1), datetime(2026, 6, 1), snapshot)
    _fetch_pages(collector.OPERATION, params, snapshot)
    assert "indstrytyCd" not in captured
    assert captured["inqryDiv"] == snapshot.inqry_div
    assert captured["intrntnlDivCd"] == snapshot.intrntnl_div_cd
    assert captured["numOfRows"] == "999"
    assert captured["inqryBgnDt"] == "202605010000"
    assert captured["inqryEndDt"] == "202606010000"
    return captured


def test_fetch_pages_base_params_includes_region_when_set(monkeypatch):
    """prtcpt_lmt_rgn_cd='00' 이면 base_params 에 prtcptLmtRgnCd='00' 포함."""
    params = _capture_base_params(monkeypatch, _snapshot("00"))
    assert params["prtcptLmtRgnCd"] == "00"
    # build_params(12번) 통과 후에도 '00' 은 살아남는다(전송됨).
    built = api_client.build_params(collector.OPERATION, params, "json")
    assert built["prtcptLmtRgnCd"] == "00"


def test_fetch_pages_base_params_region_specific_code(monkeypatch):
    """특정 지역코드(예: 28=인천)도 그대로 base_params·build_params 에 들어간다."""
    params = _capture_base_params(monkeypatch, _snapshot("28"))
    assert params["prtcptLmtRgnCd"] == "28"
    built = api_client.build_params(collector.OPERATION, params, "json")
    assert built["prtcptLmtRgnCd"] == "28"


def test_fetch_pages_base_params_drops_region_when_blank(monkeypatch):
    """prtcpt_lmt_rgn_cd 가 None/'' 이면 base_params 는 빈 문자열, build_params 통과 후 제거(=요청에서 빠짐)."""
    for blank in (None, ""):
        params = _capture_base_params(monkeypatch, _snapshot(blank))
        # base_params 단계에서는 빈 문자열로 들어감(intrntnlDivCd 와 동일 패턴).
        assert params["prtcptLmtRgnCd"] == ""
        # build_params(12번)가 빈값을 제거 → 요청에서 빠진다.
        built = api_client.build_params(collector.OPERATION, params, "json")
        assert "prtcptLmtRgnCd" not in built


# --- 5. upsert_bid_notices (인메모리 SQLite) ----------------------------
@pytest.fixture
def session():
    """인메모리 SQLite 세션. 같은 스레드에서 테이블·데이터가 유지된다."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(BidNotice)).scalar_one()


def test_upsert_insert_then_update_preserves_collected_at(session):
    t1 = datetime(2026, 6, 1, 10, 0, 0)
    t2 = datetime(2026, 6, 1, 11, 0, 0)

    # 최초 insert
    new, updated, ord_changes = repository.upsert_bid_notices(
        session,
        [
            {
                "bid_ntce_no": "R26BK0001",
                "bid_ntce_nm": "최초 공고명",
                "matched_indstryty_cds": "1468",
                "collected_at": t1,
                "updated_at": t1,
            }
        ],
    )
    assert (new, updated) == (1, 0)
    assert ord_changes == []  # insert 는 차수변경 아님
    assert _count(session) == 1

    # 같은 PK 재삽입 → update. collected_at 보존, 나머지 갱신.
    new, updated, ord_changes = repository.upsert_bid_notices(
        session,
        [
            {
                "bid_ntce_no": "R26BK0001",
                "bid_ntce_nm": "변경된 공고명",
                "matched_indstryty_cds": "1468,1470",
                "collected_at": t2,  # 무시되어야 함
                "updated_at": t2,
            }
        ],
    )
    assert (new, updated) == (0, 1)
    assert ord_changes == []  # bid_ntce_ord 없이 업데이트 — 차수변경 없음
    assert _count(session) == 1  # 행 수 그대로

    row = session.get(BidNotice, "R26BK0001")
    assert row.bid_ntce_nm == "변경된 공고명"
    assert row.matched_indstryty_cds == "1468,1470"
    assert row.collected_at == t1  # 최초 수집 시각 보존
    assert row.updated_at == t2  # 갱신


def test_upsert_empty_list(session):
    new, updated, ord_changes = repository.upsert_bid_notices(session, [])
    assert (new, updated, ord_changes) == (0, 0, [])
    assert _count(session) == 0


def test_upsert_raw_insert_update_and_duplicate_pk(session):
    t1 = datetime(2026, 6, 1, 10)
    t2 = datetime(2026, 6, 1, 11)
    base = {"bid_ntce_no": "RAW-13", "collected_at": t1, "updated_at": t1}
    assert repository.upsert_bid_notices(session, [{**base, "raw_json": "first"}])[:2] == (1, 0)
    session.commit()
    assert session.get(BidNotice, "RAW-13").raw_json is None
    assert session.get(BidNoticeRaw, "RAW-13").raw_json == "first"
    assert session.get(BidNoticeRaw, "RAW-13").updated_at == t1

    # 각 테이블을 IN 한 번으로 조회하고, 같은 배치 중복은 마지막 원문으로 갱신.
    selects = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = repository.upsert_bid_notices(session, [
            {**base, "updated_at": t2, "raw_json": "second"},
            {**base, "updated_at": t2, "raw_json": "last"},
            {**base, "bid_ntce_no": "RAW-NEW", "raw_json": "new-first"},
            {**base, "bid_ntce_no": "RAW-NEW", "raw_json": "new-last"},
        ])
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert result[:2] == (1, 3)
    assert len(selects) == 2 and all(" IN " in sql for sql in selects)
    session.commit()
    row = session.get(BidNotice, "RAW-13")
    raw = session.get(BidNoticeRaw, "RAW-13")
    assert row.raw_json is None and row.collected_at == t1
    assert raw.raw_json == "last" and raw.updated_at == t2
    assert session.get(BidNotice, "RAW-NEW").raw_json is None
    assert session.get(BidNoticeRaw, "RAW-NEW").raw_json == "new-last"


def test_upsert_notice_and_raw_rollback_together(session):
    now = datetime(2026, 6, 1)
    repository.upsert_bid_notices(session, [{
        "bid_ntce_no": "ROLLBACK-13", "raw_json": "original",
        "collected_at": now, "updated_at": now,
    }])
    session.rollback()
    assert session.get(BidNotice, "ROLLBACK-13") is None
    assert session.get(BidNoticeRaw, "ROLLBACK-13") is None


def test_upsert_without_raw_preserves_legacy_raw_until_replacement(session):
    t1 = datetime(2026, 6, 1, 10)
    t2 = datetime(2026, 6, 1, 11)
    session.add(BidNotice(
        bid_ntce_no="LEGACY-RAW-13", raw_json="legacy",
        collected_at=t1, updated_at=t1,
    ))
    session.commit()
    values = {"bid_ntce_no": "LEGACY-RAW-13", "bid_ntce_nm": "갱신", "updated_at": t2}
    assert repository.upsert_bid_notices(session, [values])[:2] == (0, 1)
    session.commit()
    row = session.get(BidNotice, "LEGACY-RAW-13")
    assert row.bid_ntce_nm == "갱신" and row.updated_at == t2
    assert row.raw_json == "legacy"
    assert session.get(BidNoticeRaw, "LEGACY-RAW-13") is None

    repository.upsert_bid_notices(session, [{**values, "raw_json": "replacement"}])
    session.commit()
    assert session.get(BidNotice, "LEGACY-RAW-13").raw_json is None
    assert session.get(BidNoticeRaw, "LEGACY-RAW-13").raw_json == "replacement"


def test_upsert_mixed_new_and_existing(session):
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "P1", "collected_at": t, "updated_at": t}],
    )
    new, updated, ord_changes = repository.upsert_bid_notices(
        session,
        [
            {"bid_ntce_no": "P1", "collected_at": t, "updated_at": t},  # 기존
            {"bid_ntce_no": "P2", "collected_at": t, "updated_at": t},  # 신규
        ],
    )
    assert (new, updated) == (1, 1)
    assert ord_changes == []  # bid_ntce_ord 없음 → 차수변경 없음
    assert _count(session) == 2


# --- 6. 차수변경(bidNtceOrd) 감지 테스트 (Phase 4.7) -------------------
def test_upsert_ord_change_detected(session):
    """기존 ord='1' → 새 ord='2': ord_changes 에 기록, 행 갱신."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD001", "bid_ntce_ord": "1", "collected_at": t, "updated_at": t}],
    )
    new, updated, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD001", "bid_ntce_ord": "2", "collected_at": t, "updated_at": t}],
    )
    assert (new, updated) == (0, 1)
    assert ord_changes == [{"no": "ORD001", "old": "1", "new": "2"}]
    row = session.get(BidNotice, "ORD001")
    assert row.bid_ntce_ord == "2"


def test_upsert_ord_no_change_same_value(session):
    """ord '2' → '2': 변경 없음 → ord_changes 비어 있음."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD002", "bid_ntce_ord": "2", "collected_at": t, "updated_at": t}],
    )
    _, _, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD002", "bid_ntce_ord": "2", "collected_at": t, "updated_at": t}],
    )
    assert ord_changes == []


def test_upsert_ord_no_change_int_normalization(session):
    """'1' 저장 후 '01' 로 업데이트 → 정규화 후 동일(int 1) → 오탐 없음."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD003", "bid_ntce_ord": "1", "collected_at": t, "updated_at": t}],
    )
    _, _, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD003", "bid_ntce_ord": "01", "collected_at": t, "updated_at": t}],
    )
    assert ord_changes == []


def test_upsert_ord_none_new_not_recorded(session):
    """new 가 None 이면 차수 변경으로 기록하지 않는다."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD004", "bid_ntce_ord": "1", "collected_at": t, "updated_at": t}],
    )
    _, _, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD004", "bid_ntce_ord": None, "collected_at": t, "updated_at": t}],
    )
    assert ord_changes == []


def test_upsert_ord_empty_string_new_not_recorded(session):
    """new 가 빈 문자열이면 차수 변경으로 기록하지 않는다."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD005", "bid_ntce_ord": "1", "collected_at": t, "updated_at": t}],
    )
    _, _, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD005", "bid_ntce_ord": "", "collected_at": t, "updated_at": t}],
    )
    assert ord_changes == []


def test_upsert_ord_old_none_new_value_recorded(session):
    """old 가 None(미정)이고 new 가 유효값이면 기록(차수 신규 부여)."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    # bid_ntce_ord 없이 최초 삽입 → None
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD006", "collected_at": t, "updated_at": t}],
    )
    _, _, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD006", "bid_ntce_ord": "1", "collected_at": t, "updated_at": t}],
    )
    assert len(ord_changes) == 1
    assert ord_changes[0]["no"] == "ORD006"
    assert ord_changes[0]["old"] is None
    assert ord_changes[0]["new"] == "1"


def test_upsert_insert_not_counted_as_ord_change(session):
    """신규 insert 는 차수변경 아님 — ord 값이 있어도 ord_changes 비어 있음."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    new, updated, ord_changes = repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "ORD007", "bid_ntce_ord": "3", "collected_at": t, "updated_at": t}],
    )
    assert (new, updated) == (1, 0)
    assert ord_changes == []


# --- 7. _build_detail_json 구조 테스트 (Phase 4.7) ----------------------
from app.collector import _build_detail_json  # noqa: E402


def test_build_detail_json_structure():
    import json
    calls = {"servc": {"outcome": "ok", "items": 1, "notices": 1}}
    changes = [{"no": "ABC", "old": "1", "new": "2"}]
    stats = {"visible": 1}
    data = json.loads(_build_detail_json(calls, changes, stats))
    assert data == {"by_call": calls, "classify": stats,
                    "ord_changes": changes, "ord_changed_count": 1}


def test_build_detail_json_empty_ord_changes():
    import json
    data = json.loads(_build_detail_json({}, []))
    assert data["ord_changes"] == []
    assert data["ord_changed_count"] == 0
    assert data["classify"] is None


# --- Phase 13.3 면허 변환·저장 -----------------------------------------
@pytest.mark.parametrize("name, code, expected_name", [
    (" 소프트웨어사업자/1468 ", "1468", "소프트웨어사업자"),
    (" 이름만 ", None, "이름만"),
    ("이름/분야 / 1468", "1468", "이름/분야"),
])
def test_license_values(name, code, expected_name):
    row = collector.license_values({
        "bidNtceNo": " A ", "bidNtceOrd": " 000 ", "lmtGrpNo": " 1 ", "lmtSno": " 2 ",
        "lcnsLmtNm": name, "permsnIndstrytyList": "[A/1253],[B/1143],[C/1253]",
        "rgstDt": "2026-05-01 09:10:11",
    })
    assert row == {
        "bid_ntce_no": "A", "bid_ntce_ord": "000", "lmt_grp_no": "1", "lmt_sno": "2",
        "lcns_cd": code, "lcns_nm": expected_name, "permsn_cds": "1253,1143",
        "rgst_dt": datetime(2026, 5, 1, 9, 10, 11),
    }


def test_license_values_missing_fields():
    for item in ({}, {"bidNtceNo": "A"}, {"bidNtceNo": " ", "bidNtceOrd": "000"}):
        assert collector.license_values(item) is None
    for date in (None, "bad date"):
        row = collector.license_values({"bidNtceNo": "A", "bidNtceOrd": "000", "rgstDt": date})
        assert row["rgst_dt"] is None and row["permsn_cds"] is None


@pytest.mark.parametrize("fields, expected_group, expected_sequence", [
    ({}, "", "hca738a74f"),
    ({"lmtGrpNo": None, "lmtSno": "  "}, "", "hca738a74f"),
    ({"lmtGrpNo": "1"}, "1", "h9ca985a7c"),
    ({"lmtSno": "2"}, "", "hda3c40e7c"),
    ({"lcnsLmtNm": "면허/9999"}, "", "hab5757036"),
    ({"lcnsLmtNm": "다른 면허/1468"}, "", "h3395c4384"),
    ({"permsnIndstrytyList": "[허용/9999]"}, "", "h041691ff9"),
    ({"lmtGrpNo": "2"}, "2", "h4735eb080"),
    ({"lmtSno": "3"}, "", "h893a849d0"),
    ({"lmtGrpNo": " 1 ", "lmtSno": " 2 "}, "1", "2"),
])
def test_license_values_content_key_for_optional_fields(fields, expected_group, expected_sequence):
    row = collector.license_values({
        "bidNtceNo": "A", "bidNtceOrd": "000", "lcnsLmtNm": "면허/1468", **fields,
    })
    assert (row["lmt_grp_no"], row["lmt_sno"]) == (expected_group, expected_sequence)
    assert len(row["lmt_sno"]) <= 10


def test_upsert_notice_defaults_hidden_and_preserves_classification(session):
    now = datetime(2026, 5, 1)
    repository.upsert_bid_notices(session, [{
        "bid_ntce_no": "NEW", "collected_at": now, "updated_at": now,
    }])
    assert session.get(BidNotice, "NEW").list_visible is False
    row = session.get(BidNotice, "NEW")
    row.list_visible, row.match_basis, row.matched_indstryty_cds = True, "업종", "1468"
    repository.upsert_bid_notices(session, [{"bid_ntce_no": "NEW", "bid_ntce_nm": "갱신"}])
    assert (row.list_visible, row.match_basis, row.matched_indstryty_cds) == (True, "업종", "1468")
    session.rollback()
    assert session.get(BidNotice, "NEW") is None


def test_upsert_license_limits_batched_last_wins_and_preserves_timestamp(session):
    from app.models import BidLicenseLimit
    t1, t2 = datetime(2026, 5, 1), datetime(2026, 5, 2)
    rows = [collector.license_values({
        "bidNtceNo": f"A-{i}", "bidNtceOrd": "000", "lcnsLmtNm": "이름/1468",
    }) for i in range(501)]
    assert repository.upsert_license_limits(session, rows, t1) == (501, 0)
    session.commit()
    selects = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append((statement, parameters))

    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = repository.upsert_license_limits(session, rows + [{**rows[0], "lcns_cd": "1470"}], t2)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert result == (0, 501)
    assert len(selects) == 3
    assert all(" IN " in sql and len(params) <= 800 for sql, params in selects)
    key = collector._license_key(rows[0])
    row = session.get(BidLicenseLimit, key)
    assert row.collected_at == t1 and row.lcns_cd == "1470"
    session.rollback()
    assert session.get(BidLicenseLimit, key).lcns_cd == "1468"
    assert repository.upsert_license_limits(session, [], t2) == (0, 0)


@pytest.mark.parametrize("order,expected", [
    ("1", "001"), (" 0001 ", "001"), ("001", "001"), ("1234", "1234"),
    (" A ", "00A"), (" 00A ", "00A"),
])
def test_license_values_normalizes_stored_order(order, expected):
    from app import classifier
    from app.ordering import normalize_order
    row = collector.license_values({"bidNtceNo": "ORDER", "bidNtceOrd": order})
    assert row["bid_ntce_ord"] == expected
    assert normalize_order(row["bid_ntce_ord"]) == normalize_order(order)
    assert classifier.normalize_order is normalize_order


@pytest.mark.parametrize("stored_ord", ["001", "1"])
def test_license_upsert_preserves_superseded_state_even_if_input_overrides_it(session, stored_ord):
    from app.models import BidLicenseLimit
    t1, t2 = datetime(2026, 5, 1), datetime(2026, 5, 2)
    values = collector.license_values({
        "bidNtceNo": "RESTORE", "bidNtceOrd": "001", "lmtGrpNo": "1", "lmtSno": "1",
        "lcnsLmtNm": "이름/1468",
    })
    row = BidLicenseLimit(**{**values, "bid_ntce_ord": stored_ord}, collected_at=t1, superseded_at=t1)
    session.add(row)
    session.flush()
    assert repository.upsert_license_limits(session, [
        {**values, "bid_ntce_ord": "1", "superseded_at": None, "lcns_nm": "변경"},
    ], t2) == (0, 1)
    assert row.superseded_at == t1 and row.collected_at == t1 and row.lcns_nm == "변경"
    assert repository.upsert_license_limits(session, [
        {**values, "lmt_sno": "2", "superseded_at": t1},
    ], t2) == (1, 0)
    assert session.get(BidLicenseLimit, ("RESTORE", "001", "1", "2")).superseded_at is None


def test_reconcile_batches_notices_and_counts_only_final_state_changes(session):
    from app.models import BidLicenseLimit
    t1, t2 = datetime(2026, 5, 1), datetime(2026, 5, 2)
    snapshots = []
    for i in range(501):
        no = f"RECONCILE-{i}"
        # 기존 비정규 숫자 차수도 보존 PK 비교에서는 정규화한다.
        session.add_all([
            BidLicenseLimit(bid_ntce_no=no, bid_ntce_ord="1", lmt_grp_no="1", lmt_sno="1",
                            collected_at=t1, superseded_at=t1),
            BidLicenseLimit(bid_ntce_no=no, bid_ntce_ord="001", lmt_grp_no="1", lmt_sno="2",
                            collected_at=t1),
            BidLicenseLimit(bid_ntce_no=no, bid_ntce_ord="002", lmt_grp_no="1", lmt_sno="1",
                            collected_at=t1),
        ])
        snapshots.append((no, "001", {(no, "001", "1", "1")}))
    session.flush()
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement, parameters))

    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        changes = repository.reconcile_license_limits(session, snapshots, t2)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert len(queries) == 2 and all(" IN " in sql and len(params) <= 500 for sql, params in queries)
    assert changes == {"superseded": 501, "restored": 501, "superseded_notices_count": 501,
                       "superseded_notices": sorted(f"RECONCILE-{i}-001" for i in range(501))[:50]}
    assert session.get(BidLicenseLimit, ("RECONCILE-0", "1", "1", "1")).superseded_at is None
    assert session.get(BidLicenseLimit, ("RECONCILE-0", "001", "1", "2")).superseded_at == t2
    assert session.get(BidLicenseLimit, ("RECONCILE-0", "002", "1", "1")).superseded_at is None
    assert repository.reconcile_license_limits(session, snapshots, t2) == {
        "superseded": 0, "restored": 0, "superseded_notices": [], "superseded_notices_count": 0,
    }
    assert repository.reconcile_license_limits(session, [("RECONCILE-0", "001", set())], t2) == {
        "superseded": 0, "restored": 0, "superseded_notices": [], "superseded_notices_count": 0,
    }


def test_keep_licenses_queries_candidates_in_batches(session):
    now = datetime(2026, 5, 1)
    session.add(BidNotice(bid_ntce_no="OLD", collected_at=now, updated_at=now))
    session.commit()
    items = [{"bidNtceNo": f"OTHER-{i}", "bidNtceOrd": "000"} for i in range(1001)]
    items += [{"bidNtceNo": no, "bidNtceOrd": "000"} for no in ("OLD", "NEW")]
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append((statement, parameters))

    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        kept = collector._keep_licenses(session, items, {"NEW"})
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert {row["bid_ntce_no"] for row in kept.values()} == {"OLD", "NEW"}
    assert len(queries) == 3
    assert all(" IN " in sql and len(params) <= 500 for sql, params in queries)


def test_license_endpoint_params_and_base_url():
    raw = {"inqryDiv": "2", "inqryBgnDt": "202605010000", "inqryEndDt": "202606010000",
           "bidNtceNo": "A", "bidNtceOrd": "000", "pageNo": "1", "numOfRows": "20"}
    built = api_client.build_params(collector.LICENSE_OPERATION, {**raw, "indstrytyCd": "1468",
                                                              "intrntnlDivCd": "1"}, "json")
    assert built == {**raw, "type": "json"}
    assert api_client.base_url_for(collector.LICENSE_OPERATION) == api_client.BASE_URL


@pytest.mark.parametrize("source", ["bulk", "db"])
@pytest.mark.parametrize("notice_order", ["0001", "1", "001"])
@pytest.mark.parametrize("license_order", ["0001", "1", "001"])
def test_missing_license_targets_recognizes_padded_current_order(
    session, source, notice_order, license_order,
):
    from app.models import BidLicenseLimit
    notices = [{"bidNtceNo": "SAME", "bidNtceOrd": notice_order, "indstrytyLmtYn": "Y"}]
    row = collector.license_values({"bidNtceNo": "SAME", "bidNtceOrd": license_order})
    licenses = {}
    if source == "bulk":
        licenses[collector._license_key(row)] = row
    else:
        session.add(BidLicenseLimit(**row, collected_at=datetime(2026, 5, 1)))
        session.commit()
    assert collector._missing_license_targets(session, notices, licenses) == []
