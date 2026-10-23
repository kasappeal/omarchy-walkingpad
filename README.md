# BA04 Threadmill widget for Omarchy

Superun BA04 calorie count in your Omarchy bar: live counter, daily goal
tracking, and a GitHub-style history graph. All data stays on your machine.

![bar](docs/bar.png)
![popup](docs/popup.png)

## Features

- **Auto-connect**: a lightweight daemon idles in a BLE scan loop and connects
  the moment your threadmill powers on. No pairing rituals, no app.
- **Live counter in the bar**: today's calories (or `current/goal` once you set
  a goal), run icon while you walk, theme accent color when the goal is met,
  dimmed when the collector is off.
- **History popup** (left-click): today's progress, live session stats
  (speed / distance / session time), a 15-week contribution graph where goal
  days glow in your theme's accent color, hover any day for its exact count,
  streak and all-time totals.
- **Belt control**: Start, Stop and ±0.1 km/h buttons in the popup. Start
  always begins at 1.0 km/h (the lowest speed this firmware accepts), speed
  changes are clamped to the ceiling the pad itself reports, and a command
  that arrives while the pad is unreachable is dropped rather than queued, so
  the belt never starts after you have already walked away. The pad serves one
  BLE client at a time, so controlling it means the collector is connected and
  the phone app is not.
- **Daily calorie goal**: set it in the popup; crossing it mid-walk fires a
  desktop notification.
- **Enable/disable switch**: right-click the widget (or use the popup button)
  to start/stop the collector. Stopping it ends all BLE scanning and frees the
  threadmill for the phone app.
- **Pad picker**: several BA04 pads nearby? Pick yours from the popup, or stay
  in auto mode (strongest signal wins).
- **Crash-safe local history**: every status notification is recorded as it
  arrives, and each day is compacted into per-day stats at day rollover. The
  pad's own memory is volatile, so this is what protects your sessions.

## Supported pads

Any Superun BA04-protocol threadmill over the `0xFFFF` / `0xFF01` / `0xFF02`
GATT service — confirmed against a **PitPat-T01** (firmware 27), and the
protocol is shared with other BA04-branded pads (they advertise under various
names; use the pad picker or `device_address` if yours is unnamed).

