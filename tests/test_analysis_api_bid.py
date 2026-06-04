"""Phase 6.2b — POST /api/analysis/bid/{bid_ntce_no} 엔드포인트 테스트.

- analyzer_service 전체 mock → Claude API / LibreOffice 비의존.
- 임시 SQLite DB + SessionLocal 교체 패턴(test_screens.py 와 동일).
- 결정적 테스트: datetime.now() 의존 없음.

실행: `pytest tests/test_analysis_api_bid.py`
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ANTHROPIC_API_KEY 가 없어도 import 가능하도록 미리 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main
from app.analysis.analyzer_service import AnalysisResult
from app.analysis.rfp_schema import RFPAnalysis
from app.db import Base
from app.models import AppConfig, BidNotice


# ---------------------------------------------------------------------------
# 공용 픽스처
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


_META = datetime(2026, 6, 1, 12, 0, 0)


def _notice(no: str, **kwargs) -> BidNotice:
    """테스트용 BidNotice 헬퍼(필수 컬럼 기본값 포함)."""
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=f"공고 {no}",
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=_META,
        collected_at=_META,
        updated_at=_META,
        **kwargs,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 SQLite + SessionLocal 교체 + TestClient 반환.

    시드:
    - TEST-PDF: ntce_spec_file_nm1="기타문서.hwp", ntce_spec_file_nm2="제안요청서.hwp",
                ntce_spec_file_nm3="제안요청서.pdf" → PDF 우선 선택 검증용.
    - TEST-NORFP: ntce_spec_file_nm1="규격서.pdf" → 제안요청서 없음(no_file).
    - TEST-ONLYHWP: ntce_spec_file_nm1="제안요청서.hwp" → hwp만 있는 경우.
    - (DB에 없는 ID): 404 검증용.
    """
    db_path = tmp_path / "bid_api_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())

        # PDF 우선순위 검증용: hwp(1번) + hwp(2번) + pdf(3번) 제안요청서
        s.add(
            _notice(
                "TEST-PDF",
                ntce_spec_file_nm1="기타문서.hwp",
                ntce_spec_doc_url1="https://example.test/etc.hwp",
                ntce_spec_file_nm2="제안요청서.hwp",
                ntce_spec_doc_url2="https://example.test/rfp.hwp",
                ntce_spec_file_nm3="제안요청서.pdf",
                ntce_spec_doc_url3="https://example.test/rfp.pdf",
            )
        )

        # 제안요청서 없음(no_file)
        s.add(
            _notice(
                "TEST-NORFP",
                ntce_spec_file_nm1="규격서.pdf",
                ntce_spec_doc_url1="https://example.test/spec.pdf",
            )
        )

        # hwp 파일만 있는 제안요청서
        s.add(
            _notice(
                "TEST-ONLYHWP",
                ntce_spec_file_nm1="제안요청서.hwp",
                ntce_spec_doc_url1="https://example.test/rfp.hwp",
            )
        )

        # 파일 없는 공고
        s.add(_notice("TEST-NOFILE"))

        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

def test_bid_analysis_not_found(client):
    """DB에 없는 bid_ntce_no → 404."""
    resp = client.post("/api/analysis/bid/NOTEXIST-12345")
    assert resp.status_code == 404


def test_bid_analysis_no_rfp_file(client):
    """'제안요청서' 파일명이 없는 공고 → status=no_file."""
    resp = client.post("/api/analysis/bid/TEST-NORFP")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_file"
    assert "제안요청서" in body["message"]


def test_bid_analysis_no_file_attached(client):
    """첨부파일이 아예 없는 공고 → status=no_file."""
    resp = client.post("/api/analysis/bid/TEST-NOFILE")
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_file"


def test_bid_analysis_pdf_priority(client):
    """'제안요청서' 파일이 여러 개일 때 PDF 우선 선택 → analyze_from_url에 .pdf URL이 전달된다."""
    mock_analysis = RFPAnalysis(
        project_name="테스트 프로젝트",
        client_name="테스트 기관",
        project_overview="테스트 개요",
    )
    mock_result = AnalysisResult(status="ok", analysis=mock_analysis)

    with patch("app.main.analyze_from_url", new_callable=AsyncMock, return_value=mock_result) as mock_fn:
        resp = client.post("/api/analysis/bid/TEST-PDF")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["analysis"] is not None
    assert body["analysis"]["project_name"] == "테스트 프로젝트"

    # PDF URL이 전달됐는지 확인
    mock_fn.assert_called_once()
    called_url = mock_fn.call_args[0][0]
    assert called_url == "https://example.test/rfp.pdf"


def test_bid_analysis_hwp_selected_when_only_hwp(client):
    """HWP만 있는 경우 HWP URL로 분석 호출 → status=ok."""
    mock_result = AnalysisResult(
        status="ok",
        analysis=RFPAnalysis(
            project_name="HWP 프로젝트",
            client_name="기관",
            project_overview="개요",
        ),
    )
    with patch("app.main.analyze_from_url", new_callable=AsyncMock, return_value=mock_result) as mock_fn:
        resp = client.post("/api/analysis/bid/TEST-ONLYHWP")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    called_url = mock_fn.call_args[0][0]
    assert "rfp.hwp" in called_url


def test_bid_analysis_unsupported(client):
    """analyze_from_url이 unsupported 반환 → status=unsupported."""
    mock_result = AnalysisResult(
        status="unsupported",
        message="파일 변환에 실패했습니다.",
    )
    with patch("app.main.analyze_from_url", new_callable=AsyncMock, return_value=mock_result):
        resp = client.post("/api/analysis/bid/TEST-ONLYHWP")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unsupported"
    assert "변환" in body["message"]


def test_bid_analysis_error(client):
    """analyze_from_url이 error 반환 → status=error, message 포함."""
    mock_result = AnalysisResult(
        status="error",
        message="분석 중 오류: 예시 오류",
    )
    with patch("app.main.analyze_from_url", new_callable=AsyncMock, return_value=mock_result):
        resp = client.post("/api/analysis/bid/TEST-ONLYHWP")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "오류" in body["message"]


def test_get_notice_files_ordering(tmp_path, monkeypatch):
    """get_notice_files가 URL 있는 것만 반환하고 idx 순서를 유지한다."""
    from app import repository

    db_path = tmp_path / "files_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(
            _notice(
                "FILE-ORDER",
                ntce_spec_file_nm1="파일1.pdf",
                ntce_spec_doc_url1="https://example.test/1.pdf",
                # 2번 URL 없음 → 반환 안 됨
                ntce_spec_file_nm3="파일3.hwp",
                ntce_spec_doc_url3="https://example.test/3.hwp",
                ntce_spec_doc_url5="https://example.test/5.docx",
                # 5번 name 없음 → '첨부5' 폴백
            )
        )
        s.commit()

        files = repository.get_notice_files(s, "FILE-ORDER")

    assert len(files) == 3
    assert files[0]["idx"] == 1
    assert files[0]["name"] == "파일1.pdf"
    assert files[1]["idx"] == 3
    assert files[1]["name"] == "파일3.hwp"
    assert files[2]["idx"] == 5
    assert files[2]["name"] == "첨부5"

    # DB에 없는 공고 → 빈 리스트
    with Local() as s:
        empty = repository.get_notice_files(s, "NOTEXIST")
    assert empty == []
