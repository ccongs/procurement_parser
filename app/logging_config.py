"""중앙 로깅 설정 — Phase 4.5.

setup_logging() 한 번만 호출하면 루트 로거에
  · 콘솔 StreamHandler
  · RotatingFileHandler (logs/app.log, 5 MB × 5 개)
두 핸들러를 붙인다.

보안: _SecretRedactionFilter 가 serviceKey 값을 *** 로 마스킹한다.
      httpx / httpcore 로거는 WARNING 으로 올려 요청 URL 자체를 남기지 않는다.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

# 멱등 플래그 — 같은 프로세스 안에서 중복 핸들러 추가 방지.
_SETUP_DONE: bool = False

# 우리가 붙인 핸들러를 식별하기 위한 마커 속성명.
_HANDLER_ATTR = "_procparser_handler"

# serviceKey 마스킹 정규식.
_SERVICEKEY_RE = re.compile(r"serviceKey=[^&\s\"]+")


class _SecretRedactionFilter(logging.Filter):
    """로그 레코드에서 serviceKey 값을 마스킹하는 필터.

    logger.info("url=%s", url) 처럼 args 로 들어온 경우에도
    record.getMessage() 로 먼저 합친 뒤 치환하고 record.args 를 비운다.
    이렇게 해야 Formatter 가 다시 %-치환할 때 원본이 노출되지 않는다.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # args 를 포함해 최종 메시지로 평탄화.
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)

        masked = _SERVICEKEY_RE.sub("serviceKey=***", msg)

        # 실제 서비스 키 값도 추가 마스킹(지연 import — 순환 방지).
        try:
            from app import api_client  # noqa: PLC0415
            key = getattr(api_client, "SERVICE_KEY", None)
            if key and len(key) > 4:
                masked = masked.replace(key, "***")
        except Exception:  # noqa: BLE001
            pass

        record.msg = masked
        record.args = ()
        return True  # 레코드는 항상 통과


def _make_handler(handler: logging.Handler, level: int) -> logging.Handler:
    """핸들러에 포맷터·레벨·마스킹 필터를 부착하고 마커를 설정한다."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(fmt)
    handler.setLevel(level)
    handler.addFilter(_SecretRedactionFilter())
    setattr(handler, _HANDLER_ATTR, True)
    return handler


def setup_logging(
    log_dir: str | Path | None = None,
    level: int | str | None = None,
    *,
    force: bool = False,
) -> None:
    """루트 로거에 콘솔 + 파일 핸들러를 붙인다 (멱등).

    Parameters
    ----------
    log_dir:
        파일 핸들러가 쓸 디렉터리. 없으면 프로젝트 루트의 ``logs/``.
        (테스트에서 tmp 경로를 주입할 때 사용)
    level:
        로그 레벨 정수·문자열. 우선순위: ``level`` 인자 > 환경변수 ``LOG_LEVEL`` > ``INFO``.
    force:
        True 이면 기존에 우리가 붙인 핸들러를 제거 후 재구성한다(테스트 격리용).
    """
    global _SETUP_DONE  # noqa: PLW0603

    root = logging.getLogger()

    if force:
        # 우리가 붙인 핸들러만 제거한다.
        for h in list(root.handlers):
            if getattr(h, _HANDLER_ATTR, False):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
        _SETUP_DONE = False

    # 이미 구성됐으면 중복 추가하지 않는다.
    if _SETUP_DONE:
        return
    if any(getattr(h, _HANDLER_ATTR, False) for h in root.handlers):
        _SETUP_DONE = True
        return

    # --- 레벨 결정 ---
    if level is None:
        env_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        resolved_level: int = getattr(logging, env_level, logging.INFO)
    elif isinstance(level, str):
        resolved_level = getattr(logging, level.upper(), logging.INFO)
    else:
        resolved_level = level

    root.setLevel(resolved_level)

    # --- 콘솔 핸들러 ---
    console_handler = _make_handler(logging.StreamHandler(), resolved_level)
    root.addHandler(console_handler)

    # --- 파일 핸들러 ---
    if log_dir is None:
        # 이 파일 위치를 기준으로 프로젝트 루트 산출.
        project_root = Path(__file__).resolve().parent.parent
        log_dir = project_root / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = _make_handler(
        logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
        resolved_level,
    )
    root.addHandler(file_handler)

    # --- httpx / httpcore 요청 URL 로그 차단 ---
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _SETUP_DONE = True
