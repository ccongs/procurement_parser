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
        # 개찰일 NULL → 기본(지난 개찰 숨김)에서도 항상 표시(결정적).
        s.add(_bn("S-1", "스모크 테스트 공고", datetime(2026, 5, 1, 9, 0), None))
        # 개찰일이 확실히 과거(2000년) → 기본 숨김, "지난 개찰 포함" 시 노출(결정적).
        s.add(_bn("S-PAST", "지난 개찰 공고", datetime(2026, 5, 2, 9, 0), datetime(2000, 1, 1, 10, 0)))
        # 첨부 보유 공고(파일 컬럼·drawer·zip 테스트용). 1·3번 URL 보유, 2번 비어 있음.
        s.add(
            BidNotice(
                bid_ntce_no="S-FILE",
                bid_ntce_nm="첨부 있는 공고",
                ntce_instt_nm="테스트기관",
                bid_ntce_dt=datetime(2026, 5, 3, 9, 0),
                openg_dt=None,
                ntce_spec_doc_url1="https://example.test/a.pdf",
                ntce_spec_file_nm1="규격서.pdf",
                ntce_spec_doc_url3="https://example.test/c.hwp",
                # file_nm3 은 비워 폴백(첨부3) 확인.
                collected_at=datetime(2026, 6, 1, 12, 0),
                updated_at=datetime(2026, 6, 1, 12, 0),
            )
        )
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


# =====================================================================
#  Phase 4.1 — 개찰 지난 공고 숨김 · 첨부 파일
# =====================================================================

# --- search_bid_notices(include_past_openg) ----------------------------
def test_search_include_past_false_hides_past_keeps_null(session):
    _seed_notices(session)
    now = datetime(2026, 6, 1, 12, 0, 0)
    rows, total = repository.search_bid_notices(
        session, include_past_openg=False, now=now
    )
    # openg>=now(A-1 7/1, A-2 6/20) + NULL(A-4) 유지, 과거(A-3 5/25) 제외.
    assert total == 3
    assert {r.bid_ntce_no for r in rows} == {"A-1", "A-2", "A-4"}


def test_search_include_past_true_shows_all(session):
    _seed_notices(session)
    now = datetime(2026, 6, 1, 12, 0, 0)
    rows, total = repository.search_bid_notices(
        session, include_past_openg=True, now=now
    )
    assert total == 4
    assert {r.bid_ntce_no for r in rows} == {"A-1", "A-2", "A-3", "A-4"}


# --- get_notice_files --------------------------------------------------
def test_get_notice_files_url_present_order_and_fallback(session):
    session.add(
        BidNotice(
            bid_ntce_no="F-1",
            bid_ntce_nm="첨부 테스트",
            ntce_spec_doc_url1="https://example.test/1.pdf",
            ntce_spec_file_nm1="규격서.pdf",
            # 2번은 URL 없음 → 제외
            ntce_spec_doc_url3="https://example.test/3.hwp",
            # 3번 파일명 없음 → '첨부3' 폴백
            collected_at=datetime(2026, 6, 1, 12, 0),
            updated_at=datetime(2026, 6, 1, 12, 0),
        )
    )
    session.commit()

    files = repository.get_notice_files(session, "F-1")
    # URL 있는 것만(2건), idx 오름차순
    assert [f["idx"] for f in files] == [1, 3]
    assert files[0]["name"] == "규격서.pdf"
    assert files[1]["name"] == "첨부3"  # 파일명 폴백
    assert files[1]["url"] == "https://example.test/3.hwp"


def test_get_notice_files_missing_notice(session):
    assert repository.get_notice_files(session, "NOPE") == []


# --- 라우트: 기본 숨김 / 토글 -----------------------------------------
def test_list_default_hides_past_openg(client):
    resp = client.get("/list")
    assert resp.status_code == 200
    # 개찰일 NULL(S-1)·미래 없음 → 표시, 과거(S-PAST)는 기본 숨김.
    assert "스모크 테스트 공고" in resp.text
    assert "지난 개찰 공고" not in resp.text


def test_list_include_past_shows_past(client):
    resp = client.get("/list", params={"include_past": "1"})
    assert resp.status_code == 200
    assert "지난 개찰 공고" in resp.text
    # 페이지네이션·쿼리스트링에 include_past 보존
    assert "include_past=1" in resp.text


def test_list_korean_only_headers(client):
    resp = client.get("/list")
    # 한글 헤더 노출, 영문 컬럼명/병기 code 미노출.
    assert "<th>공고번호</th>" in resp.text
    assert "<th>공고명</th>" in resp.text
    assert "<th>파일</th>" in resp.text
    assert "bid_ntce_no</code>" not in resp.text  # /list 표 헤더에 영문 병기 없음


# --- 라우트: 파일 목록 JSON -------------------------------------------
def test_list_files_json(client):
    resp = client.get("/list/S-FILE/files")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bid_ntce_no"] == "S-FILE"
    assert data["bid_ntce_nm"] == "첨부 있는 공고"
    names = [f["name"] for f in data["files"]]
    assert names == ["규격서.pdf", "첨부3"]  # 1·3번, 3번은 폴백


def test_list_files_json_no_attachments(client):
    resp = client.get("/list/S-1/files")
    assert resp.status_code == 200
    assert resp.json()["files"] == []


def test_list_files_json_missing(client):
    resp = client.get("/list/NOPE/files")
    assert resp.status_code == 404


# --- 라우트: zip (외부 httpx monkeypatch) -----------------------------
class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    """app.main 의 httpx.AsyncClient 대체 — 외부 호출 없이 가짜 바이트 반환."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        return _FakeResp(b"FAKEBYTES:" + url.encode("utf-8"))


def test_list_files_zip(client, monkeypatch):
    import zipfile as _zip
    from io import BytesIO

    from app import main

    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.get("/list/S-FILE/files.zip")
    assert resp.status_code == 200
    assert resp.content[:4] == b"PK\x03\x04"  # zip 매직넘버
    assert "attachment" in resp.headers["content-disposition"]
    assert "UTF-8''" in resp.headers["content-disposition"]

    # zip 내용: 첨부 2건(인덱스 접두)
    zf = _zip.ZipFile(BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) == 2
    assert any(n.startswith("1_") for n in names)
    assert any(n.startswith("3_") for n in names)


def test_list_files_zip_no_attachments(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    resp = client.get("/list/S-1/files.zip")
    assert resp.status_code == 404


def test_list_files_zip_all_fail_returns_502(client, monkeypatch):
    """외부 다운로드가 전부 실패하면 502(부분 성공 0건)."""
    from app import main

    class _FailingClient(_FakeAsyncClient):
        async def get(self, url):
            raise RuntimeError("network down")

    monkeypatch.setattr(main.httpx, "AsyncClient", _FailingClient)
    resp = client.get("/list/S-FILE/files.zip")
    assert resp.status_code == 502
