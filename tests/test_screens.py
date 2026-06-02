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


# 공고일 기본 기간(오늘-1개월~오늘)에 의존하지 않도록, 시드(2026-05-xx)를 덮는 넓은 범위를 명시.
# (서버는 쿼리에 날짜가 없을 때만 기본 기간을 적용하므로, 명시하면 실행 시점과 무관하게 결정적.)
_WIDE = {"dt_from": "2000-01-01", "dt_to": "2099-12-31"}


def test_list_page_ok(client):
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "스모크 테스트 공고" in resp.text
    assert "공고 검색" in resp.text


def test_list_page_filter_querystring(client):
    resp = client.get(
        "/list", params={**_WIDE, "q": "없는공고", "sort": "openg_dt_asc", "page": "1"}
    )
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
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 개찰일 NULL(S-1)·미래 없음 → 표시, 과거(S-PAST)는 기본 숨김.
    assert "스모크 테스트 공고" in resp.text
    assert "지난 개찰 공고" not in resp.text


def test_list_include_past_shows_past(client):
    resp = client.get("/list", params={**_WIDE, "include_past": "1"})
    assert resp.status_code == 200
    assert "지난 개찰 공고" in resp.text
    # 페이지네이션·쿼리스트링에 include_past 보존
    assert "include_past=1" in resp.text


def test_list_korean_only_headers(client):
    resp = client.get("/list", params=_WIDE)
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


# =====================================================================
#  Phase 4.2 — 매칭업종·정렬·날짜필드·가격필터·설정 기본값
# =====================================================================

from datetime import date as _date  # noqa: E402

from decimal import Decimal  # noqa: E402

from app import industry_codes, main  # noqa: E402


def _bnp(no, nm, ntce_dt, openg_dt, price):
    """가격(presmpt_prce) 포함 BidNotice. price=None 이면 NULL."""
    base = datetime(2026, 6, 1, 12, 0, 0)
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=nm,
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=ntce_dt,
        openg_dt=openg_dt,
        presmpt_prce=(None if price is None else Decimal(price)),
        collected_at=base,
        updated_at=base,
    )


def _seed_priced(session):
    session.add_all(
        [
            _bnp("P-1", "공고1", datetime(2026, 5, 1, 9, 0), datetime(2026, 7, 1, 10, 0), 10_000_000),
            _bnp("P-2", "공고2", datetime(2026, 5, 10, 9, 0), datetime(2026, 6, 20, 10, 0), 50_000_000),
            _bnp("P-3", "공고3", datetime(2026, 5, 20, 9, 0), datetime(2026, 5, 25, 10, 0), 100_000_000),
            _bnp("P-4", "공고4(가격없음)", datetime(2026, 4, 1, 9, 0), None, None),
        ]
    )
    session.commit()


# --- date_field 범위 필터 ---------------------------------------------
def test_search_date_field_openg(session):
    _seed_priced(session)
    # 개찰일 2026-06-01 ~ 2026-07-31 → P-1(7/1), P-2(6/20). P-3(5/25)·P-4(NULL) 제외.
    rows, total = repository.search_bid_notices(
        session,
        date_field="openg_dt",
        dt_from=datetime(2026, 6, 1, 0, 0, 0),
        dt_to=datetime(2026, 7, 31, 23, 59, 59),
    )
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-2"}


def test_search_date_field_default_is_bid_ntce_dt(session):
    _seed_priced(session)
    # 기본(date_field 미지정)은 공고일 기준 — 5/5~5/31 → P-2(5/10), P-3(5/20).
    rows, total = repository.search_bid_notices(
        session,
        dt_from=datetime(2026, 5, 5, 0, 0, 0),
        dt_to=datetime(2026, 5, 31, 23, 59, 59),
    )
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-2", "P-3"}


def test_search_date_field_invalid_falls_back(session):
    _seed_priced(session)
    # 허용 외 값 → 공고일(bid_ntce_dt)로 폴백.
    rows, _ = repository.search_bid_notices(
        session,
        date_field="HACK",
        dt_from=datetime(2026, 5, 5, 0, 0, 0),
        dt_to=datetime(2026, 5, 31, 23, 59, 59),
    )
    assert {r.bid_ntce_no for r in rows} == {"P-2", "P-3"}


