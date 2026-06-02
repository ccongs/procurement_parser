"""lifespan 자동시작 통합 테스트 — Phase 4.4.

`main.app` 의 lifespan 이 설정 게이트를 읽어 scheduler.start_scheduler 를
조건부 호출하는 것을 pytest + TestClient(with 블록) 으로 검증한다.

- 네트워크/실 DB/실 스케줄러 비의존.
- `with TestClient(main.app) as client:` 를 써야 lifespan startup/shutdown 이 실행된다.
- 기존 test_screens.py 의 `client` 픽스처(with 없음)와는 별개의 픽스처를 사용한다.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, scheduler
from app.db import Base
from app.models import AppConfig


# ---------------------------------------------------------------------------
# 헬퍼: 케이스별 임시 DB 를 돌려주는 컨텍스트
# ---------------------------------------------------------------------------

def _make_local(db_path: str, *, enabled: bool, auto_halted: bool, pre_spec_enabled: bool):
    """임시 SQLite 파일에 AppConfig 1행을 시드하고 sessionmaker 를 반환한다."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)
    with Local() as s:
        s.add(
            AppConfig(
                id=1,
                enabled=enabled,
                auto_halted=auto_halted,
                pre_spec_enabled=pre_spec_enabled,
                interval_minutes=60,
                window_overlap_minutes=90,
                backfill_days=30,
                num_of_rows=20,
                max_retries=2,
                inqry_div="1",
                intrntnl_div_cd="1",
                indstryty_cds="1426,1468,1469,1470",
                updated_at=datetime(2026, 1, 1),
            )
        )
        s.commit()
    return Local


# ---------------------------------------------------------------------------
# 게이트 충족 케이스 — start_scheduler 가 정확히 1회 호출되어야 한다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "enabled,auto_halted,pre_spec_enabled,label",
    [
        (True, False, False, "입찰 on"),
        (False, False, True, "사전규격 on"),
        (True, False, True, "둘 다 on"),
        (False, True, True, "입찰 halt·사전규격 on"),
    ],
)
def test_lifespan_starts_scheduler_when_gate_passes(
    tmp_path, monkeypatch, enabled, auto_halted, pre_spec_enabled, label
):
    """게이트 충족 시 start_scheduler 가 정확히 1회, run_now=False 로 호출된다."""
    db_path = tmp_path / f"lifespan_{label}.db"
    Local = _make_local(str(db_path), enabled=enabled, auto_halted=auto_halted, pre_spec_enabled=pre_spec_enabled)

    # init_db 는 no-op 으로(실 DB 접근 방지)
    monkeypatch.setattr(main, "init_db", lambda: None)
    # SessionLocal 은 임시 DB 로 교체
    monkeypatch.setattr(main, "SessionLocal", Local)

    # start_scheduler 호출 기록용 recorder
    calls: list[dict] = []

    def fake_start(run_now=False):
        calls.append({"run_now": run_now})

    monkeypatch.setattr(scheduler, "start_scheduler", fake_start)
    # shutdown_scheduler 는 no-op 으로(전역 오염 방지)
    monkeypatch.setattr(scheduler, "shutdown_scheduler", lambda: None)

    with TestClient(main.app) as _client:
        pass  # lifespan startup/shutdown 이 여기서 실행된다

    assert len(calls) == 1, f"[{label}] start_scheduler 호출 횟수 불일치: {calls}"
    assert calls[0]["run_now"] is False, f"[{label}] run_now 가 False 가 아님: {calls[0]}"


# ---------------------------------------------------------------------------
# 게이트 미충족 케이스 — start_scheduler 가 호출되지 않아야 한다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "enabled,auto_halted,pre_spec_enabled,label",
    [
        (False, False, False, "둘 다 off"),
        (True, True, False, "입찰 halt·사전규격 off"),
        (False, True, False, "disabled·halt·사전규격 off"),
    ],
)
def test_lifespan_skips_scheduler_when_gate_fails(
    tmp_path, monkeypatch, enabled, auto_halted, pre_spec_enabled, label
):
    """게이트 미충족 시 start_scheduler 가 한 번도 호출되지 않는다."""
    db_path = tmp_path / f"lifespan_fail_{label}.db"
    Local = _make_local(str(db_path), enabled=enabled, auto_halted=auto_halted, pre_spec_enabled=pre_spec_enabled)

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "SessionLocal", Local)

    calls: list[dict] = []

    def fake_start(run_now=False):
        calls.append({"run_now": run_now})

    monkeypatch.setattr(scheduler, "start_scheduler", fake_start)
    monkeypatch.setattr(scheduler, "shutdown_scheduler", lambda: None)

    with TestClient(main.app) as _client:
        pass

    assert len(calls) == 0, f"[{label}] start_scheduler 가 호출되었으나 호출되지 않아야 함: {calls}"


# ---------------------------------------------------------------------------
# 자동시작 예외 삼킴 — init_db 예외가 앱 기동을 막지 않는다
# ---------------------------------------------------------------------------

def test_lifespan_exception_does_not_block_app(tmp_path, monkeypatch):
    """lifespan 의 try/except 가 예외를 삼켜 앱(HTTP 응답)은 정상 기동해야 한다."""

    def boom():
        raise RuntimeError("init_db 폭발")

    monkeypatch.setattr(main, "init_db", boom)
    monkeypatch.setattr(scheduler, "shutdown_scheduler", lambda: None)

    calls: list = []
    monkeypatch.setattr(scheduler, "start_scheduler", lambda run_now=False: calls.append(run_now))

    with TestClient(main.app) as client:
        # 예외 후에도 앱이 떠 있어 HTTP 응답이 가능해야 한다.
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (200, 302, 303, 307)

    # 예외가 났으므로 start_scheduler 는 호출되지 않았어야 한다.
    assert calls == []
