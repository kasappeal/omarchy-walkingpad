#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""BA04 threadmill calorie collector daemon.

Idles in a BLE scan loop until a Superun BA04 threadmill (PitPat-T01 and
similar) powers on and starts advertising, then connects and records every
status notification it pushes to an append-only JSONL history.

The pad streams roughly one status packet per second while connected and drops
the GATT link after 10-20 s unless every notification is answered with a poll
packet, so the connection is a push loop with a heartbeat rather than the poll
loop a KingSmith pad needs. Drops are routine (the official phone app sees them
too), which is why "connected" is judged by packet arrival and a reconnect is
attempted immediately instead of after a long backoff.

The pad reports calories, distance and session time; it does NOT count steps,
so calories are the widget's primary metric. All counters are per-workout and
reset when the pad starts a new session, which is what makes them safe to
record as deltas.

Data layout:
  ~/.local/share/walkingpad-ba04/history.jsonl  today's uncompacted records,
      one JSON record per line; older days compact into rollup.json
  ~/.local/share/walkingpad-ba04/status.json    live state, rewritten atomically
  ~/.local/share/walkingpad-ba04/devices.json   candidate pads seen while scanning
  ~/.local/share/walkingpad-ba04/rollup.json    per-day stats for all but today

History record type:
  {"ts", "session", "calories", "dist_m", "time_s", "speed", "state"}

Config at ~/.config/walkingpad-ba04/config.json (all keys optional):
  {"device_address": "AA:BB:CC:DD:EE:FF",  # pin to one pad; absent = auto,
                                           # strongest candidate wins
   "device_name_contains": "pitpat",       # extra case-insensitive matcher
   "record_interval": 5.0}                 # min seconds between history rows
