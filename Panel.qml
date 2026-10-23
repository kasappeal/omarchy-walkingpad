// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// WalkingPad BA04 popup: today's calories, GitHub-style history graph, goal
// editor and the collector enable/disable switch. All data comes from stats.py;
// the QML stays a dumb renderer over its JSON.
Panel {
  id: root
  moduleName: "io.github.kasappeal.omarchy-walkingpad-ba04"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color muted: Color.muted
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string homeDir: Quickshell.env("HOME")
  readonly property string backendDir: Qt.resolvedUrl("backend").toString().replace("file://", "")
  readonly property string venvPython: homeDir + "/.local/share/walkingpad-ba04/venv/bin/python"
  readonly property int goalCalories: Math.max(0, Number(setting("goalCalories", 0)) || 0)
  readonly property bool imperial: Model.useImperial(setting("units", "Metric"), Qt.locale().name)
  readonly property int gridWeeks: 15
  readonly property int cellSize: 18
  readonly property int cellSpacing: 3
  readonly property int cellPitch: cellSize + cellSpacing
  readonly property int gridPixelWidth: gridWeeks * cellSize + (gridWeeks - 1) * cellSpacing
  readonly property int monthStripHeight: Math.ceil(Style.font.caption * 1.4)

  property var stats: null
  property var cells: []
  property var months: []
  readonly property var weekdays: Model.weekdayLabels()
  property var hovered: null
  property int prevTodayCalories: 0
  property int celebratedCalories: 0
  property double nowMs: Date.now()

  // ---- Belt control state
  // Controls appear only while the collector holds a live connection, so there
  // is never a dead button that looks like it should have worked.
  readonly property bool canControl: root.enabled && !!(stats && stats.connected)
  readonly property bool beltMoving: !!(stats && stats.walking)
  readonly property string speedText: {
    if (!stats || !stats.live) return ""
    return Model.fmtSpeed(Number(stats.live.target_speed) || 0, imperial)
  }
  readonly property var controlButtons: [
    {action: beltMoving ? "stop" : "start", label: beltMoving ? "Stop" : "Start"},
    {action: "speed_down", label: "−"},
    {action: "speed_up", label: "+"},
  ]
  // A command the pad could not honour, shown briefly next to the buttons.
  readonly property string commandNote: {
    var cmd = stats && stats.live ? stats.live.last_command : null
    if (!cmd || cmd.ok) return ""
    if (nowMs / 1000 - Number(cmd.at) > 15) return ""
    return String(cmd.error || "refused")
  }
  property bool padPickerOpen: false

  readonly property bool enabled: stats ? stats.enabled === true : false
  readonly property bool connected: stats ? stats.connected === true : false
  readonly property bool walking: stats ? stats.walking === true : false
  readonly property int todayCalories: stats ? Number(stats.today.calories) || 0 : 0
  readonly property bool goalMet: goalCalories > 0 && todayCalories >= goalCalories
  readonly property real goalProgress: goalCalories > 0 ? Math.min(1, todayCalories / goalCalories) : 0

  // Popup status line, in pad-state priority order.
  readonly property string statusLine: {
    if (!stats) return ""
    if (!connected) return "Pad off, collector watching"
    var live = stats.live
    var state = live ? String(live.state || "") : ""
    if (live && (state === "running" || walking)) {
      return "Walking · " + Model.fmtSpeed(live.speed, imperial) + " · "
        + Model.fmtDist(live.dist_m, imperial) + " · " + Model.fmtTime(sessionSeconds)
    }
    if (state === "paused") return "Paused · belt held"
    if (state === "countdown") return "Starting…"
    return "Pad on, belt idle"
  }
  readonly property int sessionSeconds: {
    if (!walking || !stats || !stats.live) return 0
    // The pad's own duration counter: unlike wall-clock time since we opened a
    // session, it survives the belt stopping and a collector restart, and it is
    // the number printed on the pad itself.
    return Number(stats.live.time_s) || 0
  }

  readonly property string barText: {
    var icon = walking ? "󰜎" : "󰈸"
    if (!enabled) return icon
    if (goalCalories > 0) return icon + " " + Model.fmt(todayCalories) + "/" + Model.fmt(goalCalories)
    return icon + " " + Model.fmt(todayCalories)
  }

  readonly property string tooltipText: {
    if (!stats) return "WalkingPad BA04"
    if (!enabled) return "WalkingPad BA04 collector is off\nClick: details · Right-click: enable"
    var text = "WalkingPad BA04 · " + Model.fmt(todayCalories) + " kcal today"
    if (goalCalories > 0) text += " (goal " + Model.fmt(goalCalories) + " kcal)"
    if (walking) text += "\nWalking now"
    return text + "\nClick: details · Right-click: " + (enabled ? "disable" : "enable")
  }

  function alpha(color, opacity) {
    return Qt.rgba(color.r, color.g, color.b, opacity)
  }

  function open() {
    root.controller.show()
    root.refresh()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function refresh() {
    if (!statsProc.running) statsProc.running = true
  }

  function sendControl(action) {
    controlProc.command = [root.venvPython, root.backendDir + "/control.py", action]
    controlProc.running = true
  }

  function toggleEnabled() {
    toggleProc.command = ["systemctl", "--user", enabled ? "stop" : "start", "walkingpad-ba04.service"]
    toggleProc.running = true
  }

  function saveGoal(text) {
    var n = parseInt(text, 10)
    if (isNaN(n) || n < 0) n = 0
    setGoalProc.command = ["omarchy-shell", "shell", "setBarWidget", "io.github.kasappeal.omarchy-walkingpad-ba04", "goalCalories", String(n), "{}"]
    setGoalProc.running = true
  }

  function selectPad(address) {
    selectProc.command = [root.venvPython, root.backendDir + "/select-pad.py", address]
    selectProc.running = true
    padPickerOpen = false
  }

  function padDisplayName() {
    if (!stats) return ""
    if (stats.live && stats.live.device_name) return stats.live.device_name
    var selected = String(stats.selected_address || "")
    var devices = stats.devices || []
    for (var i = 0; i < devices.length; i++)
      if (String(devices[i].address) === selected) return devices[i].name || devices[i].address
    return selected || ""
  }

  function padStateText() {
    if (!enabled) return "collector off"
    if (connected) return "connected"
    return "scanning..."
  }

  onTodayCaloriesChanged: {
    // Celebrate only a fresh crossing: a previous reading below the goal must
    // exist, so opening the bar with the goal already met stays quiet. Capture
    // the count now so the toast cannot race a later poll and under-report.
    if (goalCalories > 0 && prevTodayCalories > 0 && prevTodayCalories < goalCalories && todayCalories >= goalCalories) {
      root.celebratedCalories = todayCalories
      celebrateProc.running = true
    }
    prevTodayCalories = todayCalories
  }

  Process {
    id: statsProc
    command: [root.venvPython, root.backendDir + "/stats.py", "--goal", String(root.goalCalories)]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var raw = String(text || "").trim()
        if (!raw) return
        try {
          root.stats = JSON.parse(raw)
          root.nowMs = Date.now()
          root.cells = Model.buildCells(root.stats.start, root.stats.days, root.goalCalories, root.gridWeeks)
          root.months = Model.monthLabels(root.stats.start, root.gridWeeks)
        } catch (e) {
          console.warn("walkingpad-ba04: stats parse failed: " + e)
        }
      }
    }
  }

  Process { id: toggleProc; onExited: refreshDelay.start() }
  Process { id: controlProc; onExited: refreshDelay.start() }
  Process { id: selectProc; onExited: refreshDelay.start() }
  Process { id: setGoalProc }
  Process {
    id: celebrateProc
    // Clicking the toast opens the popup (the shell keeps and runs the
    // command, so this survives shell restarts).
    command: ["omarchy-notification-send", "-g", "󰈸",
      "--exec", "omarchy-shell shell summon io.github.kasappeal.omarchy-walkingpad-ba04 '{}'",
      "Calorie goal reached", Model.fmt(root.celebratedCalories) + " kcal today"]
  }

  Timer {
    id: refreshDelay
    interval: 800
    onTriggered: root.refresh()
  }

  Timer {
    interval: root.walking || root.opened ? 3000 : 15000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: goalField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroll.width
          spacing: Style.space(14)
          topPadding: Style.space(6)
          bottomPadding: Style.space(6)

          // ---- Today: big count, goal progress, streak and totals.
          Item {
            width: parent.width
            height: Math.max(todayLeft.height, todayRight.height)

            Row {
              id: todayLeft
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(12)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.walking ? "󰜎" : "󰈸"
                color: root.goalMet ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: 44
              }

              Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2

                Row {
                  spacing: Style.space(6)
                  Text {
                    text: Model.fmt(root.todayCalories)
                    color: root.goalMet ? root.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: 36
                    font.bold: true
                  }
                  Text {
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 6
                    text: root.goalCalories > 0 ? "/ " + Model.fmt(root.goalCalories) : "kcal today"
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Text {
                  visible: root.enabled
                  text: root.statusLine
                  color: root.walking ? root.accent : root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  visible: !root.enabled
                  text: "Collector is off, no data is being recorded"
                  color: root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Column {
              id: todayRight
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
              spacing: 2

              Text {
                anchors.right: parent.right
                text: root.stats && root.stats.streak > 0 ? "󰔵 " + root.stats.streak + " day streak" : ""
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                visible: text !== ""
              }
              Text {
                anchors.right: parent.right
                text: root.stats ? Model.fmtKcal(root.stats.totals.calories) + " all time" : ""
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              Text {
                anchors.right: parent.right
                text: root.stats ? Model.fmtDist(root.stats.totals.dist_m, root.imperial) + " · " + Model.fmtTime(root.stats.totals.time_s) : ""
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ---- Goal progress bar.
          Rectangle {
            visible: root.goalCalories > 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            height: 6
            radius: 3
            color: root.alpha(root.foreground, 0.12)

            Rectangle {
              height: parent.height
              width: parent.width * root.goalProgress
              radius: 3
              color: root.accent
              Behavior on width { NumberAnimation { duration: 250 } }
            }
          }

          // ---- History graph.
          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(16)
            text: "Last " + root.gridWeeks + " weeks"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 6

            // Weekday labels (Mon/Wed/Fri), aligned with the cell rows.
            Column {
              spacing: root.cellSpacing
              topPadding: root.monthStripHeight + root.cellSpacing

              Repeater {
                model: root.weekdays
                delegate: Text {
                  width: 32
                  height: root.cellSize
                  verticalAlignment: Text.AlignVCenter
                  horizontalAlignment: Text.AlignRight
                  text: modelData
                  color: root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Column {
              spacing: root.cellSpacing

              // Month strip: a label at the first column of each new month.
              Item {
                width: root.gridPixelWidth
                height: root.monthStripHeight

                Repeater {
                  model: root.months
                  delegate: Text {
                    x: modelData.col * root.cellPitch
                    anchors.bottom: parent.bottom
                    text: modelData.label
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Grid {
                columns: root.gridWeeks
                rows: 7
                spacing: root.cellSpacing
                flow: Grid.LeftToRight

                Repeater {
                  model: root.cells
                  delegate: Rectangle {
                    width: root.cellSize
                    height: root.cellSize
                    radius: 3
                    color: modelData.state === 2 ? root.accent
                      : modelData.state === 1 ? root.alpha(root.accent, 0.3)
                      : modelData.state === 3 ? root.alpha(root.foreground, 0.05)
                      : root.alpha(root.foreground, 0.12)
                    border.color: modelData.today ? root.accent : "transparent"
                    border.width: modelData.today ? 1 : 0

                    MouseArea {
                      anchors.fill: parent
                      hoverEnabled: true
                      onEntered: root.hovered = modelData
                      onExited: if (root.hovered === modelData) root.hovered = null
                    }
                  }
                }
              }
            }
          }

          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(16)
            // Fixed two-line height so the metrics line on hover does not
            // resize the popup.
            height: Math.ceil(Style.font.caption * 2.5)
            verticalAlignment: Text.AlignTop
            lineHeight: 1.25
            text: root.hovered ? Model.dayLabel(root.hovered, root.goalCalories, root.imperial)
              : root.goalCalories > 0 ? "Bright cells are days the goal was met"
              : "Set a daily goal below to light up goal days"
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // ---- Pad picker.
          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.alpha(root.foreground, 0.12)
          }

          Column {
            id: padSection
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            spacing: Style.space(4)

            Rectangle {
              width: padSection.width
              height: 30
              radius: 6
              color: padHeaderMouse.containsMouse ? root.alpha(root.foreground, 0.10) : "transparent"

              Text {
                anchors.left: parent.left
                anchors.leftMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: "󰜎 " + (root.padDisplayName() || "Walking pad")
                textFormat: Text.PlainText  // BLE-controlled device name
                color: root.connected ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                anchors.right: padChevron.left
                anchors.rightMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                text: root.padStateText()
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              Text {
                id: padChevron
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: root.padPickerOpen ? "▴" : "▾"
                color: root.muted
                font.pixelSize: Style.font.caption
              }
              MouseArea {
                id: padHeaderMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.padPickerOpen = !root.padPickerOpen
              }
            }

            Column {
              id: padList
              visible: root.padPickerOpen
              width: padSection.width
              spacing: 3

              Rectangle {
                width: padList.width
                height: 26
                radius: 6
                color: autoMouse.containsMouse ? root.alpha(root.accent, 0.18) : root.alpha(root.foreground, 0.05)

                Text {
                  anchors.left: parent.left
                  anchors.leftMargin: 8
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Auto (strongest signal)"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  anchors.right: parent.right
                  anchors.rightMargin: 8
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.stats && String(root.stats.selected_address || "") === "" ? "✓" : ""
                  color: root.accent
                  font.pixelSize: Style.font.bodySmall
                }
                MouseArea {
                  id: autoMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.selectPad("auto")
                }
              }

              Repeater {
                model: root.stats ? root.stats.devices : []
                delegate: Rectangle {
                  width: padList.width
                  height: 26
                  radius: 6
                  property bool selected: String(modelData.address) === String(root.stats.selected_address || "")
                  color: devMouse.containsMouse ? root.alpha(root.accent, 0.18) : root.alpha(root.foreground, 0.05)

                  Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: (modelData.name || modelData.address) + (modelData.protocol ? " · " + modelData.protocol : "")
                    textFormat: Text.PlainText  // BLE-controlled name/protocol
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    anchors.right: devCheck.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: (modelData.rssi || "") + " dBm · " + Model.fmtAge(modelData.last_seen)
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    id: devCheck
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: selected ? "✓" : ""
                    color: root.accent
                    font.pixelSize: Style.font.bodySmall
                  }
                  MouseArea {
                    id: devMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.selectPad(modelData.address)
                  }
                }
              }
            }
          }

          // ---- Belt control. The widget never touches Bluetooth itself: it
          // drops a request that the collector answers in place of its next
          // heartbeat, because the pad serves one connection at a time.
          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            visible: root.canControl
            color: root.alpha(root.foreground, 0.12)
          }

          Row {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            spacing: Style.space(10)
            visible: root.canControl

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: root.speedText + (root.beltMoving ? " · belt moving" : " · belt idle")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Repeater {
              model: root.controlButtons
              delegate: Rectangle {
                id: controlButton
                required property var modelData
                anchors.verticalCenter: parent.verticalCenter
                width: controlLabel.implicitWidth + 20
                height: 28
                radius: 6
                color: controlMouse.containsMouse
                  ? root.alpha(root.accent, 0.25)
                  : root.alpha(root.accent, 0.12)

                Text {
                  id: controlLabel
                  anchors.centerIn: parent
                  text: controlButton.modelData.label
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                MouseArea {
                  id: controlMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.sendControl(controlButton.modelData.action)
                }
              }
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              visible: root.commandNote !== ""
              text: root.commandNote
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // ---- Goal editor and collector switch.
          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.alpha(root.foreground, 0.12)
          }

          Item {
            width: parent.width
            height: footerRow.height

            Row {
              id: footerRow
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              spacing: Style.space(10)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Daily calorie goal"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              TextField {
                id: goalField
                width: 90
                anchors.verticalCenter: parent.verticalCenter
                placeholderText: root.goalCalories > 0 ? String(root.goalCalories) : "200"
                text: ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 0; top: 10000 }
                onAccepted: {
                  root.saveGoal(text)
                  text = ""
                  focus = false
                }
                background: Rectangle {
                  radius: 6
                  color: root.alpha(root.foreground, goalField.activeFocus ? 0.16 : 0.08)
                  border.color: goalField.activeFocus ? root.accent : "transparent"
                  border.width: 1
                }
              }

              Item { width: 1; height: 1 }

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: toggleLabel.implicitWidth + 20
                height: 28
                radius: 6
                color: toggleMouse.containsMouse ? root.alpha(root.accent, 0.25) : root.alpha(root.accent, 0.12)

                Text {
                  id: toggleLabel
                  anchors.centerIn: parent
                  text: root.enabled ? "Disable collector" : "Enable collector"
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                MouseArea {
                  id: toggleMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.toggleEnabled()
                }
              }
            }
          }
        }
      }
    }
  }
}
