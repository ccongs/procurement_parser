"""collector 순수 헬퍼 + repository.upsert 단위테스트 — Phase 3.3.

네트워크/실 DB(procurement.db) 비의존. 순수 함수는 직접 호출하고,
upsert 는 인메모리 SQLite 로 검증한다. 실 API 백필은 통합 검증에서 수행.
실행: `pytest tests/test_collector.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import api_client, collector, repository
from app.collector import (
    classify_result_code,
    fmt_dt,
    merge_and_dedup,
    total_pages,
)
from app.collector import _ConfigSnapshot, _fetch_cd
from app.db import Base
from app.models import BidNotice


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


# --- 3. merge_and_dedup ------------------------------------------------
def test_merge_and_dedup_union():
    results_by_cd = {
        "1470": [{"bidNtceNo": "A", "x": 1}, {"bidNtceNo": "B"}],
        "1468": [{"bidNtceNo": "A", "x": 2}, {"bidNtceNo": "C"}],
    }
    items, matched = merge_and_dedup(results_by_cd)

    # 중복 제거 → 3건
    assert {i["bidNtceNo"] for i in items} == {"A", "B", "C"}
    assert len(items) == 3
    # 같은 공고 A 는 처음 본 item("1470" 쪽) 유지
    a_item = next(i for i in items if i["bidNtceNo"] == "A")
    assert a_item["x"] == 1
    # matched 는 두 코드 합집합(정렬 CSV)
    assert matched["A"] == "1468,1470"
    assert matched["B"] == "1470"
    assert matched["C"] == "1468"


def test_merge_and_dedup_skips_blank_pk():
    results_by_cd = {
        "1426": [{"bidNtceNo": "  "}, {"bidNtceNo": None}, {"foo": "bar"}, {"bidNtceNo": "X"}],
    }
    items, matched = merge_and_dedup(results_by_cd)
    assert [i["bidNtceNo"] for i in items] == ["X"]
    assert matched == {"X": "1426"}


# --- 4. fmt_dt ---------------------------------------------------------
def test_fmt_dt():
    assert fmt_dt(datetime(2025, 7, 1, 9, 0)) == "202507010900"
    assert fmt_dt(datetime(2026, 12, 31, 23, 59)) == "202612312359"


# --- 4.5 _fetch_cd base_params: 참가제한지역 prtcptLmtRgnCd (Phase 4.3) ---
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
    """_fetch_cd 가 만드는 첫 페이지 params(=base_params + pageNo)를 캡처.

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

    def _fake_call_with_retry(params, max_retries):
        captured.update(params)
        return fake_result, 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", _fake_call_with_retry)
    _fetch_cd("1468", datetime(2026, 5, 1, 0, 0), datetime(2026, 6, 1, 0, 0), snapshot)
    return captured


def test_fetch_cd_base_params_includes_region_when_set(monkeypatch):
    """prtcpt_lmt_rgn_cd='00' 이면 base_params 에 prtcptLmtRgnCd='00' 포함."""
    params = _capture_base_params(monkeypatch, _snapshot("00"))
    assert params["prtcptLmtRgnCd"] == "00"
    # build_params(12번) 통과 후에도 '00' 은 살아남는다(전송됨).
    built = api_client.build_params(collector.OPERATION, params, "json")
    assert built["prtcptLmtRgnCd"] == "00"


def test_fetch_cd_base_params_region_specific_code(monkeypatch):
    """특정 지역코드(예: 28=인천)도 그대로 base_params·build_params 에 들어간다."""
    params = _capture_base_params(monkeypatch, _snapshot("28"))
    assert params["prtcptLmtRgnCd"] == "28"
    built = api_client.build_params(collector.OPERATION, params, "json")
    assert built["prtcptLmtRgnCd"] == "28"


def test_fetch_cd_base_params_drops_region_when_blank(monkeypatch):
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
    new, updated = repository.upsert_bid_notices(
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
    assert _count(session) == 1

    # 같은 PK 재삽입 → update. collected_at 보존, 나머지 갱신.
    new, updated = repository.upsert_bid_notices(
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
    assert _count(session) == 1  # 행 수 그대로

    row = session.get(BidNotice, "R26BK0001")
    assert row.bid_ntce_nm == "변경된 공고명"
    assert row.matched_indstryty_cds == "1468,1470"
    assert row.collected_at == t1  # 최초 수집 시각 보존
    assert row.updated_at == t2  # 갱신


def test_upsert_empty_list(session):
    assert repository.upsert_bid_notices(session, []) == (0, 0)
    assert _count(session) == 0


def test_upsert_mixed_new_and_existing(session):
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_bid_notices(
        session,
        [{"bid_ntce_no": "P1", "collected_at": t, "updated_at": t}],
    )
    new, updated = repository.upsert_bid_notices(
        session,
        [
            {"bid_ntce_no": "P1", "collected_at": t, "updated_at": t},  # 기존
            {"bid_ntce_no": "P2", "collected_at": t, "updated_at": t},  # 신규
        ],
    )
    assert (new, updated) == (1, 1)
    assert _count(session) == 2
