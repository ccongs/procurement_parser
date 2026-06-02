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
from app.models import AppConfig, CollectionRun

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
def test_partial_when_one_cd_fails(monkeypatch, db_engine):
    """outcome='failed' 코드가 1개 이상 있으면 status='partial'.

    이 상태에서 last_success_dt 는 갱신하지 않는다(다음 회차 재수집).
    """
    engine, TestSession = db_engine

    call_count = [0]

    def fake_call_with_retry(params, max_retries):
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

    def fake_call_with_retry(params, max_retries):
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
    """모든 코드가 ok → status='success' + last_success_dt=window_end."""
    engine, TestSession = db_engine

    def fake_call_with_retry(params, max_retries):
        return _ok_result(), 0, "ok"  # No Data(03)

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.status == "success"
    with TestSession() as s:
        cfg = s.get(AppConfig, 1)
        assert cfg.last_success_dt == WIN_END


# --- 4. detail_json 구조 확인 — by_cd / ord_changes / ord_changed_count --
def test_detail_json_structure(monkeypatch, db_engine):
    """collect_window 완료 후 run.detail_json 이 4.7 구조를 가진다."""
    engine, TestSession = db_engine

    def fake_call_with_retry(params, max_retries):
        return _ok_result(), 0, "ok"

    monkeypatch.setattr(collector, "_call_with_retry", fake_call_with_retry)

    run = collector.collect_window(WIN_BGN, WIN_END, trigger="test")

    assert run is not None
    assert run.detail_json is not None
    data = json.loads(run.detail_json)
    assert "by_cd" in data
    assert "ord_changes" in data
    assert "ord_changed_count" in data
    assert isinstance(data["by_cd"], list)
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

    def fake_call_with_retry(params, max_retries):
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
