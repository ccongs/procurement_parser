"""Phase 7.2 — 장바구니 UI 라우트 스모크 테스트.

UI 동작(JS/CSS)은 pytest로 완전 검증 불가능하므로:
- /list, /pre-spec 라우트 응답 200 + 핵심 UI 요소 HTML 포함 확인
- 헤더 "선택항목" 버튼 포함 확인
- 체크박스(.row-select-chk) + 전체선택(chk-all) 포함 확인
- 담기 버튼(btn-add-to-cart) 포함 확인
- 장바구니 모달(cart-modal) 포함 확인
- 기존 목록 기능(검색·페이지네이션 등) 정상 동작 유지 확인

실행: `pytest tests/test_cart_ui.py`
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main
from app.db import Base
from app.models import AppConfig, BidNotice, ExportCartItem, PreSpec


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

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


_TODAY = date.today()
_META = datetime.combine(_TODAY - timedelta(days=1), time(12, 0, 0))
_FUTURE_OPEN = datetime.combine(_TODAY + timedelta(days=30), time(10, 0, 0))
_FUTURE_CLOSE = datetime.combine(_TODAY + timedelta(days=30), time(18, 0, 0))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 SQLite + SessionLocal 교체 + TestClient 반환."""
    db_path = tmp_path / "cart_ui_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        # 입찰공고 시드
        s.add(BidNotice(
            bid_ntce_no="BID-UI-001",
            bid_ntce_nm="장바구니 테스트 입찰공고",
            ntce_instt_nm="테스트기관",
            bid_ntce_dt=_META,
            openg_dt=_FUTURE_OPEN,
            collected_at=_META,
            updated_at=_META,
        ))
        # 사전규격 시드
        s.add(PreSpec(
            bf_spec_rgst_no="PS-UI-001",
            prdct_clsfc_no_nm="장바구니 테스트 사전규격",
            order_instt_nm="테스트기관",
            rcpt_dt=_META,
            opnin_rgst_clse_dt=_FUTURE_CLOSE,
            collected_at=_META,
            updated_at=_META,
        ))
        s.commit()

    def _make_local():
        return Local()

    monkeypatch.setattr(main, "SessionLocal", Local)
    monkeypatch.setenv("USE_ANALYSIS_PROVIDER", "true")  # btn-analyze 존재 전제 테스트: 명시적 true 설정

    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# /list 장바구니 UI 확인
# ---------------------------------------------------------------------------

class TestListCartUI:
    def test_list_200(self, client):
        r = client.get("/list")
        assert r.status_code == 200

    def test_list_has_checkbox_col(self, client):
        r = client.get("/list")
        assert "col-chk" in r.text, "체크박스 컬럼 클래스 누락"
        assert "row-select-chk" in r.text, "row-select-chk 클래스 누락"

    def test_list_has_chk_all(self, client):
        r = client.get("/list")
        assert 'id="chk-all"' in r.text, "전체선택 체크박스 누락"

    def test_list_has_cart_add_button(self, client):
        r = client.get("/list")
        assert "btn-add-to-cart" in r.text, "담기 버튼 누락"
        assert "cart-add-count" in r.text, "담기 건수 span 누락"

    def test_list_has_cart_menu_button(self, client):
        r = client.get("/list")
        assert "btn-cart-menu" in r.text, "선택항목 헤더 버튼 누락"
        assert "cart-badge" in r.text, "cart-badge span 누락"

    def test_list_has_cart_modal(self, client):
        r = client.get("/list")
        assert 'id="cart-modal"' in r.text, "장바구니 모달 누락"
        assert "cart-items-list" in r.text, "cart-items-list 누락"

    def test_list_has_cart_script_functions(self, client):
        r = client.get("/list")
        assert "addToCart" in r.text
        assert "toggleCartModal" in r.text
        assert "loadCartItems" in r.text
        assert "updateCartBadge" in r.text
        assert "downloadCart" in r.text

    def test_list_item_type_bid(self, client):
        """각 행에 data-item-type='bid' 속성 존재 확인."""
        r = client.get("/list")
        assert 'data-item-type="bid"' in r.text, "bid 타입 데이터 속성 누락"

    def test_list_existing_features_intact(self, client):
        """기존 기능(파일 버튼, 분석 버튼, 페이저) 유지 확인."""
        r = client.get("/list")
        assert "btn-analyze" in r.text
        assert "pager-wrap" in r.text


# ---------------------------------------------------------------------------
# /pre-spec 장바구니 UI 확인
# ---------------------------------------------------------------------------

class TestPreSpecCartUI:
    def test_pre_spec_200(self, client):
        r = client.get("/pre-spec")
        assert r.status_code == 200

    def test_pre_spec_has_checkbox_col(self, client):
        r = client.get("/pre-spec")
        assert "col-chk" in r.text
        assert "row-select-chk" in r.text

    def test_pre_spec_has_chk_all(self, client):
        r = client.get("/pre-spec")
        assert 'id="chk-all"' in r.text

    def test_pre_spec_has_cart_add_button(self, client):
        r = client.get("/pre-spec")
        assert "btn-add-to-cart" in r.text
        assert "cart-add-count" in r.text

    def test_pre_spec_has_cart_menu_button(self, client):
        r = client.get("/pre-spec")
        assert "btn-cart-menu" in r.text
        assert "cart-badge" in r.text

    def test_pre_spec_has_cart_modal(self, client):
        r = client.get("/pre-spec")
        assert 'id="cart-modal"' in r.text
        assert "cart-items-list" in r.text

    def test_pre_spec_item_type_pre_spec(self, client):
        """각 행에 data-item-type='pre_spec' 속성 존재 확인."""
        r = client.get("/pre-spec")
        assert 'data-item-type="pre_spec"' in r.text, "pre_spec 타입 데이터 속성 누락"

    def test_pre_spec_existing_features_intact(self, client):
        """기존 기능(분석 버튼, 페이저) 유지 확인."""
        r = client.get("/pre-spec")
        assert "btn-analyze" in r.text
        assert "pager-wrap" in r.text

    def test_pre_spec_has_cart_script_functions(self, client):
        r = client.get("/pre-spec")
        assert "addToCart" in r.text
        assert "toggleCartModal" in r.text
        assert "loadCartItems" in r.text


