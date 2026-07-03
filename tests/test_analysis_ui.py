"""Phase 8.2 — 분석 UI 라우트 스모크 테스트.

UI 동작(JS/CSS)은 pytest로 완전 검증 불가능하므로:
- /list, /pre-spec 라우트 응답 코드 + btn-analyze 키워드 포함 확인
- 기존 목록 기능(검색·페이지네이션·파일버튼 등) 정상 동작 확인
- 업로드 모달 HTML 포함 확인

실행: `pytest tests/test_analysis_ui.py`
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ANTHROPIC_API_KEY 없어도 import 가능하도록 미리 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main, repository
from app.db import Base
from app.models import AppConfig, BidNotice, PreSpec


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


def _has_class_token(html: str, token: str) -> bool:
    return any(
        token in match.group(2).split()
        for match in re.finditer(r"class=(['\"])(.*?)\1", html)
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 SQLite + SessionLocal 교체 + TestClient 반환."""
    db_path = tmp_path / "ui_test.db"
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
            bid_ntce_no="BID-001",
            bid_ntce_nm="소프트웨어 유지보수 용역",
            ntce_instt_nm="테스트기관",
            bid_ntce_dt=_META,
            openg_dt=_FUTURE_OPEN,
            collected_at=_META,
            updated_at=_META,
            ntce_spec_file_nm1="제안요청서.pdf",
            ntce_spec_doc_url1="https://example.test/rfp.pdf",
        ))
        # 사전규격 시드
        s.add(PreSpec(
            bf_spec_rgst_no="PS-001",
            prdct_clsfc_no_nm="소프트웨어 개발",
            order_instt_nm="테스트기관",
            rcpt_dt=_META,
            opnin_rgst_clse_dt=_FUTURE_CLOSE,
            collected_at=_META,
            updated_at=_META,
            spec_doc_file_url1="https://example.test/spec.pdf",
        ))
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    monkeypatch.setenv("USE_ANALYSIS_PROVIDER", "true")  # 분석 UI 테스트: 명시적 true 설정
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

def test_list_page_returns_200(client):
    """입찰공고 목록 페이지가 200 응답을 반환한다."""
    resp = client.get("/list")
    assert resp.status_code == 200


def test_list_page_has_analyze_button(client):
    """입찰공고 목록 페이지에 btn-analyze 클래스 토큰이 있다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    assert _has_class_token(resp.text, "btn-analyze")


def test_list_page_analyze_button_data_type(client):
    """분석 버튼에 data-type='bid' 속성이 있다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    assert 'data-type="bid"' in resp.text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("none", ['data-status="none"', "분석 ▾", 'data-action="auto"', 'data-action="upload"', "파일 업로드"]),
        ("analyzing", ['data-status="analyzing"', "disabled", ">분석중</button>"]),
        ("done", ['data-status="done"', "분석완료", 'data-action="view"', 'data-action="auto"', "자동 재분석", 'data-action="upload"', "파일 업로드"]),
        ("error", ['data-status="error"', "재분석 ▾", 'data-action="auto"', 'data-action="upload"', "파일 업로드"]),
    ],
)
def test_list_page_renders_analysis_button_by_status(client, status, expected):
    """입찰공고 목록 버튼이 analysis_result 상태별로 서버 렌더된다."""
    with main.SessionLocal() as s:
        if status == "analyzing":
            repository.start_analysis(s, "bid", "BID-001", "auto")
        elif status == "done":
            repository.set_analysis_done(s, "bid", "BID-001", "{}")
        elif status == "error":
            repository.set_analysis_error(s, "bid", "BID-001", "실패")

    resp = client.get("/list")
    assert resp.status_code == 200
    for needle in expected:
        assert needle in resp.text
    assert 'data-name="소프트웨어 유지보수 용역"' in resp.text


def test_list_page_has_upload_modal(client):
    """입찰공고 목록 페이지에 업로드 모달 HTML이 포함된다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    assert "analysisUploadModal" in resp.text
    assert "uploadFileInput" in resp.text


def test_list_page_has_analysis_script(client):
    """입찰공고 목록 페이지에 분석 스크립트(runAnalysis)가 포함된다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    assert "runAnalysis" in resp.text


def test_pre_spec_page_returns_200(client):
    """사전규격 목록 페이지가 200 응답을 반환한다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200


def test_pre_spec_page_has_analyze_button(client):
    """사전규격 목록 페이지에 btn-analyze 클래스 토큰이 있다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    assert _has_class_token(resp.text, "btn-analyze")


