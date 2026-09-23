"""입찰공고 종류 검색 — 선택 종류·NULL 보존·기존 호출 호환."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import repository
from app.db import Base
from app.models import BidNotice


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 23, 12)
    with Session(engine) as session:
        session.add_all([
            BidNotice(
                bid_ntce_no=code,
                bid_ntce_nm=f"개발 {code}",
                ntce_kind_nm=kind,
                bid_ntce_dt=now,
                collected_at=now,
                updated_at=now,
            )
            for code, kind in [
                ("reg", "등록공고"),
                ("re", "재공고"),
                ("chg", "변경공고"),
                ("cncl", "취소공고"),
                ("null", None),
            ]
        ])
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize(
    ("ntce_kinds", "expected"),
    [
        (["등록공고", "재공고", "변경공고"], {"reg", "re", "chg", "null"}),
        (["취소공고"], {"cncl", "null"}),
        (["등록공고", "취소공고"], {"reg", "cncl", "null"}),
    ],
)
def test_selected_kinds_include_null(session, ntce_kinds, expected):
    rows, total = repository.search_bid_notices(session, ntce_kinds=ntce_kinds)

    assert {row.bid_ntce_no for row in rows} == expected
    assert total == len(expected)


@pytest.mark.parametrize("kwargs", [{}, {"ntce_kinds": None}, {"ntce_kinds": []}])
def test_missing_or_empty_kinds_keep_all_notices(session, kwargs):
    rows, total = repository.search_bid_notices(session, **kwargs)

    assert {row.bid_ntce_no for row in rows} == {"reg", "re", "chg", "cncl", "null"}
    assert total == 5


def test_null_kind_still_obeys_other_filters(session):
    rows, total = repository.search_bid_notices(
        session, ntce_kinds=["등록공고"], q="reg"
    )

    assert [row.bid_ntce_no for row in rows] == ["reg"]
    assert total == 1


def test_kind_total_counts_all_matching_pages(session):
    rows, total = repository.search_bid_notices(
        session, ntce_kinds=["등록공고", "재공고", "변경공고"], page_size=2
    )

    assert len(rows) == 2
    assert {row.bid_ntce_no for row in rows} <= {"reg", "re", "chg", "null"}
    assert total == 4
