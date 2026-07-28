"""공고명 제외 키워드 필터 테스트 — Phase 10.

- repository: 제외 조건(부분일치·NULL 보존·다중 AND·LIKE 특수문자)·키워드 CRUD 를 인메모리 SQLite 로 검증.
- 라우트: /list·/pre-spec 기본 적용/폼 제출/해제, /config 목록 관리(추가·중복·삭제·기본저장)를
  TestClient + 임시 파일 SQLite 로 검증(실 API·실 procurement.db 비의존).
실행: `pytest tests/test_exclude_keywords.py`
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repository
from app.db import Base
from app.models import AppConfig, BidNotice, ExcludeKeyword, PreSpec

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


def _bn(no: str, nm: str | None) -> BidNotice:
    """개찰일 NULL(항상 표시) 공고 — 이름 필터만 검증하도록 다른 조건은 중립."""
    return BidNotice(
        bid_ntce_no=no,
        bid_ntce_nm=nm,
        ntce_instt_nm="테스트기관",
        bid_ntce_dt=datetime(2026, 5, 1, 9, 0),
        openg_dt=None,
        collected_at=_META,
        updated_at=_META,
    )


def _ps(no: str, nm: str | None) -> PreSpec:
    return PreSpec(
        bf_spec_rgst_no=no,
        prdct_clsfc_no_nm=nm,
        order_instt_nm="테스트기관",
        rcpt_dt=datetime(2026, 5, 1, 9, 0),
        opnin_rgst_clse_dt=None,
        sw_biz_obj_yn="Y",
        collected_at=_META,
        updated_at=_META,
    )


@pytest.fixture
def session():
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


# --- repository: 제외 조건 --------------------------------------------
def test_exclude_keyword_hides_matching_bid(session):
    session.add_all([
        _bn("B-1", "정보시스템 구축 감리 용역"),
        _bn("B-2", "정보시스템 구축 용역"),
    ])
    session.commit()

    rows, total = repository.search_bid_notices(session, exclude_keywords=["감리"])
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-2"]


def test_exclude_keyword_keeps_null_name(session):
    """공고명 NULL(데이터 누락)은 제외 필터로 숨기지 않는다."""
    session.add_all([_bn("B-1", "감리 용역"), _bn("B-NULL", None)])
    session.commit()

    rows, total = repository.search_bid_notices(session, exclude_keywords=["감리"])
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-NULL"]


def test_exclude_multiple_keywords_all_applied(session):
    session.add_all([
        _bn("B-1", "감리 용역"),
        _bn("B-2", "청소 용역"),
        _bn("B-3", "소프트웨어 개발"),
    ])
    session.commit()

    rows, total = repository.search_bid_notices(
        session, exclude_keywords=["감리", "청소"]
    )
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-3"]


def test_exclude_keyword_blank_and_duplicate_ignored(session):
    session.add_all([_bn("B-1", "감리 용역"), _bn("B-2", "개발 용역")])
    session.commit()

    rows, total = repository.search_bid_notices(
        session, exclude_keywords=["", "  ", "감리", "감리"]
    )
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-2"]


def test_exclude_keyword_like_wildcards_escaped(session):
    """`%`·`_` 는 LIKE 와일드카드가 아니라 문자 그대로 비교한다(과잉 제외 방지)."""
    session.add_all([_bn("B-1", "100% 사전규격 용역"), _bn("B-2", "일반 용역")])
    session.commit()

    rows, total = repository.search_bid_notices(session, exclude_keywords=["%"])
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-2"]


def test_exclude_keyword_limit(session):
    """상한(EXCLUDE_KEYWORD_MAX) 초과분은 무시 — 조건 무한 누적 방지."""
    session.add(_bn("B-1", "감리 용역"))
    session.commit()

    over = [f"단어{i}" for i in range(repository.EXCLUDE_KEYWORD_MAX)] + ["감리"]
    rows, total = repository.search_bid_notices(session, exclude_keywords=over)
    # 상한까지만 적용되므로 마지막 "감리"는 조건에 들어가지 않는다.
    assert total == 1
    assert [r.bid_ntce_no for r in rows] == ["B-1"]


def test_exclude_keyword_pre_spec(session):
    session.add_all([_ps("P-1", "감리 사업"), _ps("P-2", "유지보수 사업"), _ps("P-3", None)])
    session.commit()

    rows, total = repository.search_pre_specs(session, exclude_keywords=["감리"])
    assert total == 2
    assert {r.bf_spec_rgst_no for r in rows} == {"P-2", "P-3"}


def test_exclude_keyword_none_is_noop(session):
    session.add_all([_bn("B-1", "감리 용역"), _bn("B-2", "개발 용역")])
    session.commit()

    _, total = repository.search_bid_notices(session, exclude_keywords=None)
    assert total == 2


# --- repository: 키워드 CRUD ------------------------------------------
def test_add_and_list_exclude_keywords(session):
    assert repository.add_exclude_keyword(session, " 감리 ") is True
    assert repository.add_exclude_keyword(session, "청소", is_default=True) is True

    rows = repository.list_exclude_keywords(session)
    # 기본 적용 우선 정렬.
    assert [r["keyword"] for r in rows] == ["청소", "감리"]
    assert rows[0]["is_default"] is True
    assert rows[1]["is_default"] is False


def test_add_exclude_keyword_rejects_blank_and_duplicate(session):
    assert repository.add_exclude_keyword(session, "   ") is False
    assert repository.add_exclude_keyword(session, ",") is False
    assert repository.add_exclude_keyword(session, "감리") is True
    assert repository.add_exclude_keyword(session, "감리") is False   # 정확 중복
    assert repository.add_exclude_keyword(session, "AI") is True
    assert repository.add_exclude_keyword(session, "ai") is False     # 대소문자 무시 중복
    assert len(repository.list_exclude_keywords(session)) == 2


def test_normalize_keyword_strips_comma_and_limits_length(session):
    assert repository.normalize_keyword(" 감리 ,용역 ") == "감리 용역"
    assert len(repository.normalize_keyword("가" * 100)) == repository.EXCLUDE_KEYWORD_MAX_LEN
    assert repository.normalize_keyword(None) == ""


def test_delete_exclude_keyword(session):
    repository.add_exclude_keyword(session, "감리")
    kw_id = repository.list_exclude_keywords(session)[0]["id"]

    assert repository.delete_exclude_keyword(session, kw_id) is True
    assert repository.list_exclude_keywords(session) == []
    assert repository.delete_exclude_keyword(session, kw_id) is False


def test_set_default_exclude_keywords_replaces_all(session):
    repository.add_exclude_keyword(session, "감리", is_default=True)
    repository.add_exclude_keyword(session, "청소")
    rows = {r["keyword"]: r["id"] for r in repository.list_exclude_keywords(session)}

    repository.set_default_exclude_keywords(session, [rows["청소"]])
    assert repository.get_default_exclude_keywords(session) == ["청소"]

    repository.set_default_exclude_keywords(session, [])
    assert repository.get_default_exclude_keywords(session) == []


# --- 라우트 --------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 파일 SQLite 로 main.SessionLocal 을 교체한 TestClient."""
    from fastapi.testclient import TestClient

    from app import main

    db_path = tmp_path / "kw_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, future=True)

    with Local() as s:
        s.add(_cfg())
        s.add(_bn("K-1", "감리 용역 공고"))
        s.add(_bn("K-2", "소프트웨어 개발 공고"))
        s.add(_ps("KP-1", "감리 사업"))
        s.add(_ps("KP-2", "소프트웨어 유지보수 사업"))
        s.add(ExcludeKeyword(keyword="감리", is_default=True, created_at=_META))
        s.add(ExcludeKeyword(keyword="청소", is_default=False, created_at=_META))
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Local)
    return TestClient(main.app)


