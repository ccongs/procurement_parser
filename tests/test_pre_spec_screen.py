"""사전규격 화면(/pre-spec·/config 토글·실행이력 source) 테스트 — Phase 5.5.

- repository.search_pre_specs(필터·정렬·페이지네이션)는 인메모리 SQLite 로 검증.
- 라우트 스모크는 FastAPI TestClient + 임시 파일 SQLite(실 API·실 procurement.db 비의존).
- 시간 의존 로직(include_past_opnin)은 now 를 주입해 결정적으로 검증.
- /list·/config·root 리다이렉트 회귀 0 을 함께 확인한다.
실행: `pytest tests/test_pre_spec_screen.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repository
from app.db import Base
from app.models import AppConfig, CollectionRun, PreSpec


# --- 공용 픽스처 -------------------------------------------------------
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
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )


@pytest.fixture
def session():
    """인메모리 SQLite 세션 + app_config(id=1) 시드."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, future=True)
    s = Local()
    s.add(_cfg())
    s.commit()
    try:
        yield s
    finally:
        s.close()


_META = datetime(2026, 6, 1, 12, 0, 0)


def _ps(
    no,
    nm,
    *,
    order=None,
    rl=None,
    amt=None,
    rcpt=None,
    opnin=None,
):
    """테스트용 PreSpec(필수 NOT NULL 컬럼 채움)."""
    return PreSpec(
        bf_spec_rgst_no=no,
        prdct_clsfc_no_nm=nm,
        order_instt_nm=order,
        rl_dminstt_nm=rl,
        asign_bdgt_amt=amt,
        rcpt_dt=rcpt,
        opnin_rgst_clse_dt=opnin,
        sw_biz_obj_yn="Y",
        collected_at=_META,
        updated_at=_META,
    )


def _seed_specs(session):
    session.add_all(
        [
            _ps("P-1", "소프트웨어 유지보수 용역",
                order="행정안전부", rl="국세청", amt=30000000,
                rcpt=datetime(2026, 5, 1, 9, 0), opnin=datetime(2026, 7, 1, 10, 0)),
            _ps("P-2", "데이터베이스 구축 용역",
                order="조달청", rl="행정안전부", amt=10000000,
                rcpt=datetime(2026, 5, 10, 9, 0), opnin=datetime(2026, 6, 20, 10, 0)),
            _ps("P-3", "소프트웨어 개발 사업",
                order="국방부", rl="해군본부", amt=50000000,
                rcpt=datetime(2026, 5, 20, 9, 0), opnin=datetime(2026, 5, 25, 10, 0)),
            _ps("P-4", "청소 용역",
                order="교육부", rl=None, amt=None,
                rcpt=datetime(2026, 4, 1, 9, 0), opnin=None),
        ]
    )
    session.commit()


