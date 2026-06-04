"""Phase 8.1 — analysis_result 모델/repository 단위 테스트."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app import repository
from app.db import Base
from app.models import AnalysisResult


@pytest.fixture
def engine(tmp_path):
    db_path = tmp_path / "analysis_repository.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Local = sessionmaker(bind=engine, autoflush=False, future=True)
    with Local() as s:
        yield s


def test_analysis_result_model_shape(engine):
    inspector = inspect(engine)

    assert "analysis_result" in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns("analysis_result")}
    assert {
        "id",
        "source_type",
        "source_id",
        "status",
        "result_json",
        "error_message",
        "source_kind",
        "created_at",
        "updated_at",
    } <= cols
    uniques = inspector.get_unique_constraints("analysis_result")
    assert any(
        set(u["column_names"]) == {"source_type", "source_id"}
        for u in uniques
    )


def test_start_analysis_inserts_and_upserts(session):
    repository.start_analysis(session, "bid", "BID-1", "auto")
    row = repository.get_analysis(session, "bid", "BID-1")

    assert row is not None
    assert row.status == "analyzing"
    assert row.source_kind == "auto"
    assert row.result_json is None
    assert row.error_message is None
    assert isinstance(row.created_at, datetime)
    first_id = row.id

    repository.set_analysis_error(session, "bid", "BID-1", "실패")
    repository.start_analysis(session, "bid", "BID-1", "upload")
    row = repository.get_analysis(session, "bid", "BID-1")

    assert row.id == first_id
    assert row.status == "analyzing"
    assert row.source_kind == "upload"
    assert row.error_message is None
    assert row.result_json is None


def test_set_analysis_done_and_error(session):
    repository.start_analysis(session, "pre_spec", "PS-1", "auto")

    repository.set_analysis_done(session, "pre_spec", "PS-1", '{"ok": true}')
    row = repository.get_analysis(session, "pre_spec", "PS-1")
    assert row is not None
    assert row.status == "done"
    assert row.result_json == '{"ok": true}'
    assert row.error_message is None

    repository.set_analysis_error(session, "pre_spec", "PS-1", "분석 오류")
    row = repository.get_analysis(session, "pre_spec", "PS-1")
    assert row.status == "error"
    assert row.result_json == '{"ok": true}'
    assert row.error_message == "분석 오류"


def test_reset_stale_analyzing(session):
    session.add_all(
        [
            AnalysisResult(source_type="bid", source_id="B1", status="analyzing"),
            AnalysisResult(source_type="bid", source_id="B2", status="done"),
            AnalysisResult(source_type="pre_spec", source_id="P1", status="analyzing"),
        ]
    )
    session.commit()

    count = repository.reset_stale_analyzing(session)

    assert count == 2
    assert repository.get_analysis(session, "bid", "B1").status == "error"
    assert repository.get_analysis(session, "bid", "B1").error_message == "서버 재시작으로 중단됨"
    assert repository.get_analysis(session, "pre_spec", "P1").status == "error"
    assert repository.get_analysis(session, "bid", "B2").status == "done"


def test_get_analysis_status_map_single_query_contract(session):
    repository.start_analysis(session, "bid", "B1", "auto")
    repository.set_analysis_done(session, "bid", "B2", "{}")
    repository.start_analysis(session, "pre_spec", "P1", "auto")

    status_map = repository.get_analysis_status_map(session, "bid", ["B1", "B2", "B3"])

    assert status_map == {"B1": "analyzing", "B2": "done"}
    assert repository.get_analysis_status_map(session, "bid", []) == {}