_WIDE = {"dt_from": "2000-01-01", "dt_to": "2099-12-31"}


def test_list_applies_default_keyword_on_first_visit(client):
    resp = client.get("/list", params=_WIDE)
    assert resp.status_code == 200
    assert "감리 용역 공고" not in resp.text          # 기본 적용 키워드로 제외
    assert "소프트웨어 개발 공고" in resp.text
    # 칩·요약 배지·등록 목록 드롭다운 노출.
    assert 'data-kw="감리"' in resp.text
    assert "제외 키워드 1개" in resp.text
    assert 'id="exclKwMenu"' in resp.text
    # 드롭다운 항목: 기본 적용=warning(is-default), 현재 적용 중=success(is-selected).
    assert 'class="kw-opt is-default is-selected" data-kw="감리"' in resp.text
    assert 'class="kw-opt" data-kw="청소"' in resp.text
    assert "(기본)" not in resp.text  # 혼동되는 텍스트 표기 대신 색상으로 구분


def test_list_form_submit_overrides_default(client):
    """폼 제출(excl_kw_set=1) 이면 화면 칩 목록이 기준 — 기본값을 덮어쓴다."""
    resp = client.get(
        "/list", params={**_WIDE, "excl_kw": "소프트웨어", "excl_kw_set": "1"}
    )
    assert resp.status_code == 200
    assert "감리 용역 공고" in resp.text
    assert "소프트웨어 개발 공고" not in resp.text


