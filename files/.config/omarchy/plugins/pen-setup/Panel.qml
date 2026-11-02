import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Pen Setup — live control for the Wacom pen daemon.
//
// Everything is applied by `~/.local/bin/wacom-pen-ctl`, which talks to the
// `wacom-pen.service` daemon over a UNIX socket. The daemon owns the pen and
// the tablet pad, so changing mode, a button mapping, the active area, or a
// per-app rule takes effect in milliseconds without restarting a unit.
Panel {
  id: root
  moduleName: "pen-setup"
  ipcTarget: "pen-setup"
  manageIpc: false

  // The installed plugin's own folder, used to read its manifest version.
  readonly property string pluginId: "pen-setup"
  readonly property string manifestPath:
    (Quickshell.env("HOME") || "") + "/.config/omarchy/plugins/" + pluginId + "/manifest.json"

  // Installed version, read from the plugin manifest so it always matches the
  // build that is actually installed (a release bump changes manifest.json once).
  property string version: ""

  FileView {
    id: manifestView
    path: root.manifestPath
    watchChanges: true
    printErrors: false
    onLoaded: {
      try {
        var m = JSON.parse(String(manifestView.text() || "{}"))
        if (m && m.version) root.version = String(m.version)
      } catch (e) {
        root.version = ""
      }
    }
  }

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string glyphPen: String.fromCodePoint(0xF040)
  readonly property string glyphRefresh: String.fromCodePoint(0xF0450)
  readonly property string glyphPlus: String.fromCodePoint(0xF0415)
  readonly property string glyphClose: String.fromCodePoint(0xF0156)

  property var st: ({})
  property bool busy: false
  property string notice: ""

  readonly property bool daemonUp: st.daemon === true
  readonly property bool enabled: st.enabled === true
  readonly property string mode: st.mode || "mouse"
  readonly property string effective: st.effectiveMode || (daemonUp ? "?" : "off")
  readonly property string activeClass: st.activeClass || ""
  readonly property bool autoSwitch: st.auto === true
  readonly property bool padPresent: st.padPresent === true

  // Standby: the daemon is running and the plugin is enabled, but no tablet is
  // connected. The daemon waits for a tablet and re-attaches automatically —
  // the panel just reflects that and re-renders the new tablet's layout.
  readonly property bool tabletConnected: st.penPresent === true || st.padPresent === true
  readonly property bool standby: daemonUp && enabled && !tabletConnected

  // Tablet layout is read from the live daemon status, so the panel renders the
  // express keys and touch strips of whatever tablet is actually connected
  // instead of a fixed model. The "unknown" fallbacks (no daemon yet) draw a
  // full 8-key / 2-strip pad so settings stay editable.
  readonly property int padButtonCount: {
    var n = Number(st.padButtons)
    if (root.padPresent) return isFinite(n) && n > 0 ? n : 0
    return isFinite(n) && n > 0 ? n : 8
  }
  readonly property int expressPerColumn: Math.ceil(padButtonCount / 2)
  readonly property int stripCount: {
    var n = Number(st.strips)
    if (n === 0) return 0
    return isFinite(n) && n > 0 ? 2 : 2
  }
  readonly property string tabletName: {
    if (standby) return ""
    var n = String(st.penName || st.padName || "").trim()
    return n !== "" ? n : "Wacom tablet"
  }

  // Other pad controls the connected tablet may report: a ring/dial and any
  // keyboard-style buttons beyond the express keys.
  readonly property bool ringPresent: Number(st.ring) > 0
  readonly property var ringMappings: {
    var b = st.buttons || ({})
    return root.ringPresent
      ? [{ id: "ring:0", label: "Ring · turn", action: b["ring:0"] || "none" }]
      : []
  }
  readonly property var ringButtonMappings: {
    var arr = st.ringButtons || []
    if (!Array.isArray(arr)) return []
    var b = st.buttons || ({})
    var out = []
    for (var i = 0; i < arr.length; i++) {
      out.push({
        id: arr[i],
        label: arr.length === 1 ? "Ring button" : "Ring button " + (i + 1),
        action: b[arr[i]] || "none"
      })
    }
    return out
  }
  readonly property var extraButtons: {
    var arr = st.extraButtons || []
    if (!Array.isArray(arr)) return []
    var b = st.buttons || ({})
    var out = []
    for (var i = 0; i < arr.length; i++) {
      out.push({
        id: arr[i],
        label: "Button · " + String(arr[i]).slice(6),
        action: b[arr[i]] || "none"
      })
    }
    return out
  }

  // Tablet active-area crop (normalized 0..1 of the physical surface).
  property var area: ({ x: 0, y: 0, w: 1, h: 1 })
  property bool areaDragging: false
  property bool areaDirty: false
  property var areaDragStart: null

  // Which mapping row (if any) has its keyboard-shortcut editor open.
  property string editingButton: ""
  // Briefly highlighted when its express key is clicked on the drawing.
  property string highlightButton: ""
  readonly property bool keyEditing: editingButton !== ""

  // Rules rendered in insertion order (Python's JSON object keeps it).
  readonly property var ruleRows: {
    var rules = st.rules || ({})
    var out = []
    for (var key in rules) out.push({ pattern: key, mode: rules[key] })
    return out
  }

  readonly property var penMappings: {
    var b = st.buttons || ({})
    return [
      { id: "tip", label: "Pen tip", action: b["tip"] || "none" },
      { id: "stylus", label: "Lower side button", action: b["stylus"] || "none" },
      { id: "stylus2", label: "Upper side button", action: b["stylus2"] || "none" }
    ]
  }

  readonly property var padMappings: {
    var b = st.buttons || ({})
    var out = []
    for (var i = 0; i < root.padButtonCount; i++) {
      var key = "pad:" + i
      out.push({ id: key, label: "Express key " + (i + 1), action: b[key] || "none" })
    }
    return out
  }

  readonly property var stripMappings: {
    var b = st.buttons || ({})
    var out = []
    for (var i = 0; i < root.stripCount; i++) {
      out.push({
        id: "strip:" + i,
        label: i === 0 ? "Touch strip · left" : "Touch strip · right",
        action: b["strip:" + i] || "none"
      })
    }
    return out
  }

  readonly property real stripSensitivity: {
    var v = Number(st.stripSensitivity)
    return isNaN(v) || v <= 0 ? 1 : v
  }

  readonly property string modeLabel: {
    if (!daemonUp) return "DAEMON NOT RUNNING"
    if (!enabled) return "OFF · NATIVE TABLET"
    if (standby) return "STANDBY · WAITING FOR TABLET"
    return effective === "tablet" ? "PRESSURE MODE" : "MOUSE MODE"
  }

  readonly property string icon: glyphPen

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v))
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function runAction(args) {
    if (actionProc.running) return
    busy = true
    notice = ""
    actionProc.command = ["wacom-pen-ctl"].concat(args)
    actionProc.running = true
  }

  function setAuto(value) {
    runAction(["auto", value ? "on" : "off"])
  }

  function toggleRule(row) {
    if (!row) return
    runAction(["set-rule", row.pattern, row.mode === "tablet" ? "mouse" : "tablet"])
  }

  function removeRule(row) {
    if (!row) return
    runAction(["del-rule", row.pattern])
  }

  function addCurrentApp() {
    runAction(["add-app", mode === "mouse" ? "tablet" : "mouse"])
  }

  function toggleMouse() {
    runAction(["set-mode", enabled && mode === "mouse" ? "native" : "mouse"])
  }

  function togglePressure() {
    runAction(["set-mode", enabled && mode === "tablet" ? "native" : "tablet"])
  }

  function prettyAction(action) {
    if (!action || action === "none") return "Do nothing"
    if (action.indexOf("mouse:") === 0) return action.slice(6) + " click"
    if (action.indexOf("key:") === 0) return "Key: " + action.slice(4)
    if (action === "scroll:vertical") return "Scroll up / down"
    if (action === "scroll:vertical-inv") return "Scroll down / up"
    if (action === "scroll:horizontal") return "Scroll right / left"
    if (action === "scroll:horizontal-inv") return "Scroll left / right"
    return action
  }

  // --------------------------------------------------------------- area

  // Work in window coordinates: the crop rect (and its handles) move while
  // dragging, so mapping into them would cancel the pointer delta.
  function areaPoint(item, mx, my) {
    var p = item.mapToItem(null, mx, my)
    var origin = areaSurface.mapToItem(null, 0, 0)
    return {
      x: clamp((p.x - origin.x) / areaSurface.width, 0, 1),
      y: clamp((p.y - origin.y) / areaSurface.height, 0, 1)
    }
  }

  function beginAreaDrag(corner, item, mx, my) {
    var n = areaPoint(item, mx, my)
    areaDragStart = {
      corner: corner,
      x: area.x, y: area.y, w: area.w, h: area.h,
      nx: n.x, ny: n.y
    }
  }

  function updateAreaDrag(item, mx, my) {
    var s = areaDragStart
    if (!s) return
    var n = areaPoint(item, mx, my)
    var dx = n.x - s.nx
    var dy = n.y - s.ny
    var minW = 0.1
    var minH = 0.1
    var x = s.x, y = s.y, w = s.w, h = s.h

    if (s.corner === "move") {
      x = clamp(s.x + dx, 0, 1 - s.w)
      y = clamp(s.y + dy, 0, 1 - s.h)
    } else {
      if (s.corner.indexOf("l") >= 0) {
        x = clamp(s.x + dx, 0, s.x + s.w - minW)
        w = s.x + s.w - x
      }
      if (s.corner.indexOf("r") >= 0) {
        w = clamp(s.w + dx, minW, 1 - s.x)
      }
      if (s.corner.indexOf("t") >= 0) {
        y = clamp(s.y + dy, 0, s.y + s.h - minH)
        h = s.y + s.h - y
      }
      if (s.corner.indexOf("b") >= 0) {
        h = clamp(s.h + dy, minH, 1 - s.y)
      }
    }
    area = { x: x, y: y, w: w, h: h }
    areaDragging = true
  }

  function endAreaDrag() {
    if (!areaDragStart) return
    areaDragStart = null
    areaDragging = false
    areaDirty = true
    runAction([
      "set-area",
      area.x.toFixed(4), area.y.toFixed(4),
      area.w.toFixed(4), area.h.toFixed(4)
    ])
  }

  function resetArea() {
    area = { x: 0, y: 0, w: 1, h: 1 }
    areaDirty = true
    runAction(["reset-area"])
  }

  function mappedActionFor(id) {
    return (st.buttons || ({}))[id] || "none"
  }

  function focusMapping(id) {
    highlightButton = id
    highlightTimer.restart()
    var target = id.indexOf("pad:") === 0 ? padMappingColumn
      : (id.indexOf("strip:") === 0 ? stripMappingColumn
      : (id.indexOf("ring:") === 0 || id.indexOf("ring-btn:") === 0 ? ringMappingColumn
      : (id.indexOf("extra:") === 0 ? extraMappingColumn : mappingColumn)))
    flick.contentY = clamp(target.y - Style.spacing.md, 0,
                           Math.max(0, flick.contentHeight - flick.height))
  }

  function applyAreaFromStatus(parsed) {
    var a = parsed.area || ({})
    var next = {
      x: Number(a.x || 0),
      y: Number(a.y || 0),
      w: Number(a.w === undefined ? 1 : a.w),
      h: Number(a.h === undefined ? 1 : a.h)
    }
    var same = Math.abs(next.x - area.x) < 0.001
      && Math.abs(next.y - area.y) < 0.001
      && Math.abs(next.w - area.w) < 0.001
      && Math.abs(next.h - area.h) < 0.001
    if (!areaDragging && (!areaDirty || same)) {
      area = next
      areaDirty = false
    }
  }

  Component.onCompleted: refresh()

  onOpenedChanged: {
    if (opened) {
      areaDirty = false
      editingButton = ""
      refresh()
    }
  }

  Timer {
    interval: 1500
    repeat: true
    running: root.opened
    onTriggered: root.refresh()
  }

  // Background check while the panel is closed, so the bar icon flips to its
  // "standby" look the moment the tablet is unplugged (and back when one is
  // plugged in again).
  Timer {
    interval: 3000
    repeat: true
    running: !root.opened
    onTriggered: root.refresh()
  }

  Process {
    id: statusProc
    command: ["wacom-pen-ctl", "status", "--json"]
    stdout: StdioCollector {
      id: statusOut
      waitForEnd: true
    }
    onExited: function(code) {
      var raw = String(statusOut.text || "").trim()
      try {
        var parsed = JSON.parse(raw)
        if (parsed && parsed.ok) {
          root.st = parsed
          root.applyAreaFromStatus(parsed)
          if (parsed.daemon !== true)
            root.notice = "Pen daemon is not running"
        }
      } catch (e) {
        root.notice = "Could not read pen status"
      }
    }
  }

  Process {
    id: actionProc
    onExited: function(code) {
      root.busy = false
      if (code !== 0) root.notice = "Command failed (exit " + code + ")"
      refreshTimer.restart()
    }
  }

  Timer {
    id: refreshTimer
    interval: 250
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: highlightTimer
    interval: 2000
    repeat: false
    onTriggered: root.highlightButton = ""
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function mouse(): string { root.runAction(["set-mode", "mouse"]); return "ok" }
    function pressure(): string { root.runAction(["set-mode", "tablet"]); return "ok" }
    function off(): string { root.runAction(["set-mode", "native"]); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    active: root.enabled
    activeColor: root.standby ? root.urgent : root.accent
    tooltipText: {
      if (!root.daemonUp) return "Pen Setup · daemon not running"
      if (!root.enabled) return "Pen Setup · off (native tablet)"
      if (root.standby) return "Pen Setup · standby — waiting for a tablet"
      return "Pen Setup · " + (root.effective === "tablet" ? "pressure" : "mouse") +
        (root.activeClass ? " · " + root.activeClass : "")
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
      else if (buttonCode === Qt.RightButton) root.refresh()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(980))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.keyEditing
      onCloseRequested: root.close()

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.spacing.md

          // ------------------------------------------------------------- hero
          Item {
            width: parent.width
            implicitHeight: Math.max(heroGlyph.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroGlyph
              text: root.glyphPen
              color: root.foreground
              opacity: root.enabled ? 1.0 : 0.45
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroGlyph.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "Pen Setup"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                text: (root.version !== "" ? "Version " + root.version : "") +
                  (root.tabletName !== "" ? "  ·  " + root.tabletName : "")
                visible: root.version !== "" || root.daemonUp
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                text: root.modeLabel
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.1
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                visible: root.activeClass !== ""
                text: "Active app: " + root.activeClass
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                width: parent.width
              }
            }
          }

          // ------------------------------------------------------------- mode
          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "MODE"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Toggle {
            width: parent.width
            label: "Mouse mode"
            description: "Absolute pointer, no pressure. Super+drag move/resize works."
            checked: root.enabled && root.mode === "mouse"
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.toggleMouse()
          }

          Toggle {
            width: parent.width
            label: "Pressure mode"
            description: "Native tablet with pressure and tilt. Super+drag is unavailable."
            checked: root.enabled && root.mode === "tablet"
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.togglePressure()
          }

          Toggle {
            width: parent.width
            label: "Switch modes by app"
            description: root.autoSwitch
              ? "Rules are applied as focus changes."
              : "Rules are ignored; the mode above is used everywhere."
            checked: root.autoSwitch
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            onClicked: root.setAuto(!root.autoSwitch)
          }

          // --------------------------------------------------------- per-app
          PanelSeparator { foreground: root.foreground }

          Item {
            width: parent.width
            implicitHeight: perAppHeader.implicitHeight

            PanelSectionHeader {
              id: perAppHeader
              text: "PER-APPLICATION"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              anchors.right: parent.right
              anchors.verticalCenter: perAppHeader.verticalCenter
              text: root.ruleRows.length + (root.ruleRows.length === 1 ? " rule" : " rules")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Text {
            visible: root.ruleRows.length === 0
            text: "No per-app rules. Add the focused window below."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            width: parent.width
          }

          Repeater {
            model: root.ruleRows

            delegate: Row {
              id: ruleRow
              required property var modelData
              width: column.width
              spacing: Style.spacing.sm

              Toggle {
                width: ruleRow.width - ruleRemove.width - ruleRow.spacing
                label: modelData.pattern
                description: modelData.mode === "tablet"
                  ? "Pressure in this app"
                  : "Mouse in this app"
                checked: modelData.mode === "tablet"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                onClicked: root.toggleRule(modelData)
              }

              Item {
                width: ruleRemove.width
                height: ruleRow.height

                PanelActionButton {
                  id: ruleRemove
                  anchors.centerIn: parent
                  iconText: root.glyphClose
                  tooltipText: "Remove rule"
                  foreground: root.foreground
                  hoverColor: root.urgent
                  fontFamily: root.fontFamily
                  onClicked: root.removeRule(ruleRow.modelData)
                }
              }
            }
          }

          Button {
            width: parent.width
            text: root.activeClass !== ""
              ? "Add \u201c" + root.activeClass + "\u201d"
              : "Add focused app"
            iconText: root.glyphPlus
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            enabled: root.daemonUp && root.activeClass !== "" && !root.busy
            onClicked: root.addCurrentApp()
          }

          // ----------------------------------------------------------- tablet
          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "TABLET · ACTIVE AREA"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            visible: root.standby
            text: "Tablet disconnected — standing by. Plug in a tablet and it resumes automatically, with that tablet's buttons and area."
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            wrapMode: Text.WordWrap
            width: parent.width
          }

          Text {
            visible: !root.standby
            text: root.tabletName + " · drag the corners to choose the part of the tablet that covers the whole desktop."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            width: parent.width
          }

          // Generic pad: express keys (two columns) and touch strips are drawn
          // from the live tablet layout, plus the active surface with a
          // draggable crop rectangle.
          Item {
            id: tabletGraphic
            visible: !root.standby
            width: parent.width
            height: Math.max(186, 40 + root.expressPerColumn * 34)

            Rectangle {
              anchors.fill: parent
              radius: 14
              color: Style.controlFill(false, false, root.foreground, root.accent)
              border.width: 1
              border.color: Qt.darker(root.foreground, 1.9)
            }

            // express keys, drawn from the detected pad layout
            Repeater {
              model: root.padPresent ? root.padButtonCount : 0
              delegate: Rectangle {
                required property int index
                readonly property int columnIndex: index < root.expressPerColumn ? 0 : 1
                readonly property int rowIndex: index % root.expressPerColumn
                x: columnIndex === 0 ? 12 : tabletGraphic.width - 12 - width
                y: 28 + rowIndex * 34
                width: 22
                height: 26
                readonly property string keyId: "pad:" + index
                readonly property bool mapped: root.mappedActionFor(keyId) !== "none"
                radius: 4
                color: root.highlightButton === keyId
                  ? root.accent
                  : (mapped ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22)
                            : Style.controlFill(false, false, root.foreground, root.accent))
                border.width: 1
                border.color: mapped ? root.accent : Qt.darker(root.foreground, 1.7)

                Text {
                  anchors.centerIn: parent
                  text: (index + 1)
                  color: root.highlightButton === keyId
                    ? (root.bar ? root.bar.background : Color.background)
                    : (mapped ? root.accent : root.dim)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.focusMapping(parent.keyId)
                }
              }
            }

            // touch strips, only when the pad reports them
            Repeater {
              model: root.stripCount
              delegate: Rectangle {
                required property int index
                readonly property string keyId: "strip:" + index
                readonly property bool mapped: root.mappedActionFor(keyId) !== "none"
                x: index === 0 ? 40 : tabletGraphic.width - 48
                y: 26
                width: 8
                height: tabletGraphic.height - 52
                radius: 4
                color: root.highlightButton === keyId
                  ? root.accent
                  : (mapped ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.28)
                            : Style.controlFill(false, false, root.foreground, root.accent))
                border.width: 1
                border.color: mapped ? root.accent : Qt.darker(root.foreground, 1.7)

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.focusMapping(parent.keyId)
                }
              }
            }

            // touch ring / "roulette", shown when the tablet reports one
            Item {
              id: ringDial
              visible: root.ringPresent
              z: 5
              width: 34
              height: 34
              x: tabletGraphic.width / 2 - width / 2
              y: 4

              Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: Style.controlFill(false, false, root.foreground, root.accent)
                border.width: 2
                border.color: root.highlightButton === "ring:0"
                  ? root.accent : Qt.darker(root.foreground, 1.7)
              }

              Rectangle {
                anchors.centerIn: parent
                width: 8
                height: 8
                radius: 4
                color: root.highlightButton === "ring:0" ? root.accent : root.dim
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.focusMapping("ring:0")
              }
            }

            // active surface
            Item {
              id: areaSurface
              x: 48
              y: 18
              width: tabletGraphic.width - 96
              height: tabletGraphic.height - 36

              Rectangle {
                anchors.fill: parent
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.04)
                border.width: 1
                border.color: Qt.darker(root.foreground, 2.2)
              }

              Rectangle {
                id: areaCrop
                x: root.area.x * areaSurface.width
                y: root.area.y * areaSurface.height
                width: Math.max(4, root.area.w * areaSurface.width)
                height: Math.max(4, root.area.h * areaSurface.height)
                color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.20)
                border.width: 1.5
                border.color: root.accent

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.SizeAllCursor
                  preventStealing: true
                  onPressed: root.beginAreaDrag("move", parent, mouse.x, mouse.y)
                  onPositionChanged: function(mouse) {
                    if (pressed) root.updateAreaDrag(parent, mouse.x, mouse.y)
                  }
                  onReleased: root.endAreaDrag()
                }
              }

              Repeater {
                model: 4
                delegate: Rectangle {
                  id: handle
                  readonly property string corner: ["tl", "tr", "bl", "br"][index]
                  readonly property bool atLeft: corner === "tl" || corner === "bl"
                  readonly property bool atTop: corner === "tl" || corner === "tr"
                  width: 14
                  height: 14
                  radius: 3
                  color: root.accent
                  border.width: 1
                  border.color: root.bar ? root.bar.background : Color.background
                  // Keep the whole handle inside the surface: a child that
                  // pokes outside its parent gets no pointer events.
                  x: Math.max(0, Math.min(areaSurface.width - width,
                    (atLeft ? areaCrop.x : areaCrop.x + areaCrop.width) - width / 2))
                  y: Math.max(0, Math.min(areaSurface.height - height,
                    (atTop ? areaCrop.y : areaCrop.y + areaCrop.height) - height / 2))

                  MouseArea {
                    anchors.fill: parent
                    preventStealing: true
                    cursorShape: handle.atLeft === handle.atTop
                      ? Qt.SizeFDiagCursor : Qt.SizeBDiagCursor
                    onPressed: root.beginAreaDrag(handle.corner, parent, mouse.x, mouse.y)
                    onPositionChanged: function(mouse) {
                    if (pressed) root.updateAreaDrag(parent, mouse.x, mouse.y)
                  }
                    onReleased: root.endAreaDrag()
                  }
                }
              }
            }
          }

          Row {
            visible: !root.standby
            width: parent.width
            spacing: Style.spacing.sm

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - resetAreaButton.width - parent.spacing
              text: "Active area: "
                + Math.round(root.area.w * 100) + "% \u00d7 "
                + Math.round(root.area.h * 100) + "%"
                + "  (x " + Math.round(root.area.x * 100)
                + "%, y " + Math.round(root.area.y * 100) + "%)"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Button {
              id: resetAreaButton
              text: "Full area"
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              enabled: !root.busy
              onClicked: root.resetArea()
            }
          }

          // ---------------------------------------------------------- buttons
          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "BUTTON MAPPING"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            text: "Mouse clicks or keyboard shortcuts for the pen and the tablet keys."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            width: parent.width
          }

          Column {
            id: mappingColumn
            width: parent.width
            spacing: Style.spacing.xs

            Repeater {
              model: root.penMappings
              delegate: Item {
                required property var modelData
                width: mappingColumn.width
                height: penRow.implicitHeight

                MappingRow {
                  id: penRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                }
              }
            }
          }

          Column {
            id: padMappingColumn
            width: parent.width
            spacing: Style.spacing.xs
            visible: root.padPresent

            PanelSectionHeader {
              visible: root.padPresent
              text: "EXPRESS KEYS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: !root.padPresent
              text: "No tablet pad detected."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Repeater {
              model: root.padMappings
              delegate: Item {
                required property var modelData
                width: padMappingColumn.width
                height: padRow.implicitHeight

                MappingRow {
                  id: padRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                }
              }
            }
          }

          Column {
            id: stripMappingColumn
            width: parent.width
            spacing: Style.spacing.xs
            visible: root.padPresent && root.stripCount > 0

            PanelSectionHeader {
              text: "TOUCH STRIPS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              text: "Slide a finger along the strips next to the express keys to scroll."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              width: parent.width
            }

            Repeater {
              model: root.stripMappings
              delegate: Item {
                required property var modelData
                width: stripMappingColumn.width
                height: stripRow.implicitHeight

                MappingRow {
                  id: stripRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                  kind: "strip"
                }
              }
            }

            Row {
              width: parent.width
              height: 30
              spacing: Style.spacing.sm

              Text {
                width: parent.width - sensitivityDropdown.width - parent.spacing
                anchors.verticalCenter: parent.verticalCenter
                text: "Scroll sensitivity"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }

              Dropdown {
                id: sensitivityDropdown
                width: 200
                rowHeight: 28
                popupRowHeight: 26
                showLabel: false
                value: String(root.stripSensitivity)
                options: [
                  { value: "0.5", label: "Gentle" },
                  { value: "1", label: "Normal" },
                  { value: "1.75", label: "Fast" },
                  { value: "3", label: "Very fast" }
                ]
                foreground: root.foreground
                background: root.bar ? root.bar.background : Color.background
                accent: root.accent
                fontFamily: root.fontFamily
                onChanged: function(v) {
                  if (String(root.stripSensitivity) === v) return
                  root.runAction(["set-strip-sensitivity", v])
                }
              }
            }
          }

          Column {
            id: ringMappingColumn
            width: parent.width
            spacing: Style.spacing.xs
            visible: root.ringPresent

            PanelSectionHeader {
              text: "RING"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              text: "Turn the ring/dial to scroll (or to whatever you map it to). Its press button, if the tablet has one, is mapped below."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              width: parent.width
            }

            Repeater {
              model: root.ringMappings
              delegate: Item {
                required property var modelData
                width: ringMappingColumn.width
                height: ringRow.implicitHeight

                MappingRow {
                  id: ringRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                  kind: "strip"
                }
              }
            }

            Repeater {
              model: root.ringButtonMappings
              delegate: Item {
                required property var modelData
                width: ringMappingColumn.width
                height: ringBtnRow.implicitHeight

                MappingRow {
                  id: ringBtnRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                }
              }
            }
          }

          Column {
            id: extraMappingColumn
            width: parent.width
            spacing: Style.spacing.xs
            visible: root.extraButtons.length > 0

            PanelSectionHeader {
              text: "EXTRA BUTTONS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              text: "Buttons this tablet reports beyond the express keys, remappable like any other button."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              width: parent.width
            }

            Repeater {
              model: root.extraButtons
              delegate: Item {
                required property var modelData
                width: extraMappingColumn.width
                height: extraRow.implicitHeight

                MappingRow {
                  id: extraRow
                  width: parent.width
                  buttonId: modelData.id
                  buttonLabel: modelData.label
                }
              }
            }
          }

          // ----------------------------------------------------------- general
          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "GENERAL"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Row {
            width: parent.width
            spacing: Style.spacing.sm

            Button {
              text: "Re-apply"
              iconText: root.glyphRefresh
              tooltipText: "Restart the pen daemon and rebuild its virtual devices"
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              enabled: !root.busy
              onClicked: root.runAction(["apply"])
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - 120
              text: root.busy
                ? "Applying\u2026"
                : (root.notice !== "" ? root.notice
                  : (root.daemonUp ? "Daemon running \u00b7 pid " + root.st.pid
                                   : "Daemon not running"))
              color: root.notice !== "" ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Text {
            text: "Changes apply immediately. Barrel buttons: lower = right, upper = middle."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            width: parent.width
          }
        }
      }
    }
  }

  // One button → one action. Mouse actions apply instantly; "Keyboard
  // shortcut…" opens an inline field that accepts combos like ctrl+z.
  component MappingRow: Item {
    id: row
    property string buttonId: ""
    property string buttonLabel: ""
    // "button" for click/key mappings, "strip" for the scroll gestures.
    property string kind: "button"
    readonly property string action: (root.st.buttons || ({}))[buttonId] || "none"
    readonly property bool isKey: action.indexOf("key:") === 0
    readonly property bool editing: kind === "button" && root.editingButton === buttonId

    width: parent ? parent.width : implicitWidth
    implicitHeight: rowContent.height + (editing ? editorRow.height + Style.spacing.xs : 0)

    onActionChanged: actionDropdown.value = action

    function options() {
      if (kind === "strip") {
        var scrolls = [
          { value: "scroll:vertical", label: "Scroll up / down" },
          { value: "scroll:vertical-inv", label: "Scroll down / up" },
          { value: "scroll:horizontal", label: "Scroll right / left" },
          { value: "scroll:horizontal-inv", label: "Scroll left / right" },
          { value: "none", label: "Do nothing" }
        ]
        var known = false
        for (var i = 0; i < scrolls.length; i++)
          if (scrolls[i].value === action) known = true
        if (!known && action !== "none")
          scrolls.unshift({ value: action, label: root.prettyAction(action) })
        return scrolls
      }
      var out = [
        { value: "mouse:left", label: "Left click" },
        { value: "mouse:right", label: "Right click" },
        { value: "mouse:middle", label: "Middle click" },
        { value: "mouse:back", label: "Back" },
        { value: "mouse:forward", label: "Forward" },
        { value: "none", label: "Do nothing" },
        { value: "key:custom", label: "Keyboard shortcut\u2026" }
      ]
      if (isKey) out.unshift({ value: action, label: root.prettyAction(action) })
      return out
    }

    function applyKey() {
      var combo = String(shortcutField.text || "").trim().toLowerCase()
      if (combo === "") return
      root.runAction(["set-button", buttonId, "key:" + combo])
      root.editingButton = ""
    }

    Row {
      id: rowContent
      width: parent.width
      height: 30
      spacing: Style.spacing.sm

      Text {
        width: parent.width - actionDropdown.width - parent.spacing
        anchors.verticalCenter: parent.verticalCenter
        text: row.buttonLabel
        color: row.editing || root.highlightButton === row.buttonId
          ? root.accent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
      }

      Dropdown {
        id: actionDropdown
        width: 200
        rowHeight: 28
        popupRowHeight: 26
        showLabel: false
        value: row.action
        options: row.options()
        foreground: root.foreground
        background: root.bar ? root.bar.background : Color.background
        accent: root.accent
        fontFamily: root.fontFamily
        onChanged: function(v) {
          if (v === "key:custom") {
            actionDropdown.value = row.action
            root.editingButton = row.buttonId
            return
          }
          root.editingButton = ""
          root.runAction(["set-button", row.buttonId, v])
        }
      }
    }

    Row {
      id: editorRow
      width: parent.width
      height: 30
      spacing: Style.spacing.sm
      visible: row.editing

      TextField {
        id: shortcutField
        width: parent.width - setKeyButton.width - clearKeyButton.width - 2 * parent.spacing
        height: 30
        verticalPadding: Style.spacing.xs
        placeholderText: "ctrl+z"
        focus: row.editing
        text: row.isKey ? row.action.slice(4) : ""
        foreground: root.foreground
        accent: root.accent
        font.family: root.fontFamily
        Keys.onEscapePressed: root.editingButton = ""
        onAccepted: row.applyKey()
      }

      Button {
        id: setKeyButton
        text: "Set"
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        onClicked: row.applyKey()
      }

      PanelActionButton {
        id: clearKeyButton
        anchors.verticalCenter: parent.verticalCenter
        iconText: root.glyphClose
        tooltipText: "Do nothing"
        foreground: root.foreground
        hoverColor: root.urgent
        fontFamily: root.fontFamily
        onClicked: {
          root.runAction(["set-button", row.buttonId, "none"])
          root.editingButton = ""
        }
      }
    }
  }
}