# --- price_min / price_max 경계 ---------------------------------------
def test_search_price_min(session):
    _seed_priced(session)
    # >= 50,000,000 → P-2, P-3 (P-1 1천만 제외, P-4 NULL 제외).
    rows, total = repository.search_bid_notices(session, price_min=50_000_000)
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-2", "P-3"}


def test_search_price_max(session):
    _seed_priced(session)
    # <= 50,000,000 → P-1, P-2 (경계 포함, P-3 1억 제외, P-4 NULL 제외).
    rows, total = repository.search_bid_notices(session, price_max=50_000_000)
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-2"}


def test_search_price_min_and_max(session):
    _seed_priced(session)
    # 10,000,000 ~ 50,000,000 (경계 포함) → P-1, P-2.
    rows, total = repository.search_bid_notices(
        session, price_min=10_000_000, price_max=50_000_000
    )
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-2"}


def test_search_price_none_keeps_null_rows(session):
    _seed_priced(session)
    # 가격 필터 없으면 NULL 가격(P-4)도 포함.
    rows, total = repository.search_bid_notices(session)
    assert total == 4
    assert "P-4" in {r.bid_ntce_no for r in rows}


# --- 신규 sort 6종 (NULL 뒤로) ----------------------------------------
def test_sort_bid_ntce_dt_asc(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="bid_ntce_dt_asc")
    # 오름차순: 4/1(P-4) < 5/1(P-1) < 5/10(P-2) < 5/20(P-3)
    assert [r.bid_ntce_no for r in rows] == ["P-4", "P-1", "P-2", "P-3"]


def test_sort_openg_dt_desc_nulls_last(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="openg_dt_desc")
    # 내림차순: 7/1(P-1) > 6/20(P-2) > 5/25(P-3) > NULL(P-4) 뒤로
    assert [r.bid_ntce_no for r in rows] == ["P-1", "P-2", "P-3", "P-4"]


def test_sort_presmpt_prce_desc_nulls_last(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="presmpt_prce_desc")
    # 1억(P-3) > 5천만(P-2) > 1천만(P-1) > NULL(P-4) 뒤로
    assert [r.bid_ntce_no for r in rows] == ["P-3", "P-2", "P-1", "P-4"]


def test_sort_presmpt_prce_asc_nulls_last(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="presmpt_prce_asc")
    # 1천만(P-1) < 5천만(P-2) < 1억(P-3) < NULL(P-4) 뒤로
    assert [r.bid_ntce_no for r in rows] == ["P-1", "P-2", "P-3", "P-4"]


def test_sort_unknown_falls_back_to_default(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="bogus")
    # 기본 = 최신 공고일순(desc): 5/20 > 5/10 > 5/1 > 4/1
    assert [r.bid_ntce_no for r in rows] == ["P-3", "P-2", "P-1", "P-4"]


# --- update_config: 가격 기본값 화이트리스트 --------------------------
def test_update_config_price_defaults_whitelist(session):
    cfg = repository.update_config(
        session, presmpt_prce_bgn="1000", presmpt_prce_end="9000"
    )
    assert cfg.presmpt_prce_bgn == "1000"
    assert cfg.presmpt_prce_end == "9000"
    # 비허용 키는 여전히 무시.
    cfg2 = repository.update_config(session, presmpt_prce_bgn="2000", bid_ntce_no="X")
    assert cfg2.presmpt_prce_bgn == "2000"


def test_update_config_price_defaults_none(session):
    repository.update_config(session, presmpt_prce_bgn="500")
    cfg = repository.update_config(session, presmpt_prce_bgn=None)
    assert cfg.presmpt_prce_bgn is None


# --- main 헬퍼: 날짜 기본 기간(결정적: today 주입) --------------------
def test_months_after_basic():
    assert main._months_after(_date(2026, 6, 1), 1) == _date(2026, 7, 1)
    # 말일 보정: 1/31 + 1개월 → 2/28(2026 평년)
    assert main._months_after(_date(2026, 1, 31), 1) == _date(2026, 2, 28)
    # 연도 넘김
    assert main._months_after(_date(2026, 12, 15), 1) == _date(2027, 1, 15)


