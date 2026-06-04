"""Phase 7.3 — USE_ANALYSIS_PROVIDER env 토글 테스트.

- USE_ANALYSIS_PROVIDER=false → /list·/pre-spec에서 분석 컬럼·버튼 숨김, 파일 컬럼·장바구니 유지
- USE_ANALYSIS_PROVIDER=true (또는 기본) → 분석 컬럼·버튼 표시

실행: `pytest tests/test_analysis_toggle.py`
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main
from app.db import Base
from app.models import AppConfig, BidNotice, PreSpec


def _cfg() -> AppConfig:
    return AppConfig(
        id=1,
        enabled=True,
        pre_spec_enabled=True,
        auto_halted=False,
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


_META = datetime(2026, 6, 1, 12, 0, 0)


@pytest.fixture
def client_with_seed(tmp_path, monkeypatch):
    """임시 SQLite + 시드 데이터(입찰공고·사전규격 각 1건) + TestClient."""
    db_path = tmp_path / "toggle_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        s.add(BidNotice(
            bid_ntce_no="BID-TOG-001",
            bid_ntce_nm="토글 테스트 입찰공고",
            ntce_instt_nm="테스트기관",
            bid_ntce_dt=_META,
            openg_dt=datetime(2026, 8, 1, 10, 0, 0),
            collected_at=_META,
            updated_at=_META,
        ))
        s.add(PreSpec(
            bf_spec_rgst_no="PS-TOG-001",
            prdct_clsfc_no_nm="토글 테스트 사전규격",
            order_instt_nm="테스트기관",
            rcpt_dt=_META,
            opnin_rgst_clse_dt=datetime(2026, 8, 1, 18, 0, 0),
            collected_at=_META,
            updated_at=_META,
        ))
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)

    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c, monkeypatch


# ---------------------------------------------------------------------------
# USE_ANALYSIS_PROVIDER=false — 분석 컬럼·버튼 숨김
# ---------------------------------------------------------------------------

class TestAnalysisToggleFalse:
    """env=false → 분석 숨김, 파일·장바구니 유지."""

    def test_list_no_analyze_button(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/list")
        assert r.status_code == 200
        assert 'class="btn-analyze"' not in r.text, "false일 때 btn-analyze가 존재하면 안 됨"

    def test_list_no_analysis_th(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/list")
        assert "<th>분석</th>" not in r.text, "false일 때 분석 th가 존재하면 안 됨"

    def test_list_file_th_exists(self, client_with_seed):
        """파일 컬럼은 항상 유지."""
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/list")
        assert "<th>파일</th>" in r.text, "파일 컬럼이 사라지면 안 됨"

    def test_list_cart_menu_exists(self, client_with_seed):
        """장바구니 버튼(btn-cart-menu)은 항상 유지."""
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/list")
        assert "btn-cart-menu" in r.text, "장바구니 버튼이 사라지면 안 됨"

    def test_pre_spec_no_analyze_button(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/pre-spec")
        assert r.status_code == 200
        assert 'class="btn-analyze"' not in r.text, "false일 때 btn-analyze가 존재하면 안 됨"

    def test_pre_spec_no_analysis_th(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/pre-spec")
        assert "<th>분석</th>" not in r.text, "false일 때 분석 th가 존재하면 안 됨"

    def test_pre_spec_file_th_exists(self, client_with_seed):
        """파일 컬럼은 항상 유지."""
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/pre-spec")
        assert "<th>파일</th>" in r.text, "파일 컬럼이 사라지면 안 됨"

    def test_pre_spec_cart_menu_exists(self, client_with_seed):
        """장바구니 버튼(btn-cart-menu)은 항상 유지."""
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "false")
        r = client.get("/pre-spec")
        assert "btn-cart-menu" in r.text, "장바구니 버튼이 사라지면 안 됨"


# ---------------------------------------------------------------------------
# USE_ANALYSIS_PROVIDER=true (또는 기본) — 분석 컬럼·버튼 표시
# ---------------------------------------------------------------------------

class TestAnalysisToggleTrue:
    """env=true(또는 미설정 기본값) → 분석 표시."""

    def test_list_analyze_button_present(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "true")
        r = client.get("/list")
        assert r.status_code == 200
        assert 'class="btn-analyze"' in r.text, "true일 때 btn-analyze가 없음"

    def test_list_analysis_th_present(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "true")
        r = client.get("/list")
        assert "<th>분석</th>" in r.text, "true일 때 분석 th가 없음"

    def test_pre_spec_analyze_button_present(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "true")
        r = client.get("/pre-spec")
        assert r.status_code == 200
        assert 'class="btn-analyze"' in r.text, "true일 때 btn-analyze가 없음"

    def test_pre_spec_analysis_th_present(self, client_with_seed):
        client, mp = client_with_seed
        mp.setenv("USE_ANALYSIS_PROVIDER", "true")
        r = client.get("/pre-spec")
        assert "<th>분석</th>" in r.text, "true일 때 분석 th가 없음"

    def test_list_default_shows_analysis(self, client_with_seed):
        """USE_ANALYSIS_PROVIDER 미설정 시 기본값 true → 분석 표시."""
        client, mp = client_with_seed
        # 환경변수를 명시적으로 제거해 기본값 동작 확인
        mp.delenv("USE_ANALYSIS_PROVIDER", raising=False)
        r = client.get("/list")
        assert 'class="btn-analyze"' in r.text, "기본값(true)일 때 btn-analyze가 없음"

    def test_pre_spec_default_shows_analysis(self, client_with_seed):
        """USE_ANALYSIS_PROVIDER 미설정 시 기본값 true → 분석 표시."""
        client, mp = client_with_seed
        mp.delenv("USE_ANALYSIS_PROVIDER", raising=False)
        r = client.get("/pre-spec")
        assert 'class="btn-analyze"' in r.text, "기본값(true)일 때 btn-analyze가 없음"