The file is watched: changes apply within seconds, no restart needed.
"""

import asyncio
import json
import logging
import signal
import time
import uuid
from datetime import date
from pathlib import Path

import ba04
import rollup
import safeio
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad-ba04"
HISTORY_FILE = DATA_DIR / "history.jsonl"
STATUS_FILE = DATA_DIR / "status.json"
DEVICES_FILE = DATA_DIR / "devices.json"
CONFIG_FILE = Path.home() / ".config" / "walkingpad-ba04" / "config.json"

SCAN_TIMEOUT = 10.0
RETRY_DELAY = 3.0
RECORD_MIN_INTERVAL = 5.0  # seconds between history rows for one session
CONNECT_ATTEMPTS = 3  # connect attempts per scan hit before rescanning
CONNECT_RETRY_DELAY = 2.0
STATUS_MIN_INTERVAL = 1.0  # throttle status.json writes
LINK_LOST_AFTER = 20.0  # no notification for this long -> treat as disconnected
DEVICE_FORGET_AFTER = 24 * 3600  # drop pads not seen for a day
FIRST_PACKET_TIMEOUT = 10.0  # the pad speaks first after connecting; writing
# before it does is rejected with a GATT "unlikely error"
CONNECT_TIMEOUT = 12.0  # the pad advertises intermittently, so a connect can
# simply miss its window; failing fast gets us back to scanning sooner
HEARTBEAT_WAIT = 0.3  # answer a notification, or poll again after this
MAX_STATE_BYTES = 64 * 1024  # status, devices, config are all tiny by design
MAX_NAME_LENGTH = 64  # BLE names are short; cap before storage and QML

# BA04 pads advertise the 0xFFFF service; names vary by brand.
NAME_PREFIXES = ("PITPAT", "SUPERUN", "BA04")

log = logging.getLogger("walkingpad-ba04-collector")


def load_config() -> dict:
    config = safeio.read_json(CONFIG_FILE, MAX_STATE_BYTES)
    return config if isinstance(config, dict) else {}


def sanitize_name(name) -> str:
    """BLE names come from the radio; keep only printable characters and
    cap the length before they reach storage and QML."""
    cleaned = "".join(ch for ch in str(name or "") if ch.isprintable())
    return cleaned[:MAX_NAME_LENGTH]


def config_mtime() -> float:
    try:
        return CONFIG_FILE.stat().st_mtime
    except OSError:
        return 0.0


def name_matches(name: str | None, extra_needle: str = "") -> bool:
    lowered = str(name or "").lower()
    if not lowered:
        return False
    if extra_needle and extra_needle in lowered:
        return True
    return any(lowered.startswith(prefix.lower()) for prefix in NAME_PREFIXES)


def is_candidate(name: str | None, service_uuids, extra_needle: str = "") -> bool:
    """Detection for the candidate list.

    0xFFFF is a common vendor UUID, so a name match is the *stronger* signal:
    service-only devices qualify (an unnamed pad must stay usable) but are
    ranked below named ones when auto-connecting. Unknown gear can therefore
    never out-sign a real pad that is in range.
    """
    services = {str(u).lower() for u in service_uuids or []}
    return name_matches(name, extra_needle) or ba04.SERVICE_BA04_UUID in services


def candidate_rank(device, adv, extra_needle: str = "") -> tuple:
    """Sort key: named pads first, then signal strength."""
    return (name_matches(device.name, extra_needle), adv.rssi or -999)


class Collector:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.config_mtime = config_mtime()
        self.address = ""
        self.device_name = ""
        self.firmware = 0
        self.max_speed = 0.0
        self.session_id: str | None = None
        self.session_start = 0.0
        self.counters: tuple | None = None
        self.last_packet = 0.0
        self.last_reading: ba04.Reading | None = None
        self.last_record_key: tuple | None = None
        self.last_record_time = 0.0
        self.last_status_write = 0.0
        self.rollup_day: date | None = None

    @property
    def record_interval(self) -> float:
        return float(self.config.get("record_interval", RECORD_MIN_INTERVAL))

    @property
    def pinned_address(self) -> str:
        return str(self.config.get("device_address", "")).strip().lower()

    def reload_config_if_changed(self) -> bool:
        mtime = config_mtime()
        if mtime == self.config_mtime:
            return False
        self.config_mtime = mtime
        self.config = load_config()
        log.info("Config reloaded: %s", self.config)
        return True

    # --- persistence ---

    def append_history(self, record: dict) -> None:
        try:
            safeio.append_line(HISTORY_FILE, json.dumps(record, separators=(",", ":")))
        except OSError:
            log.exception("Failed to append history")

    def write_status(self, reading: ba04.Reading | None = None, connected=False, force=False) -> None:
        now = time.time()
        if not force and now - self.last_status_write < STATUS_MIN_INTERVAL:
            return
        self.last_status_write = now
        reading = reading or self.last_reading
        payload = {
            "enabled": True,
            "connected": connected,
            "device_name": sanitize_name(self.device_name),
            "address": self.address,
            "firmware": self.firmware,
            "max_speed": self.max_speed,
            "state": reading.state if reading else ba04.STATE_STOPPED,
            "walking": bool(reading and reading.state == ba04.STATE_RUNNING),
            "calories": reading.calories if reading else 0,
            "dist_m": reading.distance_m if reading else 0.0,
            "time_s": reading.duration_s if reading else 0,
            "speed": reading.speed if reading else 0.0,
            "target_speed": reading.target_speed if reading else 0.0,
            "session_start": self.session_start if self.session_id else None,
            "ts": now,
        }
        try:
            safeio.atomic_write(STATUS_FILE, json.dumps(payload, separators=(",", ":")))
        except OSError:
            log.exception("Failed to write status")

    def remember_devices(self, candidates: list) -> None:
        """Update the seen-pads registry consumed by the widget's picker."""
        now = time.time()
        existing = safeio.read_json(DEVICES_FILE, MAX_STATE_BYTES)
        devices = existing if isinstance(existing, dict) else {}
        for device, adv in candidates:
            devices[device.address] = {
                "name": sanitize_name(device.name or ""),
                "address": device.address,
                "rssi": adv.rssi,
                "protocol": "ba04",
                "last_seen": now,
            }
        devices = {
            address: info
            for address, info in devices.items()
            if now - float(info.get("last_seen", 0)) < DEVICE_FORGET_AFTER
        }
        try:
            safeio.atomic_write(DEVICES_FILE, json.dumps(devices, separators=(",", ":")))
        except OSError:
            log.exception("Failed to write devices")

    # --- rollup ---

    def maybe_rebuild_rollup(self) -> None:
        """Compact history older than today into rollup.json once per day.

        Runs at startup (rollup may be missing or stale after downtime) and
        whenever the calendar day changes while the daemon is up.
        """
        today = date.today()
        if self.rollup_day == today:
            return
        try:
            if rollup.rebuild_needed(today):
                built, dropped = rollup.rebuild_rollup(today)
                log.info(
                    "History rollup rebuilt: %d days through %s, %d history lines compacted",
                    len(built["days"]),
                    built["through"],
                    dropped,
                )
            self.rollup_day = today
        except Exception:
            log.exception("Rollup rebuild failed")

    # --- sessions ---

    def open_session(self, duration_s: int = 0) -> None:
        if self.session_id:
            log.info("Session %s ended", self.session_id)
        self.session_id = uuid.uuid4().hex[:12]
        # Backdate to when the workout really began: connecting mid-session
        # means the pad already holds counters for the earlier part.
        self.session_start = time.time() - duration_s
        self.last_record_key = None
        log.info("Session %s started", self.session_id)

    # --- readings ---

    def on_reading(self, reading: ba04.Reading) -> None:
        """Record one pushed status notification.

        A session is the pad's own workout epoch, not a belt-active window. A
        PAUSE holds the counters and resumes them later (measured: 63 s / 17 m /
        1 kcal survived a 40 s pause), so the epoch stays open across it. Only
        a STOPPED belt ends an epoch, and the pad then zeroes its counters
        within a few seconds, so the next workout reads as a fresh one.
        """
        now = time.time()
        self.last_packet = now
        self.max_speed = reading.max_speed or self.max_speed
        self.firmware = reading.firmware or self.firmware

        # A COUNTDOWN whose counters are still all zero is the 3-2-1 lead-in of
        # a workout that may never start (someone steps on then off), so it does
        # not open a session; once the belt runs, or the pad is resuming a held
        # countdown with counters on it, the epoch starts.
        moved = reading.state == ba04.STATE_RUNNING or (
            reading.calories > 0 or reading.distance_m > 0 or reading.duration_s > 0
        )
        if reading.state in ba04.ACTIVE_STATES and moved:
            counters = (reading.calories, reading.distance_m, reading.duration_s)
            new_workout = self.counters is None or counters < self.counters
            if new_workout or self.session_id is None:
                self.open_session(reading.duration_s)
            self.counters = counters
            self.record(reading, force=new_workout)
        elif reading.state == ba04.STATE_PAUSED:
            pass  # epoch held; nothing to flush yet
        elif self.counters is not None:
            # Belt stopped: flush the workout's final counters before the pad
            # zeroes them, then close the epoch.
            if self.last_reading is not None:
                self.record(self.last_reading, force=True)
            self.counters = None
            self.session_id = None

        self.last_reading = reading
        self.write_status(reading, connected=True)

    def record(self, reading: ba04.Reading, force: bool = False) -> None:
        """Append a history row when the session actually moved on.

        Rows are throttled to `record_interval` because the pad pushes about
        one packet a second and the duration counter ticks every second; the
        deltas recorded are what rollup sums, so a skipped row only loses the
        counters of a few seconds, never a session.
        """
        now = time.time()
        key = (reading.calories, reading.distance_m, reading.duration_s, reading.state)
        if key == self.last_record_key:
            return
        if not force and now - self.last_record_time < self.record_interval:
            return
        self.last_record_key = key
        self.last_record_time = now
        self.append_history(
            {
                "ts": now,
                "session": self.session_id,
                "calories": reading.calories,
                "dist_m": reading.distance_m,
                "time_s": reading.duration_s,
                "speed": reading.speed,
                "state": 1 if reading.state == ba04.STATE_RUNNING else 0,
            }
        )

    # --- connection lifecycle ---

    async def scan_for_pad(self) -> tuple[str, str] | None:
        """Return (address, name) for the pad to connect to, or None.

        The name is only remembered for display: bleak needs the address, and
        a stale BLEDevice object makes connects time out on this pad.
        """
        log.info("Scanning for BA04 threadmill...")
        needle = str(self.config.get("device_name_contains", "pitpat")).lower()
        devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
        candidates = [
            (device, adv)
            for device, adv in devices.values()
            if is_candidate(device.name, adv.service_uuids, needle)
        ]
        self.remember_devices(candidates)

        pinned = self.pinned_address
        if pinned:
            for device, _adv in candidates:
                if device.address.lower() == pinned:
                    log.info("Found pinned pad: %s (%s)", device.name, device.address)
                    return device.address, device.name or device.address
            return None
        if not candidates:
            return None
        device, adv = max(
            candidates, key=lambda pair: candidate_rank(pair[0], pair[1], needle)
        )
        log.info("Found pad: %s (%s, %s dBm)", device.name, device.address, adv.rssi)
        return device.address, device.name or device.address

    async def connect_with_retry(self, address: str):
        """Open a client, retrying the connect a few times.

        The scan just proved the pad exists; what is flaky is the advertising
        window, which the idle pad opens only every few seconds. Retrying the
        connect is much quicker than falling back to a full scan per attempt.
        """
        last_error = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            client = BleakClient(address, timeout=CONNECT_TIMEOUT)
            try:
                await client.connect()
                return client
            except (OSError, ValueError, asyncio.TimeoutError, BleakError) as error:
                last_error = error
                log.info(
                    "Connect attempt %d/%d to %s failed: %s",
                    attempt,
                    CONNECT_ATTEMPTS,
                    address,
                    type(error).__name__,
                )
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(CONNECT_RETRY_DELAY)
        raise last_error

    async def run_connected(self, address: str, name: str) -> None:
        """Hold one connection, answering every notification with a poll.

        `address` is passed to bleak as a string on purpose: this pad
        advertises intermittently, so a BLEDevice snapshot taken by an earlier
        scan is often already stale and BlueZ times out connecting to it. Given
        an address, bleak re-resolves the device at connect time instead.
        """
        notify_event = asyncio.Event()
        counter = {"n": 0}
        if address != self.address:
            # A different pad: its counters are unrelated to the last ones.
            self.address = address
            self.counters = None
            self.session_id = None
            self.last_reading = None

        def on_notify(_characteristic, data: bytearray) -> None:
            raw = bytes(data)
            reading = ba04.parse_status(raw)
            if reading is None:
                # Unrelated beacon: nothing to record, but it still proves the
                # link is alive and must be answered by the heartbeat loop.
                self.last_packet = time.time()
            else:
                self.on_reading(reading)
            notify_event.set()

        client = await self.connect_with_retry(address)
        try:
            self.device_name = name
            log.info("Connected to %s (%s)", self.device_name, address)
            await client.start_notify(ba04.CHARACTERISTIC_NOTIFY_BA04_UUID, on_notify)
            self.last_packet = time.time()

            # The pad introduces itself unprompted right after connecting, and
            # refuses writes until it has. Sending the first poll without this
            # handshake costs a reconnect cycle per attempt.
            try:
                await asyncio.wait_for(notify_event.wait(), timeout=FIRST_PACKET_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("%s: no opening packet, dropping link", self.device_name)
                return
            self.write_status(force=True, connected=True)

            while client.is_connected and self.link_is_live():
                if self.reload_config_if_changed() and not self.is_selected(address):
                    log.info("Selection changed, dropping %s", address)
                    return
                self.maybe_rebuild_rollup()
                notify_event.clear()
                try:
                    await client.write_gatt_char(
                        ba04.CHARACTERISTIC_WRITE_BA04_UUID,
                        ba04.poll_frame(counter["n"]),
                        response=True,
                    )
                    counter["n"] = (counter["n"] + 1) & 0xFF
                except (OSError, ValueError, asyncio.TimeoutError, BleakError) as error:
                    # Expected: this is how the pad ends a session.
                    log.info("Heartbeat write failed (%s)", type(error).__name__)
                    return
                try:
                    await asyncio.wait_for(notify_event.wait(), timeout=HEARTBEAT_WAIT)
                except asyncio.TimeoutError:
                    pass  # no reply within the window; poll again regardless
        finally:
            # The pad drops us routinely; a clean teardown keeps BlueZ from
            # holding a stale object that would slow the next connect.
            try:
                await client.stop_notify(ba04.CHARACTERISTIC_NOTIFY_BA04_UUID)
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def link_is_live(self) -> bool:
        return time.time() - self.last_packet < LINK_LOST_AFTER

    def is_selected(self, address: str) -> bool:
        pinned = self.pinned_address
        return not pinned or address.lower() == pinned

    async def cleanup(self) -> None:
        self.last_reading = None
        # Session id, counters, address and name all stay: the pad drops the
        # link by design, and a reconnect inside the same workout resumes the
        # same epoch instead of counting it a second time.
        self.write_status(force=True, connected=False)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.maybe_rebuild_rollup()
            self.reload_config_if_changed()
            try:
                found = await self.scan_for_pad()
                if found is None:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                await self.run_connected(*found)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Connection cycle failed")
            finally:
                await self.cleanup()
            if not stop.is_set():
                # The pad drops the link on its own every so often; reconnect
                # fast so a session crossing a drop is still recorded whole.
                await asyncio.sleep(RETRY_DELAY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    collector = Collector(load_config())

    async def amain() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        task = asyncio.create_task(collector.run(stop))
        await stop.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await collector.cleanup()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    log.info("Collector stopped")


if __name__ == "__main__":
    main()