def test_pre_spec_page_analyze_button_data_type(client):
    """분석 버튼에 data-type='pre-spec' 속성이 있다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    assert 'data-type="pre-spec"' in resp.text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("none", ['data-status="none"', "분석 ▾", 'data-action="auto"', 'data-action="upload"', "파일 업로드"]),
        ("analyzing", ['data-status="analyzing"', "disabled", ">분석중</button>"]),
        ("done", ['data-status="done"', "분석완료", 'data-action="view"', 'data-action="auto"', "자동 재분석", 'data-action="upload"', "파일 업로드"]),
        ("error", ['data-status="error"', "재분석 ▾", 'data-action="auto"', 'data-action="upload"', "파일 업로드"]),
    ],
)
def test_pre_spec_page_renders_analysis_button_by_status(client, status, expected):
    """사전규격 목록 버튼이 analysis_result 상태별로 서버 렌더된다."""
    with main.SessionLocal() as s:
        if status == "analyzing":
            repository.start_analysis(s, "pre_spec", "PS-001", "auto")
        elif status == "done":
            repository.set_analysis_done(s, "pre_spec", "PS-001", "{}")
        elif status == "error":
            repository.set_analysis_error(s, "pre_spec", "PS-001", "실패")

    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    for needle in expected:
        assert needle in resp.text
    assert 'data-name="소프트웨어 개발"' in resp.text


def test_pre_spec_page_has_upload_modal(client):
    """사전규격 목록 페이지에 업로드 모달 HTML이 포함된다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    assert "analysisUploadModal" in resp.text
    assert "uploadFileInput" in resp.text


def test_pre_spec_page_has_analysis_script(client):
    """사전규격 목록 페이지에 분석 스크립트(runAnalysis)가 포함된다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    assert "runAnalysis" in resp.text


def test_list_page_existing_features_preserved(client):
    """기존 목록 기능(검색·파일버튼·페이지네이션)이 정상 동작한다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    # 기존 파일 버튼 클래스
    assert "filebtn" in resp.text
    # 페이지네이션 영역
    assert "pager" in resp.text
    # 필터 카드
    assert "filter-card" in resp.text


def test_pre_spec_page_existing_features_preserved(client):
    """기존 사전규격 목록 기능(검색·파일버튼·페이지네이션)이 정상 동작한다."""
    resp = client.get("/pre-spec")
    assert resp.status_code == 200
    # 기존 파일 버튼 클래스
    assert "filebtn" in resp.text
    # 페이지네이션 영역
    assert "pager" in resp.text
    # 필터 카드
    assert "filter-card" in resp.text


def test_list_page_filter_query(client):
    """검색 쿼리 파라미터가 작동한다 — 히트 없는 q도 200 반환."""
    resp = client.get("/list?q=존재하지않는공고명XYZ")
    assert resp.status_code == 200
    assert "조건에 맞는 공고가 없습니다" in resp.text


def test_pre_spec_page_filter_query(client):
    """검색 쿼리 파라미터가 작동한다 — 히트 없는 q도 200 반환."""
    resp = client.get("/pre-spec?q=존재하지않는사전규격XYZ")
    assert resp.status_code == 200
    assert "조건에 맞는 사전규격이 없습니다" in resp.text


def test_list_page_sort_works(client):
    """정렬 파라미터가 작동한다."""
    resp = client.get("/list?sort=bid_ntce_dt_asc")
    assert resp.status_code == 200


def test_pre_spec_page_sort_works(client):
    """사전규격 정렬 파라미터가 작동한다."""
    resp = client.get("/pre-spec?sort=rcpt_dt_asc")
    assert resp.status_code == 200


def test_list_page_pagination(client):
    """페이지네이션 파라미터가 작동한다."""
    resp = client.get("/list?page=1&page_size=10")
    assert resp.status_code == 200


def test_analysis_css_included(client):
    """분석 관련 CSS 클래스가 페이지에 포함된다."""
    resp = client.get("/list")
    assert resp.status_code == 200
    assert "analysis-actions" in resp.text
    assert "analysis-menu" in resp.text
    assert "analysis-menu-toggle" in resp.text
    assert ".analysis-menu { display: none; position: fixed" in resp.text
    assert "is-analyzing" in resp.text
    assert "is-done" in resp.text
    assert "upload-area" in resp.text
