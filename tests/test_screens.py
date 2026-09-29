"""화면(/list·/config·/api-test) repository 조회 + 라우트 스모크 테스트 — Phase 3.5.

- repository 조회(search_bid_notices/update_config/list_recent_runs)는 인메모리 SQLite 로 검증.
- 라우트 스모크는 FastAPI TestClient + 임시 파일 SQLite(실 API·실 procurement.db 비의존).
- 시간 의존 로직(openg_only_future)은 now 를 주입해 결정적으로 검증.
실행: `pytest tests/test_screens.py`
"""

from __future__ import annotations

import re
import json
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


def _bn(no, nm, ntce_dt, openg_dt, cntrct=None):
    """테스트용 BidNotice(필수 NOT NULL 컬럼 채움). cntrct=계약체결방법명(기본 NULL)."""
    base = datetime(2026, 6, 1, 12, 0, 0)
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=nm,
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=ntce_dt,
        openg_dt=openg_dt,
        cntrct_cncls_mthd_nm=cntrct,
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


def test_search_exclude_privt_cntrct(session):
    session.add_all(
        [
            _bn("P-1", "일반경쟁 공고", datetime(2026, 5, 1, 9, 0), None, cntrct="일반경쟁"),
            _bn("P-2", "수의계약건 공고", datetime(2026, 5, 2, 9, 0), None, cntrct="수의계약"),
            _bn("P-3", "방법미상 공고", datetime(2026, 5, 3, 9, 0), None),  # cntrct NULL
        ]
    )
    session.commit()
    # 기본(False) = 필터 없음 → 전체
    _, total = repository.search_bid_notices(session)
    assert total == 3
    # True → 수의계약만 제외, 방법 미상(NULL)은 유지
    rows, total = repository.search_bid_notices(session, exclude_privt_cntrct=True)
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-3"}


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
        # 수의계약 공고 → 기본("수의계약제외" 체크) 숨김, excl_privt=0 시 노출(개찰일 NULL=항상 유효).
        s.add(_bn("S-PRIVT", "수의계약건 스모크 공고", datetime(2026, 5, 4, 9, 0), None, cntrct="수의계약"))
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


def _bid_time_header_parts(
    text: str, heading: str = "공고일시"
) -> tuple[str, dict[str, tuple[str, bool]], tuple[str, str, str]]:
    """목록 응답에서 일시 헤더의 옵션과 방향 링크를 추출한다."""
    thead = text[text.index("<thead>"):text.index("</thead>")]
    header_match = re.search(
        rf'<th class="bidtime-head bidtime-col [^"]+">{heading} .*?</th>', thead
    )
    assert header_match is not None
    header = header_match.group(0)
    options = {
        label: (url, bool(selected))
        for url, selected, label in re.findall(
            r'<option value="([^"]+)"( selected)?>([^<]+)</option>', header
        )
    }
    direction_match = re.search(
        r'<a class="sortcol time-dir" href="([^"]+)" title="정렬 방향 전환">'
        r'<span class="([^"]+)">([^<]+)</span></a>',
        header,
    )
    assert direction_match is not None
    return thead, options, direction_match.groups()


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


