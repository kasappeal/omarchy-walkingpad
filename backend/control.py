# SPDX-License-Identifier: GPL-3.0-or-later
#!/usr/bin/env python3
"""Ask the collector to control the BA04 threadmill.

Usage: control.py start | stop | speed_up | speed_down

Writes ~/.local/share/walkingpad-ba04/control.json, which the running
collector consumes on its next heartbeat and turns into a BLE command. This
script never touches Bluetooth: the pad accepts a single connection and the
collector holds it, so a second central would only collide with it.

The request carries its own timestamp and the collector drops anything older
than a few seconds, so a command written while the pad is unreachable cannot
arrive late, after you have already stepped off.
"""

import json
import sys
import time
from pathlib import Path

import safeio

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad-ba04"
CONTROL_FILE = DATA_DIR / "control.json"
ACTIONS = ("start", "stop", "speed_up", "speed_down")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].strip() not in ACTIONS:
        print(__doc__.strip())
        return 2

    action = sys.argv[1].strip()
    payload = {"action": action, "requested_at": time.time()}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        safeio.atomic_write(CONTROL_FILE, json.dumps(payload))
    except OSError:
        print(f"could not write {CONTROL_FILE}", file=sys.stderr)
        return 1
    print(action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
