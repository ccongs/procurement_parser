"""화면(/list·/config·/api-test) repository 조회 + 라우트 스모크 테스트 — Phase 3.5.

- repository 조회(search_bid_notices/update_config/list_recent_runs)는 인메모리 SQLite 로 검증.
- 라우트 스모크는 FastAPI TestClient + 임시 파일 SQLite(실 API·실 procurement.db 비의존).
- 시간 의존 로직(openg_only_future)은 now 를 주입해 결정적으로 검증.
실행: `pytest tests/test_screens.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repository
from app.db import Base
from app.models import AppConfig, BidNotice, CollectionRun


# --- 공용 픽스처 -------------------------------------------------------
@pytest.fixture
def session():
    """인메모리 SQLite 세션 + app_config(id=1) 시드."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, future=True)
    s = Local()
    s.add(
        AppConfig(
            id=1,
            enabled=True,
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
    )
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _bn(no, nm, ntce_dt, openg_dt):
    """테스트용 BidNotice(필수 NOT NULL 컬럼 채움)."""
    base = datetime(2026, 6, 1, 12, 0, 0)
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=nm,
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=ntce_dt,
        openg_dt=openg_dt,
        collected_at=base,
        updated_at=base,
    )


def _seed_notices(session):
    session.add_all(
        [
            _bn("A-1", "소프트웨어 유지보수 용역", datetime(2026, 5, 1, 9, 0), datetime(2026, 7, 1, 10, 0)),
            _bn("A-2", "데이터베이스 구축 용역", datetime(2026, 5, 10, 9, 0), datetime(2026, 6, 20, 10, 0)),
            _bn("A-3", "소프트웨어 개발", datetime(2026, 5, 20, 9, 0), datetime(2026, 5, 25, 10, 0)),
            _bn("A-4", "청소 용역", datetime(2026, 4, 1, 9, 0), None),
        ]
    )
    session.commit()


# --- search_bid_notices -----------------------------------------------
def test_search_partial_name(session):
    _seed_notices(session)
    rows, total = repository.search_bid_notices(session, q="소프트웨어")
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"A-1", "A-3"}


def test_search_date_range(session):
    _seed_notices(session)
    # 2026-05-05 ~ 2026-05-31 → A-2, A-3
    rows, total = repository.search_bid_notices(
        session,
        dt_from=datetime(2026, 5, 5, 0, 0, 0),
        dt_to=datetime(2026, 5, 31, 23, 59, 59),
    )
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"A-2", "A-3"}


def test_search_date_from_only(session):
    _seed_notices(session)
    rows, total = repository.search_bid_notices(session, dt_from=datetime(2026, 5, 15, 0, 0, 0))
    # 5/20 만 해당
    assert total == 1
    assert rows[0].bid_ntce_no == "A-3"


def test_search_openg_only_future(session):
    _seed_notices(session)
    now = datetime(2026, 6, 1, 12, 0, 0)
    rows, total = repository.search_bid_notices(session, openg_only_future=True, now=now)
    # openg_dt >= now → A-1(7/1), A-2(6/20). A-3(5/25)=과거, A-4=NULL 제외
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"A-1", "A-2"}


def test_search_sort_default_desc(session):
    _seed_notices(session)
    rows, _ = repository.search_bid_notices(session, sort="bid_ntce_dt_desc")
    # 최신 공고일순: 5/20(A-3) > 5/10(A-2) > 5/1(A-1) > 4/1(A-4)
    assert [r.bid_ntce_no for r in rows] == ["A-3", "A-2", "A-1", "A-4"]


def test_search_sort_openg_asc_nulls_last(session):
    _seed_notices(session)
    rows, _ = repository.search_bid_notices(session, sort="openg_dt_asc")
    # 개찰 임박순: 5/25(A-3) < 6/20(A-2) < 7/1(A-1) < NULL(A-4) 뒤로
    assert [r.bid_ntce_no for r in rows] == ["A-3", "A-2", "A-1", "A-4"]