# =====================================================================
#  search_pre_specs 단위(인메모리)
# =====================================================================
def test_search_partial_product_name(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(session, q="소프트웨어")
    assert total == 2
    assert {r.bf_spec_rgst_no for r in rows} == {"P-1", "P-3"}


def test_search_product_name_no_match(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(session, q="존재하지않는품명")
    assert total == 0
    assert rows == []


def test_search_instt_matches_order_instt(session):
    _seed_specs(session)
    # "국방부"는 발주기관(order_instt_nm)에만 존재 → P-3.
    rows, total = repository.search_pre_specs(session, instt="국방부")
    assert total == 1
    assert rows[0].bf_spec_rgst_no == "P-3"


def test_search_instt_matches_rl_dminstt(session):
    _seed_specs(session)
    # "국세청"은 실수요기관(rl_dminstt_nm)에만 존재 → P-1.
    rows, total = repository.search_pre_specs(session, instt="국세청")
    assert total == 1
    assert rows[0].bf_spec_rgst_no == "P-1"


def test_search_instt_matches_either_column(session):
    _seed_specs(session)
    # "행정안전부"는 P-1 발주기관 + P-2 실수요기관 양쪽 → 둘 다.
    rows, total = repository.search_pre_specs(session, instt="행정안전부")
    assert total == 2
    assert {r.bf_spec_rgst_no for r in rows} == {"P-1", "P-2"}


def test_search_rcpt_dt_from(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(
        session, dt_from=datetime(2026, 5, 10, 0, 0)
    )
    # rcpt_dt >= 5/10 → P-2(5/10), P-3(5/20). P-1(5/1)·P-4(4/1) 제외.
    assert total == 2
    assert {r.bf_spec_rgst_no for r in rows} == {"P-2", "P-3"}


def test_search_rcpt_dt_to(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(
        session, dt_to=datetime(2026, 5, 10, 23, 59, 59)
    )
    # rcpt_dt <= 5/10 → P-1(5/1), P-2(5/10), P-4(4/1). P-3(5/20) 제외.
    assert total == 3
    assert {r.bf_spec_rgst_no for r in rows} == {"P-1", "P-2", "P-4"}


def test_search_rcpt_dt_range(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(
        session,
        dt_from=datetime(2026, 5, 5, 0, 0),
        dt_to=datetime(2026, 5, 15, 23, 59, 59),
    )
    # 5/5 ~ 5/15 → P-2(5/10)만.
    assert total == 1
    assert rows[0].bf_spec_rgst_no == "P-2"


def test_search_include_past_false_hides_past_keeps_null(session):
    _seed_specs(session)
    now = datetime(2026, 6, 1, 12, 0, 0)
    rows, total = repository.search_pre_specs(
        session, include_past_opnin=False, now=now
    )
    # opnin>=now(P-1 7/1, P-2 6/20) + NULL(P-4) 유지, 과거(P-3 5/25) 제외.
    assert total == 3
    assert {r.bf_spec_rgst_no for r in rows} == {"P-1", "P-2", "P-4"}


def test_search_include_past_true_shows_all(session):
    _seed_specs(session)
    now = datetime(2026, 6, 1, 12, 0, 0)
    rows, total = repository.search_pre_specs(
        session, include_past_opnin=True, now=now
    )
    assert total == 4
    assert {r.bf_spec_rgst_no for r in rows} == {"P-1", "P-2", "P-3", "P-4"}


# --- 정렬 6종(NULL 뒤로) ----------------------------------------------
def test_sort_rcpt_dt_desc_default(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="rcpt_dt_desc")
    # 최신 접수순: P-3(5/20), P-2(5/10), P-1(5/1), P-4(4/1).
    assert [r.bf_spec_rgst_no for r in rows] == ["P-3", "P-2", "P-1", "P-4"]


def test_sort_rcpt_dt_asc(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="rcpt_dt_asc")
    assert [r.bf_spec_rgst_no for r in rows] == ["P-4", "P-1", "P-2", "P-3"]


def test_sort_opnin_desc_nulls_last(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="opnin_rgst_clse_dt_desc")
    # opnin: P-1 7/1, P-2 6/20, P-3 5/25, P-4 NULL(뒤로).
    assert [r.bf_spec_rgst_no for r in rows] == ["P-1", "P-2", "P-3", "P-4"]


def test_sort_opnin_asc_nulls_last(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="opnin_rgst_clse_dt_asc")
    # asc: P-3 5/25, P-2 6/20, P-1 7/1, NULL(P-4) 뒤로.
    assert [r.bf_spec_rgst_no for r in rows] == ["P-3", "P-2", "P-1", "P-4"]


def test_sort_amt_desc_nulls_last(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="asign_bdgt_amt_desc")
    # amt: P-3 5천만, P-1 3천만, P-2 1천만, P-4 NULL(뒤로).
    assert [r.bf_spec_rgst_no for r in rows] == ["P-3", "P-1", "P-2", "P-4"]


def test_sort_amt_asc_nulls_last(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="asign_bdgt_amt_asc")
    # asc: P-2 1천만, P-1 3천만, P-3 5천만, NULL(P-4) 뒤로.
    assert [r.bf_spec_rgst_no for r in rows] == ["P-2", "P-1", "P-3", "P-4"]


def test_sort_invalid_falls_back_to_default(session):
    _seed_specs(session)
    rows, _ = repository.search_pre_specs(session, sort="존재하지않는정렬")
    # rcpt_dt_desc 로 폴백.
    assert [r.bf_spec_rgst_no for r in rows] == ["P-3", "P-2", "P-1", "P-4"]


# --- 페이지네이션 -----------------------------------------------------
def test_pagination(session):
    _seed_specs(session)
    rows1, total1 = repository.search_pre_specs(
        session, sort="rcpt_dt_desc", page=1, page_size=2
    )
    rows2, total2 = repository.search_pre_specs(
        session, sort="rcpt_dt_desc", page=2, page_size=2
    )
    assert total1 == total2 == 4
    assert [r.bf_spec_rgst_no for r in rows1] == ["P-3", "P-2"]
    assert [r.bf_spec_rgst_no for r in rows2] == ["P-1", "P-4"]


def test_pagination_total_respects_filter(session):
    _seed_specs(session)
    rows, total = repository.search_pre_specs(
        session, q="소프트웨어", page=1, page_size=1
    )
    # 필터 결과 2건 → total=2, 페이지엔 1건.
    assert total == 2
    assert len(rows) == 1


def test_search_empty_db(session):
    rows, total = repository.search_pre_specs(session)
    assert total == 0
    assert rows == []


# =====================================================================
#  라우트 스모크 (FastAPI TestClient + 임시 DB)
# =====================================================================
@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 파일 SQLite 로 main.SessionLocal 을 교체한 TestClient.

    실 API·실 procurement.db 에 의존하지 않는다. app_config 시드 + 사전규격 + run.
    """
    from fastapi.testclient import TestClient

    from app import main

    db_path = tmp_path / "pre_spec_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        # 의견마감 미래(확실히 안 지남) → 기본(지난 마감 숨김)에서도 표시(결정적).
        s.add(
            _ps("S-FUTURE", "스모크 미래마감 사전규격",
                order="행정안전부", rl="국세청", amt=12000000,
                rcpt=datetime(2026, 5, 20, 9, 0),
                opnin=datetime(2099, 1, 1, 10, 0))
        )
        # 의견마감 NULL → 기본에서도 항상 표시(결정적).
        s.add(
            _ps("S-NULL", "스모크 마감미정 사전규격",
                order="조달청", rl=None, amt=None,
                rcpt=datetime(2026, 5, 21, 9, 0), opnin=None)
        )
        # 의견마감 확실히 과거(2000년) → 기본 숨김, "지난 마감 포함" 시 노출(결정적).
        s.add(
            _ps("S-PAST", "스모크 지난마감 사전규격",
                order="국방부", rl="해군본부", amt=7000000,
                rcpt=datetime(2026, 5, 22, 9, 0),
                opnin=datetime(2000, 1, 1, 10, 0))
        )
        # 실행이력: 입찰 1건(source 기본 bid 확인)·사전규격 1건.
        base = datetime(2026, 6, 1, 12, 0)
        s.add(
            CollectionRun(
                trigger="scheduled", run_started_at=base,
                window_bgn_dt=base, window_end_dt=base, status="success",
                total_fetched=10, total_new=10, total_updated=0, retry_count=0,
                source="bid",
            )
        )
        s.add(
            CollectionRun(
                trigger="scheduled", run_started_at=base,
                window_bgn_dt=base, window_end_dt=base, status="success",
                total_fetched=5, total_new=5, total_updated=0, retry_count=0,
                source="pre_spec",
            )
        )
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return TestClient(main.app)


# 접수일 기본 기간(오늘-1개월~오늘)에 의존하지 않도록 넓은 범위를 명시(결정적).
_WIDE = {"dt_from": "2000-01-01", "dt_to": "2099-12-31"}


def test_pre_spec_page_ok(client):
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "사전규격 검색" in resp.text
    assert "스모크 미래마감 사전규격" in resp.text
    assert "사전규격번호" in resp.text  # 헤더


def test_pre_spec_default_hides_past_opnin_keeps_null(client):
    # 기본(지난 마감 숨김): 미래·NULL 은 보이고, 과거(S-PAST)는 숨김.
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "스모크 미래마감 사전규격" in resp.text
    assert "스모크 마감미정 사전규격" in resp.text  # NULL 항상 표시
    assert "스모크 지난마감 사전규격" not in resp.text  # 기본 숨김


def test_pre_spec_include_past_shows_past(client):
    resp = client.get("/pre-spec", params={**_WIDE, "include_past": "1"})
    assert resp.status_code == 200
    assert "스모크 지난마감 사전규격" in resp.text  # 포함 시 노출


def test_pre_spec_filter_q(client):
    resp = client.get("/pre-spec", params={**_WIDE, "q": "마감미정"})
    assert resp.status_code == 200
    assert "스모크 마감미정 사전규격" in resp.text
    assert "스모크 미래마감 사전규격" not in resp.text


def test_pre_spec_filter_instt(client):
    # "해군본부"는 S-PAST 실수요기관 → include_past 와 함께여야 노출.
    resp = client.get("/pre-spec", params={**_WIDE, "instt": "해군본부", "include_past": "1"})
    assert resp.status_code == 200
    assert "스모크 지난마감 사전규격" in resp.text
    assert "스모크 미래마감 사전규격" not in resp.text


def test_pre_spec_filter_rcpt_date_range(client):
    # 접수 5/21 하루만 → S-NULL(5/21)만.
    resp = client.get(
        "/pre-spec", params={"dt_from": "2026-05-21", "dt_to": "2026-05-21"}
    )
    assert resp.status_code == 200
    assert "스모크 마감미정 사전규격" in resp.text
    assert "스모크 미래마감 사전규격" not in resp.text


def test_pre_spec_empty_result_message(client):
    resp = client.get("/pre-spec", params={**_WIDE, "q": "절대없는품명xyz"})
    assert resp.status_code == 200
    assert "조건에 맞는 사전규격이 없습니다" in resp.text


def test_pre_spec_sort_header_links_and_default_arrow(client):
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    # 헤더 정렬 링크는 /pre-spec 경로로 생성.
    assert "/pre-spec?" in resp.text
    assert "sort=" in resp.text
    # 기본 정렬(rcpt_dt_desc) → 접수일시 헤더에 ▼.
    assert "▼" in resp.text


def test_pre_spec_pager_path(client):
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "전체" in resp.text and "페이지" in resp.text


# --- 네비 -------------------------------------------------------------
def test_nav_has_pre_spec_link_on_pre_spec(client):
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/pre-spec"' in resp.text
    # active 표기(현재 페이지).
    assert 'href="/pre-spec" class="active"' in resp.text


def test_nav_has_pre_spec_link_on_list(client):
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/pre-spec"' in resp.text


def test_nav_has_pre_spec_link_on_config(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'href="/pre-spec"' in resp.text


# --- / 리다이렉트 회귀 -------------------------------------------------
def test_root_still_redirects_to_list(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/list"


# --- /config 사전규격 토글 + 실행이력 source ---------------------------
def test_config_page_renders_pre_spec_toggle_checked(client):
    # 시드 pre_spec_enabled=True → 체크박스 checked.
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "pre_spec_enabled" in resp.text
    assert 'name="pre_spec_enabled" value="1" checked' in resp.text


def test_config_runs_table_has_source_column(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "수집원" in resp.text  # 헤더
    assert "pre_spec" in resp.text  # 사전규격 run 배지
    assert "bid" in resp.text  # 입찰 run 배지


def test_config_save_pre_spec_enabled_on(client):
    resp = client.post(
        "/config",
        data={
            "interval_minutes": "60",
            "window_overlap_minutes": "90",
            "backfill_days": "30",
            "num_of_rows": "20",
            "max_retries": "2",
            "inqry_div": "1",
            "intrntnl_div_cd": "1",
            "indstryty_cds": "1426,1468",
            "enabled": "1",
            "pre_spec_enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # 저장값 확인.
    from app import main
    with main.SessionLocal() as s:
        cfg = repository.get_config(s)
        assert cfg.pre_spec_enabled is True


def test_config_save_pre_spec_enabled_off_when_unchecked(client):
    # 체크박스 미포함 → False 로 저장(토글 off).
    resp = client.post(
        "/config",
        data={
            "interval_minutes": "60",
            "window_overlap_minutes": "90",
            "backfill_days": "30",
            "num_of_rows": "20",
            "max_retries": "2",
            "inqry_div": "1",
            "intrntnl_div_cd": "1",
            "indstryty_cds": "1426",
            "enabled": "1",
            # pre_spec_enabled 미포함
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from app import main
    with main.SessionLocal() as s:
        cfg = repository.get_config(s)
        assert cfg.pre_spec_enabled is False


def test_config_save_invalid_still_400_no_regression(client):
    # 기존 /config 검증 회귀 0: 범위 위반은 여전히 400.
    resp = client.post(
        "/config",
        data={
            "interval_minutes": "0",  # 범위 위반(>=1)
            "window_overlap_minutes": "90",
            "backfill_days": "30",
            "num_of_rows": "20",
            "max_retries": "2",
            "inqry_div": "1",
            "intrntnl_div_cd": "1",
            "indstryty_cds": "1426",
            "pre_spec_enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "범위" in resp.text


# --- /list·/config 기존 동작 회귀(base_path 일반화 확인) ---------------
def test_list_page_still_uses_list_path_in_sort_and_pager(client):
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # /list 정렬·페이저 링크는 여전히 /list 경로(일반화로 인한 회귀 없음).
    assert "/list?" in resp.text
    assert "/pre-spec?" not in resp.text


# =====================================================================
#  Phase 4.9-A Wave A 회귀: 탭 active·페이저·정렬 화살표
# =====================================================================

def test_pre_spec_tab_active_on_pre_spec_page(client):
    """/pre-spec 에서 사전규격목록 탭이 active, 입찰공고목록 탭은 비활성."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/pre-spec" class="active"' in resp.text
    assert 'href="/list" class="active"' not in resp.text


def test_list_tab_present_on_pre_spec_page(client):
    """/pre-spec 에 /list 탭 링크가 존재한다."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/list"' in resp.text


def test_pre_spec_pager_has_page_info(client):
    """페이저에 전체건수·페이지 텍스트가 보존된다."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "전체" in resp.text
    assert "페이지" in resp.text


def test_pre_spec_sort_header_neutral_arrow(client):
    """미정렬 컬럼에 ↕ 중립 화살표가 존재한다."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "↕" in resp.text


def test_pre_spec_config_has_api_test_link(client):
    """/config 에 API테스트 링크(새 탭)가 존재한다."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'href="/api-test"' in resp.text
    assert "API테스트 열기" in resp.text
