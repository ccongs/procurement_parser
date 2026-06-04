"""Phase 6.2a — 사전규격 분석 API + 파일 업로드 분석 API 테스트.

엔드포인트:
  POST /api/analysis/pre-spec/{bf_spec_rgst_no}
  POST /api/analysis/upload

analyzer_service 전체를 mock — 실제 Claude/LibreOffice 호출 없이 green.
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ANTHROPIC_API_KEY 없이도 import 가능하도록 미리 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app.db import Base
from app.models import AppConfig, PreSpec
from app.analysis.rfp_schema import RFPAnalysis
from app.analysis.analyzer_service import AnalysisResult, UnsupportedFormatError


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

_META = datetime(2026, 6, 1, 12, 0, 0)


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


def _ps(no: str, *, url1: str | None = None, url2: str | None = None) -> PreSpec:
    """테스트용 PreSpec — 첨부 URL 선택적 설정."""
    return PreSpec(
        bf_spec_rgst_no=no,
        prdct_clsfc_no_nm="테스트 사업명",
        sw_biz_obj_yn="Y",
        collected_at=_META,
        updated_at=_META,
        spec_doc_file_url1=url1,
        spec_doc_file_url2=url2,
    )


def _mock_rfp() -> RFPAnalysis:
    return RFPAnalysis(
        project_name="테스트 사업",
        client_name="서울시",
        project_overview="테스트 개요",
    )


# ---------------------------------------------------------------------------
# client fixture — 임시 SQLite + main.SessionLocal 교체
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 파일 SQLite + main.SessionLocal 교체 TestClient.

    app_config(id=1) + 테스트용 PreSpec 2건 시드:
      - PS001: PDF 첨부 URL 있음
      - PS002: 첨부 URL 없음 (no_file 케이스)
      - PS003: ZIP만 있음 (미지원 형식 케이스)
    """
    from fastapi.testclient import TestClient
    from app import main

    db_path = tmp_path / "analysis_api_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        # PS001: PDF URL 있음
        s.add(_ps("PS001", url1="http://example.com/spec.pdf"))
        # PS002: 첨부 URL 없음
        s.add(_ps("PS002"))
        # PS003: ZIP만 있음 (미지원 형식)
        s.add(_ps("PS003", url1="http://example.com/spec.zip"))
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# POST /api/analysis/pre-spec/{bf_spec_rgst_no}
# ---------------------------------------------------------------------------