def test_list_default_date_range_bid_ntce_dt():
    today = _date(2026, 6, 1)
    f, t = main._list_default_date_range("bid_ntce_dt", today=today)
    assert f == "2026-05-01"
    assert t == "2026-06-01"


def test_list_default_date_range_openg_dt():
    today = _date(2026, 6, 1)
    f, t = main._list_default_date_range("openg_dt", today=today)
    assert f == "2026-06-01"
    assert t == "2026-07-01"


# --- industry_codes 매핑 ----------------------------------------------
def test_industry_matched_labels():
    """라운드2b: 단축 라벨 `업무명 [코드]`(바깥 소프트웨어사업자 래퍼 제거)."""
    labels = industry_codes.matched_labels("1426,1468")
    assert labels == [
        "패키지소프트웨어개발·공급사업 [1426]",
        "컴퓨터관련서비스사업 [1468]",
    ]
    # 4종 단축명 확인.
    assert industry_codes.matched_labels("1469,1470") == [
        "디지털콘텐츠개발서비스사업 [1469]",
        "데이터베이스제작및검색서비스사업 [1470]",
    ]


def test_industry_unknown_code_passthrough():
    assert industry_codes.matched_labels("9999") == ["9999"]
    assert industry_codes.matched_labels("") == []
    assert industry_codes.matched_labels(None) == []


def test_industry_matched_label_pairs_tooltip():
    """라운드2b: (단축 표시, tooltip 전체명) 쌍. 전체명은 래퍼 포함 + [코드]."""
    pairs = industry_codes.matched_label_pairs("1468")
    assert pairs == [
        ("컴퓨터관련서비스사업 [1468]", "소프트웨어사업자(컴퓨터관련서비스사업) [1468]"),
    ]
    # 모르는 코드는 단축·전체 모두 코드 그대로.
    assert industry_codes.matched_label_pairs("9999") == [("9999", "9999")]


# --- main 헬퍼: 가격 파싱 ---------------------------------------------
def test_parse_price():
    assert main._parse_price("1,000,000") == 1_000_000
    assert main._parse_price("500") == 500
    assert main._parse_price("") is None
    assert main._parse_price(None) is None
    assert main._parse_price("abc") is None
    assert main._parse_price("1234.0") == 1234


# --- 라우트 스모크: Phase 4.2 -----------------------------------------
def test_list_header_sort_links_present(client):
    """컬럼 헤더 정렬 링크가 존재(공고일/개찰일/추정가격)."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'class="sortcol"' in resp.text
    assert "sort=bid_ntce_dt_asc" in resp.text  # 기본이 desc 이므로 헤더는 다음 클릭=asc
    assert "sort=openg_dt_desc" in resp.text
    assert "sort=presmpt_prce_desc" in resp.text
    # 정렬 select 는 검색폼에서 제거됨(헤더 클릭으로 대체).
    assert '<select name="sort"' not in resp.text


def test_list_default_sort_is_latest(client):
    """기본 정렬 = 최신 공고일(bid_ntce_dt_desc) — 현재 컬럼 헤더에 ▼ 표시."""
    resp = client.get("/list", params=_WIDE)
    # 현재 정렬이 desc 이므로 공고일 헤더에 ▼ 화살표.
    assert "▼" in resp.text


def test_list_price_filter_query(client):
    """가격 필터 쿼리 — price_max 로 좁히면 결과·입력값 반영."""
    resp = client.get("/list", params={**_WIDE, "price_min": "1", "price_max": "999999999999"})
    assert resp.status_code == 200
    # 입력칸에 값 반영
    assert 'name="price_min"' in resp.text
    assert 'name="price_max"' in resp.text


def test_list_date_field_switch_openg(client):
    """date_field=openg_dt 전환 — select 에 개찰일 선택, 라우트 200."""
    resp = client.get("/list", params={**_WIDE, "date_field": "openg_dt"})
    assert resp.status_code == 200
    assert 'name="date_field"' in resp.text
    assert '<option value="openg_dt" selected>개찰일</option>' in resp.text


def test_list_matched_industry_korean_vertical(client):
    """매칭업종 한글(코드) 세로 표기 — 시드에 매칭코드 부여 후 한글명 노출 확인."""
    from app import main as _main

    # 매칭코드가 있는 공고를 임시로 추가(client 픽스처 DB 에).
    with _main.SessionLocal() as s:
        s.add(
            BidNotice(
                bid_ntce_no="S-IND",
                bid_ntce_nm="매칭업종 표기 공고",
                ntce_instt_nm="테스트기관",
                bid_ntce_dt=datetime(2026, 5, 4, 9, 0),
                openg_dt=None,
                matched_indstryty_cds="1426,1468",
                collected_at=datetime(2026, 6, 1, 12, 0),
                updated_at=datetime(2026, 6, 1, 12, 0),
            )
        )
        s.commit()

    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 단축 라벨 `업무명 [코드]` 세로(span.indrow) 표기.
    assert 'class="indrow"' in resp.text
    assert "컴퓨터관련서비스사업 [1468]" in resp.text
    assert "패키지소프트웨어개발·공급사업 [1426]" in resp.text
    # tooltip(title)에는 전체명 유지.
    assert 'title="소프트웨어사업자(컴퓨터관련서비스사업) [1468]"' in resp.text


def test_config_page_has_price_defaults(client):
    """/config 에 추정가격 기본 하한/상한 입력 노출."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'name="presmpt_prce_bgn"' in resp.text
    assert 'name="presmpt_prce_end"' in resp.text