def test_search_pagination(session):
    _seed_notices(session)
    rows1, total1 = repository.search_bid_notices(
        session, sort="bid_ntce_dt_desc", page=1, page_size=2
    )
    rows2, total2 = repository.search_bid_notices(
        session, sort="bid_ntce_dt_desc", page=2, page_size=2
    )
    assert total1 == total2 == 4  # 전체건수는 페이지와 무관
    assert [r.bid_ntce_no for r in rows1] == ["A-3", "A-2"]
    assert [r.bid_ntce_no for r in rows2] == ["A-1", "A-4"]


def test_search_empty(session):
    _seed_notices(session)
    rows, total = repository.search_bid_notices(session, q="존재하지않는공고명")
    assert total == 0
    assert rows == []


# --- update_config -----------------------------------------------------
def test_update_config_whitelist_and_updated_at(session):
    before = repository.get_config(session).updated_at
    cfg = repository.update_config(
        session,
        interval_minutes=15,
        indstryty_cds="1426",
        enabled=False,
        # 비허용 키 — 무시되어야 한다.
        auto_halted=True,
        halt_code="06",
        bid_ntce_no="HACK",
    )
    assert cfg.interval_minutes == 15
    assert cfg.indstryty_cds == "1426"
    assert cfg.enabled is False
    # 비허용 키는 반영되지 않음
    assert cfg.auto_halted is False
    assert cfg.halt_code is None
    # updated_at 갱신
    assert cfg.updated_at != before
    assert cfg.updated_at > before


def test_update_config_persists(session):
    repository.update_config(session, num_of_rows=99)
    # 같은 세션에서 다시 읽어도 반영
    assert repository.get_config(session).num_of_rows == 99


# --- list_recent_runs --------------------------------------------------
def test_list_recent_runs_desc_and_limit(session):
    for i in range(3):
        session.add(
            CollectionRun(
                trigger="scheduled",
                status="success",
                window_bgn_dt=datetime(2026, 6, 1, 0, 0),
                window_end_dt=datetime(2026, 6, 1, 1, 0),
                total_fetched=i,
                total_new=i,
                total_updated=0,
                retry_count=0,
            )
        )
    session.commit()

    runs = repository.list_recent_runs(session, limit=2)
    assert len(runs) == 2
    # id DESC
    assert runs[0].id > runs[1].id


def test_list_recent_runs_empty(session):
    assert repository.list_recent_runs(session) == []


# --- 라우트 스모크 (FastAPI TestClient + 임시 DB) ----------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 파일 SQLite 로 main.SessionLocal 을 교체한 TestClient.

    실 API·실 procurement.db 에 의존하지 않는다. app_config 시드 + 공고 2건.
    """
    from fastapi.testclient import TestClient

    from app import main

    db_path = tmp_path / "screens_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(
            AppConfig(
                id=1,
                enabled=True,
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
        )
        s.add(_bn("S-1", "스모크 테스트 공고", datetime(2026, 5, 1, 9, 0), datetime(2026, 7, 1, 10, 0)))
        s.commit()

    # 라우트가 참조하는 main.SessionLocal 을 임시 DB 로 교체.
    monkeypatch.setattr(main, "SessionLocal", Local)
    return TestClient(main.app)


def test_root_redirects_to_list(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"] == "/list"


def test_list_page_ok(client):
    resp = client.get("/list")
    assert resp.status_code == 200
    assert "스모크 테스트 공고" in resp.text
    assert "공고 검색" in resp.text


def test_list_page_filter_querystring(client):
    resp = client.get("/list", params={"q": "없는공고", "sort": "openg_dt_asc", "page": "1"})
    assert resp.status_code == 200
    assert "조건에 맞는 공고가 없습니다" in resp.text


def test_config_page_ok(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "수집 설정" in resp.text
    assert "스케줄러 제어" in resp.text


def test_api_test_page_ok(client):
    resp = client.get("/api-test")
    assert resp.status_code == 200
    assert "엔드포인트" in resp.text


def test_config_save_valid(client):
    resp = client.post(
        "/config",
        data={
            "interval_minutes": "30",
            "window_overlap_minutes": "90",
            "backfill_days": "30",
            "num_of_rows": "20",
            "max_retries": "2",
            "inqry_div": "1",
            "intrntnl_div_cd": "1",
            "indstryty_cds": "1426,1468",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/config" in resp.headers["location"]


def test_config_save_invalid_returns_400(client):
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
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "범위" in resp.text
