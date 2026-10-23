#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Select which BA04 threadmill the collector connects to.

Usage: select-pad.py <AA:BB:CC:DD:EE:FF | auto>

Writes ~/.config/walkingpad-ba04/config.json (preserving other keys). The
running collector watches the file and applies the change within seconds;
passing "auto" clears the pin and returns to strongest-signal auto-connect.
"""

import json
import sys
from pathlib import Path

import safeio

CONFIG_FILE = Path.home() / ".config" / "walkingpad-ba04" / "config.json"
MAX_CONFIG_BYTES = 64 * 1024


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2

    arg = sys.argv[1].strip()
    existing = safeio.read_json(CONFIG_FILE, MAX_CONFIG_BYTES)
    config = existing if isinstance(existing, dict) else {}

    if arg.lower() in ("auto", ""):
        config.pop("device_address", None)
    else:
        config["device_address"] = arg.upper()

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    safeio.atomic_write(CONFIG_FILE, json.dumps(config, indent=2) + "\n")
    print("selected:", config.get("device_address", "auto"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