This is a fork of
[msegoviadev/omarchy-walkingpad](https://github.com/msegoviadev/omarchy-walkingpad),
which drives KingSmith WalkingPads. The BA04 speaks a different protocol and
**does not count steps**, so the primary metric here is calories.

The BA04 protocol layer follows the reverse engineering published in
[scphyle/ha-pitpat-treadmill](https://github.com/scphyle/ha-pitpat-treadmill)
and [sirfergy/HomeAssistantWalkingPad](https://github.com/sirfergy/HomeAssistantWalkingPad)
(both GPL-3.0), which in turn cross-check against
[cagnulein/qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift) and
[azmke/pitpat-treadmill-control](https://github.com/azmke/pitpat-treadmill-control).
See LICENSE for how that interacts with this repo's license.

## Requirements

- Omarchy (Arch + Hyprland + the omarchy-shell Quickshell bar)
- A Bluetooth adapter (`bluez`, present on any Omarchy install)
- Python 3.10+

## Install

```sh
omarchy plugin add https://github.com/kasappeal/omarchy-walkingpad.git --enable
```

Then set up the collector backend (Python venv + systemd **user** service):

```sh
~/.config/omarchy/plugins/io.github.kasappeal.omarchy-walkingpad-ba04/backend/install.sh
```

The installer creates a venv in `~/.local/share/walkingpad-ba04/`, installs the
BLE library there, and registers `walkingpad-ba04.service` so the collector
starts at login. Everything runs unprivileged as your user; no data leaves the
machine.

## Updating

```sh
omarchy plugin update io.github.kasappeal.omarchy-walkingpad-ba04 --yes
```

Then tell the two halves of the widget to pick the new files up, because the
shell loads its QML/JS once at startup (it runs with `QS_DISABLE_FILE_WATCHER=1`)
and the collector is a separate service:

| You changed | Apply with |
|-------------|------------|
| `Panel.qml`, `BarWidget.qml`, `Model.js`, `manifest.json` | `omarchy restart shell` |
| `backend/*.py` | `systemctl --user restart walkingpad-ba04` |

Without the shell restart the bar keeps rendering the previous code from
memory, so an interface change can look like it did not take effect even
though the file on disk is correct.

Do not use `omarchy refresh shell` for this: despite the name it resets
`~/.config/omarchy/shell.json` to factory defaults, which wipes your bar
layout and widget settings.

## Usage

| Action | Result |
|--------|--------|
| Left-click widget | Open/close the history popup |
| `Start` / `Stop` in the popup | Start the belt at 1.0 km/h / end the workout |
| `−` / `+` in the popup | Step the target speed by 0.1 km/h |
| Right-click widget | Enable/disable the collector |
| Middle-click widget | Refresh stats |
| Walk | Everything else happens by itself |

## Configure

Set your daily calorie goal from the popup, or inline in
`~/.config/omarchy/shell.json`:

```json
{ "id": "io.github.kasappeal.omarchy-walkingpad-ba04", "goalCalories": 300, "units": "Metric" }
```

`units` accepts `Metric` (the default: km and km/h), `Imperial` (mph and
feet/miles), or `Auto` (follows your system locale: US English gets imperial).
All data is stored in metric; the setting only changes display.

Your pad's own panel can independently be set to mph. That does not affect
storage or the widget: the collector reads the pad's unit flag and normalises to
metric, so a metric `units` setting always shows km and km/h even when the pad's
display says mph. Keep the pad's panel on km/h if you want it to agree with the
bar. Some BA04 panels can themselves be switched to mph; the
collector reads the pad's unit flag and converts, so the setting is independent
of the panel.

Move the widget between bar sections:

```sh
omarchy bar move io.github.kasappeal.omarchy-walkingpad-ba04 --section center
```

## Remove

```sh
omarchy plugin remove io.github.kasappeal.omarchy-walkingpad-ba04
systemctl --user disable --now walkingpad-ba04
rm ~/.config/systemd/user/walkingpad-ba04.service && systemctl --user daemon-reload
rm -rf ~/.local/share/walkingpad-ba04   # deletes your calorie history
```

## How it works

```
pad (BLE) -> collector.py (systemd user service)
                -> ~/.local/share/walkingpad-ba04/history.jsonl  (today's
                   records; older days compact into rollup.json and are dropped)
                -> ~/.local/share/walkingpad-ba04/status.json    (live state)
                -> ~/.local/share/walkingpad-ba04/devices.json   (seen pads)
                -> ~/.local/share/walkingpad-ba04/rollup.json    (per-day stats,
                   rebuilt at startup and day rollover)
ba04.py   -> the Superun BA04 wire protocol (framing, checksum, decoding)
stats.py  -> merges rollup + today's raw tail into today/days/streak/totals JSON
Panel.qml -> renders the bar widget and popup from that JSON
```

Only one BLE client can be connected to the pad at a time: while the collector
is connected, the vendor phone app cannot connect (and vice versa). Stop the
collector if you want to use the app.

## About the BA04 protocol

The pad pushes roughly one status packet per second while connected, and each
packet is answered with a small poll packet — it drops the GATT link after
10-20 s if you stop answering. Reconnects are therefore normal and are handled
silently; the widget does not flash "disconnected" for them.

Three consequences worth knowing:

- **Calories are a whole-kcal counter.** On a slow walk the pad adds roughly
  1 kcal per minute, so a very short session can legitimately show 0 kcal even
  though you covered distance. Distance and time are tracked alongside, so
  those sessions still appear in the popup's session stats.
- **Counters are per-workout, and the counters themselves define the workout.**
  The pad restarts calories/distance/time at zero when a new workout begins, so
  a counter going backwards is the only reliable signal that a workout ended.
  Everything else splits a workout without ending it, and the widget counts
  deltas instead: a pause holds the counters, a belt that stops because nobody
  stands on it resumes them (measured: 43 s continued to 65 s), and restarting
  the collector changes nothing on the pad. Your totals therefore match the
  number on the pad's own display, workout by workout.
- **No pad memory.** KingSmith pads could report a last-run summary, so a
  workout walked while the collector was off could be recovered. The BA04 has
  no such readout: sessions are only saved while the collector runs.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

Two lineages meet in this repo, and the license has to satisfy both:

- The KingSmith bar widget this forks was MIT-licensed. That text is kept
  verbatim in [LICENSE.upstream-MIT.txt](LICENSE.upstream-MIT.txt) as the MIT
  terms require, and MIT is GPL-compatible, so the combined work can be GPL.
- The BA04 protocol layer follows reverse-engineering published in
  [scphyle/ha-pitpat-treadmill](https://github.com/scphyle/ha-pitpat-treadmill)
  and [sirfergy/HomeAssistantWalkingPad](https://github.com/sirfergy/HomeAssistantWalkingPad),
  both GPL-3.0. Their framing, byte offsets and unit findings are re-implemented
  here with attribution above, so this repo is GPL-3.0-or-later rather than MIT.

Every source file carries an `SPDX-License-Identifier: GPL-3.0-or-later` header.

## Troubleshooting

```bash
journalctl --user -u walkingpad-ba04 -f     # collector logs
systemctl --user status walkingpad-ba04     # service state
omarchy-shell shell summon io.github.kasappeal.omarchy-walkingpad-ba04 '{}'   # open popup
omarchy-shell shell hide io.github.kasappeal.omarchy-walkingpad-ba04          # close it
```

- **Widget shows 0 / pad off**: the pad only advertises while powered on. Turn
  it on and watch the logs; "Found pad" should appear within ~15 s.
- **Pad not detected**: if your pad advertises an unusual name, pick it from
  the popup's pad list, or add `"device_address": "AA:BB:CC:DD:EE:FF"` to
  `~/.config/walkingpad-ba04/config.json` by hand.
- **Bar stays dimmed**: the collector service is not running; start it with
  `systemctl --user start walkingpad-ba04` or right-click the widget.
