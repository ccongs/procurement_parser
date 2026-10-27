"""앱 import 전에 기본 DB를 시험 세션 전용 임시 경로로 격리한다."""

import os
from tempfile import TemporaryDirectory


_database_dir = None
if not os.environ.get("DATABASE_URL", "").strip():
    _database_dir = TemporaryDirectory(prefix="procurement-pytest-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_database_dir.name}/test.db"


def pytest_unconfigure(config):
    if _database_dir is not None:
        _database_dir.cleanup()
