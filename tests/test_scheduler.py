"""scheduler 단위테스트 — Phase 3.4.

네트워크/실 DB(procurement.db)·실제 타이머 비의존. 순수 헬퍼(compute_window/should_run)와
tick 의 분기(게이트·위임·예외 삼킴)만 검증한다. APScheduler 의 실제 주기 동작은
통합 검증(§4)에서 짧은 주기로 확인한다.
실행: `pytest tests/test_scheduler.py`
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import scheduler


# --- 1. compute_window --------------------------------------------------
def test_compute_window_backfill_when_no_last_success():
    now = datetime(2026, 6, 1, 12, 0)
    bgn, end = scheduler.compute_window(None, now, overlap_minutes=90, backfill_days=30)
    assert end == now
    assert bgn == now - timedelta(days=30)


def test_compute_window_overlap_when_last_success():
    now = datetime(2026, 6, 1, 12, 0)
    last_success = datetime(2026, 6, 1, 11, 0)
    bgn, end = scheduler.compute_window(
        last_success, now, overlap_minutes=90, backfill_days=30
    )
    assert end == now
    assert bgn == last_success - timedelta(minutes=90)  # 10:30


def test_compute_window_clamps_future_last_success():
    now = datetime(2026, 6, 1, 12, 0)
    # 미래 last_success(설정 이상) + overlap 0 → bgn 이 end 보다 뒤 → 클램프
    last_success = datetime(2026, 6, 1, 13, 0)
    bgn, end = scheduler.compute_window(
        last_success, now, overlap_minutes=0, backfill_days=30
    )
    assert end == now
    assert bgn == now  # 클램프


# --- 2. should_run ------------------------------------------------------
def test_should_run_truth_table():
    assert scheduler.should_run(True, False) is True
    assert scheduler.should_run(False, False) is False
    assert scheduler.should_run(True, True) is False
    assert scheduler.should_run(False, True) is False


# --- 3. tick 분기(monkeypatch) -----------------------------------------
class _FakeConfig:
    """tick 이 읽는 설정 필드만 가진 가짜 config."""

    def __init__(
        self,
        *,
        enabled=True,
        auto_halted=False,
        halt_code=None,
        window_overlap_minutes=90,
        backfill_days=30,
        last_success_dt=None,
    ):
        self.enabled = enabled
        self.auto_halted = auto_halted
        self.halt_code = halt_code
        self.window_overlap_minutes = window_overlap_minutes
        self.backfill_days = backfill_days
        self.last_success_dt = last_success_dt


class _DummySession:
    """SessionLocal() 대체 — with 컨텍스트만 만족하면 된다(DB 비접근)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_tick(monkeypatch, config, collect_fn=None):
    """tick 의 외부 의존(SessionLocal·get_config·collect_window)을 가짜로 교체.

    반환: collect_window 호출 인자를 모으는 리스트(calls).
    """
    calls: list[tuple] = []

    def default_collect(window_bgn, window_end, trigger="manual"):
        calls.append((window_bgn, window_end, trigger))

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(scheduler.repository, "get_config", lambda session: config)
    monkeypatch.setattr(
        scheduler.collector, "collect_window", collect_fn or default_collect
    )
    return calls


def test_tick_skips_when_disabled(monkeypatch):
    calls = _patch_tick(monkeypatch, _FakeConfig(enabled=False))
    scheduler.tick()
    assert calls == []  # collect_window 미호출


def test_tick_skips_when_halted(monkeypatch):
    calls = _patch_tick(monkeypatch, _FakeConfig(auto_halted=True, halt_code="30"))
    scheduler.tick()
    assert calls == []


def test_tick_runs_when_enabled_and_passes_window(monkeypatch):
    last_success = datetime(2026, 6, 1, 11, 0)
    now = datetime(2026, 6, 1, 12, 0)  # now 를 주입해 윈도우를 결정적으로 검증(flaky 방지)
    config = _FakeConfig(
        enabled=True,
        auto_halted=False,
        window_overlap_minutes=90,
        backfill_days=30,
        last_success_dt=last_success,
    )
    calls = _patch_tick(monkeypatch, config)

    scheduler.tick(now=now)

    assert len(calls) == 1
    window_bgn, window_end, trigger = calls[0]
    assert trigger == "scheduled"
    # 주입한 now 기준 양끝이 결정적: bgn=last_success-overlap, end=now
    assert window_bgn == last_success - timedelta(minutes=90)  # 09:30
    assert window_end == now


def test_tick_swallows_collect_exception(monkeypatch):
    def boom(window_bgn, window_end, trigger="manual"):
        raise RuntimeError("collect 실패")

    _patch_tick(monkeypatch, _FakeConfig(enabled=True), collect_fn=boom)
    # 예외가 tick 밖으로 전파되지 않아야 한다(로그만).
    scheduler.tick()


# --- 4. should_autostart 진리표 -----------------------------------------
def test_should_autostart_bid_only():
    """입찰 게이트만 충족(enabled=T, halted=F, pre_spec=F) → True."""
    assert scheduler.should_autostart(True, False, False) is True


def test_should_autostart_pre_spec_only():
    """사전규격만 충족(enabled=F, halted=F, pre_spec=T) → True."""
    assert scheduler.should_autostart(False, False, True) is True


def test_should_autostart_both_false():
    """둘 다 미충족(enabled=F, halted=F, pre_spec=F) → False."""
    assert scheduler.should_autostart(False, False, False) is False


def test_should_autostart_halted_and_pre_spec_off():
    """입찰 halt·사전규격 off(enabled=T, halted=T, pre_spec=F) → False."""
    assert scheduler.should_autostart(True, True, False) is False


def test_should_autostart_halted_but_pre_spec_on():
    """입찰 halt여도 사전규격 on(enabled=F, halted=T, pre_spec=T) → True."""
    assert scheduler.should_autostart(False, True, True) is True