def test_list_default_excludes_privt_cntrct(client):
    """파라미터 없음 = '수의계약제외' 기본 체크 → 수의계약 공고 숨김."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "수의계약건 스모크 공고" not in resp.text
    # 링크 쿼리스트링에 상태 보존(기본 제외=1)
    assert "excl_privt=1" in resp.text


def test_list_excl_privt_off_shows_privt(client):
    """excl_privt=0(체크 해제) → 수의계약 공고 포함."""
    resp = client.get("/list", params={**_WIDE, "excl_privt": "0"})
    assert resp.status_code == 200
    assert "수의계약건 스모크 공고" in resp.text
    assert "excl_privt=0" in resp.text


def test_list_excl_privt_form_pair_checked(client):
    """폼 제출(hidden 0 + checkbox 1 동시 전송) → 제외로 해석."""
    resp = client.get(
        "/list",
        params=[("dt_from", "2000-01-01"), ("dt_to", "2099-12-31"),
                ("excl_privt", "0"), ("excl_privt", "1")],
    )
    assert resp.status_code == 200
    assert "수의계약건 스모크 공고" not in resp.text


def test_list_korean_only_headers(client):
    resp = client.get("/list", params=_WIDE)
    # 한글 헤더 노출, 영문 컬럼명/병기 code 미노출.
    assert "<th>공고명</th>" in resp.text
    assert "<th>공고번호</th>" not in resp.text
    assert "공고번호 / 공고명" not in resp.text
    assert '<th class="file-col">파일</th>' in resp.text
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


def test_list_sort_allowlist_matches_repository_columns():
    """목록 정렬 허용 키가 repository 정렬 컬럼과 같은지 확인합니다."""
    assert main._LIST_SORTS == frozenset(repository._SORT_COLUMNS)


def _bnp(no, nm, ntce_dt, openg_dt, price):
    """배정예산과 추정가격을 같은 값으로 채운 BidNotice. price=None 이면 NULL."""
    base = datetime(2026, 6, 1, 12, 0, 0)
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=nm,
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=ntce_dt,
        openg_dt=openg_dt,
        presmpt_prce=(None if price is None else Decimal(price)),
        asign_bdgt_amt=(None if price is None else Decimal(price)),
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


# --- price_min / price_max 배정예산 경계 -------------------------------
def test_search_price_min(session):
    _seed_priced(session)
    # 배정예산 >= 50,000,000 → P-2, P-3 (P-1 1천만 제외, P-4 NULL 제외).
    rows, total = repository.search_bid_notices(session, price_min=50_000_000)
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-2", "P-3"}


def test_search_price_max(session):
    _seed_priced(session)
    # 배정예산 <= 50,000,000 → P-1, P-2 (경계 포함, P-3 1억 제외, P-4 NULL 제외).
    rows, total = repository.search_bid_notices(session, price_max=50_000_000)
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-2"}


def test_search_price_min_and_max(session):
    _seed_priced(session)
    # 배정예산 10,000,000 ~ 50,000,000 (경계 포함) → P-1, P-2.
    rows, total = repository.search_bid_notices(
        session, price_min=10_000_000, price_max=50_000_000
    )
    assert total == 2
    assert {r.bid_ntce_no for r in rows} == {"P-1", "P-2"}


def test_search_price_none_keeps_null_rows(session):
    _seed_priced(session)
    # 배정예산 필터 없으면 NULL 배정예산(P-4)도 포함.
    rows, total = repository.search_bid_notices(session)
    assert total == 4
    assert "P-4" in {r.bid_ntce_no for r in rows}


def test_search_price_filters_use_budget_not_estimated_price(session):
    base = datetime(2026, 6, 1, 12, 0)
    session.add_all(
        [
            BidNotice(
                bid_ntce_no="BUDGET-LOW-PRICE-HIGH-BUDGET",
                bid_ntce_nm="예산 필터 낮은 추정가격 높은 예산",
                bid_ntce_dt=datetime(2026, 5, 1),
                presmpt_prce=Decimal("400"),
                asign_bdgt_amt=Decimal("600"),
                collected_at=base,
                updated_at=base,
            ),
            BidNotice(
                bid_ntce_no="BUDGET-HIGH-PRICE-LOW-BUDGET",
                bid_ntce_nm="예산 필터 높은 추정가격 낮은 예산",
                bid_ntce_dt=datetime(2026, 5, 2),
                presmpt_prce=Decimal("600"),
                asign_bdgt_amt=Decimal("400"),
                collected_at=base,
                updated_at=base,
            ),
            BidNotice(
                bid_ntce_no="BUDGET-EXACT",
                bid_ntce_nm="예산 필터 경계값",
                bid_ntce_dt=datetime(2026, 5, 3),
                presmpt_prce=Decimal("800"),
                asign_bdgt_amt=Decimal("500"),
                collected_at=base,
                updated_at=base,
            ),
            BidNotice(
                bid_ntce_no="BUDGET-NULL",
                bid_ntce_nm="예산 필터 배정예산 NULL",
                bid_ntce_dt=datetime(2026, 5, 4),
                presmpt_prce=Decimal("100"),
                asign_bdgt_amt=None,
                collected_at=base,
                updated_at=base,
            ),
        ]
    )
    session.commit()

    rows_max, total_max = repository.search_bid_notices(session, price_max=500)
    assert total_max == 2
    assert {row.bid_ntce_no for row in rows_max} == {
        "BUDGET-HIGH-PRICE-LOW-BUDGET",
        "BUDGET-EXACT",
    }

    rows_min, total_min = repository.search_bid_notices(session, price_min=500)
    assert total_min == 2
    assert {row.bid_ntce_no for row in rows_min} == {
        "BUDGET-LOW-PRICE-HIGH-BUDGET",
        "BUDGET-EXACT",
    }

    rows_unfiltered, total_unfiltered = repository.search_bid_notices(session)
    assert total_unfiltered == 4
    assert "BUDGET-NULL" in {row.bid_ntce_no for row in rows_unfiltered}

    rows_with_budget_condition, total_with_budget_condition = repository.search_bid_notices(
        session, price_max=1000
    )
    assert total_with_budget_condition == 3
    assert "BUDGET-NULL" not in {row.bid_ntce_no for row in rows_with_budget_condition}


# --- 신규 sort (NULL 뒤로) --------------------------------------------
def test_sort_bid_ntce_dt_asc(session):
    _seed_priced(session)
    rows, _ = repository.search_bid_notices(session, sort="bid_ntce_dt_asc")
    # 오름차순: 4/1(P-4) < 5/1(P-1) < 5/10(P-2) < 5/20(P-3)
    assert [r.bid_ntce_no for r in rows] == ["P-4", "P-1", "P-2", "P-3"]


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("desc", ["BEGIN-2", "BEGIN-1", "BEGIN-NULL"]),
     ("asc", ["BEGIN-1", "BEGIN-2", "BEGIN-NULL"])],
)
def test_sort_bid_begin_dt_nulls_last(session, direction, expected):
    base = datetime(2026, 6, 1, 12, 0)
    notices = [
        _bn("BEGIN-1", "개시일시 정렬 첫째", datetime(2026, 5, 1), None),
        _bn("BEGIN-2", "개시일시 정렬 둘째", datetime(2026, 5, 2), None),
        _bn("BEGIN-NULL", "개시일시 정렬 미정", datetime(2026, 5, 3), None),
    ]
    notices[0].bid_begin_dt = datetime(2026, 6, 1, 9, 0)
    notices[1].bid_begin_dt = datetime(2026, 6, 2, 9, 0)
    for notice in notices:
        notice.collected_at = base
        notice.updated_at = base
    session.add_all(notices)
    session.commit()

    rows, total = repository.search_bid_notices(session, sort=f"bid_begin_dt_{direction}")
    assert total == 3
    assert [row.bid_ntce_no for row in rows] == expected


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
    for invalid in ("inf", "-inf", "nan", "1e999", "-5", "9223372036854775808"):
        assert main._parse_price(invalid) is None


def test_new_database_seed_sets_default_budget_max(monkeypatch):
    """신규 app_config 시드의 입찰 배정예산 상한은 1억이다."""
    from app import db

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(db, "SessionLocal", local)
    try:
        db._seed_app_config()
        with local() as session:
            cfg = session.get(AppConfig, 1)
            assert cfg.presmpt_prce_bgn is None
            assert cfg.presmpt_prce_end == "100000000"
    finally:
        engine.dispose()


# --- 라우트 스모크: Phase 4.2 -----------------------------------------
def test_list_header_sort_links_present(client):
    """기본 정렬에서 일시 헤더별 두 옵션과 방향 표시를 검증한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    thead, notice_options, (direction_url, arrow_class, arrow) = _bid_time_header_parts(resp.text)
    assert "공고일시" in thead and "제출일시" in thead
    assert "입찰일시" not in thead
    assert 'class="bidtime-sortfield"' in thead
    assert 'autocomplete="off"' in thead
    assert list(notice_options) == ["게시", "개찰"]
    assert notice_options["게시"][1] is True
    assert notice_options["개찰"][1] is False
    assert "sort=bid_ntce_dt_desc" in notice_options["게시"][0]
    assert "sort=openg_dt_desc" in notice_options["개찰"][0]
    assert "sort=bid_ntce_dt_asc" in direction_url
    assert arrow_class == "arrow"
    assert arrow == "▼"
    _, submit_options, (_submit_url, submit_arrow_class, submit_arrow) = _bid_time_header_parts(
        resp.text, "제출일시"
    )
    assert list(submit_options) == ["시작", "마감"]
    assert submit_options["시작"][1] is True
    assert submit_options["마감"][1] is False
    assert "sort=bid_begin_dt_desc" in submit_options["시작"][0]
    assert "sort=bid_clse_dt_desc" in submit_options["마감"][0]
    assert (submit_arrow_class, submit_arrow) == ("arrow neutral", "↕")
    assert "sort=asign_bdgt_amt_desc" in thead
    assert "sort=presmpt_prce_desc" not in thead
    # 정렬 select 는 검색폼에서 제거됨(헤더 클릭으로 대체).
    assert '<select name="sort"' not in resp.text


