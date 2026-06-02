"""Phase 4.5 — app/logging_config.py 단위 테스트.

설계 원칙:
- 전역 루트 로거를 건드리므로, 각 테스트는 force=True + tmp_path 로
  격리(setup) 하고 teardown 에서 우리가 붙인 핸들러를 반드시 제거한다.
- 기존 243개 테스트(caplog 등)가 깨지지 않도록
  teardown 후 루트 로거를 초기 상태로 복원한다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

# 테스트 대상 모듈
from app.logging_config import (
    _HANDLER_ATTR,
    _SecretRedactionFilter,
    setup_logging,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_logging(tmp_path: Path):
    """테스트 전/후 루트 로거를 격리한다.

    - 시작 전: 루트 로거의 기존 핸들러·레벨을 저장.
    - 각 테스트: tmp_path 를 log_dir 로 넘겨 우리 핸들러만 추가.
    - 종료 후: 우리가 붙인 핸들러를 모두 제거, 원 상태 복원.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    # 이전 실행에서 남은 우리 핸들러 초기화
    import app.logging_config as lc
    lc._SETUP_DONE = False

    yield tmp_path

    # teardown — 우리가 붙인 핸들러 제거
    for h in list(root.handlers):
        if getattr(h, _HANDLER_ATTR, False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass

    # 원본 핸들러·레벨 복원
    root.handlers = original_handlers
    root.setLevel(original_level)

    # 멱등 플래그도 초기화
    lc._SETUP_DONE = False


# ---------------------------------------------------------------------------
# 1. 멱등(idempotency)
# ---------------------------------------------------------------------------

def test_idempotency(isolated_logging: Path) -> None:
    """force 없이 두 번 호출해도 우리 핸들러가 중복 추가되지 않는다."""
    tmp = isolated_logging
    setup_logging(log_dir=tmp, force=True)
    root = logging.getLogger()
    count_after_first = sum(1 for h in root.handlers if getattr(h, _HANDLER_ATTR, False))

    setup_logging(log_dir=tmp)  # force=False — 멱등
    count_after_second = sum(1 for h in root.handlers if getattr(h, _HANDLER_ATTR, False))

    assert count_after_first == count_after_second, (
        f"핸들러 중복 추가: 첫 호출 {count_after_first}개 → 두 번째 {count_after_second}개"
    )


def test_force_replaces_handlers(isolated_logging: Path) -> None:
    """force=True 는 기존 우리 핸들러를 제거 후 새로 부착한다(수가 늘지 않음)."""
    tmp = isolated_logging
    setup_logging(log_dir=tmp, force=True)
    root = logging.getLogger()
    count1 = sum(1 for h in root.handlers if getattr(h, _HANDLER_ATTR, False))

    setup_logging(log_dir=tmp, force=True)  # 다시
    count2 = sum(1 for h in root.handlers if getattr(h, _HANDLER_ATTR, False))

    assert count1 == count2, f"force=True 후 핸들러 수가 달라졌다: {count1} → {count2}"


# ---------------------------------------------------------------------------
# 2. 마스킹(순수 필터 로직)
# ---------------------------------------------------------------------------

class _FakeRecord:
    """최소한의 LogRecord 모사체."""

    def __init__(self, msg: str, args: tuple = ()) -> None:
        self.msg = msg
        self.args = args
        self.levelno = logging.INFO
        self.name = "test"

    def getMessage(self) -> str:  # noqa: N802
        if self.args:
            return self.msg % self.args
        return self.msg


def _apply_filter(msg: str, args: tuple = ()) -> str:
    """필터를 적용하고 최종 record.msg 를 반환한다."""
    record = _FakeRecord(msg, args)
    f = _SecretRedactionFilter()
    f.filter(record)  # type: ignore[arg-type]
    return record.msg  # type: ignore[return-value]


def test_mask_servicekey_in_url_middle() -> None:
    """URL 중간에 있는 serviceKey 값이 마스킹된다."""
    url = "GET https://apis.data.go.kr/op?serviceKey=ABCDEF123&type=json"
    result = _apply_filter(url)
    assert "serviceKey=***" in result
    assert "ABCDEF123" not in result


def test_mask_servicekey_at_end_of_url() -> None:
    """URL 끝에 있는 serviceKey 값도 마스킹된다."""
    url = "https://apis.data.go.kr/op?type=json&serviceKey=XYZ999"
    result = _apply_filter(url)
    assert "serviceKey=***" in result
    assert "XYZ999" not in result


def test_mask_servicekey_from_args() -> None:
    """logger.info('%s', url) 처럼 args 로 들어온 경우에도 마스킹된다."""
    url = "http://example.com?serviceKey=SECRETKEY42&foo=bar"
    result = _apply_filter("%s", (url,))
    assert "serviceKey=***" in result
    assert "SECRETKEY42" not in result


def test_no_mask_without_servicekey() -> None:
    """serviceKey 가 없으면 메시지가 그대로다."""
    msg = "정상 로그: 수집 시작"
    result = _apply_filter(msg)
    assert result == msg


def test_args_cleared_after_filter() -> None:
    """필터 적용 후 record.args 가 비워진다(formatter 재치환 방지)."""
    url = "http://x.com?serviceKey=SHOULD_BE_MASKED"
    record = _FakeRecord("%s", (url,))
    f = _SecretRedactionFilter()
    f.filter(record)  # type: ignore[arg-type]
    assert record.args == ()


# ---------------------------------------------------------------------------
# 3. httpx / httpcore 레벨
# ---------------------------------------------------------------------------

def test_httpx_level_is_warning(isolated_logging: Path) -> None:
    """setup_logging 후 httpx 로거가 WARNING 레벨로 설정된다."""
    setup_logging(log_dir=isolated_logging, force=True)
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpcore_level_is_warning(isolated_logging: Path) -> None:
    """setup_logging 후 httpcore 로거가 WARNING 레벨로 설정된다."""
    setup_logging(log_dir=isolated_logging, force=True)
    assert logging.getLogger("httpcore").level == logging.WARNING


# ---------------------------------------------------------------------------
# 4. 파일 출력 + 마스킹 확인
# ---------------------------------------------------------------------------

def test_file_created_and_no_secret_in_file(isolated_logging: Path) -> None:
    """로그 파일이 생성되고 serviceKey 값이 파일에 남지 않는다."""
    tmp = isolated_logging
    setup_logging(log_dir=tmp, force=True)

    test_logger = logging.getLogger("app.test_secret")
    test_logger.info("hello serviceKey=SECRET123 world")

    # 핸들러가 파일에 flush 하도록 명시적으로 닫지 않고 flush 한다.
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, _HANDLER_ATTR, False):
            h.flush()

    log_file = tmp / "app.log"
    assert log_file.exists(), "app.log 파일이 생성되지 않았습니다."

    content = log_file.read_text(encoding="utf-8")
    assert "hello" in content, "정상 메시지가 파일에 없습니다."
    assert "SECRET123" not in content, "serviceKey 값이 파일에 노출되었습니다!"
    assert "serviceKey=***" in content, "마스킹 결과(***) 가 파일에 없습니다."


def test_log_dir_created_if_missing(tmp_path: Path) -> None:
    """존재하지 않는 log_dir 도 자동 생성된다."""
    import app.logging_config as lc
    lc._SETUP_DONE = False

    new_dir = tmp_path / "subdir" / "logs"
    assert not new_dir.exists()

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    try:
        setup_logging(log_dir=new_dir, force=True)
        assert new_dir.exists(), "log_dir 가 자동 생성되지 않았습니다."
    finally:
        for h in list(root.handlers):
            if getattr(h, _HANDLER_ATTR, False):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
        root.handlers = original_handlers
        root.setLevel(original_level)
        lc._SETUP_DONE = False


# ---------------------------------------------------------------------------
# 5. LOG_LEVEL 환경변수 적용
# ---------------------------------------------------------------------------

def test_env_log_level_debug(isolated_logging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_LEVEL=DEBUG 환경변수가 반영된다."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging(log_dir=isolated_logging, force=True)
    assert logging.getLogger().level == logging.DEBUG


def test_level_arg_overrides_env(isolated_logging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """level 인자가 환경변수보다 우선한다."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging(log_dir=isolated_logging, level=logging.WARNING, force=True)
    assert logging.getLogger().level == logging.WARNING
