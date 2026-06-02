"""DB 연결·세션·초기화 — Phase 3.1.

SQLAlchemy engine / SessionLocal / Base 를 제공하고, init_db()로
테이블 4종(bid_notice·collection_run·app_config·pre_spec)을 생성한 뒤
app_config 단일 행(id=1)을 기본값으로 시드한다(pre_spec 시드는 없음).

- DB 경로는 .env 의 DATABASE_URL 에서 로드(없으면 sqlite:///procurement.db).
- SQLite 파일은 프로젝트 루트에 생성한다.
- 실행: `python -m app.db` (테이블 생성 + 시드 + 생성 결과 출력).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# .env 로드 (프로젝트 루트의 .env)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _default_sqlite_url() -> str:
    """기본 SQLite URL — 프로젝트 루트의 procurement.db (절대경로)."""
    db_path = _PROJECT_ROOT / "procurement.db"
    return f"sqlite:///{db_path}"


DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or _default_sqlite_url()

# SQLite 는 동일 스레드 제약이 있어 connect_args 로 완화(스케줄러/FastAPI 대비).
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


# --- SQLite 동시성 PRAGMA (운영 핫픽스) ----------------------------------
# 입찰/사전규격 두 수집 잡이 동시 tick 으로 뜨면, 한쪽이 쓰기 트랜잭션을 연 채로 긴
# 네트워크 페이징을 진행하는 동안 다른 잡의 INSERT 가 잠금을 못 얻어 `database is locked`
# 가 났다. WAL(읽기가 쓰기를 막지 않음)·busy_timeout(즉시 실패 대신 대기)·synchronous=NORMAL
# 로 잠금 경쟁을 흡수한다. SQLite 연결마다(풀에서 새 연결이 열릴 때) 적용한다.
#
# ⚠️ SQLite 전용. PostgreSQL 이전 시에는 적용하지 않는다(아래 가드).
# ⚠️ `:memory:` DB 는 WAL 을 지원하지 않아 journal_mode 가 `memory` 로 떨어질 수 있으나
#    에러 없이 무시된다(반환값을 단정하지 않는다).
def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """SQLite 연결마다 동시성 PRAGMA 를 설정한다(connect 이벤트 리스너).

    드라이버 커서로 직접 실행한다. 여러 PRAGMA 를 순차 실행하며, 한 줄이 실패해도
    (예: `:memory:` 의 WAL 미지원) 다른 PRAGMA 적용을 막지 않도록 개별 try 로 감싼다.
    """
    pragmas = (
        "PRAGMA journal_mode=WAL",      # 읽기/쓰기 동시성↑ (웹 GET 이 수집 쓰기를 막지 않음)
        "PRAGMA busy_timeout=30000",    # 잠금 대기 30초 (짧은 쓰기 경쟁 흡수)
        "PRAGMA synchronous=NORMAL",    # WAL 과 함께 안전·고속
    )
    for stmt in pragmas:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(stmt)
        finally:
            cursor.close()


def _is_sqlite_engine(eng) -> bool:  # noqa: ANN001
    """엔진이 SQLite dialect 인지 — PRAGMA 리스너 부착 가드(PG 미적용)."""
    return eng.dialect.name == "sqlite"


def _register_sqlite_pragmas(eng) -> None:  # noqa: ANN001
    """SQLite 엔진이면 connect 이벤트에 PRAGMA 리스너를 1회 등록한다(PG 엔진엔 미부착).

    테스트에서 임의로 만든 SQLite 엔진에도 동일 PRAGMA 를 적용할 수 있도록 헬퍼로 분리.
    """
    if _is_sqlite_engine(eng):
        event.listen(eng, "connect", _apply_sqlite_pragmas)


# 모듈 전역 engine(실 운영용)에 PRAGMA 리스너 부착(SQLite 한정).
_register_sqlite_pragmas(engine)


def init_db() -> None:
    """테이블 4종(bid_notice·collection_run·app_config·pre_spec) 생성 + 자동 마이그레이션 + app_config 기본행 시드.

    - models 를 임포트하면 PreSpec 를 포함한 모든 모델이 Base.metadata 에 등록되어
      create_all 이 pre_spec 테이블까지 자동 생성한다(별도 호출 불필요).
    - 이미 있는 테이블/행은 건드리지 않는다(create_all 은 멱등, 시드는 존재 검사).
    - 순서: create_all → _migrate_add_columns(기존 DB 누락 컬럼 ALTER) → _seed_app_config.
      마이그레이션이 시드보다 **먼저** 와야 신규 컬럼을 시드 행이 채울 수 있다.
    """
    # models 를 임포트해야 Base.metadata 에 테이블이 등록된다(순환참조 회피용 지연 임포트).
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()
    _seed_app_config()


def _migrate_add_columns() -> None:
    """기존(구 스키마) DB 에 Phase 5.4 신규 컬럼이 없으면 ALTER TABLE ADD COLUMN.

    멱등·inspector 가드: 신규 DB 는 create_all 이 이미 컬럼을 만들었으므로 inspector 가
    present 로 보고 ALTER 를 스킵(no-op)한다. 기존 DB 만 ADD COLUMN 하며 **기존 행은 무손상**
    (`collection_run.source` 기존 행은 DEFAULT 'bid' 로 채워진다). init_db() 를 여러 번 호출해도
    안전하다.

    ⚠️ PostgreSQL 이전 시: boolean 기본값 리터럴 `1`(SQLite 표기)을 `true` 로 바꿔야 한다
    (SQLite 우선·표준 ALTER 사용). 컬럼 추가만 하므로 그 외 표준 SQL 로 이식 가능하다.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    def colset(t: str) -> set[str]:
        return {c["name"] for c in insp.get_columns(t)}

    stmts: list[str] = []
    if "collection_run" in tables and "source" not in colset("collection_run"):
        stmts.append(
            "ALTER TABLE collection_run ADD COLUMN source VARCHAR(10) DEFAULT 'bid'"
        )
    if "app_config" in tables:
        ac = colset("app_config")
        if "pre_spec_enabled" not in ac:
            # PG 이전 시 DEFAULT 1 → DEFAULT true 로 조정.
            stmts.append(
                "ALTER TABLE app_config ADD COLUMN pre_spec_enabled BOOLEAN NOT NULL DEFAULT 1"
            )
        if "pre_spec_last_success_dt" not in ac:
            stmts.append(
                "ALTER TABLE app_config ADD COLUMN pre_spec_last_success_dt DATETIME"
            )

    if stmts:
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))