def test_config_save_price_defaults(client):
    """/config 저장 시 가격 기본값(숫자) 반영, 빈값은 None."""
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
            "indstryty_cds": "1426",
            "presmpt_prce_bgn": "1000000",
            "presmpt_prce_end": "",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from app import main as _main

    with _main.SessionLocal() as s:
        cfg = repository.get_config(s)
        assert cfg.presmpt_prce_bgn == "1000000"
        assert cfg.presmpt_prce_end is None


def test_config_save_price_invalid_returns_400(client):
    """가격 기본값이 비숫자면 400."""
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
            "indstryty_cds": "1426",
            "presmpt_prce_bgn": "abc",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "추정가격" in resp.text


def test_list_price_default_from_config(client):
    """설정의 가격 기본값이 /list 입력칸 기본값으로 노출(쿼리 미지정 시)."""
    from app import main as _main

    with _main.SessionLocal() as s:
        repository.update_config(s, presmpt_prce_bgn="7000000", presmpt_prce_end="8000000")

    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'value="7000000"' in resp.text
    assert 'value="8000000"' in resp.text


# --- 라운드 2b: 배정예산·원 단위·기관 통합 셀 -------------------------
def test_fmt_amt_won_unit():
    """라운드2b: 금액은 천단위 콤마 + ` 원`, 빈값/None 은 빈칸."""
    assert main._fmt_amt(1_000_000) == "1,000,000 원"
    assert main._fmt_amt(Decimal("50000000")) == "50,000,000 원"
    assert main._fmt_amt(0) == "0 원"
    assert main._fmt_amt(None) == ""
    assert main._fmt_amt("") == ""


def test_render_instt_two_lines():
    """라운드2b: 공고기관/수요기관 2줄. 위=공고기관(.ntcorg), 아래=수요기관(.dmnorg)."""
    cell = main._render_instt("발주기관A", "수요기관B")
    assert 'class="ntcorg"' in cell
    assert ">발주기관A<" in cell
    assert 'class="dmnorg"' in cell
    assert ">수요기관B<" in cell
    # 한쪽 없으면 그 줄 생략.
    only_ntce = main._render_instt("발주기관A", None)
    assert 'class="ntcorg"' in only_ntce
    assert "dmnorg" not in only_ntce
    only_dmin = main._render_instt(None, "수요기관B")
    assert "ntcorg" not in only_dmin
    assert 'class="dmnorg"' in only_dmin
    # 둘 다 없으면 빈 셀.
    assert main._render_instt(None, "") == '<td class="insttcell"></td>'