def test_list_form_submit_empty_clears_filter(client):
    """칩을 모두 지우고 검색하면 제외 필터 없음(기본값이 되살아나지 않는다)."""
    resp = client.get("/list", params={**_WIDE, "excl_kw": "", "excl_kw_set": "1"})
    assert resp.status_code == 200
    assert "감리 용역 공고" in resp.text
    assert "소프트웨어 개발 공고" in resp.text


def test_list_keyword_preserved_in_links(client):
    """정렬·페이저 링크에 제외 키워드가 URL 인코딩되어 보존된다."""
    resp = client.get("/list", params=_WIDE)
    assert "excl_kw=%EA%B0%90%EB%A6%AC" in resp.text
    assert "excl_kw_set=1" in resp.text


def test_list_script_guards_ime_composition(client):
    """한글(IME) 조합 중 Enter 는 무시해야 한다 — 없으면 조합 확정 글자가 중복 칩으로 들어간다."""
    from app import main

    assert "compositionstart" in main._EXCL_KW_SCRIPT
    assert "compositionend" in main._EXCL_KW_SCRIPT
    assert "e.isComposing || e.keyCode === 229" in main._EXCL_KW_SCRIPT
    # 실제 화면에도 그대로 실려야 한다.
    assert "e.isComposing" in client.get("/list", params=_WIDE).text


def test_pre_spec_applies_default_keyword(client):
    resp = client.get("/pre-spec", params=_WIDE)
    assert resp.status_code == 200
    assert "감리 사업" not in resp.text
    assert "소프트웨어 유지보수 사업" in resp.text
    assert "품명/사업명 제외 키워드" in resp.text


def test_pre_spec_form_submit_overrides_default(client):
    resp = client.get(
        "/pre-spec", params={**_WIDE, "excl_kw": "유지보수", "excl_kw_set": "1"}
    )
    assert resp.status_code == 200
    assert "감리 사업" in resp.text
    assert "소프트웨어 유지보수 사업" not in resp.text


def test_config_page_shows_keyword_card(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "공고명 제외 키워드 목록" in resp.text
    assert 'name="default_ids"' in resp.text
    assert "감리" in resp.text and "청소" in resp.text


def test_config_keyword_add(client):
    resp = client.post(
        "/config/keywords",
        data={"action": "add", "new_keyword": " 임대 ", "default_ids": []},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/config").text
    assert "임대" in page


def test_config_keyword_add_duplicate_shows_error(client):
    resp = client.post(
        "/config/keywords",
        data={"action": "add", "new_keyword": "감리"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "이미 등록된 키워드입니다" in resp.text


def test_config_keyword_add_blank_shows_error(client):
    resp = client.post(
        "/config/keywords",
        data={"action": "add", "new_keyword": "   "},
        follow_redirects=True,
    )
    assert "추가할 키워드를 입력하세요" in resp.text


def test_config_keyword_save_defaults(client, monkeypatch):
    """체크 해제 저장 → 목록 화면 기본 적용에서도 빠진다."""
    from app import main

    with main.SessionLocal() as s:
        ids = {r["keyword"]: r["id"] for r in repository.list_exclude_keywords(s)}

    resp = client.post(
        "/config/keywords",
        data={"action": "save", "default_ids": [str(ids["청소"])]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with main.SessionLocal() as s:
        assert repository.get_default_exclude_keywords(s) == ["청소"]

    # 기본 적용이 바뀌었으므로 감리 공고가 다시 보인다.
    page = client.get("/list", params=_WIDE).text
    assert "감리 용역 공고" in page


def test_config_keyword_delete(client):
    from app import main

    with main.SessionLocal() as s:
        ids = {r["keyword"]: r["id"] for r in repository.list_exclude_keywords(s)}

    resp = client.post(
        "/config/keywords",
        data={"delete_id": str(ids["감리"]), "default_ids": [str(ids["감리"])]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with main.SessionLocal() as s:
        assert [r["keyword"] for r in repository.list_exclude_keywords(s)] == ["청소"]
