# SPDX-License-Identifier: GPL-3.0-or-later
#!/usr/bin/env python3
"""Per-day rollup of BA04 threadmill history.

The widget only ever displays day granularity, so records older than today
are compacted into rollup.json (rebuilt by the collector at startup and at
day rollover) and then dropped from history.jsonl, which therefore only ever
holds today's uncompacted records. stats.py reads the small rollup plus
today's raw tail instead of re-parsing a growing history on every poll.

The primary metric is the pad's calorie counter: a whole-kcal integer that
ticks roughly once a minute, so a short workout can legitimately add 0 kcal
while still adding distance and time. Day totals keep all three.

Workout attribution is derived from the counters, not from the collector's
session id, because a session id splits far more often than a workout does:
the collector restarts when the service restarts, and the belt stops whenever
nobody is standing on it, both times with the pad resuming the very same
counters. Counting deltas and treating a counter *decrease* as the only
evidence of a new workout is what keeps a workout counted exactly once.

Rollup layout:
  {"version": 3,
   "through": "YYYY-MM-DD",        # last fully compacted day (<= yesterday)
   "carry": {"epoch": [calories, dist_m, time_s],   # counters of the last
                                   # reading compacted, so a workout spanning
                                   # midnight continues its delta chain at the
                                   # tail instead of re-counting itself whole
           "epoch_id": "session"},  # id the open workout was counted under
   "days": {"YYYY-MM-DD": {
       "calories", "dist_m", "time_s", "sessions",
       "avg_speed",                # dist/time in km/h, time-weighted
       "median_speed", "max_speed",  # over live samples with speed > 0
       "active_s",                 # belt time while the pad reported running
       "longest_session_s"}}}
"""

import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import safeio

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad-ba04"
HISTORY_FILE = DATA_DIR / "history.jsonl"
ROLLUP_FILE = DATA_DIR / "rollup.json"

CARRY_VALID_FOR = 6 * 3600  # a workout idle longer than this is over, so its
# counters are no longer worth carrying across the rollup boundary

ROLLUP_VERSION = 3

# history.jsonl normally holds only today's records (hundreds of lines);
# these caps stop a wedged or planted file from exhausting the shell's poll.
MAX_HISTORY_BYTES = 8 * 1024 * 1024
MAX_HISTORY_LINES = 100_000
# One compact day entry is ~160 B, so 4 MB fits ~26,700 days (~73 years);
# this is a hostile/corrupt file backstop, not a usage limit.
MAX_ROLLUP_BYTES = 4 * 1024 * 1024


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def load_records(since: date | None = None) -> list:
    """Parse history records. With `since`, lines older than that day are
    skipped via their timestamp prefix without JSON parsing."""
    cutoff = None
    if since is not None:
        cutoff = datetime.combine(since, datetime.min.time()).timestamp()
    records = []
    lines = safeio.read_lines(HISTORY_FILE, MAX_HISTORY_BYTES, MAX_HISTORY_LINES)
    for line in lines or []:
        line = line.strip()
        if not line:
            continue
        if cutoff is not None:
            # Every record is written with "ts" as the first key.
            try:
                ts = float(line[6 : line.index(",", 6)])
            except (ValueError, IndexError):
                continue
            if ts < cutoff:
                continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def counters_of(record: dict) -> tuple:
    return (
        int(record.get("calories", 0)),
        float(record.get("dist_m", 0)),
        int(record.get("time_s", 0)),
    )


def _new_day():
    return {
        "calories": 0,
        "dist_m": 0.0,
        "time_s": 0,
        "sessions": set(),
        "speeds": [],
        "active_s": 0,
        "session_time": defaultdict(int),
    }


def aggregate_days(records: list, seed_carry: dict | None = None) -> dict:
    """Per-day stats from raw records: {date: {...metrics...}}.

    Each record contributes the delta against the previous reading's counters.
    A decrease means the pad restarted its counters, i.e. a genuinely new
    workout, and that reading is counted whole. `seed_carry` continues the
    chain across the rollup boundary so a workout spanning midnight is not
    counted twice.
    """
    records = sorted(records, key=lambda r: float(r.get("ts", 0)))
    days = defaultdict(_new_day)
    carry = seed_carry or {}
    epoch = tuple(carry["epoch"]) if carry.get("epoch") else None
    epoch_id = carry.get("epoch_id")

    for rec in records:
        ts = float(rec.get("ts", 0))
        day = local_date(ts)
        session = rec.get("session")
        if not session:
            continue
        cur = counters_of(rec)
        if epoch is None or cur < epoch:
            delta = cur  # new workout: counters came from zero
            epoch_id = session
        else:
            delta = tuple(c - p for c, p in zip(cur, epoch))
        epoch = cur

        stats = days[day]
        stats["sessions"].add(epoch_id)
        stats["calories"] += int(delta[0])
        stats["dist_m"] += delta[1]
        stats["time_s"] += int(delta[2])
        if rec.get("state") == 1:
            stats["active_s"] += max(1, int(delta[2]))
        stats["session_time"][epoch_id] += int(delta[2])

        speed = float(rec.get("speed") or 0)
        if rec.get("state") == 1 and speed > 0:
            stats["speeds"].append(speed)

    result = {}
    for day, stats in days.items():
        dist, duration, speeds = stats["dist_m"], stats["time_s"], stats["speeds"]
        result[day] = {
            "calories": stats["calories"],
            "dist_m": round(dist, 1),
            "time_s": duration,
            "sessions": len(stats["sessions"]),
            # m/s -> km/h; the time-weighted "how fast did I actually walk".
            "avg_speed": round(dist / duration * 3.6, 1) if duration > 0 else None,
            "median_speed": round(statistics.median(speeds), 1) if speeds else None,
            "max_speed": round(max(speeds), 1) if speeds else None,
            "active_s": stats["active_s"],
            "longest_session_s": max(stats["session_time"].values(), default=0),
        }
    return result


