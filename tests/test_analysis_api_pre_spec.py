"""Phase 8.1 — 사전규격 분석/업로드 비동기 API 테스트."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytest
from fastapi import BackgroundTasks, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

from app import main, repository
from app.analysis.analyzer_service import AnalysisResult as ServiceAnalysisResult
from app.analysis.rfp_schema import RFPAnalysis
from app.db import Base
from app.models import AppConfig, PreSpec


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
    return PreSpec(
        bf_spec_rgst_no=no,
        prdct_clsfc_no_nm=f"사전규격 {no}",
        order_instt_nm="테스트기관",
        sw_biz_obj_yn="Y",
        collected_at=_META,
        updated_at=_META,
        spec_doc_file_url1=url1,
        spec_doc_file_url2=url2,
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "pre_spec_analysis_api.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        s.add(_ps("PS001", url1="https://example.test/spec.pdf"))
        s.add(_ps("PS002"))
        s.add(_ps("PS003", url1="https://example.test/spec.zip"))
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return Local


@pytest.fixture
def client(db):
    return TestClient(main.app)


def _analysis() -> RFPAnalysis:
    return RFPAnalysis(
        project_name="사전규격 분석 사업",
        client_name="행정안전부",
        project_overview="사전규격 기반 사업 개요",
        budget={"total_budget": "55,000,000원"},
        timeline={"total_duration": "3개월", "phases": [{"name": "구축", "duration": "2개월"}]},
        evaluation_criteria=[
            {"category": "수행", "item": "사업 이해도", "weight": 20, "description": "이해도 평가"}
        ],
        deliverables=[{"name": "착수보고서", "phase": "착수"}],
        pain_points=["짧은 의견 기간"],
        hidden_needs=["빠른 착수"],
        key_success_factors=["사전 협의"],
        potential_risks=["요구 변경"],
        differentiation_points=["전담 PM"],
        winning_strategy="초기 리스크를 선제 관리합니다.",
        win_theme_candidates=[
            {"name": "빠른 안정화", "rationale": "기간 단축", "rfp_alignment": "납기와 부합"}
        ],
    )


class _CountingUploadFile:
    def __init__(self, filename: str, payload=b"%PDF-1.4", *, fail_on_read: bool = False):
        self.filename = filename
        self._payload = payload
        self._fail_on_read = fail_on_read
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        if self._fail_on_read:
            raise AssertionError("read() 호출 전에 반환되어야 합니다.")
        if callable(self._payload):
            return self._payload()
        return self._payload


def test_find_auto_analysis_urls_pre_spec_first_file(db):
    with db() as s:
        urls = main._find_auto_analysis_urls(s, "pre_spec", "PS001")

    assert urls == [
        {"url": "https://example.test/spec.pdf", "name": "첨부1", "doc_kind": "기타"}
    ]


def test_find_auto_analysis_urls_pre_spec_no_file(db):
    with db() as s:
        assert main._find_auto_analysis_urls(s, "pre_spec", "PS002") == []


def test_pre_spec_trigger_registers_background_task_with_standard_type(db):
    bg = BackgroundTasks()

    body = asyncio.run(main.analysis_trigger("pre_spec", "PS001", bg))

    assert body == {"status": "analyzing"}
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is main._run_analysis_bg
    assert task.args == ("pre_spec", "PS001")
    assert task.kwargs["items"] == [
        {"url": "https://example.test/spec.pdf", "name": "첨부1", "doc_kind": "기타"}
    ]
    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "analyzing"
        assert row.source_kind == "auto"


def test_pre_spec_trigger_accepts_dash_alias(db):
    bg = BackgroundTasks()

    body = asyncio.run(main.analysis_trigger("pre-spec", "PS001", bg))

    assert body["status"] == "analyzing"
    with db() as s:
        assert repository.get_analysis(s, "pre_spec", "PS001") is not None


def test_pre_spec_need_upload(client):
    resp = client.post("/api/analysis/pre_spec/PS002")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "need_upload"
    assert "업로드" in body["message"]


def test_pre_spec_not_found(client):
    resp = client.post("/api/analysis/pre_spec/NOT-FOUND")

    assert resp.status_code == 404


def test_pre_spec_background_success_updates_done(db, monkeypatch):
    async def fake_analyze_from_urls(items: list[dict]) -> ServiceAnalysisResult:
        assert items == [
            {"url": "https://example.test/spec.pdf", "doc_kind": "기타"}
        ]
        return ServiceAnalysisResult(status="ok", analysis=_analysis())

    monkeypatch.setattr(main, "analyze_from_urls", fake_analyze_from_urls)
    with db() as s:
        repository.start_analysis(s, "pre_spec", "PS001", "auto")

    asyncio.run(
        main._run_analysis_bg(
            "pre_spec",
            "PS001",
            items=[{"url": "https://example.test/spec.pdf", "doc_kind": "기타"}],
        )
    )

    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "done"
        assert json.loads(row.result_json)["project_name"] == "사전규격 분석 사업"


def test_pre_spec_background_exception_updates_error(db, monkeypatch):
    async def fake_analyze_from_urls(items: list[dict]) -> ServiceAnalysisResult:
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(main, "analyze_from_urls", fake_analyze_from_urls)
    with db() as s:
        repository.start_analysis(s, "pre_spec", "PS001", "auto")

    asyncio.run(
        main._run_analysis_bg(
            "pre_spec",
            "PS001",
            items=[{"url": "https://example.test/spec.pdf", "doc_kind": "기타"}],
        )
    )

    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "error"
        assert "provider timeout" in row.error_message


def test_upload_starts_analysis_and_registers_background_task(client, db, monkeypatch):
    calls: list[dict] = []

    async def fake_run_bg(source_type: str, source_id: str, **kwargs):
        calls.append({"source_type": source_type, "source_id": source_id, **kwargs})

    monkeypatch.setattr(main, "_run_analysis_bg", fake_run_bg)

    resp = client.post(
        "/api/analysis/upload",
        data={"source_type": "pre_spec", "source_id": "PS001"},
        files=[
            ("files", ("manual.pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("task.docx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ],
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "analyzing"}
    assert calls == [
        {
            "source_type": "pre_spec",
            "source_id": "PS001",
            "items": [
                {"filename": "manual.pdf", "bytes": b"%PDF-1.4", "label": "manual.pdf"},
                {"filename": "task.docx", "bytes": b"PK\x03\x04", "label": "task.docx"},
            ],
        }
    ]
    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "analyzing"
        assert row.source_kind == "upload"


def test_upload_accepts_legacy_file_field_and_registers_background_task(client, monkeypatch):
    calls: list[dict] = []
    registered_tasks: list[tuple[object, tuple, dict]] = []
    original_add_task = BackgroundTasks.add_task

    async def fake_run_bg(source_type: str, source_id: str, **kwargs):
        calls.append({"source_type": source_type, "source_id": source_id, **kwargs})

    def recording_add_task(self, func, *args, **kwargs):  # noqa: ANN001
        registered_tasks.append((func, args, kwargs))
        return original_add_task(self, func, *args, **kwargs)

    monkeypatch.setattr(main, "_run_analysis_bg", fake_run_bg)
    monkeypatch.setattr(BackgroundTasks, "add_task", recording_add_task)

    resp = client.post(
        "/api/analysis/upload",
        data={"source_type": "pre_spec", "source_id": "PS001"},
        files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
    )

    expected_items = [{"filename": "manual.pdf", "bytes": b"%PDF-1.4", "label": "manual.pdf"}]
    assert resp.status_code == 200
    assert resp.json() == {"status": "analyzing"}
    assert len(registered_tasks) == 1
    func, args, kwargs = registered_tasks[0]
    assert func is fake_run_bg
    assert args == ("pre_spec", "PS001")
    assert kwargs == {"items": expected_items}
    assert calls == [{"source_type": "pre_spec", "source_id": "PS001", "items": expected_items}]


def test_upload_accepts_type_id_alias(client, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_run_bg(source_type: str, source_id: str, **kwargs):
        calls.append((source_type, source_id))

    monkeypatch.setattr(main, "_run_analysis_bg", fake_run_bg)

    resp = client.post(
        "/api/analysis/upload",
        data={"type": "pre_spec", "id": "PS001"},
        files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "analyzing"
    assert calls == [("pre_spec", "PS001")]


def test_upload_already_analyzing_does_not_read_files_or_register_background_task(db):
    with db() as s:
        repository.start_analysis(s, "pre_spec", "PS001", "auto")

    bg = BackgroundTasks()
    response = Response()
    upload_file = _CountingUploadFile(
        "too-large.pdf",
        payload=lambda: b"x" * (main._ANALYSIS_MAX_BYTES + 1),
    )

    body = asyncio.run(
        main.analysis_upload(
            background_tasks=bg,
            response=response,
            files=[upload_file],
            source_type="pre_spec",
            source_id="PS001",
        )
    )

    assert response.status_code == 200
    assert body == {"status": "analyzing"}
    assert upload_file.read_count == 0
    assert bg.tasks == []
    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "analyzing"
        assert row.source_kind == "auto"


def test_upload_file_too_large(client):
    large_bytes = b"x" * (50 * 1024 * 1024 + 1)

    resp = client.post(
        "/api/analysis/upload",
        data={"source_type": "pre_spec", "source_id": "PS001"},
        files={"files": ("big.pdf", large_bytes, "application/pdf")},
    )

    assert resp.status_code == 413
    assert resp.json()["status"] == "error"
    assert "50MB" in resp.json()["message"]


def test_upload_total_too_large(client):
    chunk = b"x" * (50 * 1024 * 1024)

    resp = client.post(
        "/api/analysis/upload",
        data={"source_type": "pre_spec", "source_id": "PS001"},
        files=[
            ("files", ("a.pdf", chunk, "application/pdf")),
            ("files", ("b.pdf", chunk, "application/pdf")),
            ("files", ("c.pdf", b"x", "application/pdf")),
        ],
    )

    assert resp.status_code == 413
    assert resp.json()["status"] == "error"
    assert "100MB" in resp.json()["message"]


def test_upload_zero_files_returns_400(client):
    resp = client.post(
        "/api/analysis/upload",
        data={"source_type": "pre_spec", "source_id": "PS001"},
    )

    assert resp.status_code == 400
    assert resp.json()["status"] == "error"


def test_upload_too_many_files_returns_413_without_reading_files(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", db)
    bg = BackgroundTasks()
    response = Response()
    upload_files = [
        _CountingUploadFile(f"manual-{idx}.pdf", fail_on_read=True)
        for idx in range(main._ANALYSIS_UPLOAD_MAX_FILES + 1)
    ]

    body = asyncio.run(
        main.analysis_upload(
            background_tasks=bg,
            response=response,
            files=upload_files,
            source_type="pre_spec",
            source_id="PS001",
        )
    )

    assert response.status_code == 413
    assert body["status"] == "error"
    assert str(main._ANALYSIS_UPLOAD_MAX_FILES) in body["message"]
    assert [file.read_count for file in upload_files] == [0] * len(upload_files)
    assert bg.tasks == []


def test_upload_background_unsupported_updates_error(db, monkeypatch):
    async def fake_analyze_documents(items: list[dict]) -> ServiceAnalysisResult:
        return ServiceAnalysisResult(status="unsupported", message="지원하지 않는 형식: .zip")

    monkeypatch.setattr(main, "analyze_documents", fake_analyze_documents)
    with db() as s:
        repository.start_analysis(s, "pre_spec", "PS001", "upload")

    asyncio.run(
        main._run_analysis_bg(
            "pre_spec",
            "PS001",
            items=[{"bytes": b"PK\x03\x04", "filename": "archive.zip", "label": "archive.zip"}],
        )
    )

    with db() as s:
        row = repository.get_analysis(s, "pre_spec", "PS001")
        assert row is not None
        assert row.status == "error"
        assert ".zip" in row.error_message


def test_pre_spec_status_and_html(client, db):
    with db() as s:
        repository.set_analysis_done(
            s,
            "pre_spec",
            "PS001",
            json.dumps(_analysis().model_dump(), ensure_ascii=False),
        )

    status_resp = client.get("/api/analysis/pre_spec/PS001/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "done"

    html_resp = client.get("/analysis/pre_spec/PS001")
    assert html_resp.status_code == 200
    html = html_resp.text
    assert "[object Object]" not in html
    assert "사전규격 분석 사업" in html
    assert "55,000,000원" in html
    assert "사업 이해도" in html
    assert "빠른 안정화" in html