def test_list_time_header_openg_asc(client):
    """개찰 오름차순이면 공고일시 헤더가 개찰 선택과 ▲를 표시한다."""
    resp = client.get("/list", params={**_WIDE, "sort": "openg_dt_asc"})
    assert resp.status_code == 200
    _thead, options, (direction_url, arrow_class, arrow) = _bid_time_header_parts(resp.text)
    assert options["개찰"][1] is True
    assert "sort=bid_ntce_dt_asc" in options["게시"][0]
    assert "sort=openg_dt_asc" in options["개찰"][0]
    assert "sort=openg_dt_desc" in direction_url
    assert arrow_class == "arrow"
    assert arrow == "▲"


def test_list_bid_time_headers_keep_sort_state_within_each_column(client):
    close = client.get("/list", params={**_WIDE, "sort": "bid_clse_dt_asc"})
    assert close.status_code == 200
    _, submit_options, (_url, submit_class, submit_arrow) = _bid_time_header_parts(
        close.text, "제출일시"
    )
    _, notice_options, (_url, notice_class, notice_arrow) = _bid_time_header_parts(close.text)
    assert submit_options["마감"][1] is True
    assert (submit_class, submit_arrow) == ("arrow", "▲")
    assert notice_options["게시"][1] is True
    assert (notice_class, notice_arrow) == ("arrow neutral", "↕")

    openg = client.get("/list", params={**_WIDE, "sort": "openg_dt_desc"})
    assert openg.status_code == 200
    _, notice_options, (_url, notice_class, notice_arrow) = _bid_time_header_parts(openg.text)
    _, submit_options, (_url, submit_class, submit_arrow) = _bid_time_header_parts(
        openg.text, "제출일시"
    )
    assert notice_options["개찰"][1] is True
    assert (notice_class, notice_arrow) == ("arrow", "▼")
    assert submit_options["시작"][1] is True
    assert (submit_class, submit_arrow) == ("arrow neutral", "↕")


def test_list_bid_begin_sort_nulls_last(client):
    notices = [
        _bn("S-BEGIN-1", "개시일시 라우트 첫째", datetime(2026, 5, 1), None),
        _bn("S-BEGIN-2", "개시일시 라우트 둘째", datetime(2026, 5, 2), None),
        _bn("S-BEGIN-NULL", "개시일시 라우트 미정", datetime(2026, 5, 3), None),
    ]
    notices[0].bid_begin_dt = datetime(2026, 6, 1, 9, 0)
    notices[1].bid_begin_dt = datetime(2026, 6, 2, 9, 0)
    with main.SessionLocal() as s:
        s.add_all(notices)
        s.commit()

    for direction, expected in (
        ("desc", ["S-BEGIN-2", "S-BEGIN-1", "S-BEGIN-NULL"]),
        ("asc", ["S-BEGIN-1", "S-BEGIN-2", "S-BEGIN-NULL"]),
    ):
        resp = client.get(
            "/list",
            params={**_WIDE, "q": "개시일시 라우트", "sort": f"bid_begin_dt_{direction}"},
        )
        assert resp.status_code == 200
        assert re.findall(r'<tr data-row-id="([^"]+)">', resp.text) == expected
        _thead, options, _direction = _bid_time_header_parts(resp.text, "제출일시")
        assert options["시작"][1] is True

    for direction in ("desc", "asc"):
        assert client.get("/list", params={**_WIDE, "sort": f"bid_begin_dt_{direction}"}).status_code == 200


def test_list_time_header_neutral_on_price_sort(client):
    """추정가격 정렬 중에는 게시 선택과 중립 화살표를 표시하고 옵션은 desc로 시작한다."""
    resp = client.get("/list", params={**_WIDE, "sort": "presmpt_prce_desc"})
    assert resp.status_code == 200
    thead, options, (direction_url, arrow_class, arrow) = _bid_time_header_parts(resp.text)
    assert options["게시"][1] is True
    assert all("_desc" in url for url, _selected in options.values())
    assert "sort=bid_ntce_dt_desc" in direction_url
    assert arrow_class == "arrow neutral"
    assert arrow == "↕"
    # 기존 추정가격 정렬 URL은 허용하되 화면 정렬 링크는 노출하지 않는다.
    assert "sort=presmpt_prce_asc" not in thead
    assert "추정가격" not in thead


def test_list_time_header_urls_preserve_filters_and_reset_page(client):
    """공고일시·제출일시 옵션과 방향 URL은 필터를 보존하고 page=1로 초기화한다."""
    from app import main as _main

    with _main.SessionLocal() as s:
        s.add_all(
            [
                _bn(
                    f"S-BT-PAGE-{i}",
                    f"필터보존 공고 {i}",
                    datetime(2026, 5, i + 1, 9, 0),
                    None,
                )
                for i in range(11)
            ]
        )
        s.commit()

    params = {
        **_WIDE,
        "q": "필터보존",
        "date_field": "bid_ntce_dt",
        "sort": "bid_clse_dt_asc",
        "page": "2",
        "page_size": "10",
    }
    resp = client.get("/list", params=params)
    assert resp.status_code == 200
    _thead, options, (direction_url, _arrow_class, _arrow) = _bid_time_header_parts(
        resp.text, "제출일시"
    )
    for url, _selected in options.values():
        assert "q=필터보존" in url
        assert "dt_from=2000-01-01" in url
        assert "dt_to=2099-12-31" in url
        assert "date_field=bid_ntce_dt" in url
        assert "page=1" in url
        assert "page=2" not in url
    assert "q=필터보존" in direction_url
    assert "dt_from=2000-01-01" in direction_url
    assert "dt_to=2099-12-31" in direction_url
    assert "page=1" in direction_url
    assert "page=2" not in direction_url


