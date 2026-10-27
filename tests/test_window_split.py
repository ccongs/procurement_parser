"""수집 창 경계와 조각 실행의 중단·예외 전파 계약."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.window_split import MAX_WINDOW_DAYS, collect_in_chunks, split_window

BGN = datetime(2026, 5, 1, 12)


@pytest.mark.parametrize("duration,count", [
    (timedelta(0), 1), (timedelta(minutes=-1), 1),
    (timedelta(days=27), 1), (timedelta(days=28), 1),
    (timedelta(days=28, minutes=1), 2), (timedelta(days=60), 3),
])
def test_split_window_boundaries(duration, count):
    end = BGN + duration
    chunks = split_window(BGN, end)
    assert MAX_WINDOW_DAYS == 28
    assert len(chunks) == count
    assert chunks[0][0] == BGN and chunks[-1][1] == end
    assert all(e - b <= timedelta(days=28) for b, e in chunks)
    assert all(left[1] == right[0] for left, right in zip(chunks, chunks[1:]))
    if count > 1:
        assert all(e - b == timedelta(days=28) for b, e in chunks[:-1])
        assert chunks[-1][1] - chunks[-1][0] < timedelta(days=28)


@pytest.mark.parametrize("max_days", [0, -1])
def test_split_window_rejects_nonpositive_limit_even_for_empty_window(max_days):
    with pytest.raises(ValueError):
        split_window(BGN, BGN, max_days)


def test_split_window_custom_limit():
    assert split_window(BGN, BGN + timedelta(days=3), 2) == [
        (BGN, BGN + timedelta(days=2)),
        (BGN + timedelta(days=2), BGN + timedelta(days=3)),
    ]


@pytest.mark.parametrize("stop_status", ["success", "partial", "failed", "running", None])
def test_collect_in_chunks_stops_or_returns_last_run(stop_status, caplog):
    calls = []
    runs = [SimpleNamespace(status="success"),
            SimpleNamespace(status=stop_status) if stop_status else None,
            SimpleNamespace(status="success")]

    def collect(bgn, end, *, trigger):
        calls.append((bgn, end, trigger))
        return runs[len(calls) - 1]

    with caplog.at_level("INFO", logger="app.window_split"):
        result = collect_in_chunks(collect, BGN, BGN + timedelta(days=60), trigger="scheduled")
    count = 3 if stop_status == "success" else 2
    assert result is runs[count - 1]
    assert calls == [(b, e, "scheduled") for b, e in
                     split_window(BGN, BGN + timedelta(days=60))[:count]]
    assert "1/3 조각 window=202605011200~202605291200" in caplog.text


def test_collect_in_chunks_stops_at_first_none():
    calls = []

    def collect(*args, **kwargs):
        calls.append(args)
        return None

    assert collect_in_chunks(collect, BGN, BGN + timedelta(days=60), trigger="manual") is None
    assert len(calls) == 1


def test_collect_in_chunks_propagates_exception():
    error = RuntimeError("수집 실패")

    def collect(*args, **kwargs):
        raise error

    with pytest.raises(RuntimeError) as raised:
        collect_in_chunks(collect, BGN, BGN + timedelta(days=60), trigger="manual")
    assert raised.value is error