def trailing_carry(records: list) -> dict:
    """Carry state for the last reading in `records`, empty when none."""
    usable = [r for r in records if r.get("session")]
    if not usable:
        return {}
    last = max(usable, key=lambda r: float(r.get("ts", 0)))
    return {"epoch": list(counters_of(last)), "epoch_id": last["session"]}


def load_rollup() -> dict | None:
    rollup = safeio.read_json(ROLLUP_FILE, MAX_ROLLUP_BYTES)
    if not isinstance(rollup, dict):
        return None
    if rollup.get("version") != ROLLUP_VERSION:
        return None  # older or unknown layout; the rebuild below replaces it
    return rollup


def rebuild_needed(today: date | None = None) -> bool:
    rollup = load_rollup()
    if not rollup:
        return True
    try:
        through = date.fromisoformat(str(rollup.get("through", "")))
    except ValueError:
        return True
    return through < (today or date.today()) - timedelta(days=1)


def truncate_history(today: date) -> int:
    """Drop records older than `today` from history.jsonl. rollup.json must
    already cover them (rebuild_rollup writes it first). Atomic via
    tmp+rename; returns the number of lines dropped. Unparseable lines are
    kept rather than silently lost."""
    cutoff = datetime.combine(today, datetime.min.time()).timestamp()
    kept, dropped = [], 0
    lines = safeio.read_lines(HISTORY_FILE, MAX_HISTORY_BYTES, MAX_HISTORY_LINES)
    if lines is None:
        return 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Every record is written with "ts" as the first key.
        try:
            ts = float(stripped[6 : stripped.index(",", 6)])
        except (ValueError, IndexError):
            kept.append(line)
            continue
        if ts >= cutoff:
            kept.append(line)
        else:
            dropped += 1
    safeio.atomic_write(HISTORY_FILE, "".join(line + "\n" for line in kept))
    return dropped


def rebuild_rollup(today: date | None = None) -> tuple[dict, int]:
    """Extend rollup.json with all history up to yesterday, then truncate
    those records from history.jsonl. Returns (rollup, lines dropped).

    Incremental: history only holds uncompacted records, so each build merges
    newly compacted days into the existing rollup. The previous rollup's carry
    seeds the delta chain of a workout spanning the boundary.

    Crash safety: the rollup lands on disk before truncation, and both writes
    are atomic renames, so a crash at any point leaves rollup and history
    consistent (worst case the same records live in both, which the next
    rebuild just compacts again).
    """
    today = today or date.today()
    through = today - timedelta(days=1)

    existing = load_rollup() or {}
    try:
        existing_through = date.fromisoformat(str(existing.get("through", "")))
    except ValueError:
        existing_through = None
    if existing_through is not None and existing_through >= through:
        return existing, 0  # already current

    records = load_records()
    if existing_through is not None:
        # Only records newer than the existing rollup are uncompacted.
        new_records = [r for r in records if local_date(float(r.get("ts", 0))) > existing_through]
        seed_carry = existing.get("carry", {})
    else:
        new_records = records
        seed_carry = {}

    compacted = [r for r in new_records if local_date(float(r.get("ts", 0))) <= through]
    new_days = aggregate_days(compacted, seed_carry=seed_carry)

    days = dict(existing.get("days", {}))
    for day, stats in sorted(new_days.items()):
        days[day.isoformat()] = stats

    # Carry the newest compacted counters, but only while a workout could
    # plausibly still be open: the pad holds counters across a belt stop, so a
    # stale carry would merge tomorrow's first workout into yesterday's totals.
    carry = trailing_carry(compacted)
    last_ts = max((float(r.get("ts", 0)) for r in compacted if r.get("session")), default=0.0)
    if carry and last_ts and (datetime.now().timestamp() - last_ts) > CARRY_VALID_FOR:
        carry = {}

    rollup = {
        "version": ROLLUP_VERSION,
        "through": through.isoformat(),
        "carry": carry,
        "days": days,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safeio.atomic_write(ROLLUP_FILE, json.dumps(rollup, separators=(",", ":")))
    dropped = truncate_history(today)
    return rollup, dropped


if __name__ == "__main__":
    built, dropped = rebuild_rollup()
    print(json.dumps(built, indent=2))
    print(f"compacted {dropped} history lines", file=sys.stderr)