def test_list_time_headers_are_split_into_notice_and_submission(client):
    """목록 thead에는 공고일시·제출일시가 있고 예전 입찰일시 헤더는 없다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    thead, _options, _direction = _bid_time_header_parts(resp.text)
    assert "공고일시" in thead
    assert "제출일시" in thead
    assert "입찰일시" not in thead


def test_list_notice_and_submission_time_columns_show_two_fields_and_missing_values(client):
    """공고일시와 제출일시를 각각 두 필드와 시각 툴팁으로 표시한다."""
    from app import main as _main

    base = datetime(2026, 6, 1, 12, 0)
    with _main.SessionLocal() as s:
        s.add_all(
            [
                BidNotice(
                    bid_ntce_no="S-BT-FULL",
                    bid_ntce_nm="일시 분리 렌더 전체",
                    ntce_instt_nm="테스트기관",
                    bid_ntce_dt=datetime(2026, 9, 23, 10, 0),
                    bid_begin_dt=datetime(2026, 9, 24, 11, 30),
                    bid_clse_dt=datetime(2026, 10, 3, 18, 0),
                    openg_dt=datetime(2026, 10, 10, 11, 0),
                    collected_at=base,
                    updated_at=base,
                ),
                BidNotice(
                    bid_ntce_no="S-BT-NULL",
                    bid_ntce_nm="일시 분리 렌더 NULL",
                    ntce_instt_nm="테스트기관",
                    bid_ntce_dt=datetime(2026, 9, 24, 9, 30),
                    bid_clse_dt=None,
                    openg_dt=None,
                    collected_at=base,
                    updated_at=base,
                ),
            ]
        )
        s.commit()

    resp = client.get(
        "/list",
        params={**_WIDE, "q": "일시 분리 렌더", "include_past": "1"},
    )
    assert resp.status_code == 200

    full_pos = resp.text.index("일시 분리 렌더 전체")
    full_row = resp.text[resp.text.rfind("<tr", 0, full_pos):resp.text.index("</tr>", full_pos)]
    assert full_row.index("게시 : 2026-09-23") < full_row.index("개찰 : 2026-10-10")
    assert full_row.index("시작 : 2026-09-24") < full_row.index("마감 : 2026-10-03")
    assert 'title="2026-09-23 10:00"' in full_row
    assert 'title="2026-09-24 11:30"' in full_row
    assert 'title="2026-10-03 18:00"' in full_row
    assert 'title="2026-10-10 11:00"' in full_row

    null_pos = resp.text.index("일시 분리 렌더 NULL")
    null_row = resp.text[resp.text.rfind("<tr", 0, null_pos):resp.text.index("</tr>", null_pos)]
    assert '<span class="bt-line">게시 : -</span>' not in null_row
    assert '<span class="bt-line">개찰 : -</span>' in null_row
    assert '<span class="bt-line">시작 : -</span>' in null_row
    assert '<span class="bt-line">마감 : -</span>' in null_row


def test_render_list_time_cell_string_fallback_and_escape():
    """문자열 날짜는 앞 10자를 표시하고 title·본문의 HTML을 이스케이프한다."""
    row = {
        "bid_ntce_dt": "2026-09-23 10:00",
        "bid_begin_dt": "2026-09-24 11:30",
        "bid_clse_dt": None,
        "openg_dt": '"><script>alert(1)</script>',
    }
    notice_html = main._render_list_time_cell(
        row, main._LIST_TIME_FIELDS["notice_time"], "notice_times"
    )
    submit_html = main._render_list_time_cell(
        row, main._LIST_TIME_FIELDS["submit_time"], "submit_times"
    )
    assert 'title="2026-09-23 10:00">게시 : 2026-09-23</span>' in notice_html
    assert 'title="2026-09-24 11:30">시작 : 2026-09-24</span>' in submit_html
    assert '<span class="bt-line">마감 : -</span>' in submit_html
    assert "<script>" not in notice_html
    assert "&lt;script&gt;" in notice_html


def test_list_sort_bid_clse_dt_asc_nulls_last(client):
    """목록 라우트에서 입찰마감 오름차순 정렬 시 NULL은 마지막에 둔다."""
    from app import main as _main

    notices = [
        _bn("S-CLSE-2", "마감정렬 둘째", datetime(2026, 5, 2, 9, 0), None),
        _bn("S-CLSE-N", "마감정렬 미정", datetime(2026, 5, 3, 9, 0), None),
        _bn("S-CLSE-1", "마감정렬 첫째", datetime(2026, 5, 1, 9, 0), None),
    ]
    notices[0].bid_clse_dt = datetime(2026, 10, 2, 18, 0)
    notices[2].bid_clse_dt = datetime(2026, 10, 1, 18, 0)
    with _main.SessionLocal() as s:
        s.add_all(notices)
        s.commit()

    resp = client.get(
        "/list",
        params={**_WIDE, "q": "마감정렬", "sort": "bid_clse_dt_asc"},
    )
    assert resp.status_code == 200
    assert resp.text.index("마감정렬 첫째") < resp.text.index("마감정렬 둘째")
    assert resp.text.index("마감정렬 둘째") < resp.text.index("마감정렬 미정")


def test_list_time_sort_change_uses_event_delegation():
    """일시 정렬 select는 복원값을 초기화하고 change 위임으로 이동한다."""
    from app import main as _main

    assert "window.addEventListener('pageshow'" in _main._LIST_SCRIPT
    assert "document.querySelectorAll('select.bidtime-sortfield')" in _main._LIST_SCRIPT
    assert "option.selected = option.defaultSelected" in _main._LIST_SCRIPT
    assert "document.addEventListener('change'" in _main._LIST_SCRIPT
    assert "e.target.matches('select.bidtime-sortfield')" in _main._LIST_SCRIPT
    assert "location.href = e.target.value" in _main._LIST_SCRIPT


def test_render_ntce_kind_with_kind_and_bid_method():
    """공고종류와 입찰방식이 모두 있으면 kindcell 안에 두 줄을 렌더한다."""
    html = main._render_ntce_kind("재공고", "일반경쟁입찰", cell_class="kindcell")
    assert html.startswith('<td class="kindcell">')
    assert '<span class="badge warn">재공고</span>' in html
    assert '<span class="bidmethd" title="일반경쟁입찰">일반경쟁입찰</span>' in html


def test_render_ntce_kind_with_kind_only():
    """공고종류만 있으면 배지만 렌더한다."""
    assert main._render_ntce_kind("등록공고", None, cell_class="kindcell") == (
        '<td class="kindcell"><span class="badge">등록공고</span></td>'
    )


def test_render_ntce_kind_with_bid_method_only():
    """입찰방식만 있으면 kindcell 안에 입찰방식 줄만 렌더한다."""
    assert main._render_ntce_kind(None, "제한경쟁입찰", cell_class="kindcell") == (
        '<td class="kindcell"><span class="bidmethd" title="제한경쟁입찰">'
        '제한경쟁입찰</span></td>'
    )


def test_render_ntce_kind_empty():
    """공고종류와 입찰방식이 None·공백이면 대시 셀을 렌더한다."""
    assert main._render_ntce_kind(None, "   ", cell_class="kindcell") == '<td class="kindcell">-</td>'


def test_render_ntce_kind_escapes_bid_method():
    """입찰방식명의 title과 본문을 HTML 이스케이프한다."""
    html = main._render_ntce_kind(None, '<일반&"경쟁>', cell_class="kindcell")
    escaped = "&lt;일반&amp;&quot;경쟁&gt;"
    assert f'title="{escaped}">{escaped}</span>' in html
    assert '<일반&"경쟁>' not in html


def test_list_renders_bid_method_name(client):
    """목록 라우트가 입찰방식명을 bidmethd 줄로 표시한다."""
    from app import main as _main

    notice = _bn(
        "S-BID-METHOD",
        "입찰방식 렌더 공고",
        datetime(2026, 5, 7, 9, 0),
        None,
    )
    notice.ntce_kind_nm = "등록공고"
    notice.bid_methd_nm = "전자입찰"
    with _main.SessionLocal() as s:
        s.add(notice)
        s.commit()

    resp = client.get(
        "/list",
        params={**_WIDE, "q": "입찰방식 렌더", "include_past": "1"},
    )
    assert resp.status_code == 200
    assert 'class="bidmethd"' in resp.text
    assert "전자입찰" in resp.text


def test_list_default_sort_is_latest(client):
    """기본 정렬 = 최신 공고일(bid_ntce_dt_desc) — 공고일시 기준에 ▼ 표시."""
    resp = client.get("/list", params=_WIDE)
    # 현재 정렬이 desc 이므로 공고일시 게시 기준에 ▼ 화살표.
    assert "▼" in resp.text


def test_list_price_filter_query(client):
    """price_max 쿼리와 설정 기본 상한을 배정예산 기준으로 적용한다."""
    with main.SessionLocal() as s:
        repository.update_config(s, presmpt_prce_end="550")
        notices = [
            _bn("S-PRICE-LOW-BUDGET", "배정예산 라우트 낮은 예산", datetime(2026, 5, 1), None),
            _bn("S-PRICE-HIGH-BUDGET", "배정예산 라우트 높은 예산", datetime(2026, 5, 2), None),
            _bn("S-PRICE-CONFIG-IN", "배정예산 라우트 기본값 포함", datetime(2026, 5, 3), None),
        ]
        notices[0].asign_bdgt_amt, notices[0].presmpt_prce = 400, 600
        notices[1].asign_bdgt_amt, notices[1].presmpt_prce = 600, 400
        notices[2].asign_bdgt_amt, notices[2].presmpt_prce = 500, 900
        s.add_all(notices)
        s.commit()

    query_resp = client.get(
        "/list",
        params={**_WIDE, "q": "배정예산 라우트", "price_max": "450"},
    )
    assert query_resp.status_code == 200
    assert re.findall(r'<tr data-row-id="([^"]+)">', query_resp.text) == [
        "S-PRICE-LOW-BUDGET"
    ]
    assert 'name="price_min"' in query_resp.text
    assert 'name="price_max"' in query_resp.text
    assert 'value="450"' in query_resp.text

    default_resp = client.get("/list", params={**_WIDE, "q": "배정예산 라우트"})
    assert default_resp.status_code == 200
    assert re.findall(r'<tr data-row-id="([^"]+)">', default_resp.text) == [
        "S-PRICE-CONFIG-IN",
        "S-PRICE-LOW-BUDGET",
    ]
    assert 'value="550"' in default_resp.text
    assert "배정예산 최대" in default_resp.text
    assert "배정예산 최소" in default_resp.text
    assert "추정가격 최대" not in default_resp.text
    assert "추정가격 최소" not in default_resp.text


def test_list_date_field_switch_openg(client):
    """date_field=openg_dt 전환 — select 에 개찰일 선택, 라우트 200."""
    resp = client.get("/list", params={**_WIDE, "date_field": "openg_dt"})
    assert resp.status_code == 200
    assert 'name="date_field"' in resp.text
    assert '<option value="openg_dt" selected>개찰일</option>' in resp.text


def test_list_matched_industry_codes_and_tooltips(client):
    """매칭업종은 코드 목록으로 표시하고 각 코드의 툴팁에 전체 업종명을 담는다."""
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
    cell = re.search(r'<td class="matchind">\[.*?</td>', resp.text).group(0)
    assert re.sub(r"<[^>]+>", "", cell) == "[1426,1468]"
    assert (
        '<span class="indcode" title="소프트웨어사업자(패키지소프트웨어개발·공급사업)">1426</span>'
        in cell
    )
    assert (
        '<span class="indcode" title="소프트웨어사업자(컴퓨터관련서비스사업)">1468</span>'
        in cell
    )
    assert 'class="indrow"' not in cell


@pytest.mark.parametrize("value", [None, "", "   ", ", ,"])
def test_render_matched_industry_empty(value):
    """코드가 없으면 매칭업종 셀에 대시를 표시한다."""
    assert main._render_matched_inds(value) == '<td class="matchind">-</td>'


def test_render_matched_industry_unknown_order_and_escape(monkeypatch):
    """CSV 순서를 유지하며 모르는 코드·업종명·툴팁 모두 안전하게 이스케이프한다."""
    monkeypatch.setitem(industry_codes.INDUSTRY_NAMES, "1468", '<업종&"명>')
    cell = main._render_matched_inds(' 9999 ,1468,<code&"x>,1426')
    assert '<span class="indcode">9999</span>' in cell
    assert '<span class="indcode">&lt;code&amp;&quot;x&gt;</span>' in cell
    assert (
        '<span class="indcode" title="&lt;업종&amp;&quot;명&gt;">1468</span>' in cell
    )
    assert re.sub(r"<[^>]+>", "", cell) == '[9999,1468,&lt;code&amp;&quot;x&gt;,1426]'
    assert '<업종&"명>' not in cell
    assert '<code&"x>' not in cell


def test_config_page_has_price_defaults(client):
    """/config 에 입찰공고 배정예산 기본 하한/상한을 표시한다."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "입찰공고 검색 기본값(배정예산)" in resp.text
    assert "배정예산 기본 하한" in resp.text
    assert "배정예산 기본 상한" in resp.text
    assert "목록(/list)·사전규격(/pre-spec) 배정예산 검색" in resp.text
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
    assert "입찰공고 배정예산 기본 하한" in resp.text


