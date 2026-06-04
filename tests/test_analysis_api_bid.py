"""Phase 8.1 — 입찰공고 분석 비동기 API 테스트."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main, repository
from app.analysis.analyzer_service import AnalysisResult as ServiceAnalysisResult
from app.analysis.rfp_schema import RFPAnalysis
from app.db import Base
from app.models import AppConfig, BidNotice


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


def _notice(no: str, **kwargs) -> BidNotice:
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
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "bid_analysis_api.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        s.add(
            _notice(
                "TEST-PDF",
                ntce_spec_file_nm1="제안요청서.hwp",
                ntce_spec_doc_url1="https://example.test/rfp.hwp",
                ntce_spec_file_nm2="제안요청서.pdf",
                ntce_spec_doc_url2="https://example.test/rfp.pdf",
            )
        )
        s.add(
            _notice(
                "TEST-NORFP",
                ntce_spec_file_nm1="규격서.pdf",
                ntce_spec_doc_url1="https://example.test/spec.pdf",
            )
        )
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return Local


@pytest.fixture
def client(db):
    return TestClient(main.app)


def _full_analysis() -> RFPAnalysis:
    return RFPAnalysis(
        project_name="스마트 통합 플랫폼",
        client_name="서울시",
        project_overview="도시 데이터를 통합 관리합니다.",
        budget={
            "total_budget": "100,000,000원",
            "payment_terms": "검수 후 지급",
            "notes": "부가세 포함",
        },
        timeline={
            "total_duration": "6개월",
            "phases": [{"name": "분석", "duration": "1개월"}],
        },
        key_requirements=[
            {
                "category": "기능",
                "requirement": "실시간 데이터 수집",
                "priority": "필수",
                "notes": "API 연계",
            }
        ],
        technical_requirements=[
            {"category": "기술", "requirement": "클라우드 배포", "priority": "필수"}
        ],
        functional_requirements=[
            {"category": "기능", "requirement": "관리자 대시보드", "priority": "권장"}
        ],
        evaluation_criteria=[
            {"category": "기술", "item": "아키텍처", "weight": 30, "description": "확장성"}
        ],
        deliverables=[
            {"name": "결과보고서", "description": "최종 산출물", "phase": "종료"}
        ],
        key_success_factors=["기관 협업"],
        potential_risks=["일정 지연"],
        winning_strategy="검증된 구축 방법론을 강조합니다.",
        differentiation_points=["공공 프로젝트 경험"],
        project_type="it_system",
        pain_points=["분산된 데이터"],
        hidden_needs=["운영 자동화"],
        evaluation_strategy={"high_weight_items": ["아키텍처"]},
        win_theme_candidates=[
            {
                "name": "안정적 전환",
                "rationale": "중단 없는 이전",
                "rfp_alignment": "가용성 요구와 부합",
            }
        ],
        competitive_landscape="대형 SI와 경쟁 예상",
        raw_sections={"section": "원문"},
    )


def test_bid_trigger_registers_background_task_and_sets_analyzing(db):
    bg = BackgroundTasks()

    body = asyncio.run(main.analysis_trigger("bid", "TEST-PDF", bg))

    assert body == {"status": "analyzing"}
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is main._run_analysis_bg
    assert task.args == ("bid", "TEST-PDF")
    assert task.kwargs["url"] == "https://example.test/rfp.pdf"

    with db() as s:
        row = repository.get_analysis(s, "bid", "TEST-PDF")
        assert row is not None
        assert row.status == "analyzing"
        assert row.source_kind == "auto"


def test_bid_trigger_already_analyzing_does_not_register_task(db):
    with db() as s:
        repository.start_analysis(s, "bid", "TEST-PDF", "auto")
    bg = BackgroundTasks()

    body = asyncio.run(main.analysis_trigger("bid", "TEST-PDF", bg))

    assert body == {"status": "analyzing"}
    assert bg.tasks == []


def test_bid_trigger_need_upload_without_db_change(db):
    bg = BackgroundTasks()

    body = asyncio.run(main.analysis_trigger("bid", "TEST-NORFP", bg))

    assert body["status"] == "need_upload"
    assert "업로드" in body["message"]
    assert bg.tasks == []
    with db() as s:
        assert repository.get_analysis(s, "bid", "TEST-NORFP") is None


def test_bid_trigger_not_found(client):
    resp = client.post("/api/analysis/bid/NOT-FOUND")

    assert resp.status_code == 404


def test_bid_background_success_updates_done(db, monkeypatch):
    async def fake_analyze_from_url(url: str) -> ServiceAnalysisResult:
        assert url == "https://example.test/rfp.pdf"
        return ServiceAnalysisResult(status="ok", analysis=_full_analysis())

    monkeypatch.setattr(main, "analyze_from_url", fake_analyze_from_url)
    with db() as s:
        repository.start_analysis(s, "bid", "TEST-PDF", "auto")

    asyncio.run(
        main._run_analysis_bg("bid", "TEST-PDF", url="https://example.test/rfp.pdf")
    )

    with db() as s:
        row = repository.get_analysis(s, "bid", "TEST-PDF")
        assert row is not None
        assert row.status == "done"
        assert row.error_message is None
        saved = json.loads(row.result_json)
        assert saved["project_name"] == "스마트 통합 플랫폼"


def test_bid_background_error_updates_error(db, monkeypatch):
    async def fake_analyze_from_url(url: str) -> ServiceAnalysisResult:
        return ServiceAnalysisResult(status="error", message="분석 실패")

    monkeypatch.setattr(main, "analyze_from_url", fake_analyze_from_url)
    with db() as s:
        repository.start_analysis(s, "bid", "TEST-PDF", "auto")

    asyncio.run(
        main._run_analysis_bg("bid", "TEST-PDF", url="https://example.test/rfp.pdf")
    )

    with db() as s:
        row = repository.get_analysis(s, "bid", "TEST-PDF")
        assert row is not None
        assert row.status == "error"
        assert row.error_message == "분석 실패"


def test_bid_status_endpoint(client, db):
    with db() as s:
        repository.start_analysis(s, "bid", "TEST-PDF", "auto")

    resp = client.get("/api/analysis/bid/TEST-PDF/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "analyzing"
    assert body["source_kind"] == "auto"
    assert body["updated_at"]


def test_bid_status_none(client):
    resp = client.get("/api/analysis/bid/TEST-PDF/status")

    assert resp.status_code == 200
    assert resp.json() == {"status": "none"}


def test_bid_analysis_html_renders_object_fields(client, db):
    with db() as s:
        repository.set_analysis_done(
            s,
            "bid",
            "TEST-PDF",
            json.dumps(_full_analysis().model_dump(), ensure_ascii=False),
        )

    resp = client.get("/analysis/bid/TEST-PDF")

    assert resp.status_code == 200
    html = resp.text
    assert "[object Object]" not in html
    assert "스마트 통합 플랫폼" in html
    assert "100,000,000원" in html
    assert "분석 — 1개월" in html
    assert "[기능] 실시간 데이터 수집 (필수)" in html
    assert "아키텍처" in html
    assert "안정적 전환" in html
