"""Tests for the ActivityWatch adapter — pure-helper coverage.

The HTTP layer is tested via `_AWUnavailable` only; the focus-block math
(AFK subtraction, merging) is what actually matters and is fully unit-tested.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from billable.adapters.activitywatch import (
    ActivityWatchAdapter,
    FocusBlock,
    _afk_intervals,
    _merge_into_blocks,
    _pick_bucket,
    _subtract_afk,
)


def _utc(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 5, 8, h, m, s, tzinfo=timezone.utc)


def _aw_event(start: datetime, duration_s: float, app: str, title: str) -> dict:
    return {
        "timestamp": start.isoformat(),
        "duration": duration_s,
        "data": {"app": app, "title": title},
    }


def _afk_event(start: datetime, duration_s: float, status: str) -> dict:
    return {
        "timestamp": start.isoformat(),
        "duration": duration_s,
        "data": {"status": status},
    }


# --- _pick_bucket -----------------------------------------------------------


def test_pick_bucket_prefers_local_hostname(monkeypatch) -> None:
    import billable.adapters.activitywatch as aw

    monkeypatch.setattr(aw.socket, "gethostname", lambda: "MyMachine")
    buckets = {
        "aw-watcher-window_OtherHost": {},
        "aw-watcher-window_mymachine": {},
    }
    assert _pick_bucket(buckets, "aw-watcher-window") == "aw-watcher-window_mymachine"


def test_pick_bucket_falls_back_to_first(monkeypatch) -> None:
    import billable.adapters.activitywatch as aw

    monkeypatch.setattr(aw.socket, "gethostname", lambda: "no-match")
    buckets = {"aw-watcher-window_other": {}}
    assert _pick_bucket(buckets, "aw-watcher-window") == "aw-watcher-window_other"


def test_pick_bucket_returns_none_when_absent() -> None:
    assert _pick_bucket({}, "aw-watcher-window") is None


# --- _afk_intervals --------------------------------------------------------


def test_afk_intervals_only_includes_afk_status() -> None:
    events = [
        _afk_event(_utc(9, 0, 0), 600, "afk"),       # 09:00-09:10
        _afk_event(_utc(9, 10, 0), 1800, "not-afk"), # ignored
        _afk_event(_utc(11, 0, 0), 300, "afk"),      # 11:00-11:05
    ]
    intervals = _afk_intervals(events)
    assert intervals == [
        (_utc(9, 0, 0), _utc(9, 10, 0)),
        (_utc(11, 0, 0), _utc(11, 5, 0)),
    ]


# --- _subtract_afk ---------------------------------------------------------


def test_subtract_afk_no_overlap_passes_through() -> None:
    win = [_aw_event(_utc(10, 0), 1800, "Code", "main.py")]  # 10:00-10:30
    afk = [(_utc(11, 0), _utc(11, 5))]                       # different time
    out = _subtract_afk(win, afk)
    assert len(out) == 1
    assert out[0]["duration"] == 1800


def test_subtract_afk_splits_window_event() -> None:
    # Window from 10:00 to 11:00, AFK from 10:20 to 10:40.
    win = [_aw_event(_utc(10, 0), 3600, "Chrome", "doc")]
    afk = [(_utc(10, 20), _utc(10, 40))]
    out = _subtract_afk(win, afk)
    assert len(out) == 2
    assert out[0]["duration"] == 1200  # 10:00-10:20 = 20 min
    assert out[1]["duration"] == 1200  # 10:40-11:00 = 20 min


def test_subtract_afk_drops_fully_covered_window() -> None:
    win = [_aw_event(_utc(10, 0), 600, "App", "x")]   # 10:00-10:10
    afk = [(_utc(9, 50), _utc(10, 30))]               # covers it
    assert _subtract_afk(win, afk) == []


# --- _merge_into_blocks ---------------------------------------------------


def test_merge_collapses_consecutive_same_window() -> None:
    win = [
        _aw_event(_utc(10, 0, 0), 30, "Code", "main.py"),
        _aw_event(_utc(10, 0, 30), 30, "Code", "main.py"),  # contiguous
        _aw_event(_utc(10, 1, 0), 30, "Code", "main.py"),   # contiguous
    ]
    blocks = _merge_into_blocks(win, gap_seconds=60)
    assert len(blocks) == 1
    assert blocks[0].duration == timedelta(seconds=90)


def test_merge_splits_on_long_gap() -> None:
    win = [
        _aw_event(_utc(10, 0, 0), 30, "Code", "main.py"),
        _aw_event(_utc(10, 5, 0), 30, "Code", "main.py"),  # 5-min gap > 60s threshold
    ]
    blocks = _merge_into_blocks(win, gap_seconds=60)
    assert len(blocks) == 2


def test_merge_separate_for_different_titles() -> None:
    win = [
        _aw_event(_utc(10, 0, 0), 30, "Code", "main.py"),
        _aw_event(_utc(10, 0, 30), 30, "Code", "other.py"),  # title changed
    ]
    blocks = _merge_into_blocks(win, gap_seconds=60)
    assert len(blocks) == 2


def test_merge_handles_empty() -> None:
    assert _merge_into_blocks([], gap_seconds=60) == []


# --- adapter.capture against an unreachable server ------------------------


def test_capture_returns_empty_when_server_unavailable() -> None:
    # Use an unused high port so the connection refuses immediately.
    adapter = ActivityWatchAdapter(
        base_url="http://127.0.0.1:1",
        min_focus_minutes=2,
        timeout_seconds=0.5,
    )
    assert adapter.capture(date(2026, 5, 8)) == []


# --- adapter.capture with mocked HTTP -------------------------------------


def test_capture_full_pipeline(monkeypatch) -> None:
    import billable.adapters.activitywatch as aw

    # Local-tz day boundary for 2026-05-08 used by the adapter.
    local_tz = datetime.now().astimezone().tzinfo
    day_start = datetime(2026, 5, 8, 0, 0, tzinfo=local_tz)
    target_start = day_start + timedelta(hours=10)  # 10am local

    buckets = {"aw-watcher-window_test": {}, "aw-watcher-afk_test": {}}
    window_events = [
        # 10:00-10:30 local — same window, should merge into one block
        _aw_event(target_start, 900, "Code", "main.py"),
        _aw_event(target_start + timedelta(seconds=900), 900, "Code", "main.py"),
        # 10:30-10:31 local — too short to survive min_focus
        _aw_event(target_start + timedelta(minutes=30), 60, "Slack", "channel"),
    ]
    afk_events: list[dict] = []  # no AFK

    monkeypatch.setattr(aw.socket, "gethostname", lambda: "test")

    def fake_http(url: str, timeout: float):
        if url.endswith("/api/0/buckets/"):
            return buckets
        if "aw-watcher-window" in url:
            return window_events
        if "aw-watcher-afk" in url:
            return afk_events
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(aw, "_http_json", fake_http)

    adapter = ActivityWatchAdapter(min_focus_minutes=2)
    events = adapter.capture(date(2026, 5, 8))

    assert len(events) == 1  # Code/main.py merged; Slack dropped
    e = events[0]
    assert e.source == "activitywatch"
    assert e.duration == timedelta(minutes=30)
    assert e.project_hint == "main.py"
    assert e.raw == {"app": "Code", "title": "main.py"}