def test_list_price_default_from_config(client):
    """설정의 가격 기본값이 /list 입력칸 기본값으로 노출(쿼리 미지정 시)."""
    from app import main as _main

    with _main.SessionLocal() as s:
        repository.update_config(s, presmpt_prce_bgn="7000000", presmpt_prce_end="8000000")

    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert 'value="7,000,000"' in resp.text
    assert 'value="8,000,000"' in resp.text


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
    """배정예산 금액은 `원` 단위로 표시하고 추정가격 컬럼은 노출하지 않는다."""
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
    # 가격 검색폼(배정예산 기준)은 유지하되 추정가격은 목록 헤더·셀에서 제거한다.
    thead = text[text.index("<thead>"):text.index("</thead>")]
    assert "배정예산" in thead
    assert "공고/수요기관" in thead
    assert "추정가격" not in thead
    assert thead.index("공고/수요기관") < thead.index("배정예산")
    assert thead.index("배정예산") < thead.index("공고일시")
    assert thead.index("공고일시") < thead.index("제출일시")
    assert thead.index("제출일시") < thead.index("매칭업종")
    # 금액 `원` 단위(배정예산).
    assert '<td class="budgetcell">12,345,678 원</td>' in text
    assert "9,000,000 원" not in text
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
    # 기본 정렬(bid_ntce_dt_desc) → 공고일시 게시 ▼, 배정예산(미정렬) ↕
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