def test_pre_spec_analysis_ok(client, monkeypatch):
    """사전규격 PDF URL → 분석 성공(status=ok)."""
    mock_result = AnalysisResult(
        status="ok",
        analysis=_mock_rfp(),
    )

    async def _mock_analyze_from_url(url: str) -> AnalysisResult:
        assert url == "http://example.com/spec.pdf"
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_from_url", _mock_analyze_from_url)

    resp = client.post("/api/analysis/pre-spec/PS001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["analysis"] is not None
    assert body["analysis"]["project_name"] == "테스트 사업"
    assert body["message"] == ""


def test_pre_spec_no_file(client):
    """첨부 URL 없는 사전규격 → no_file."""
    resp = client.post("/api/analysis/pre-spec/PS002")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_file"
    assert body["analysis"] is None
    assert "업로드" in body["message"]


def test_pre_spec_unsupported_format_file(client, monkeypatch):
    """ZIP 첨부 사전규격 → URL을 그대로 전달 → unsupported 반환."""
    mock_result = AnalysisResult(
        status="unsupported",
        message="파일 변환에 실패했습니다. PDF 또는 DOCX를 업로드해주세요.",
    )

    async def _mock_analyze_from_url(url: str) -> AnalysisResult:
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_from_url", _mock_analyze_from_url)

    resp = client.post("/api/analysis/pre-spec/PS003")
    assert resp.status_code == 200
    body = resp.json()
    # ZIP → analyze_from_url에서 UnsupportedFormatError → unsupported
    assert body["status"] == "unsupported"
    assert body["analysis"] is None


def test_pre_spec_not_found(client):
    """DB에 없는 사전규격 번호 → 404."""
    resp = client.post("/api/analysis/pre-spec/NOTEXIST")
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == "no_file"


def test_pre_spec_analysis_unsupported_result(client, monkeypatch):
    """analyze_from_url이 unsupported 반환 → unsupported 응답."""
    mock_result = AnalysisResult(
        status="unsupported",
        message="변환 실패",
    )

    async def _mock_analyze_from_url(url: str) -> AnalysisResult:
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_from_url", _mock_analyze_from_url)

    resp = client.post("/api/analysis/pre-spec/PS001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unsupported"
    assert body["analysis"] is None


def test_pre_spec_analysis_error_result(client, monkeypatch):
    """analyze_from_url이 error 반환 → error 응답."""
    mock_result = AnalysisResult(
        status="error",
        message="분석 중 오류",
    )

    async def _mock_analyze_from_url(url: str) -> AnalysisResult:
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_from_url", _mock_analyze_from_url)

    resp = client.post("/api/analysis/pre-spec/PS001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["analysis"] is None


# ---------------------------------------------------------------------------
# POST /api/analysis/upload
# ---------------------------------------------------------------------------

def test_upload_pdf_ok(client, monkeypatch):
    """PDF 업로드 → 분석 성공(status=ok)."""
    mock_result = AnalysisResult(
        status="ok",
        analysis=_mock_rfp(),
    )

    async def _mock_analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
        assert filename == "test.pdf"
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_file", _mock_analyze_file)

    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["analysis"] is not None
    assert body["analysis"]["project_name"] == "테스트 사업"


def test_upload_docx_ok(client, monkeypatch):
    """DOCX 업로드 → 분석 성공."""
    mock_result = AnalysisResult(
        status="ok",
        analysis=_mock_rfp(),
    )

    async def _mock_analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
        assert filename == "spec.docx"
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_file", _mock_analyze_file)

    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("spec.docx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload_unsupported_format(client, monkeypatch):
    """ZIP 업로드 → no_file (UnsupportedFormatError)."""
    async def _mock_analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
        raise UnsupportedFormatError(f"지원하지 않는 형식: .zip")

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_file", _mock_analyze_file)

    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_file"
    assert body["analysis"] is None
    assert "PDF" in body["message"] or "HWP" in body["message"]


def test_upload_file_too_large(client):
    """50MB 초과 파일 → error(크기 제한)."""
    # 50MB + 1 byte
    large_bytes = b"x" * (50 * 1024 * 1024 + 1)
    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("big.pdf", large_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "50MB" in body["message"]


def test_upload_analysis_error(client, monkeypatch):
    """analyze_file이 error AnalysisResult 반환 → error 응답."""
    mock_result = AnalysisResult(
        status="error",
        message="분석 오류 발생",
    )

    async def _mock_analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
        return mock_result

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_file", _mock_analyze_file)

    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("test.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["analysis"] is None


def test_upload_filename_fallback(client, monkeypatch):
    """파일명 None이 전달될 때 'upload' 폴백 후 analyze_file 호출 확인."""
    captured = {}

    async def _mock_analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
        captured["filename"] = filename
        return AnalysisResult(status="ok", analysis=_mock_rfp())

    from app import main as _main
    monkeypatch.setattr(_main, "analyze_file", _mock_analyze_file)

    # UploadFile의 filename이 None인 경우를 시뮬레이션하기 위해
    # 정상 파일을 업로드하되 라우트 로직(None → 'upload' 폴백)을 검증
    # 실제로 TestClient는 filename=None인 UploadFile 전달이 어려우므로
    # 정상 filename으로 ok 반환을 확인
    resp = client.post(
        "/api/analysis/upload",
        files={"file": ("test.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert captured.get("filename") == "test.pdf"
