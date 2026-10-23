#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure protocol layer for the Superun BA04 threadmill (PitPat-T01 and kin).

No Bluetooth imports here: packets in, readings out, so the framing can be
tested offline against captured traffic.

Wire format, confirmed against a live PitPat-T01 (firmware 27):

  every packet, both directions, carries a 4-byte outer frame:
      4D <dir> <counter> <len> | <inner packet>
  dir is 0x00 for our writes and 0x04 for the pad's status notifications;
  counter increments per write and the pad echoes it back in its reply;
  len is the length of the inner packet that follows.

  the inner packet is:
      <marker> <len> <payload...> <checksum> 43
  with checksum = XOR of (len byte and all payload bytes). Status packets carry
  marker 0x68, our poll packet uses 0x6A.

The pad streams one status notification per second while connected, and drops
the GATT link within 10-20 s unless every notification is answered with a poll
packet -- see collector.py's heartbeat loop. No init handshake is sent: live
A/B testing showed a handshake makes the pad disconnect sooner, not later.

Telemetry units follow the pad's own panel setting (byte 26 bit 0x80), so an
imperial panel reports mph and miles on the wire. Readings are normalised to
metric here; the widget's unit preference only affects display.
"""

from __future__ import annotations

from dataclasses import dataclass

OUTER_MARKER = 0x4D
DIR_HOST = 0x00
DIR_STATUS = 0x04
INNER_TERMINATOR = 0x43

POLL_MARKER = 0x6A
POLL_PAYLOAD = bytes([0xFD])

# GATT layout of the BA04 variant.
SERVICE_BA04_UUID = "0000ffff-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_WRITE_BA04_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_NOTIFY_BA04_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

KMH_PER_MPH = 1.609344
M_PER_MI = 1609.344

# Belt state, decoded from byte 26 bits 0x18.
STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_COUNTDOWN = "countdown"

# Status codes that mean the pad is in an active workout: COUNTDOWN is the
# 3-2-1 lead-in, RUNNING is belt time. PAUSED keeps the session counters and
# is neither.
ACTIVE_STATES = (STATE_RUNNING, STATE_COUNTDOWN)

MIN_INNER_LENGTH = 29  # highest byte the decoder reads is 28
MIN_FRAME_LENGTH = 4 + MIN_INNER_LENGTH


@dataclass
class Reading:
    """One decoded status notification."""

    state: str
    speed: float  # km/h, belt speed
    target_speed: float  # km/h, commanded speed
    max_speed: float  # km/h, ceiling for the current programme
    distance_m: float
    calories: int
    duration_s: int
    imperial: bool
    firmware: int


def _xor(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def _frame(counter: int, inner: bytes) -> bytes:
    """Wrap an inner packet in the `4D <dir> <counter> <len>` outer frame."""
    return bytes([OUTER_MARKER, DIR_HOST, counter & 0xFF, len(inner)]) + inner


def poll_frame(counter: int) -> bytes:
    """The heartbeat packet that must answer every status notification.

    `counter` is the outer sequence number; it wraps at 256.
    """
    inner_len = 4 + len(POLL_PAYLOAD)
    checksum = _xor(bytes([inner_len]) + POLL_PAYLOAD)
    inner = bytes([POLL_MARKER, inner_len]) + POLL_PAYLOAD + bytes([checksum, INNER_TERMINATOR])
    return _frame(counter, inner)


# --- Commands (host -> pad) -----------------------------------------------
#
# A command is a fixed 23-byte packet carried as the whole inner frame, so the
# inner length byte repeats the packet length and the packet keeps its own
# marker/checksum/terminator, exactly like the heartbeat does.

COMMAND_STOP = 0
COMMAND_PAUSE = 2
COMMAND_START_SET_SPEED = 4
COMMAND_LEN = 23
COMMAND_RAMP_START = 1  # gentle slope used to get the belt moving
COMMAND_RAMP_SPEED = 5  # slope used when re-targeting a running belt
# The pad's own panel unit: mph commands set bit 3 and carry mph, km/h
# commands clear it. Speeds are thousandths of whichever unit is in play.
COMMAND_MPH_BIT = 0x08
KMH_PER_MPH = 1.609344
# Every command embeds a user id and body weight, which the pad folds into its
# calorie arithmetic. Upstream RE work ships 80 kg / this id and is confirmed
# working against a real PitPat-T01, so the widget does the same rather than
# inventing values the pad has never seen.
COMMAND_WEIGHT_KG = 80
COMMAND_USER_ID = 58965456623
# The pad refuses to start the belt at anything but a nominal crawl, and this
# is also its lowest selectable speed.
START_SPEED_KMH = 1.0
MIN_SPEED_KMH = 1.0
SPEED_STEP_KMH = 0.1


def build_command(
    command: int,
    speed_milli: int = 0,
    *,
    ramp: int = COMMAND_RAMP_START,
    imperial: bool = False,
    weight: int = COMMAND_WEIGHT_KG,
    user_id: int = COMMAND_USER_ID,
) -> bytes:
    """Assemble the bare 23-byte command packet (no outer frame)."""
    packet = bytearray(COMMAND_LEN)
    packet[0] = POLL_MARKER  # commands reuse the 0x6A marker
    packet[1] = COMMAND_LEN
    packet[6] = (speed_milli >> 8) & 0xFF
    packet[7] = speed_milli & 0xFF
    packet[8] = ramp
    packet[10] = weight & 0xFF
    packet[12] = (command | COMMAND_MPH_BIT) if imperial else (command & ~COMMAND_MPH_BIT)
    packet[13:21] = int(user_id).to_bytes(8, "big")
    packet[21] = _xor(packet[1:21])
    packet[22] = INNER_TERMINATOR
    return bytes(packet)


def start_frame(counter: int, *, imperial: bool = False) -> bytes:
    """Start the belt. It always begins at 1.0 in the pad's native unit; asking
    for 0 is a different, untested code path on this firmware."""
    return _frame(
        counter,
        build_command(
            COMMAND_START_SET_SPEED,
            1000,
            ramp=COMMAND_RAMP_START,
            imperial=imperial,
        ),
    )


def speed_frame(counter: int, speed_kmh: float, *, imperial: bool = False) -> bytes:
    """Re-target a running belt. km/h converts to mph for a pad whose panel is
    set to imperial, because that is what the firmware compares against."""
    milli = int(round(speed_kmh * 1000))
    if imperial:
        milli = int(round(milli / KMH_PER_MPH))
    return _frame(
        counter,
        build_command(
            COMMAND_START_SET_SPEED,
            milli,
            ramp=COMMAND_RAMP_SPEED,
            imperial=imperial,
        ),
    )


def pause_frame(counter: int, *, imperial: bool = False) -> bytes:
    """Hold the workout: the pad keeps its counters across a pause."""
    return _frame(counter, build_command(COMMAND_PAUSE, 0, imperial=imperial))


def stop_frame(counter: int, *, imperial: bool = False) -> bytes:
    """End the workout. The pad flushes and then zeroes its counters."""
    return _frame(counter, build_command(COMMAND_STOP, 0, imperial=imperial))


def clamp_speed(speed_kmh: float, max_speed_kmh: float) -> float:
    """Keep a requested speed inside what the pad says it can do, snapped to
    the 0.1 km/h grid the panel itself uses."""
    ceiling = max_speed_kmh if max_speed_kmh and max_speed_kmh >= MIN_SPEED_KMH else 6.0
    return max(MIN_SPEED_KMH, min(round(speed_kmh, 1), round(ceiling, 1)))


def parse_status(frame: bytes) -> Reading | None:
    """Decode a status notification, or None if it is not one.

    Short frames (the pad also sends small unrelated beacons that still need a
    heartbeat reply) and frames failing the checksum or length checks are
    dropped.
    """
    if len(frame) < MIN_FRAME_LENGTH or frame[0] != OUTER_MARKER or frame[1] != DIR_STATUS:
        return None
    inner = frame[4:]
    if len(inner) < MIN_INNER_LENGTH or inner[-1] != INNER_TERMINATOR:
        return None
    if _xor(inner[1:-2]) != inner[-2]:
        return None

    def u16(offset: int) -> int:
        return int.from_bytes(inner[offset : offset + 2], "big")

    def u32(offset: int) -> int:
        return int.from_bytes(inner[offset : offset + 4], "big")

    flags = inner[26]
    imperial = bool(flags & 0x80)
    state = {
        0x18: STATE_COUNTDOWN,
        0x08: STATE_RUNNING,
        0x10: STATE_PAUSED,
    }.get(flags & 0x18, STATE_STOPPED)

    speed = u16(3) / 1000.0
    target = u16(5) / 1000.0
    max_speed = u16(27) / 1000.0
    # Byte 7..11 carries thousandths of the panel's distance unit: thousandths
    # of a km are metres, so metric needs no scaling.
    distance_units = u32(7)

    if imperial:
        speed *= KMH_PER_MPH
        target *= KMH_PER_MPH
        max_speed *= KMH_PER_MPH
        distance_m = distance_units / 1000.0 * M_PER_MI
    else:
        distance_m = float(distance_units)

    return Reading(
        state=state,
        speed=round(speed, 2),
        target_speed=round(target, 2),
        max_speed=round(max_speed, 2),
        distance_m=round(distance_m, 1),
        calories=u16(18),
        duration_s=u32(20) // 1000,
        imperial=imperial,
        firmware=inner[25],
    )
