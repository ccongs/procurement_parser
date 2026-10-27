"""기본 엔진이 저장소의 실 DB를 가리키지 않는지 확인한다."""

from pathlib import Path

from sqlalchemy.engine import make_url

from app import db


def test_default_engine_does_not_use_repository_database():
    repository_db = Path(__file__).resolve().parents[1] / "procurement.db"
    for url in (make_url(db.DATABASE_URL), db.engine.url):
        if url.get_backend_name() == "sqlite" and url.database:
            assert Path(url.database).resolve() != repository_db.resolve()
