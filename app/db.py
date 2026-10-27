"""DB 연결·세션·초기화 — Phase 3.1.

SQLAlchemy engine / SessionLocal / Base 를 제공하고, init_db()로
모델에 정의된 테이블 전체를 생성한 뒤 app_config 단일 행(id=1)을 기본값으로
시드하고 목록 규칙을 1회 시드한다(pre_spec 시드는 없음).

- DB 경로는 .env 의 DATABASE_URL 에서 로드(없으면 sqlite:///procurement.db).
- SQLite 파일은 프로젝트 루트에 생성한다.
- 실행: `python -m app.db` (테이블 생성 + 시드 + 생성 결과 출력).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

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
        "PRAGMA busy_timeout=30000",    # 잠금 대기 30초 (짧은 쓰기 경쟁 흡수)
        "PRAGMA journal_mode=WAL",      # 읽기/쓰기 동시성↑ (웹 GET 이 수집 쓰기를 막지 않음)
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


class IndexDefinitionMismatchError(RuntimeError):
    """동명 인덱스의 정의가 모델과 달라 초기화를 중단해야 하는 스키마 오류."""

    def __init__(self, message: str) -> None:
        super().__init__(
            f"{message}. 해당 인덱스를 DROP 한 뒤 재기동하면 모델 정의로 다시 생성됩니다"
        )


def init_db() -> None:
    """모델 테이블 생성 + 자동 마이그레이션 + 설정·표시 규칙 기본값 시드.

    - models 를 임포트하면 PreSpec 를 포함한 모든 모델이 Base.metadata 에 등록되어
      테이블 생성 단계가 pre_spec 테이블까지 자동 생성한다(별도 호출 불필요).
    - 순서: 모델 테이블 생성 → 컬럼 ALTER·기존 분류값 보정 → 인덱스 확인·생성 → 원문 이관
      → app_config 시드 → 표시 규칙 시드(완료 기록 기준 1회) → 모델 인덱스 최종 검증.
      마이그레이션이 시드보다 **먼저** 와야 신규 컬럼을 시드 행이 채울 수 있다.
    - 동시 실행 경합은 단계 내 재확인·원문 배치 1회 재처리로 우선 흡수한다.
      남은 DB 오류는 마지막 안전장치로 전체 단계를 처음부터 최대 3회 실행한다.
    - 잠금 대기 오류(database is locked)는 시도마다 busy_timeout 만큼 대기(최대 3회).
    - 하위 단계(_run_once·_seed_app_config)의 자체 재시도와 겹칠 수 있다.
    """
    # models 를 임포트해야 Base.metadata 에 테이블이 등록된다(순환참조 회피용 지연 임포트).
    from app import models  # noqa: F401

    stages = (
        ("모델 테이블 생성", _create_model_tables),
        ("컬럼 추가", _migrate_add_columns),
        ("기존 분류값 보정", _migrate_match_basis_backfill),
        ("모델 인덱스 확인", _migrate_bid_notice_indexes),
        ("원문 이관", _migrate_bid_notice_raw),
        ("설정 기본값 시드", _seed_app_config),
        ("표시 규칙 시드", _seed_list_rules),
        ("모델 인덱스 최종 검증", _validate_model_indexes),
    )
    for attempt in range(3):
        try:
            for stage_name, stage in stages:
                stage()
            return
        except (OperationalError, IntegrityError, ProgrammingError) as exc:
            if attempt == 2:
                raise
            delay = (0.2, 0.5)[attempt]
            logger.warning(
                "DB 초기화 재시도 %d/3: 단계=%s, 대기=%.1f초: %s: %s",
                attempt + 2, stage_name, delay, type(exc).__name__,
                " ".join(str(exc.orig).splitlines())[:200],
            )
            time.sleep(delay)


def _create_if_missing(create: Callable[[], None], exists: Callable[[], bool]) -> None:
    """존재 확인 후 DDL 실행. 실패한 트랜잭션 종료 뒤 경쟁자의 생성을 재확인."""
    if exists():
        return
    try:
        create()
    except (OperationalError, IntegrityError, ProgrammingError):
        if not exists():
            raise


def _create_model_tables() -> None:
    """테이블별 생성 경합은 해당 테이블의 존재를 재확인해 처리한다."""
    for table in Base.metadata.sorted_tables:
        _create_if_missing(
            lambda t=table: Base.metadata.create_all(bind=engine, tables=[t]),
            lambda t=table: inspect(engine).has_table(t.name, schema=t.schema),
        )


def _execute_ddl(statement: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement))


def _migrate_add_columns() -> None:
    """기존(구 스키마) DB 에 신규 컬럼이 없으면 ALTER TABLE ADD COLUMN.

    멱등·inspector 가드: 신규 DB 는 create_all 이 이미 컬럼을 만들었으므로 inspector 가
    present 로 보고 ALTER 를 스킵(no-op)한다. 기존 DB 만 ADD COLUMN 하며
    `collection_run.source` 기존 행은 DEFAULT 'bid' 로 채워진다. 기존 표시 공고의 NULL 분류값은
    별도 보정 함수에서 완료 기록을 기준으로 1회 보정한다. init_db() 반복 호출은 안전하다.

    BOOLEAN DEFAULT TRUE·TIMESTAMP 는 SQLite·PostgreSQL 공통 표기다.
    이 DDL은 해당 컬럼이 없어 새로 ALTER가 필요한 옛 스키마 DB에만 적용된다.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    def colset(t: str) -> set[str]:
        return {c["name"] for c in insp.get_columns(t)}

    stmts: list[tuple[str, str, str]] = []
    if "bid_notice" in tables:
        bn = colset("bid_notice")
        additions = {
            "list_visible": "BOOLEAN NOT NULL DEFAULT TRUE",
            "match_basis": "VARCHAR(10)",
            "match_detail": "VARCHAR(200)",
            "pub_prcrmnt_clsfc_nm": "VARCHAR(100)",
            "bef_bid_bbanc_no": "VARCHAR(40)",
        }
        for name, definition in additions.items():
            if name not in bn:
                stmts.append(("bid_notice", name,
                              f"ALTER TABLE bid_notice ADD COLUMN {name} {definition}"))
    if "collection_run" in tables and "source" not in colset("collection_run"):
        stmts.append(("collection_run", "source",
            "ALTER TABLE collection_run ADD COLUMN source VARCHAR(10) DEFAULT 'bid'"
        ))
    if "bid_license_limit" in tables and "superseded_at" not in colset("bid_license_limit"):
        stmts.append(("bid_license_limit", "superseded_at",
            "ALTER TABLE bid_license_limit ADD COLUMN superseded_at TIMESTAMP"
        ))
    if "app_config" in tables:
        ac = colset("app_config")
        if "pre_spec_enabled" not in ac:
            stmts.append(("app_config", "pre_spec_enabled",
                "ALTER TABLE app_config ADD COLUMN pre_spec_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            ))
        if "pre_spec_last_success_dt" not in ac:
            stmts.append(("app_config", "pre_spec_last_success_dt",
                "ALTER TABLE app_config ADD COLUMN pre_spec_last_success_dt TIMESTAMP"
            ))
        # Phase 4.9-R2-D: 사전규격 배정예산액 기본 범위(신규 컬럼 2개).
        if "pre_spec_amt_bgn" not in ac:
            stmts.append(("app_config", "pre_spec_amt_bgn",
                "ALTER TABLE app_config ADD COLUMN pre_spec_amt_bgn VARCHAR(25)"
            ))
        if "pre_spec_amt_end" not in ac:
            stmts.append(("app_config", "pre_spec_amt_end",
                "ALTER TABLE app_config ADD COLUMN pre_spec_amt_end VARCHAR(25)"
            ))

    for table_name, column_name, statement in stmts:
        _create_if_missing(
            lambda s=statement: _execute_ddl(s),
            lambda t=table_name, c=column_name: c in {
                col["name"] for col in inspect(engine).get_columns(t)
            },
        )


def _run_once(name: str, work: Callable[[Connection], None]) -> None:
    """작업·완료 기록을 함께 커밋하고, DB 오류 시 완료 여부 확인 후 1회 재시도."""
    from app.models import SchemaMigration

    migration = SchemaMigration.__table__
    completed = select(migration.c.name).where(migration.c.name == name)
    for attempt in range(2):
        try:
            with engine.begin() as conn:
                if conn.execute(completed).first():
                    return
                work(conn)
                # applied_at은 naive UTC(_utcnow) — 다른 열 일부는 지역 시각을 쓴다.
                conn.execute(migration.insert().values(name=name))
            return
        except (IntegrityError, OperationalError):
            # 실패한 트랜잭션은 이미 롤백됨. 새 트랜잭션에서 경쟁자의 완료를 확인한다.
            with engine.begin() as conn:
                if conn.execute(completed).first():
                    return
            if attempt == 1:
                raise


def _migrate_match_basis_backfill() -> None:
    """완료 기록이 없으면 기존 표시 공고의 NULL 분류값만 1회 보정·완료 기록."""
    # ALTER 와 분리된 DML 트랜잭션: 보정·완료 기록이 함께 커밋되거나 함께 롤백된다.
    def work(conn: Connection) -> None:
        conn.execute(text(
            "UPDATE bid_notice SET match_basis='업종' "
            "WHERE list_visible = true AND match_basis IS NULL"
        ))

    _run_once("phase13_match_basis_backfill", work)


def _migrate_bid_notice_indexes() -> None:
    """모든 모델 테이블의 누락 인덱스를 복구한다(이름 고정 3종 포함).

    정의 불일치 시 IndexDefinitionMismatchError 로 중단한다.
    테이블별 inspector 조회 1회를 재사용하며 DDL 실패 뒤에만 재조회한다.
    """
    for table in Base.metadata.sorted_tables:
        if not table.indexes:
            continue
        indexes = _index_definitions(table)
        for index in sorted(table.indexes, key=lambda index: index.name):
            _create_if_missing(
                lambda i=index, actual=indexes: _create_model_index(i, actual),
                lambda i=index, actual=indexes: _model_index_exists(i, actual),
            )


def _index_definitions(table) -> dict[str, dict]:  # noqa: ANN001
    definitions = {}
    for idx in inspect(engine).get_indexes(table.name, schema=table.schema):
        actual = {
            "column_names": list(idx["column_names"]), "unique": bool(idx["unique"]),
        }
        conditions = {
            key: value for key, value in idx.get("dialect_options", {}).items()
            if key.endswith("_where") and value is not None
        }
        if conditions:
            actual["dialect_options"] = conditions
        if idx.get("expressions"):
            actual["expressions"] = idx["expressions"]
        definitions[idx["name"].lower()] = actual
    return definitions


def _model_index_exists(index, indexes: dict[str, dict]) -> bool:  # noqa: ANN001
    actual = indexes.get(index.name.lower())
    if actual is None:
        return False
    expected = {"column_names": [c.name for c in index.columns], "unique": bool(index.unique)}
    if actual != expected:
        raise IndexDefinitionMismatchError(
            f"모델 인덱스 정의 불일치: {index.table.fullname}: {index.name}: "
            f"기대 정의={expected!r}, 실제 정의={actual!r}"
        )
    return True


def _create_model_index(index, indexes: dict[str, dict]) -> None:  # noqa: ANN001
    try:
        index.create(bind=engine)
    except (OperationalError, IntegrityError, ProgrammingError) as exc:
        # DDL 실패 뒤에만 새 조회로 경쟁자가 만든 인덱스의 정의를 재확인한다.
        indexes.clear()
        indexes.update(_index_definitions(index.table))
        # SQLite inspector 는 표현식 인덱스를 생략할 수 있어 중복 오류도 확인한다.
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        duplicate = sqlstate in {"42P07", "42710"} or "already exists" in str(exc.orig).lower()
        if duplicate and index.name.lower() not in indexes:
            raise IndexDefinitionMismatchError(
                f"모델 인덱스 정의 불일치: {index.table.fullname}: {index.name}: "
                "같은 이름의 인덱스가 있으나 정의를 확인할 수 없음"
            ) from exc
        raise


def _validate_model_indexes() -> None:
    """반환 전에 모델 정의 인덱스 전체를 확인하며 누락은 전체 재시도 대상으로 올린다."""
    for table in Base.metadata.sorted_tables:
        if not table.indexes:
            continue
        indexes = _index_definitions(table)
        missing = {index.name for index in table.indexes if not _model_index_exists(index, indexes)}
        if missing:
            # 정의 불일치와 달리 누락은 생성 경합으로 복구 가능하므로 전체 재시도 대상으로 올린다.
            raise OperationalError(None, None, Exception(
                f"모델 인덱스 누락: {table.name}: {', '.join(sorted(missing))}"
            ))


def _migrate_bid_notice_raw() -> None:
    """원문을 500행씩 이관·추출 후 비움. 배치별 트랜잭션으로 재실행 안전."""
    from app.models import BidNotice, BidNoticeRaw
    from app.transform import clean_str

    notice = BidNotice.__table__
    raw = BidNoticeRaw.__table__
    def migrate_batch() -> bool:
        with engine.begin() as conn:
            rows = conn.execute(
                select(
                    notice.c.bid_ntce_no, notice.c.raw_json, notice.c.updated_at,
                    notice.c.bef_bid_bbanc_no, notice.c.pub_prcrmnt_clsfc_nm,
                )
                .where(notice.c.raw_json.is_not(None))
                .order_by(notice.c.bid_ntce_no)
                .limit(500)
            ).mappings().all()
            if not rows:
                return False
            existing = set(conn.execute(
                select(raw.c.bid_ntce_no).where(
                    raw.c.bid_ntce_no.in_([row["bid_ntce_no"] for row in rows])
                )
            ).scalars())
            inserts = [
                {key: row[key] for key in ("bid_ntce_no", "raw_json", "updated_at")}
                for row in rows if row["bid_ntce_no"] not in existing
            ]
            if inserts:
                conn.execute(raw.insert(), inserts)
            for row in rows:
                values = {"raw_json": None}
                try:
                    item = json.loads(row["raw_json"])
                except (TypeError, ValueError):
                    item = None  # 깨진 원문도 저장하고 추출만 건너뛴다.
                if isinstance(item, dict):
                    for column, field in (
                        ("bef_bid_bbanc_no", "befBidBbancNo"),
                        ("pub_prcrmnt_clsfc_nm", "pubPrcrmntClsfcNm"),
                    ):
                        if clean_str(row[column]) is None:
                            values[column] = clean_str(item.get(field))
                conn.execute(
                    notice.update().where(notice.c.bid_ntce_no == row["bid_ntce_no"])
                    .values(**values)
                )
        return True

    while True:
        for attempt in range(2):
            try:
                if not migrate_batch():
                    return
                break
            except IntegrityError:
                # 해당 배치만 롤백한 뒤 새 트랜잭션에서 경쟁자의 원문을 다시 조회한다.
                if attempt == 1:
                    raise


def _seed_app_config() -> None:
    """app_config 에 행이 없으면 기본값으로 id=1 단일 행을 생성한다."""
    from app.models import AppConfig

    for attempt in range(2):
        try:
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
                        presmpt_prce_end="100000000",
                        last_success_dt=None,
                        updated_at=datetime.now(),
                        pre_spec_enabled=True,  # 사전규격 잡 기본 on (Phase 5.4)
                        pre_spec_last_success_dt=None,
                    )
                )
                session.commit()
            return
        except IntegrityError:
            # 실패한 세션은 이미 롤백·종료됨. 새 세션에서 경쟁자의 생성을 확인한다.
            with SessionLocal() as session:
                if session.get(AppConfig, 1) is not None:
                    return
            if attempt == 1:
                raise


def _seed_list_rules() -> None:
    """완료 기록이 없을 때 누락된 기본 표시 규칙만 추가한다(재분류 없음)."""
    from app.classifier import DEFAULT_LIST_RULES
    from app.models import ListRule

    rule = ListRule.__table__
    # 규칙 INSERT·완료 기록은 함께 커밋되거나 함께 롤백된다.
    def work(conn: Connection) -> None:
        existing = set(conn.execute(select(rule.c.rule_type, rule.c.value)).all())
        for rule_type, value in DEFAULT_LIST_RULES:
            if (rule_type, value) not in existing:
                conn.execute(rule.insert().values(rule_type=rule_type, value=value, enabled=True))

    _run_once("phase13_list_rule_seed", work)


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
