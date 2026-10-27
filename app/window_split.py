"""수집 창을 API 조회 범위 안에서 순서대로 실행하는 공통 헬퍼."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 28


def split_window(
    window_bgn: datetime, window_end: datetime, max_days: int = MAX_WINDOW_DAYS,
) -> list[tuple[datetime, datetime]]:
    """양끝과 경계 연속성을 보존하며 최대 max_days일씩 나눈다."""
    if max_days <= 0:
        raise ValueError("max_days는 양수여야 합니다.")
    if window_bgn >= window_end:
        return [(window_bgn, window_end)]
    chunks = []
    step = timedelta(days=max_days)
    while window_bgn < window_end:
        end = window_bgn + min(step, window_end - window_bgn)
        chunks.append((window_bgn, end))
        window_bgn = end
    return chunks


def collect_in_chunks(
    collect_fn, window_bgn: datetime, window_end: datetime, *, trigger: str,
    max_days: int = MAX_WINDOW_DAYS,
):
    """각 조각을 수집하고 성공 이외의 반환값을 만나면 즉시 멈춘다."""
    chunks = split_window(window_bgn, window_end, max_days)
    for i, (bgn, end) in enumerate(chunks, start=1):
        if len(chunks) > 1:
            logger.info("%d/%d 조각 window=%s~%s", i, len(chunks),
                        bgn.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"))
        run = collect_fn(bgn, end, trigger=trigger)
        if run is None or run.status != "success":
            return run
    return run