# ---------------------------------------------------------------------------
# API 엔드포인트 연동 확인 (백엔드 7.1 기존 API)
# ---------------------------------------------------------------------------

class TestCartApiIntegration:
    def test_get_cart_empty(self, client):
        r = client.get("/api/export-cart")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["items"] == []

    def test_post_cart_and_get(self, client):
        r = client.post(
            "/api/export-cart",
            json={"items": [{"item_type": "bid", "item_id": "BID-UI-001"}]},
        )
        assert r.status_code == 200
        assert r.json()["added"] == 1

        r2 = client.get("/api/export-cart")
        assert r2.json()["count"] == 1
        assert r2.json()["items"][0]["item_id"] == "BID-UI-001"

    def test_delete_cart_item(self, client):
        client.post(
            "/api/export-cart",
            json={"items": [{"item_type": "bid", "item_id": "BID-UI-001"}]},
        )
        r = client.get("/api/export-cart")
        cart_id = r.json()["items"][0]["cart_id"]

        r2 = client.delete(f"/api/export-cart/{cart_id}")
        assert r2.status_code == 200
        assert r2.json()["deleted"] is True

        r3 = client.get("/api/export-cart")
        assert r3.json()["count"] == 0

    def test_clear_cart(self, client):
        client.post(
            "/api/export-cart",
            json={"items": [
                {"item_type": "bid", "item_id": "BID-UI-001"},
                {"item_type": "pre_spec", "item_id": "PS-UI-001"},
            ]},
        )
        r = client.delete("/api/export-cart/all")
        assert r.status_code == 200

        r2 = client.get("/api/export-cart")
        assert r2.json()["count"] == 0

    def test_download_cart_excel(self, client):
        client.post(
            "/api/export-cart",
            json={"items": [{"item_type": "bid", "item_id": "BID-UI-001"}]},
        )
        r = client.get("/api/export-cart/download")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "spreadsheet" in ct or "octet-stream" in ct


# ---------------------------------------------------------------------------
# Phase 7.3 — "검토항목 장바구니" 명칭 변경 확인
# ---------------------------------------------------------------------------

class TestCartRenamePhase73:
    """헤더 버튼·모달 제목이 "검토항목 장바구니"로 변경되었는지 확인."""

    def test_list_header_btn_text_renamed(self, client):
        """헤더 버튼에 '검토항목 장바구니' 텍스트 포함."""
        r = client.get("/list")
        assert "검토항목 장바구니" in r.text, "헤더 버튼 텍스트 '검토항목 장바구니' 누락"

    def test_list_modal_title_renamed(self, client):
        """장바구니 모달 제목이 '검토항목 장바구니'로 변경됨."""
        r = client.get("/list")
        assert "<h3>검토항목 장바구니</h3>" in r.text, "모달 h3 제목 누락"

    def test_list_modal_aria_label_renamed(self, client):
        """모달 aria-label이 '검토항목 장바구니'로 변경됨."""
        r = client.get("/list")
        assert 'aria-label="검토항목 장바구니"' in r.text, "모달 aria-label 누락"

    def test_config_header_btn_text_renamed(self, client):
        """/config 페이지 헤더에도 '검토항목 장바구니' 텍스트 포함."""
        r = client.get("/config")
        assert "검토항목 장바구니" in r.text, "/config 헤더 버튼 텍스트 '검토항목 장바구니' 누락"

    def test_pre_spec_header_btn_text_renamed(self, client):
        """/pre-spec 페이지 헤더에도 '검토항목 장바구니' 텍스트 포함."""
        r = client.get("/pre-spec")
        assert "검토항목 장바구니" in r.text, "/pre-spec 헤더 버튼 텍스트 '검토항목 장바구니' 누락"

    def test_pre_spec_modal_title_renamed(self, client):
        """/pre-spec 장바구니 모달 제목이 '검토항목 장바구니'로 변경됨."""
        r = client.get("/pre-spec")
        assert "<h3>검토항목 장바구니</h3>" in r.text, "/pre-spec 모달 h3 제목 누락"

    def test_btn_cart_menu_id_unchanged(self, client):
        """id 'btn-cart-menu'는 변경 없이 유지됨."""
        r = client.get("/list")
        assert 'id="btn-cart-menu"' in r.text, "btn-cart-menu id 유지 실패"

    def test_cart_modal_id_unchanged(self, client):
        """id 'cart-modal'은 변경 없이 유지됨."""
        r = client.get("/list")
        assert 'id="cart-modal"' in r.text, "cart-modal id 유지 실패"