def _seed_app_config() -> None:
    """app_config 에 행이 없으면 기본값으로 id=1 단일 행을 생성한다."""
    from app.models import AppConfig

    with SessionLocal() as session:
        existing = session.get(AppConfig, 1)
        if existing is not None:
            return
        session.add(
            AppConfig(
                id=1,
                enabled=True,
                auto_halted=False,
                halt_code=None,
                halt_reason=None,
                interval_minutes=60,
                window_overlap_minutes=90,
                backfill_days=30,
                num_of_rows=20,
                max_retries=2,
                inqry_div="1",
                intrntnl_div_cd="1",
                indstryty_cds="1426,1468,1469,1470",
                prtcpt_lmt_rgn_cd="00",  # 신규 설치 기본 = 전국(지역제한 없는 공고만)
                presmpt_prce_bgn=None,
                presmpt_prce_end=None,
                last_success_dt=None,
                updated_at=datetime.now(),
                pre_spec_enabled=True,  # 사전규격 잡 기본 on (Phase 5.4)
                pre_spec_last_success_dt=None,
            )
        )
        session.commit()


def _print_summary() -> None:
    """생성 결과를 출력(완료 기준 검증용)."""
    from app.models import AppConfig

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"DB URL: {DATABASE_URL}")
    print(f"생성된 테이블({len(tables)}종): {sorted(tables)}")

    cols = [c["name"] for c in inspector.get_columns("bid_notice")]
    print(f"\nbid_notice 컬럼({len(cols)}개):")
    print("  " + ", ".join(cols))

    with SessionLocal() as session:
        cfg = session.get(AppConfig, 1)
        if cfg is None:
            print("\napp_config: (시드 없음 — 확인 필요)")
        else:
            print(
                "\napp_config 기본행: "
                f"id={cfg.id}, enabled={cfg.enabled}, auto_halted={cfg.auto_halted}, "
                f"interval_minutes={cfg.interval_minutes}, window_overlap_minutes={cfg.window_overlap_minutes}, "
                f"backfill_days={cfg.backfill_days}, num_of_rows={cfg.num_of_rows}, "
                f"max_retries={cfg.max_retries}, inqry_div={cfg.inqry_div!r}, "
                f"intrntnl_div_cd={cfg.intrntnl_div_cd!r}, indstryty_cds={cfg.indstryty_cds!r}"
            )


if __name__ == "__main__":
    # `python -m app.db` 로 실행하면 이 파일은 __main__ 으로 로드된다.
    # models.py 의 `from app.db import Base` 는 app.db 를 별도 모듈로 다시 임포트하므로,
    # Base/engine 이 두 벌이 되는 것을 막기 위해 패키지 경로로 다시 임포트해 호출한다.
    from app.db import init_db as _init_db, _print_summary as _summary

    _init_db()
    print("init_db() 완료.\n")
    _summary()
