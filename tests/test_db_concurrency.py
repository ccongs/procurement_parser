"""DB 동시성 핫픽스 회귀 테스트 — SQLite WAL/busy_timeout + create_run 즉시 commit.

운영 중 입찰·사전규격 두 수집 잡이 동시 tick 으로 뜨면, 한쪽이 `create_run` 의
쓰기 트랜잭션을 연 채로 긴 네트워크 페이징을 끝까지 진행하다가 `finish_run` 에서야
commit 해, 다른 잡의 INSERT 가 `sqlite3.OperationalError: database is locked` 로
실패했다. 이 핫픽스는 두 파트로 고친다:

1. `app/db.py` — SQLite 연결마다 PRAGMA(journal_mode=WAL·busy_timeout=30000·
   synchronous=NORMAL)를 connect 이벤트 리스너로 적용(SQLite 전용 가드, PG 미적용).
2. `app/repository.py` — `create_run` 의 `flush()`→`commit()`(쓰기 잠금을 네트워크
   fetch 전에 해제).

테스트는 결정적이며(CODER_SESSION §6) 네트워크/실 procurement.db 에 의존하지 않는다.
PRAGMA·WAL 검증은 인메모리가 아니라 **임시 파일 SQLite** 로 한다(인메모리는 WAL 이
`memory` 로 떨어질 수 있어 검증 부적합).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app import repository
from app.db import (
    Base,
    _apply_sqlite_pragmas,
    _is_sqlite_engine,
    _register_sqlite_pragmas,
)
from app.models import CollectionRun


# --- 픽스처: PRAGMA 리스너가 부착된 임시 파일 SQLite 엔진 ----------------
@pytest.fixture
def file_engine(tmp_path):
    """임시 파일 SQLite 엔진 + 핫픽스 PRAGMA 리스너 부착 + 스키마 생성.

    실 운영 engine 과 동일하게 `_register_sqlite_pragmas` 로 connect 리스너를 단다.
    파일 DB 라야 WAL 이 실제 적용된다(인메모리는 WAL 미지원).
    """
    db_path = tmp_path / "concurrency.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    _register_sqlite_pragmas(eng)
    Base.metadata.create_all(eng)
    return eng


def _make_run_kwargs(source: str = "bid") -> dict:
    """create_run 호출용 윈도우 인자(현재 기준 상대값 — 실행 시점 비의존)."""
    now = datetime.now()
    return {
        "trigger": "manual",
        "window_bgn": now - timedelta(days=1),
        "window_end": now,
        "source": source,
    }


# --- 1. PRAGMA 적용 (WAL·busy_timeout) -----------------------------------
def test_sqlite_pragmas_applied_on_connection(file_engine):
    """파일 SQLite 연결에 journal_mode=WAL·busy_timeout=30000 이 적용된다."""
    with file_engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    # 파일 DB 라야 WAL 이 실제로 잡힌다(대소문자 무시).
    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 30000


def test_sqlite_synchronous_normal(file_engine):
    """synchronous=NORMAL(=1) 이 적용된다."""
    with file_engine.connect() as conn:
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
    # NORMAL == 1.
    assert int(synchronous) == 1


def test_real_module_engine_is_sqlite_and_has_listener():
    """모듈 전역 engine(.env 기본 sqlite)도 SQLite 가드를 통과한다.

    기본 환경(DATABASE_URL 미설정 → sqlite)에서는 모듈 engine 이 sqlite 이며,
    `_is_sqlite_engine` 가 True 다(리스너 부착 대상). PG 환경이면 자연히 skip.
    """
    if not _is_sqlite_engine(db_module.engine):
        pytest.skip("DATABASE_URL 이 SQLite 가 아님 — PG 가드 경로(리스너 미부착)")
    assert _is_sqlite_engine(db_module.engine) is True


# --- 2. PG 가드 (SQLite 전용) --------------------------------------------
def test_pg_guard_does_not_attach_listener():
    """비-SQLite dialect 면 `_is_sqlite_engine` 가 False → 리스너 미부착(PG 미적용).

    실제 PostgreSQL 드라이버 없이 dialect 이름만으로 가드를 단위 검증한다.
    `_register_sqlite_pragmas` 는 비-sqlite 엔진에 대해 `event.listen` 을 호출하지
    않으므로(가드 False), fake 엔진을 넘겨도 예외 없이 no-op 이어야 한다.
    (만약 가드가 깨져 `event.listen` 을 시도하면 fake 엔진에 대해 예외가 난다.)
    """

    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    fake = _FakeEngine()
    assert _is_sqlite_engine(fake) is False
    # 비-sqlite 엔진엔 리스너를 달지 않는다(예외 없이 no-op — 가드가 event.listen 을 막음).
    _register_sqlite_pragmas(fake)  # 예외가 나면 가드 실패


def test_sqlite_guard_attaches_listener(file_engine):
    """SQLite 엔진엔 connect 리스너가 실제로 부착된다."""
    import sqlalchemy.event as sa_event

    assert _is_sqlite_engine(file_engine) is True
    assert sa_event.contains(file_engine, "connect", _apply_sqlite_pragmas) is True


# --- 3. create_run 즉시 commit (독립 세션 가시성) ------------------------
def test_create_run_commits_immediately_visible_to_other_session(file_engine):
    """create_run 직후(finish_run 전), **독립 세션**에서 그 run 행이 즉시 조회된다.

    과거 flush 만 하던 시절엔 commit 전이라 다른 연결에서 안 보였고(트랜잭션 격리),
    잠금도 계속 쥐고 있었다. 즉시 commit 으로 바꿨으니 즉시 가시·잠금 해제여야 한다.
    """
    SessionA = sessionmaker(bind=file_engine, autoflush=False, future=True)
    SessionB = sessionmaker(bind=file_engine, autoflush=False, future=True)

    with SessionA() as sa:
        run = repository.create_run(sa, **_make_run_kwargs())
        run_id = run.id
        assert run_id is not None
        assert run.status == "running"  # 반환 타입·상태 불변

        # finish_run 을 아직 호출하지 않은 시점에 별도 세션 B 에서 조회되면 = 커밋됨.
        with SessionB() as sb:
            fetched = sb.get(CollectionRun, run_id)
            assert fetched is not None
            assert fetched.status == "running"


def test_create_run_releases_write_lock(file_engine):
    """create_run 후 세션 A 가 아직 살아 있어도 **별도 연결**이 즉시 INSERT 할 수 있다.

    create_run 이 commit 으로 쓰기 잠금을 놓았으므로, 같은 세션을 닫지 않은 상태에서도
    다른 연결의 쓰기가 (busy_timeout 안에) 곧바로 성공해야 한다 — 잠금 점유 0 증명.
    """
    SessionA = sessionmaker(bind=file_engine, autoflush=False, future=True)
    SessionB = sessionmaker(bind=file_engine, autoflush=False, future=True)

    with SessionA() as sa:
        repository.create_run(sa, **_make_run_kwargs(source="bid"))
        # 세션 A 를 닫지 않은 채로 다른 세션이 곧바로 INSERT.
        with SessionB() as sb:
            run_b = repository.create_run(sb, **_make_run_kwargs(source="pre_spec"))
            assert run_b.id is not None


# --- 4. 두 run(bid→pre_spec) 순차 생성·완료 정상 -------------------------
def test_two_runs_bid_then_pre_spec_sequential(file_engine):
    """입찰 run → 사전규격 run 을 순차로 생성·finish 해도 정상 기록된다.

    create_run commit 화 이후에도 finish_run(만료된 run 의 setattr→commit)·
    두 source 태깅이 정상인지 확인.
    """
    Session = sessionmaker(bind=file_engine, autoflush=False, future=True)

    with Session() as s:
        bid_run = repository.create_run(s, **_make_run_kwargs(source="bid"))
        repository.finish_run(
            s,
            bid_run,
            status="success",
            total_fetched=10,
            total_new=10,
            total_updated=0,
            retry_count=0,
        )
        bid_id = bid_run.id

    with Session() as s:
        ps_run = repository.create_run(s, **_make_run_kwargs(source="pre_spec"))
        repository.finish_run(
            s,
            ps_run,
            status="success",
            total_fetched=5,
            total_new=5,
            total_updated=0,
            retry_count=0,
        )
        ps_id = ps_run.id

    with Session() as s:
        rows = s.execute(select(CollectionRun).order_by(CollectionRun.id)).scalars().all()
        by_id = {r.id: r for r in rows}
        assert by_id[bid_id].source == "bid"
        assert by_id[bid_id].status == "success"
        assert by_id[bid_id].total_fetched == 10
        assert by_id[ps_id].source == "pre_spec"
        assert by_id[ps_id].status == "success"
        assert by_id[ps_id].total_fetched == 5


def test_finish_run_after_create_run_commit_updates_row(file_engine):
    """create_run(commit) 후 finish_run 이 같은 run 행을 갱신한다(중복 INSERT 아님)."""
    Session = sessionmaker(bind=file_engine, autoflush=False, future=True)

    with Session() as s:
        run = repository.create_run(s, **_make_run_kwargs())
        run_id = run.id
        repository.finish_run(
            s,
            run,
            status="partial",
            total_fetched=3,
            total_new=2,
            total_updated=1,
            retry_count=1,
            error_code="01",
            error_msg="일시 장애",
        )

    with Session() as s:
        rows = s.execute(select(CollectionRun)).scalars().all()
        assert len(rows) == 1  # 한 행만(생성→갱신, 중복 아님)
        assert rows[0].id == run_id
        assert rows[0].status == "partial"
        assert rows[0].retry_count == 1
        assert rows[0].error_code == "01"


# --- 5. 동시 쓰기 비실패 (busy_timeout 이 짧은 경쟁 흡수) ------------------
def test_concurrent_insert_waits_instead_of_locking(file_engine):
    """한 연결이 짧게 쓰기 트랜잭션을 쥔 동안, 다른 연결의 INSERT 가 대기 후 성공한다.

    busy_timeout(30s) 덕분에 즉시 `database is locked` 로 실패하지 않고 잠금 해제를
    기다렸다가 성공해야 한다. 결정성을 위해 점유 시간을 짧게(0.3s) 고정하고, 그 동안
    백그라운드 스레드가 INSERT 를 시도하게 한다. 점유 해제 후 백그라운드 INSERT 가
    예외 없이 끝나면 통과.
    """
    Session = sessionmaker(bind=file_engine, autoflush=False, future=True)

    hold_started = threading.Event()
    errors: list[Exception] = []

    def _hold_write_lock():
        """별 연결로 짧은 쓰기 트랜잭션(BEGIN IMMEDIATE)을 0.3초 점유 후 commit."""
        with file_engine.connect() as conn:
            # BEGIN IMMEDIATE = 즉시 RESERVED 잠금 획득(쓰기 잠금).
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            conn.exec_driver_sql(
                "INSERT INTO collection_run (trigger, status, source) "
                "VALUES ('manual', 'running', 'bid')"
            )
            hold_started.set()
            time.sleep(0.3)  # 짧은 점유(결정적)
            conn.exec_driver_sql("COMMIT")

    def _try_insert():
        """점유 중에 다른 세션으로 INSERT 시도 — busy_timeout 안에 성공해야 한다."""
        hold_started.wait(timeout=5)
        try:
            with Session() as s:
                repository.create_run(s, **_make_run_kwargs(source="pre_spec"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_hold = threading.Thread(target=_hold_write_lock)
    t_insert = threading.Thread(target=_try_insert)
    t_hold.start()
    t_insert.start()
    t_hold.join(timeout=10)
    t_insert.join(timeout=10)

    assert not errors, f"동시 INSERT 가 실패했다(database is locked 등): {errors!r}"

    # 두 INSERT 모두 반영됐는지 확인(점유 측 1 + create_run 1 = 2).
    with Session() as s:
        count = len(s.execute(select(CollectionRun)).scalars().all())
    assert count == 2
