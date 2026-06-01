"""PreSpec(pre_spec) ORM 모델 + init_db 확장 단위테스트 — Phase 5.1.

네트워크/실 DB(procurement.db) 비의존. 임시 파일 SQLite 엔진을 만들어
테이블 생성·PK·컬럼·인덱스를 검증하고, init_db() 는 실 engine/SessionLocal 을
임시 DB 로 monkeypatch 한 뒤 호출해 테이블 4종 생성·app_config 시드 회귀를 확인한다.
시각은 고정 datetime 으로 넣어 결정적으로 만든다(datetime.now() 미사용).
실행: `pytest tests/test_models_prespec.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AppConfig, PreSpec


# pre_spec 모델이 정의해야 하는 전체 컬럼(순서 무관, 이름·개수 검증용).
EXPECTED_PRE_SPEC_COLUMNS = {
    "bf_spec_rgst_no",
    "bsns_div_nm",
    "ref_no",
    "prdct_clsfc_no_nm",
    "order_instt_nm",
    "rl_dminstt_nm",
    "asign_bdgt_amt",
    "rcpt_dt",
    "opnin_rgst_clse_dt",
    "ofcl_nm",
    "ofcl_tel_no",
    "sw_biz_obj_yn",
    "dlvr_tmlmt_dt",
    "dlvr_daynum",
    "spec_doc_file_url1",
    "spec_doc_file_url2",
    "spec_doc_file_url3",
    "spec_doc_file_url4",
    "spec_doc_file_url5",
    "prdct_dtl_list",
    "bid_ntce_no_list",
    "rgst_dt",
    "chg_dt",
    "raw_json",
    "collected_at",
    "updated_at",
}

# 인덱스가 걸려야 하는 컬럼 3종.
EXPECTED_INDEXED_COLUMNS = {"rcpt_dt", "opnin_rgst_clse_dt", "sw_biz_obj_yn"}


# --- 픽스처: 임시 파일 SQLite 엔진(스키마만 생성) ----------------------
@pytest.fixture
def engine(tmp_path):
    """임시 파일 SQLite 엔진 + Base.metadata.create_all(모든 모델 스키마)."""
    db_path = tmp_path / "prespec_schema.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


# --- 1. 테이블 생성·PK·컬럼 -------------------------------------------
def test_pre_spec_table_created(engine):
    inspector = inspect(engine)
    assert "pre_spec" in inspector.get_table_names()


def test_pre_spec_primary_key_is_bf_spec_rgst_no(engine):
    inspector = inspect(engine)
    pk = inspector.get_pk_constraint("pre_spec")
    assert pk["constrained_columns"] == ["bf_spec_rgst_no"]


def test_pre_spec_has_all_expected_columns(engine):
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("pre_spec")}
    assert cols == EXPECTED_PRE_SPEC_COLUMNS


def test_pre_spec_meta_columns_not_nullable(engine):
    inspector = inspect(engine)
    by_name = {c["name"]: c for c in inspector.get_columns("pre_spec")}
    assert by_name["collected_at"]["nullable"] is False
    assert by_name["updated_at"]["nullable"] is False


# --- 2. 인덱스 3종 -----------------------------------------------------
def test_pre_spec_indexes(engine):
    inspector = inspect(engine)
    indexes = inspector.get_indexes("pre_spec")
    # 각 인덱스는 단일 컬럼에 걸린다. 인덱싱된 컬럼 집합을 모은다.
    indexed_cols = {tuple(idx["column_names"]) for idx in indexes}
    assert {("rcpt_dt",), ("opnin_rgst_clse_dt",), ("sw_biz_obj_yn",)} <= indexed_cols
    # 인덱싱된 컬럼은 정확히 3종(과잉 인덱스 없음).
    flat = {col for idx in indexes for col in idx["column_names"]}
    assert flat == EXPECTED_INDEXED_COLUMNS


# --- 3. insert/select 라운드트립 --------------------------------------
def test_pre_spec_roundtrip(engine):
    Local = sessionmaker(bind=engine, future=True)
    fixed = datetime(2026, 1, 1, 12, 0, 0)
    with Local() as s:
        s.add(
            PreSpec(
                bf_spec_rgst_no="R26BD00222439",
                bsns_div_nm="용역",
                prdct_clsfc_no_nm="테스트 사업명",
                order_instt_nm="테스트발주기관",
                rl_dminstt_nm="테스트수요기관",
                asign_bdgt_amt=44000000,
                rcpt_dt=datetime(2026, 1, 1, 9, 0, 0),
                opnin_rgst_clse_dt=datetime(2026, 1, 6, 23, 59, 0),
                sw_biz_obj_yn="Y",
                bid_ntce_no_list="20260400091,20260413548",
                raw_json="{}",
                collected_at=fixed,
                updated_at=fixed,
            )
        )
        s.commit()

    with Local() as s:
        row = s.get(PreSpec, "R26BD00222439")
        assert row is not None
        assert row.bf_spec_rgst_no == "R26BD00222439"
        assert row.bsns_div_nm == "용역"
        assert int(row.asign_bdgt_amt) == 44000000
        assert row.sw_biz_obj_yn == "Y"
        assert row.bid_ntce_no_list == "20260400091,20260413548"
        assert row.collected_at == fixed
        assert row.updated_at == fixed


# --- 4. init_db(): 테이블 4종 + app_config 시드 회귀 없음 --------------
def test_init_db_creates_four_tables_and_seeds_app_config(tmp_path, monkeypatch):
    """init_db() 를 임시 DB 로 monkeypatch 후 호출 — 테이블 4종·시드 1행 검증."""
    from app import db as db_module

    db_path = tmp_path / "init_db_test.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Local = sessionmaker(bind=eng, autoflush=False, future=True)

    # init_db()/_seed_app_config() 가 참조하는 모듈 전역을 임시 DB 로 교체.
    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", Local)

    db_module.init_db()

    inspector = inspect(eng)
    tables = set(inspector.get_table_names())
    assert {"bid_notice", "collection_run", "app_config", "pre_spec"} <= tables

    # app_config 시드 단일 행(id=1) 회귀 없음.
    with Local() as s:
        cfg = s.get(AppConfig, 1)
        assert cfg is not None
        assert cfg.id == 1
        assert cfg.enabled is True
        # pre_spec 에는 시드 행이 없어야 한다.
        count = s.execute(select(PreSpec)).scalars().all()
        assert count == []


def test_init_db_idempotent(tmp_path, monkeypatch):
    """init_db() 재호출이 멱등(테이블·시드 중복/오류 없음)."""
    from app import db as db_module

    db_path = tmp_path / "init_db_idem.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Local = sessionmaker(bind=eng, autoflush=False, future=True)
    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", Local)

    db_module.init_db()
    db_module.init_db()  # 두 번째 호출도 안전해야 한다.

    with Local() as s:
        configs = s.execute(select(AppConfig)).scalars().all()
        assert len(configs) == 1  # 시드 행이 중복 생성되지 않음.