def test_list_budget_column_and_won(client):
    """라운드2b 라우트 스모크: 배정예산 헤더(추정가격 왼쪽) + 금액 `원` 단위."""
    from app import main as _main

    with _main.SessionLocal() as s:
        s.add(
            BidNotice(
                bid_ntce_no="B-BUD",
                bid_ntce_nm="배정예산 표기 공고",
                ntce_instt_nm="발주기관A",
                dminstt_nm="수요기관B",
                bid_ntce_dt=datetime(2026, 5, 6, 9, 0),
                openg_dt=None,
                asign_bdgt_amt=Decimal("12345678"),
                presmpt_prce=Decimal("9000000"),
                collected_at=datetime(2026, 6, 1, 12, 0),
                updated_at=datetime(2026, 6, 1, 12, 0),
            )
        )
        s.commit()

    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    text = resp.text
    # 배정예산 헤더가 추정가격 헤더 왼쪽에 위치.
    # ("추정가격"은 검색폼 라벨에도 등장하므로 테이블 thead 구간으로 한정해 비교.)
    thead = text[text.index("<thead>"):text.index("</thead>")]
    assert "배정예산" in thead
    assert "공고기관/수요기관" in thead
    assert thead.index("배정예산") < thead.index("추정가격")
    # 금액 `원` 단위(배정예산·추정가격 모두).
    assert "12,345,678 원" in text
    assert "9,000,000 원" in text
    # 기관 통합 셀 2줄.
    assert 'class="insttcell"' in text
    assert ">발주기관A<" in text
    assert ">수요기관B<" in text


# =====================================================================
#  Phase 4.3 — 참가제한지역(prtcptLmtRgnCd) 필터 + /config select
# =====================================================================

from app import region_codes  # noqa: E402


# --- region_codes 모듈 -------------------------------------------------
def test_region_options_order_and_head():
    """REGION_OPTIONS: 맨 앞 전체("") → 전국("00") → 코드 오름차순."""
    opts = region_codes.REGION_OPTIONS
    assert opts[0] == ("", "전체 (지역제한 무관)")
    assert opts[1] == ("00", "전국 (지역제한 없는 공고만)")
    # 이후는 "00" 제외 코드 오름차순.
    rest_codes = [c for c, _ in opts[2:]]
    assert rest_codes == sorted(rest_codes)
    assert "00" not in rest_codes
    # 알려진 코드가 옵션에 존재.
    flat = dict(opts)
    assert flat["11"] == "서울특별시"
    assert flat["41"] == "경기도"


def test_region_is_valid_region():
    assert region_codes.is_valid_region("") is True   # 전체
    assert region_codes.is_valid_region(None) is True  # None → 빈값=전체
    assert region_codes.is_valid_region("00") is True
    assert region_codes.is_valid_region("28") is True
    assert region_codes.is_valid_region("99") is True
    assert region_codes.is_valid_region("ZZ") is False
    assert region_codes.is_valid_region("9999") is False


def test_region_name():
    assert region_codes.region_name("00") == "전국"
    assert region_codes.region_name("28") == "인천광역시"
    assert region_codes.region_name("") == "전체"
    assert region_codes.region_name(None) == "전체"
    assert region_codes.region_name("ZZ") == "ZZ"  # 미정의 코드는 코드 그대로


# --- update_config 화이트리스트(prtcpt_lmt_rgn_cd) ---------------------
def test_update_config_region_whitelist(session):
    cfg = repository.update_config(session, prtcpt_lmt_rgn_cd="28")
    assert cfg.prtcpt_lmt_rgn_cd == "28"
    # 빈값(전체) → None 로 갱신 가능.
    cfg2 = repository.update_config(session, prtcpt_lmt_rgn_cd=None)
    assert cfg2.prtcpt_lmt_rgn_cd is None
    # 비허용 키는 여전히 무시(prtcpt_lmt_rgn_cd 만 통과).
    cfg3 = repository.update_config(session, prtcpt_lmt_rgn_cd="41", bid_ntce_no="X")
    assert cfg3.prtcpt_lmt_rgn_cd == "41"


