#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Superun BA04 protocol codec and the collector's session logic.

Runnable without pytest or a Bluetooth adapter:

    python3 test_ba04.py

The packets are real captures from a PitPat-T01 (firmware 27), recorded while
walking, pausing and stopping, so the byte offsets and state transitions are
anchored to the device rather than to the decoder's own assumptions.
"""

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ba04  # noqa: E402
import collector  # noqa: E402
import rollup  # noqa: E402

# --- captured fixtures -----------------------------------------------------

PACKET_RUNNING = bytes.fromhex(
    "4d041a2f682f0003e803e80000004c00000000000000000500043238ca1b0a177000050001000000000000000c02260000f843"
)
PACKET_PAUSED = bytes.fromhex(
    "4d04b62f682f0003e803e8000000110000000000000000010000f618cb1b121770000500000000000000000000022600005543"
)
PACKET_STOPPED_HOLDING = bytes.fromhex(
    "4d041d2f682f0003e803e80000004c00000000000000000500043620ca1b02177000050000000000000000000c02260000ed43"
)
PACKET_STOPPED_ZEROED = bytes.fromhex(
    "4d04232f682f00000000000000004c00000000000000000000000000ca1b02177000050000000000000000000000000000d243"
)
PACKET_COUNTDOWN = bytes.fromhex(
    "4d04352f682f00000000000000000000000000000000000000000000ca1b1a1770000500000000000000000000000000008643"
)
# The opening notification is longer: it carries an extra device string.
PACKET_OPENING = bytes.fromhex(
    "4d04013468340003e803e80000002500000000000000000200020b70ca1b0a1770000500745a4a5531366745554e4e32793339350e02ea43"
)


def test_parse_state():
    r = ba04.parse_status(PACKET_RUNNING)
    assert r.state == ba04.STATE_RUNNING
    assert (r.speed, r.target_speed, r.max_speed) == (1.0, 1.0, 6.0)
    assert (r.calories, r.distance_m, r.duration_s) == (5, 76.0, 275)
    assert r.firmware == 27 and r.imperial is False

    assert ba04.parse_status(PACKET_PAUSED).state == ba04.STATE_PAUSED
    assert ba04.parse_status(PACKET_STOPPED_HOLDING).state == ba04.STATE_STOPPED
    assert ba04.parse_status(PACKET_COUNTDOWN).state == ba04.STATE_COUNTDOWN
    stopped = ba04.parse_status(PACKET_STOPPED_ZEROED)
    assert stopped.state == ba04.STATE_STOPPED and stopped.duration_s == 0
    opening = ba04.parse_status(PACKET_OPENING)
    assert opening.distance_m == 37.0 and opening.calories == 2, "long frame decodes"


def test_parse_rejects_junk():
    assert ba04.parse_status(b"") is None
    assert ba04.parse_status(b"\x4d\x04\x00\x05" + b"\x00" * 2) is None, "too short"
    wrong_dir = bytearray(PACKET_RUNNING)
    wrong_dir[1] = 0x00  # a host-direction frame is not a status notification
    assert ba04.parse_status(bytes(wrong_dir)) is None
    broken = bytearray(PACKET_RUNNING)
    broken[-2] ^= 0xFF  # flip the checksum
    assert ba04.parse_status(bytes(broken)) is None, "checksum is enforced"


def test_imperial_conversion():
    """An imperial panel puts mph/miles on the wire; readings are metric out."""
    frame = bytearray(PACKET_RUNNING)
    flag = 4 + 26  # outer frame is 4 bytes, then the inner payload
    frame[flag] |= 0x80
    # Re-seal the checksum over the changed inner packet, or the decoder is
    # right to drop the frame. Note frame[4:] would be a copy, not a view.
    frame[-2] = _xor(memoryview(frame)[5:-2])
    r = ba04.parse_status(bytes(frame))
    assert r is not None, "re-sealed frame must decode"
    assert r.imperial is True
    assert abs(r.speed - 1.0 * ba04.KMH_PER_MPH) < 1e-3, r.speed
    assert abs(r.distance_m - 76.0 * ba04.KMH_PER_MPH) < 1.0, r.distance_m
    assert r.calories == 5, "kcal is unit-independent"


def _xor(data):
    out = 0
    for byte in data:
        out ^= byte
    return out


def test_poll_frame():
    """The heartbeat must be byte-identical to what a real pad accepts."""
    for counter in (0x00, 0x01, 0x7F, 0xFF):
        frame = ba04.poll_frame(counter)
        assert frame.hex() == f"4d00{counter:02x}056a05fdf843", frame.hex()


# --- collector session accounting -----------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 1_000.0

    def time(self):
        return self.t

    def advance(self, seconds=1.0):
        self.t += seconds


def make_collector(tmp, record_interval=5.0):
    collector.DATA_DIR = tmp
    collector.HISTORY_FILE = tmp / "history.jsonl"
    collector.STATUS_FILE = tmp / "status.json"
    collector.DEVICES_FILE = tmp / "devices.json"
    rollup.DATA_DIR = tmp
    rollup.HISTORY_FILE = tmp / "history.jsonl"
    rollup.ROLLUP_FILE = tmp / "rollup.json"
    collector.RECORD_MIN_INTERVAL = record_interval
    clock = FakeClock()
    collector.time.time = clock.time
    c = collector.Collector({})
    c.rollup_day = date.today()
    c.address = "F0:F5:BD:83:D3:7A"
    c.device_name = "PitPat-T01"
    return c, clock


def reading(state, calories=0, distance_m=0.0, duration_s=0, speed=0.0):
    return ba04.Reading(
        state=state,
        speed=speed,
        target_speed=speed,
        max_speed=6.0,
        distance_m=distance_m,
        calories=calories,
        duration_s=duration_s,
        imperial=False,
        firmware=27,
    )


def test_session_spans_a_pause():
    """A pause holds the counters; the workout stays ONE session, counted once."""
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        c.on_reading(reading(ba04.STATE_COUNTDOWN))
        for step in range(1, 11):  # 100 s / 100 m / 8 kcal
            c.on_reading(
                reading(ba04.STATE_RUNNING, 8, step * 10.0, step * 10, speed=3.6)
            )
            clock.advance(1)
        first_session = c.session_id
        for _ in range(40):  # a 40 s pause, longer than any throttle
            c.on_reading(reading(ba04.STATE_PAUSED, 8, 100.0, 100))
            clock.advance(1)
        c.on_reading(reading(ba04.STATE_COUNTDOWN, 8, 100.0, 100))
        for step in range(11, 16):  # resumed: 150 s / 150 m / 12 kcal total
            c.on_reading(
                reading(ba04.STATE_RUNNING, 12, step * 10.0, step * 10, speed=3.6)
            )
            clock.advance(1)
        assert c.session_id == first_session, "pause must not start a new session"
        c.on_reading(reading(ba04.STATE_STOPPED, 0, 0.0, 0))
        assert c.session_id == first_session, "a stopped belt must not close the epoch"

        days = rollup.aggregate_days(rollup.load_records())
        day = list(days.values())[0]
        assert day["sessions"] == 1, day
        assert (day["calories"], day["time_s"]) == (12, 150), day
        assert abs(day["dist_m"] - 150.0) < 0.1, day
        assert day["longest_session_s"] == 150, day


def test_stopped_then_resumed_belt_is_one_workout():
    """Measured on a PitPat-T01: the belt stops when nobody stands on it and the
    pad then resumes the same counters (43 s -> 65 s). Treating the stop as the
    end of the epoch re-counted the whole elapsed prefix on resume."""
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        for step in range(1, 5):  # 40 s / 40 m / 4 kcal
            c.on_reading(reading(ba04.STATE_RUNNING, 4, step * 10.0, step * 10, speed=3.6))
            clock.advance(6)  # past the row throttle, as a real pad's packets are
        c.on_reading(reading(ba04.STATE_STOPPED, 4, 40.0, 40))
        clock.advance(6)
        for step in range(5, 8):  # resumed on the same counters: 70 s / 70 m / 7 kcal
            c.on_reading(reading(ba04.STATE_RUNNING, 7, step * 10.0, step * 10, speed=3.6))
            clock.advance(6)
        days = rollup.aggregate_days(rollup.load_records())
        day = list(days.values())[0]
        assert day["sessions"] == 1, day
        assert (day["calories"], day["time_s"]) == (7, 70), day


def test_pad_zeroing_counters_opens_a_real_new_workout():
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        for step in range(1, 5):
            c.on_reading(reading(ba04.STATE_RUNNING, 4, step * 10.0, step * 10, speed=3.6))
            clock.advance(6)
        c.on_reading(reading(ba04.STATE_STOPPED, 0, 0.0, 0))  # pad zeroed its counters
        clock.advance(6)
        first = c.session_id
        for step in range(1, 4):
            c.on_reading(reading(ba04.STATE_RUNNING, 3, step * 10.0, step * 10, speed=3.6))
            clock.advance(6)
        assert c.session_id != first, "a counter reset must start a new workout"
        days = rollup.aggregate_days(rollup.load_records())
        day = list(days.values())[0]
        assert day["sessions"] == 2, day
        assert (day["calories"], day["time_s"]) == (7, 70), day


def test_two_workouts_are_two_sessions():
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        for calories, metres, seconds in ((5, 76.0, 276), (1, 24.0, 89)):
            c.on_reading(
                reading(ba04.STATE_RUNNING, calories, metres, seconds, speed=1.0)
            )
            clock.advance(6)
            c.on_reading(reading(ba04.STATE_STOPPED, 0, 0.0, 0))
            clock.advance(60)
        days = rollup.aggregate_days(rollup.load_records())
        day = list(days.values())[0]
        assert day["sessions"] == 2, day
        assert day["calories"] == 6, day
        assert abs(day["dist_m"] - 100.0) < 0.1, day


def test_stop_flushes_the_final_counters_whatever_the_throttle():
    """The pad zeroes its counters seconds after stopping, so the last reading
    seen while running must land in history even inside a throttle window."""
    for interval in (1.0, 5.0, 20.0):
        with tempfile.TemporaryDirectory() as dir_:
            tmp = Path(dir_)
            c, clock = make_collector(tmp, record_interval=interval)
            for step in range(1, 21):  # one reading per second, 20 s / 200 m
                c.on_reading(
                    reading(ba04.STATE_RUNNING, 2, step * 10.0, step, speed=3.6)
                )
                clock.advance(1)
            c.on_reading(reading(ba04.STATE_STOPPED, 0, 0.0, 0))
            days = rollup.aggregate_days(rollup.load_records())
            day = list(days.values())[0]
            assert (day["calories"], day["time_s"]) == (2, 20), (interval, day)
            assert abs(day["dist_m"] - 200.0) < 0.1, (interval, day)


def test_idle_countdown_blip_creates_no_session():
    """Stepping on during the 3-2-1 and off again records nothing."""
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        for _ in range(5):
            c.on_reading(reading(ba04.STATE_COUNTDOWN))
            clock.advance(1)
        c.on_reading(reading(ba04.STATE_STOPPED))
        assert c.session_id is None
        assert rollup.load_records() == []


def test_status_json_matches_the_widget_contract():
    """Panel.qml reads exactly these keys; renaming one silently breaks the UI."""
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        c.on_reading(reading(ba04.STATE_RUNNING, 4, 66.0, 250, speed=1.0))
        status = json.loads((tmp / "status.json").read_text())
        for key in (
            "enabled", "connected", "device_name", "address", "firmware",
            "max_speed", "state", "walking", "calories", "dist_m", "time_s",
            "speed", "target_speed", "session_start", "ts",
        ):
            assert key in status, f"status.json missing {key}"
        assert status["walking"] is True and status["state"] == "running"
        assert status["calories"] == 4 and status["time_s"] == 250
        assert "steps" not in status, "steps is gone from the live payload"


def test_live_payload_shape_for_stats():
    """stats.py copies these live fields into the JSON the popup consumes."""
    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        c.on_reading(reading(ba04.STATE_RUNNING, 4, 66.0, 250, speed=1.0))
        status = json.loads((tmp / "status.json").read_text())
        # stats.py's live block reads exactly these:
        for key in ("calories", "dist_m", "time_s", "speed", "target_speed",
                    "state", "max_speed", "firmware", "session_start",
                    "device_name", "address"):
            assert key in status, f"stats.py would emit a null/missing {key}"


def test_rollup_rebuild_and_truncate():
    import datetime as dt

    with tempfile.TemporaryDirectory() as dir_:
        tmp = Path(dir_)
        c, clock = make_collector(tmp)
        yesterday = date.today() - dt.timedelta(days=2)
        old_ts = dt.datetime.combine(yesterday, dt.time(9, 0)).timestamp()
        c.on_reading(reading(ba04.STATE_RUNNING, 100, 1500.0, 1800, speed=3.0))
        # rewrite the record's timestamp into the past, then roll it up
        line = json.loads((tmp / "history.jsonl").read_text().strip())
        line["ts"] = old_ts
        (tmp / "history.jsonl").write_text(json.dumps(line) + "\n")
        assert rollup.rebuild_needed() is True
        built, dropped = rollup.rebuild_rollup()
        assert dropped == 1, dropped
        assert built["through"] == (date.today() - dt.timedelta(days=1)).isoformat()
        day = built["days"][yesterday.isoformat()]
        assert day["calories"] == 100 and abs(day["dist_m"] - 1500.0) < 0.1, day
        assert (tmp / "history.jsonl").read_text().strip() == "", "truncated"
        assert rollup.rebuild_needed() is False, "rollup is current"


def test_devices_and_detection():
    assert collector.is_candidate("PitPat-T01", [ba04.SERVICE_BA04_UUID]) is True
    assert collector.is_candidate("Superun-B05", []) is True
    assert collector.is_candidate("BA04-TREADMILL", []) is True
    assert collector.is_candidate(None, [ba04.SERVICE_BA04_UUID]) is True, "service alone is enough"
    assert collector.is_candidate("WH-1000XM4", []) is False, "headphones are not a pad"
    assert collector.is_candidate("Fitness Bike", []) is False, "other gear is not a pad"
    assert collector.is_candidate(None, []) is False, "no name, no service: unknown"
    assert collector.is_candidate("My Threadmill", [], "threadmill") is True, "name override"


def test_named_pad_outranks_a_service_only_neighbour():
    """0xFFFF is a generic vendor UUID: a loud neighbour must not steal the
    auto-connect slot from the real pad in the same room."""

    class Dev:
        def __init__(self, name, address):
            self.name, self.address = name, address

    class Adv:
        def __init__(self, rssi, uuids):
            self.rssi, self.service_uuids = rssi, uuids

    neighbour = (Dev("Generic-Gadget", "11:11:11:11:11:11"), Adv(-40, [ba04.SERVICE_BA04_UUID]))
    pad = (Dev("PitPat-T01", "F0:F5:BD:83:D3:7A"), Adv(-70, []))
    ranked = max([neighbour, pad], key=lambda pair: collector.candidate_rank(*pair, "pitpat"))
    assert ranked is pad, "the named pad wins even when much quieter"
    # ... and an unnamed pad on the service still qualifies at all
    assert collector.is_candidate(None, [ba04.SERVICE_BA04_UUID]) is True


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERR  {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