def test_list_date_range_uses_hidden_value_and_quick_buttons(client):
    """/list 날짜 프리셋 네 버튼과 상시 표시 날짜 입력을 확인한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert '<input type="hidden" name="dt_range" id="dt_range" value="custom">' in resp.text
    assert not re.search(r'<select\b[^>]*name="dt_range"', resp.text)
    assert re.findall(r'data-date-preset="([^"]+)"', resp.text) == ["today", "1m", "3m", "6m"]
    assert 'class="is-active" data-date-preset="today"' not in resp.text
    assert 'type="date" name="dt_from"' in resp.text
    assert 'type="date" name="dt_to"' in resp.text
    assert "날짜지정" not in resp.text
    assert "list-custom-dates" not in resp.text
    assert "setToday" not in resp.text
    assert "setRecent" not in resp.text


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


def test_table_wrap_overflow_x_only(client):
    """.table-wrap 이 overflow-x: auto (가로 스크롤만, 내부 세로 스크롤 없음) 로 원복됐다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    text = resp.text
    idx = text.find(".table-wrap {")
    assert idx != -1, ".table-wrap 규칙이 CSS 에 없음"
    block = text[idx: idx + 60]
    assert "overflow-x: auto" in block, f".table-wrap overflow-x:auto 없음: {block!r}"
    # sticky thead/table-maxh 제거 확인
    assert "table thead th" not in text or "position: sticky" not in text[text.find("table thead th"):text.find("table thead th") + 80] if "table thead th" in text else True
    assert "var(--table-maxh" not in text, "--table-maxh 가 제거되지 않음"


def test_shell_sticky_script_present(client):
    """_shell 이 포함한 공유 스크립트에 ResizeObserver / --tabbar-h 가 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    assert "--tabbar-h" in resp.text


def test_shell_sticky_script_on_pre_spec(client):
    """사전규격 페이지에도 공유 스크립트가 존재한다."""
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    assert "--tabbar-h" in resp.text


def test_shell_sticky_script_on_config(client):
    """설정 페이지(filterCard 없음)에도 공유 스크립트가 존재한다."""
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "ResizeObserver" in resp.text
    assert "--tabbar-h" in resp.text


def test_filter_summary_flex_end(client):
    """.filter-summary 에 align-items: flex-end 가 CSS 에 존재한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "align-items: flex-end" in resp.text