# --- /config: 참가제한지역 select 렌더 ---------------------------------
def test_config_page_has_region_select(client):
    """/config 에 참가제한지역 select·옵션(전국/서울/경기 등) 노출."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'name="prtcpt_lmt_rgn_cd"' in resp.text
    # 옵션 라벨 노출.
    assert "전체 (지역제한 무관)" in resp.text
    assert "전국 (지역제한 없는 공고만)" in resp.text
    assert "서울특별시" in resp.text
    assert "경기도" in resp.text


def test_config_region_default_selected(client):
    """시드 기본값('00')이 selected 로 렌더된다."""
    from app import main as _main

    with _main.SessionLocal() as s:
        repository.update_config(s, prtcpt_lmt_rgn_cd="00")
    resp = client.get("/config")
    assert resp.status_code == 200
    assert '<option value="00" selected>전국 (지역제한 없는 공고만)</option>' in resp.text


# --- /config 저장: 참가제한지역 화이트리스트/거부 ---------------------
def _cfg_form(**overrides):
    """유효한 /config 저장 폼 기본값 + overrides."""
    base = {
        "interval_minutes": "30",
        "window_overlap_minutes": "90",
        "backfill_days": "30",
        "num_of_rows": "20",
        "max_retries": "2",
        "inqry_div": "1",
        "intrntnl_div_cd": "1",
        "indstryty_cds": "1426",
        "prtcpt_lmt_rgn_cd": "00",
        "enabled": "1",
    }
    base.update(overrides)
    return base


def test_config_save_region_valid(client):
    """유효 지역코드 저장 → 303 + DB 반영."""
    resp = client.post(
        "/config", data=_cfg_form(prtcpt_lmt_rgn_cd="28"), follow_redirects=False
    )
    assert resp.status_code == 303
    from app import main as _main

    with _main.SessionLocal() as s:
        assert repository.get_config(s).prtcpt_lmt_rgn_cd == "28"


def test_config_save_region_blank_is_none(client):
    """빈값(전체) 저장 → None(필터 안 함)으로 저장."""
    resp = client.post(
        "/config", data=_cfg_form(prtcpt_lmt_rgn_cd=""), follow_redirects=False
    )
    assert resp.status_code == 303
    from app import main as _main

    with _main.SessionLocal() as s:
        assert repository.get_config(s).prtcpt_lmt_rgn_cd is None


def test_config_save_region_invalid_returns_400(client):
    """허용 외 지역코드는 400(저장 거부)."""
    resp = client.post(
        "/config", data=_cfg_form(prtcpt_lmt_rgn_cd="ZZ"), follow_redirects=False
    )
    assert resp.status_code == 400
    assert "참가제한지역" in resp.text


# =====================================================================
#  Phase 4.9-A — Wave A: 헤더·탭·CSS·페이저·정렬·설정·필터헬퍼
# =====================================================================


def test_tab_bar_exists_on_list(client):
    """/list 에 탭바(입찰공고목록·사전규격목록) 링크가 존재하며, 입찰공고목록이 active."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/list"' in resp.text
    assert 'href="/pre-spec"' in resp.text
    # /list 활성 탭
    assert 'href="/list" class="active"' in resp.text


def test_tab_bar_pre_spec_active_on_pre_spec(client):
    """/pre-spec 에서 사전규격목록 탭이 active."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert 'href="/pre-spec" class="active"' in resp.text
    # 입찰공고목록 탭은 비활성
    assert 'href="/list" class="active"' not in resp.text


def test_tab_bar_no_active_on_config(client):
    """/config 에서 탭은 비활성(설정 버튼이 active)."""
    resp = client.get("/config")
    assert resp.status_code == 200
    # 탭 링크는 존재하지만 active 아님
    assert 'href="/list"' in resp.text
    assert 'href="/pre-spec"' in resp.text
    assert 'href="/list" class="active"' not in resp.text
    assert 'href="/pre-spec" class="active"' not in resp.text
    # 설정 버튼이 active
    assert 'hdr-btn active' in resp.text


def test_config_has_api_test_link(client):
    """/config 에 'API테스트 열기 ↗' 링크(새 탭)가 존재한다."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'href="/api-test"' in resp.text
    assert 'target="_blank"' in resp.text
    assert "API테스트 열기" in resp.text


