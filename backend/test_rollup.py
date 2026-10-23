# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for workout attribution in rollup.aggregate_days (stdlib only).

Run with the venv python or any python3 >= 3.12:
  python3 backend/test_rollup.py

These cover the counting rules that were wrong against a real PitPat-T01,
where the widget reported more than the pad's own display because a single
continuous workout had been split into several sessions and each split
re-counted the elapsed prefix.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rollup  # noqa: E402

DAY = rollup.local_date(time.time())  # today, so records land in one bucket


def rec(minutes, cal, dist, time_s, session, state=1):
    return {
        "ts": time.time() - (60 - minutes) * 60,
        "session": session,
        "calories": cal,
        "dist_m": dist,
        "time_s": time_s,
        "speed": 2.0,
        "state": state,
    }


def totals(day):
    if day is None:
        return (0, 0.0, 0, 0)
    return (day["calories"], day["dist_m"], day["time_s"], day["sessions"])


def test_one_workout_recorded_whole():
    day = rollup.aggregate_days([rec(1, 1, 20, 60, "A"), rec(2, 2, 40, 120, "A")]).get(DAY)
    assert totals(day) == (2, 40.0, 120, 1), totals(day)


def test_counters_never_subtract_within_a_workout():
    # 120 s -> 180 s is a 60 s delta, not 300 s of summed counters.
    day = rollup.aggregate_days([rec(1, 2, 40, 120, "A"), rec(2, 3, 60, 180, "A")]).get(DAY)
    assert totals(day) == (3, 60.0, 180, 1), totals(day)


def test_service_restart_mid_workout_does_not_recount():
    """The collector restarted, so the session id changed while the pad's
    counters kept running. This is the bug the user reported."""
    before = [rec(1, 2, 40, 120, "OLD"), rec(2, 4, 80, 240, "OLD")]
    after_restart = [rec(3, 4, 85, 255, "NEW"), rec(4, 5, 100, 300, "NEW")]
    day = rollup.aggregate_days(before + after_restart).get(DAY)
    # One workout: totals equal the final counters, not the sum of both.
    assert totals(day) == (5, 100.0, 300, 1), totals(day)


def test_belt_stop_then_resume_is_the_same_workout():
    """An unloaded belt stops the counters' growth; stepping back on resumes the
    same counters. Closing the epoch on a stopped belt split one workout."""
    seq = [
        rec(1, 1, 20, 60, "A"),
        rec(2, 2, 40, 120, "A"),
        rec(3, 2, 40, 120, "B", state=0),  # stopped, flushed
        rec(4, 3, 60, 180, "B"),  # resumed, counters continued
        rec(5, 4, 80, 240, "B"),
    ]
    day = rollup.aggregate_days(seq).get(DAY)
    assert totals(day) == (4, 80.0, 240, 1), totals(day)


def test_real_new_workout_counts_twice_because_the_pad_reset():
    """A counter going backwards is the only evidence of a new workout."""
    first = [rec(1, 3, 60, 180, "A"), rec(2, 5, 100, 300, "A")]
    second = [rec(3, 1, 20, 60, "B"), rec(4, 2, 40, 120, "B")]  # pad zeroed
    day = rollup.aggregate_days(first + second).get(DAY)
    assert totals(day) == (7, 140.0, 420, 2), totals(day)


def test_carry_continues_a_workout_across_the_rollup_boundary():
    """Yesterday's tail seeds today's delta chain, so today is credited only
    with what was actually walked today instead of the whole workout again."""
    carried = {"epoch": [5, 100.0, 300], "epoch_id": "A"}
    today = [rec(1, 6, 120, 360, "A"), rec(2, 7, 140, 420, "A")]
    day = rollup.aggregate_days(today, seed_carry=carried).get(DAY)
    assert totals(day) == (2, 40.0, 120, 1), totals(day)


def test_carry_still_splits_a_new_workout_after_midnight():
    """A workout that really restarted after midnight must count whole."""
    carried = {"epoch": [5, 100.0, 300], "epoch_id": "A"}
    today = [rec(1, 1, 20, 60, "B"), rec(2, 2, 40, 120, "B")]  # pad zeroed
    day = rollup.aggregate_days(today, seed_carry=carried).get(DAY)
    assert totals(day) == (2, 40.0, 120, 1), totals(day)


def test_trailing_carry_round_trips_the_last_reading():
    seq = [rec(1, 2, 40, 120, "A"), rec(2, 3, 60, 180, "B")]
    carry = rollup.trailing_carry(seq)
    assert carry["epoch"] == [3, 60.0, 180], carry
    assert carry["epoch_id"] == "B", carry
    assert rollup.trailing_carry([]) == {}


def test_record_without_session_is_ignored():
    rows = [rec(1, 1, 20, 60, "A"), {"ts": time.time(), "session": "", "calories": 9,
                                     "dist_m": 99, "time_s": 99, "state": 1}]
    day = rollup.aggregate_days(rows).get(DAY)
    assert totals(day) == (1, 20.0, 60, 1), totals(day)


def test_speeds_only_count_while_the_belt_runs():
    rows = [rec(1, 1, 20, 60, "A"), rec(2, 2, 40, 120, "A", state=0)]
    day = rollup.aggregate_days(rows).get(DAY)
    assert day["max_speed"] == 2.0 and day["active_s"] == 60, day


def test_stats_merges_rollup_carry_with_todays_tail():
    """Regression guard for the stats.py -> aggregate_days call site, which
    passes the rollup carry. Aggregation of a workout spanning midnight must
    credit today with only what was walked today."""
    import datetime
    import json
    import tempfile
    from pathlib import Path

    import stats

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        for mod in (rollup, stats):
            mod.DATA_DIR = tmp
        rollup.HISTORY_FILE = tmp / "history.jsonl"
        rollup.ROLLUP_FILE = tmp / "rollup.json"
        stats.STATUS_FILE = tmp / "status.json"
        stats.DEVICES_FILE = tmp / "devices.json"

        (tmp / "rollup.json").write_text(json.dumps({
            "version": rollup.ROLLUP_VERSION,
            "through": yesterday.isoformat(),
            "carry": {"epoch": [5, 100.0, 300], "epoch_id": "A"},
            "days": {yesterday.isoformat(): {
                "calories": 5, "dist_m": 100.0, "time_s": 300, "sessions": 1,
                "avg_speed": 1.2, "median_speed": 2.0, "max_speed": 2.0,
                "active_s": 300, "longest_session_s": 300}},
        }))
        with open(tmp / "history.jsonl", "w") as fh:
            for cal, dist, dur in ((6, 120.0, 360), (7, 140.0, 420)):
                fh.write(json.dumps({
                    "ts": time.time() - 60, "session": "A", "calories": cal,
                    "dist_m": dist, "time_s": dur, "speed": 2.0, "state": 1}) + "\n")

        days = stats.all_days()
        assert days[yesterday]["calories"] == 5, days
        assert totals(days.get(today)) == (2, 40.0, 120, 1), totals(days.get(today))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