# --- Phase 12.1: 목록 컬럼 재구성 --------------------------------------
def test_list_column_order(client):
    """구분부터 매칭업종까지 새 순서와 문구를 쓰고 체크·파일 헤더는 유지한다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    thead = resp.text[resp.text.index("<thead>"):resp.text.index("</thead>")]
    headers = re.findall(r"<th\b[^>]*>(.*?)</th>", thead)
    expected = [
        "구분", "낙찰방법", "공고명", "공고/수요기관",
        "배정예산", "공고일시", "제출일시", "매칭업종",
    ]
    for header, label in zip(headers[2:10], expected, strict=True):
        assert label in header
    assert "추정가격" not in thead
    assert "<th>공고번호</th>" not in thead
    assert "공고번호 / 공고명" not in thead
    assert "공고기관/수요기관" not in thead
    assert 'id="chk-all"' in headers[0]
    assert headers[1] == "#"
    assert headers[10] == "파일"
    assert '<th class="col-chk">' in thead
    assert '<th class="rownum-col">#</th>' in thead
    assert '<th class="kind-col kindhead">' in thead
    assert 'class="bidtime-head bidtime-col notice_times"' in thead
    assert 'class="bidtime-head bidtime-col submit_times"' in thead
    assert '<th class="file-col">파일</th>' in thead


@pytest.mark.parametrize("url", ['https://example.test/detail?a=1&b="2"', None])
def test_list_notice_cell_link_and_escape(client, url):
    """공고번호·공고명 순서와 링크 유무, 텍스트·속성 이스케이프를 검증한다."""
    notice = _bn('S-NTCE-<&"', '복합셀 <공고&"명>', datetime(2026, 5, 8), None)
    notice.bid_ntce_dtl_url = url
    with main.SessionLocal() as s:
        s.add(notice)
        s.commit()

    resp = client.get("/list", params={**_WIDE, "q": "복합셀"})
    assert resp.status_code == 200
    cell = re.search(r'<td class="ntcecell">.*?</td>', resp.text).group(0)
    number = '<span class="ntceno">S-NTCE-&lt;&amp;&quot;</span>'
    name = '복합셀 &lt;공고&amp;&quot;명&gt;'
    assert cell.index(number) < cell.index(name)
    if url:
        assert (
            '<a class="ntcenm" href="https://example.test/detail?a=1&amp;b=&quot;2&quot;" '
            f'target="_blank" rel="noopener">{name}</a>'
        ) in cell
    else:
        assert f'<span class="ntcenm">{name}</span>' in cell
        assert "<a " not in cell
    assert '<공고&"명>' not in cell
    assert 'data-item-id="S-NTCE-&lt;&amp;&quot;"' in resp.text


@pytest.mark.parametrize(
    ("kind", "raw_json", "current", "expected"),
    [
        ("재공고", '{"befBidBbancNo":" R26-OLD "}', "R26-NEW", "R26-OLD"),
        ("변경공고", '{"befBidBbancNo":"R26-OLD"}', "R26-NEW", "R26-OLD"),
        ("등록공고", '{"befBidBbancNo":"R26-OLD"}', "R26-NEW", None),
        ("재공고", '{"befBidBbancNo":"  "}', "R26-NEW", None),
        ("재공고", '{bad json', "R26-NEW", None),
        ("재공고", None, "R26-NEW", None),
        ("재공고", "{}", "R26-NEW", None),
        ("재공고", "[]", "R26-NEW", None),
        ("재공고", '{"befBidBbancNo":"R26-NEW"}', "R26-NEW", None),
    ],
)
def test_related_notice_number_helper(kind, raw_json, current, expected):
    assert main._related_ntce_no(kind, raw_json, current) == expected


def test_related_notice_number_is_escaped_in_notice_cell():
    cell = main._render_ntce(
        "R26-NEW",
        "관련번호 이스케이프",
        None,
        kind="재공고",
        raw_json=json.dumps({"befBidBbancNo": '<old&"no>'}),
    )
    assert 'R26-NEW - [&lt;old&amp;&quot;no&gt;]' in cell
    assert '<old&"no>' not in cell


def test_list_renders_related_notice_number_for_reannouncement(client):
    notice = _bn("R26BK01743383", "관련공고 라우트 검증", datetime(2026, 5, 8), None)
    notice.ntce_kind_nm = "재공고"
    notice.raw_json = '{"befBidBbancNo":" R26BK01725789 "}'
    with main.SessionLocal() as session:
        session.add(notice)
        session.commit()

    response = client.get(
        "/list", params={**_WIDE, "q": "관련공고 라우트", "include_past": "1"}
    )
    assert response.status_code == 200
    assert "R26BK01743383 - [R26BK01725789]" in response.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "-"), ("", "-"), ("   ", "-"),
     ('  <공고&"값>  ', '&lt;공고&amp;&quot;값&gt;'), (0, "0"), (False, "False")],
)
def test_list_notice_cell_normalizes_lines(value, expected):
    """공고번호·공고명은 같은 방식으로 정규화·이스케이프하고 빈 줄만 대시로 표시한다."""
    text = main._render_list_rows(
        [{"bid_ntce_no": value, "bid_ntce_nm": value}], main._DEFAULT_SORT, {}
    )
    assert (
        f'<td class="ntcecell"><span class="ntceno">{expected}</span>'
        f'<span class="ntcenm">{expected}</span></td>'
    ) in text


def test_list_notice_cell_line_spacing():
    """공고번호·공고명은 글꼴 변경 없이 두 줄로 표시하고 공고명 링크 폭은 내용에 맞춘다."""
    rules = re.search(
        r"td\.ntcecell \.ntceno, td\.ntcecell \.ntcenm\s*\{([^}]+)\}", main.BASE_CSS
    ).group(1)
    spacing = re.search(r"td\.ntcecell \.ntceno\s*\{([^}]+)\}", main.BASE_CSS).group(1)
    name_css = re.search(
        r"(?m)^\s*td\.ntcecell \.ntcenm\s*\{([^}]+)\}", main.BASE_CSS
    ).group(1)
    assert "display: block" in rules
    assert "margin-bottom: 4px" in spacing
    assert "width: fit-content" in name_css
    assert "white-space: nowrap" not in rules + name_css
    assert "font" not in rules + spacing + name_css
    assert "color" not in rules + spacing + name_css


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("협상에의한계약-협상에 의한 낙찰자 결정(SW사업)", "협상에의한계약"),
        ("수의시담-일반경쟁->수의", "수의시담"),
        ("제한적최저가(낙찰하한율)-제한적최저가(낙찰하한율)", "제한적최저가(낙찰하한율)"),
        ("적격심사제-소프트웨어용역(…) 5억원 미만-추정가격 고시금액이상", "적격심사제"),
        ("  협상에의한계약  ", "협상에의한계약"),
        ("  적격심사제  - 세부설명 ", "적격심사제"),
        (" -abc ", "-abc"),
        (None, "-"),
        ("  ", "-"),
        ('<협상&"계약>-설명', '<협상&"계약>'),
    ],
)
def test_sucsfbid_mthd_short(value, expected):
    """축약 헬퍼는 원문 텍스트를 반환하고 HTML 이스케이프는 출력 시 적용한다."""
    assert main._sucsfbid_mthd_short(value) == expected


@pytest.mark.parametrize(
    ("value", "expected_cell"),
    [
        (
            "협상에의한계약-협상에 의한 낙찰자 결정(SW사업)",
            '<td class="sucsfcell" title="협상에의한계약-협상에 의한 낙찰자 결정(SW사업)">'
            '협상에의한계약</td>',
        ),
        (
            ' <협상&"계약>-<세부&"설명> ',
            '<td class="sucsfcell" title="&lt;협상&amp;&quot;계약&gt;-&lt;세부&amp;&quot;설명&gt;">'
            '&lt;협상&amp;&quot;계약&gt;</td>',
        ),
        (None, '<td class="sucsfcell">-</td>'),
        ("  ", '<td class="sucsfcell">-</td>'),
    ],
)
def test_list_sucsfbid_method_cell(client, value, expected_cell):
    """DB 낙찰방법을 축약 표시하고 tooltip에는 앞뒤 공백을 제거한 원문을 이스케이프한다."""
    notice = _bn("S-SUCSF", "낙찰방법 표기 공고", datetime(2026, 5, 8), None)
    notice.sucsfbid_mthd_nm = value
    with main.SessionLocal() as s:
        s.add(notice)
        s.commit()

    resp = client.get("/list", params={**_WIDE, "q": "낙찰방법 표기"})
    assert resp.status_code == 200
    assert expected_cell in resp.text


def test_list_kind_alignment(client):
    """구분 데이터 셀은 가운데 정렬을 유지하고 헤더 가운데 정렬 규칙은 제거한다."""
    notice = _bn("S-KIND-EMPTY", "빈 구분 셀 공고", datetime(2026, 5, 8), datetime(2000, 1, 1))
    notice.ntce_kind_nm = None
    notice.bid_methd_nm = ""
    with main.SessionLocal() as s:
        s.add(notice)
        s.commit()

    resp = client.get(
        "/list", params={**_WIDE, "q": "빈 구분 셀 공고", "include_past": "1"}
    )
    assert resp.status_code == 200
    assert 'data-row-id="S-KIND-EMPTY"' in resp.text
    assert '<th class="kind-col kindhead">구분</th>' in resp.text
    assert '<td class="kind-col kindcell">-</td>' in resp.text
    cell_css = re.search(r"td\.kindcell\s*\{([^}]+)\}", main.BASE_CSS).group(1)
    assert "text-align: center" in cell_css
    assert "vertical-align: middle" in cell_css
    assert not re.search(r"th\.kindhead\s*\{[^}]*text-align:\s*center", main.BASE_CSS)


def test_list_table_alignment_scoped_css(client):
    """목록 표의 정렬만 바꾸고 공용 표·파일 표에 쓰는 기본 CSS는 그대로 둔다."""
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert '<table class="listtable">' in resp.text
    td_selector = "table.listtable > tbody > tr > td"
    th_selector = "table.listtable > thead > tr > th:not(.col-chk)"
    budget_selector = "table.listtable > tbody > tr > td.budgetcell"
    rules = {
        selector: re.search(re.escape(selector) + r"\s*\{([^}]+)\}", main.BASE_CSS).group(1)
        for selector in (td_selector, th_selector, budget_selector)
    }
    assert "vertical-align: middle" in rules[td_selector]
    assert "text-align" not in rules[td_selector]
    assert "text-align: left" in rules[th_selector]
    assert "vertical-align: middle" in rules[th_selector]
    chk_header = re.search(r"\.col-chk\s*\{([^}]+)\}", main.BASE_CSS).group(1)
    assert "text-align: center" in chk_header
    assert "text-align: right" in rules[budget_selector]
    base_css = re.search(r"th, td\s*\{([^}]+)\}", main.BASE_CSS).group(1)
    assert " ".join(base_css.split()) == (
        "border: 1px solid #e2e5ea; padding: 6px 8px; text-align: left; vertical-align: top; "
        "max-width: 360px; overflow: hidden; text-overflow: ellipsis;"
    )
    assert 'class="listtable"' not in client.get("/pre-spec", params=_WIDE).text
    assert 'class="listtable"' not in client.get("/config").text


def test_list_fixed_column_widths_and_nowrap_content(client, monkeypatch):
    """정해진 값만 있는 목록 컬럼 폭과 배지·버튼 nowrap 규칙을 확인한다."""
    monkeypatch.setattr(main, "analysis_enabled", lambda: True)
    notice = _bn("S-FIXED-COLS", "고정폭 검증", datetime(2026, 5, 8), None)
    notice.ntce_kind_nm = "변경공고"
    notice.bid_methd_nm = "일반경쟁"
    notice.ntce_spec_doc_url1 = "https://example.test/file"
    with main.SessionLocal() as session:
        session.add(notice)
        session.commit()

    response = client.get("/list", params={**_WIDE, "include_past": "1"})
    assert response.status_code == 200
    for cls, width in (
        ("col-chk", 36),
        ("rownum-col", 44),
        ("kind-col", 96),
        ("bidtime-col", 150),
        ("file-col", 80),
        ("analysis-col", 120),
    ):
        rule = re.search(rf"table\.listtable \.{cls}\s*\{{([^}}]+)\}}", main.BASE_CSS)
        assert rule is not None
        for property_name in ("width", "min-width", "max-width"):
            assert f"{property_name}: {width}px" in rule.group(1)
        class_sets = [
            attrs.split() for attrs in re.findall(r'class="([^"]+)"', response.text)
        ]
        assert any(cls in classes for classes in class_sets)
    badge_rule = re.search(r"(?m)^\s*\.badge\s*\{([^}]+)\}", main.BASE_CSS)
    assert badge_rule is not None and "white-space: nowrap" in badge_rule.group(1)
    for selector in (
        "table.listtable .sucsfcell",
        "table.listtable button.filebtn",
        "table.listtable button.btn-analyze",
        "table.listtable .analysis-actions button",
    ):
        assert selector in main.BASE_CSS
    assert re.search(
        r"table\.listtable \.sucsfcell, table\.listtable button\.filebtn,\s*"
        r"table\.listtable button\.btn-analyze, table\.listtable \.analysis-actions button\s*"
        r"\{[^}]*white-space: nowrap",
        main.BASE_CSS,
    )


def _seed_budgets(session):
    """날짜·추정가격과 다른 배정예산 순서 및 NULL을 가진 공고를 생성한다."""
    for i, (suffix, budget, price) in enumerate(
        [("MID", 50_000_000, 90_000_000), ("NULL", None, 70_000_000),
         ("LOW", 10_000_000, 50_000_000), ("HIGH", 90_000_000, 10_000_000)]
    ):
        notice = _bn(f"S-BUD-{suffix}", f"배정예산정렬 {suffix}", datetime(2026, 5, i + 1), None)
        notice.asign_bdgt_amt = budget
        notice.presmpt_prce = price
        session.add(notice)
    session.commit()


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("desc", ["HIGH", "MID", "LOW", "NULL"]), ("asc", ["LOW", "MID", "HIGH", "NULL"])],
)
def test_sort_budget_nulls_last(session, direction, expected):
    """repository 배정예산 정렬은 두 방향 모두 NULL을 마지막에 둔다."""
    _seed_budgets(session)
    rows, total = repository.search_bid_notices(session, sort=f"asign_bdgt_amt_{direction}")
    assert total == 4
    assert [row.bid_ntce_no for row in rows] == [f"S-BUD-{suffix}" for suffix in expected]


@pytest.mark.parametrize(
    ("direction", "expected", "next_direction", "arrow"),
    [("desc", ["HIGH", "MID", "LOW", "NULL"], "asc", "▼"),
     ("asc", ["LOW", "MID", "HIGH", "NULL"], "desc", "▲")],
)
def test_list_budget_sort_nulls_last_and_header(client, direction, expected, next_direction, arrow):
    """라우트의 예산 정렬·NULL 후순위와 헤더 토글·일시 헤더 중립 상태를 검증한다."""
    with main.SessionLocal() as s:
        _seed_budgets(s)
    resp = client.get(
        "/list", params={**_WIDE, "q": "배정예산정렬", "sort": f"asign_bdgt_amt_{direction}"}
    )
    assert resp.status_code == 200
    assert re.findall(r'<tr data-row-id="([^"]+)">', resp.text) == [
        f"S-BUD-{suffix}" for suffix in expected
    ]
    thead, options, (direction_url, arrow_class, time_arrow) = _bid_time_header_parts(resp.text)
    budget_header = next(h for h in re.findall(r"<th\b[^>]*>.*?</th>", thead) if "배정예산" in h)
    assert f"sort=asign_bdgt_amt_{next_direction}" in budget_header
    assert f'<span class="arrow">{arrow}</span>' in budget_header
    assert "q=배정예산정렬" in budget_header
    assert "page=1" in budget_header
    assert options["게시"][1] is True
    assert "sort=bid_ntce_dt_desc" in direction_url
    assert (arrow_class, time_arrow) == ("arrow neutral", "↕")


@pytest.mark.parametrize(("enabled", "column_count"), [(True, 12), (False, 11)])
def test_list_column_count_with_analysis_toggle(client, monkeypatch, enabled, column_count):
    """분석 토글에 따른 실제 헤더·셀 수와 분석 버튼의 colspan이 일치한다."""
    monkeypatch.setattr(main, "analysis_enabled", lambda: enabled)
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    thead = resp.text[resp.text.index("<thead>"):resp.text.index("</thead>")]
    tbody = resp.text[resp.text.index("<tbody>"):resp.text.index("</tbody>")]
    assert len(re.findall(r"<th\b", thead)) == column_count
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", tbody):
        assert len(re.findall(r"<td\b", row)) == column_count
    spans = re.findall(r'data-colspan="(\d+)"', tbody)
    if enabled:
        assert spans and set(spans) == {str(column_count)}
        assert 'data-name="스모크 테스트 공고"' in tbody
    else:
        assert not spans