def test_pager_has_page_numbers(client):
    """페이저에 번호 링크가 존재하고 전체건수 텍스트도 포함된다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 전체건수 텍스트 보존(테스트 의존)
    assert "전체" in resp.text
    assert "페이지" in resp.text
    # 페이저 링크(이전/다음)
    assert "← 이전" in resp.text
    assert "다음 →" in resp.text


def test_sort_header_neutral_arrow_on_unsorted_column(client):
    """미정렬 컬럼에 ↕ 중립 화살표가, 현재 정렬 컬럼에 ▼/▲ 방향 화살표가 표시된다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 기본 정렬(bid_ntce_dt_desc) → 공고일 컬럼 ▼, 다른 컬럼(미정렬) ↕
    assert "▼" in resp.text
    assert "↕" in resp.text  # 미정렬 중립 화살표


def test_filter_card_helper_basic():
    """_filter_card 시그니처 및 기본 동작 확인."""
    from app.main import _filter_card

    html = _filter_card(
        action="/list",
        summary_html='<input name="q" value="">',
        detail_html='<input name="dt_from">',
    )
    # filter-card + filter-collapsed(기본 접힘) 클래스
    assert 'filter-card' in html
    assert 'filter-collapsed' in html
    # form action
    assert 'action="/list"' in html
    assert 'method="get"' in html
    # summary·detail 내용
    assert 'name="q"' in html
    assert 'name="dt_from"' in html
    # 토글 버튼
    assert 'filter-toggle' in html


def test_filter_card_helper_custom_id_and_title():
    """card_id·title 커스텀 인자가 반영된다."""
    from app.main import _filter_card

    html = _filter_card(
        action="/pre-spec",
        summary_html="",
        detail_html="",
        title="사전규격 검색",
        card_id="myCard",
    )
    assert 'id="myCard"' in html
    assert 'aria-label="사전규격 검색"' in html
    assert 'action="/pre-spec"' in html


def test_header_no_subtitle_rendered(client):
    """헤더에 subtitle <p> 태그가 렌더되지 않는다(파라미터는 유지, 화면엔 미노출)."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 기존 subtitle 텍스트("수집·저장된 공고를 조회합니다.")가 헤더에 없음.
    assert "수집·저장된 공고를 조회합니다." not in resp.text


# =====================================================================
#  Phase 4.9-B1 — Wave B-1: list 페이지 개선
# =====================================================================


def test_list_filter_card_used(client):
    """/list 에 _filter_card 가 적용됐는지 확인(filter-card 클래스 + filter-collapsed 기본 접힘)."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'filter-card' in resp.text
    assert 'filter-collapsed' in resp.text
    # 폼 action=/list
    assert 'action="/list"' in resp.text


def test_list_no_code_labels(client):
    """/list 필터 라벨에 <code>…</code> 영문 병기가 없다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 라벨에 code 태그를 통한 영문 필드명 병기가 없어야 함.
    assert "q</code>" not in resp.text
    assert "date_field</code>" not in resp.text
    assert "dt_from</code>" not in resp.text
    assert "dt_to</code>" not in resp.text
    assert "price_min</code>" not in resp.text
    assert "price_max</code>" not in resp.text


def test_list_checkbox_no_parentheses(client):
    """/list 체크박스 라벨에 괄호 설명이 없다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 라벨 단순화: 괄호 설명 없이 "지난 개찰 포함" 만.
    assert "지난 개찰 포함" in resp.text
    # 기존 괄호 설명 텍스트 미존재 확인.
    assert "기본은 개찰 지난 공고 숨김" not in resp.text


def test_list_today_button_exists(client):
    """/list 날짜 quick 버튼에 '1일' 버튼이 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "setToday()" in resp.text
    assert "1일" in resp.text


def test_list_page_size_default_50(client):
    """기본 page_size=50 — 표기개수 select 에 50건이 selected."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'name="page_size"' in resp.text
    assert '<option value="50" selected>50건</option>' in resp.text


