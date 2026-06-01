"""DB 연결·세션·초기화 — Phase 3.1.

SQLAlchemy engine / SessionLocal / Base 를 제공하고, init_db()로
테이블 3종을 생성한 뒤 app_config 단일 행(id=1)을 기본값으로 시드한다.

- DB 경로는 .env 의 DATABASE_URL 에서 로드(없으면 sqlite:///procurement.db).
- SQLite 파일은 프로젝트 루트에 생성한다.
- 실행: `python -m app.db` (테이블 생성 + 시드 + 생성 결과 출력).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect
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


def init_db() -> None:
    """테이블 3종 생성 + app_config 기본행 시드.

    - 이미 있는 테이블/행은 건드리지 않는다(create_all 은 멱등, 시드는 존재 검사).
    """
    # models 를 임포트해야 Base.metadata 에 테이블이 등록된다(순환참조 회피용 지연 임포트).
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _seed_app_config()


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