def test_list_page_size_10(client):
    """page_size=10 파라미터가 동작한다 — select 에 10건이 selected, qs 에 보존."""
    resp = client.get("/list", params={**_WIDE, "page_size": "10"})
    assert resp.status_code == 200
    assert '<option value="10" selected>10건</option>' in resp.text
    # qs 에 page_size 보존(페이지 이동 링크에서 유지).
    assert "page_size=10" in resp.text


def test_list_page_size_invalid_falls_back_to_50(client):
    """허용 외 page_size(999) → 50으로 폴백."""
    resp = client.get("/list", params={**_WIDE, "page_size": "999"})
    assert resp.status_code == 200
    assert '<option value="50" selected>50건</option>' in resp.text


def test_list_sort_links_use_list_path(client):
    """정렬 링크·페이저가 /list 경로를 사용한다(B-1 영역 확인)."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    # 정렬 링크 경로
    assert 'href="/list?' in resp.text
    # 페이저 경로
    assert "← 이전" in resp.text
    assert "다음 →" in resp.text


# =====================================================================
#  Phase 4.9-R2-S — sticky 스택·버튼 세로정렬 테스트
# =====================================================================


def test_tab_bar_sticky_style_in_css(client):
    """.tab-bar 에 position:sticky / z-index:30 / background 가 CSS 에 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    css_block = resp.text
    # sticky 선언 존재
    assert "position: sticky" in css_block
    # z-index 30 존재(탭바)
    assert "z-index: 30" in css_block
    # .tab-bar 에 background 가 명시됨(비침 방지)
    assert ".tab-bar" in css_block
    # tab-bar 블록에 background 포함 여부 — 마크업에서 .tab-bar 정의 부분 확인
    tab_bar_idx = css_block.find(".tab-bar {")
    assert tab_bar_idx != -1
    tab_bar_block = css_block[tab_bar_idx: tab_bar_idx + 200]
    assert "background" in tab_bar_block


def test_filter_card_uses_tabbar_h_var(client):
    """.filter-card 의 top 값이 var(--tabbar-h, ...) 를 사용한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "var(--tabbar-h" in resp.text


def test_thead_th_top_zero(client):
    """table thead th 의 top 값이 0 이다(table-wrap 스크롤 컨테이너 상단 기준)."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    text = resp.text
    # CSS 에 'table thead th' 블록이 top: 0 으로 선언돼야 한다
    idx = text.find("table thead th {")
    assert idx != -1, "table thead th 규칙이 CSS 에 없음"
    block = text[idx: idx + 120]
    assert "top: 0" in block, f"thead th 에 top:0 이 없음: {block!r}"


def test_table_wrap_overflow_and_maxh(client):
    """.table-wrap 에 overflow:auto 와 max-height:var(--table-maxh) 가 선언돼 있다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    text = resp.text
    idx = text.find(".table-wrap {")
    assert idx != -1, ".table-wrap 규칙이 CSS 에 없음"
    block = text[idx: idx + 120]
    assert "overflow: auto" in block, f".table-wrap overflow:auto 없음: {block!r}"
    assert "var(--table-maxh" in block, f".table-wrap max-height var 없음: {block!r}"


def test_sticky_script_has_table_maxh_calc(client):
    """공유 스크립트에 --table-maxh 계산 코드가 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "--table-maxh" in resp.text


def test_shell_sticky_script_present(client):
    """_shell 이 포함한 공유 스크립트에 ResizeObserver / --stack-h 가 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    assert "--stack-h" in resp.text
    assert "--tabbar-h" in resp.text


def test_shell_sticky_script_on_pre_spec(client):
    """사전규격 페이지에도 공유 스크립트가 존재한다."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    assert "--stack-h" in resp.text


def test_shell_sticky_script_on_config(client):
    """설정 페이지(filterCard 없음)에도 공유 스크립트가 존재하고 null 가드 코드가 있다."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    # filterCard null 가드: getElementById('filterCard') 가 null 일 수 있음 → if(filterCard) 로 방어
    assert "filterCard" in resp.text


def test_filter_summary_flex_end(client):
    """.filter-summary 에 align-items: flex-end 가 CSS 에 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "align-items: flex-end" in resp.text
