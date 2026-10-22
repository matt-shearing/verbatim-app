import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Bar widget for Verbatim, the local meeting recorder.
//
// Reads ~/.local/share/verbatim/state.json (written atomically by the
// Python side on every transition) and runs `verbatim` detached for
// actions. Left click starts or stops, middle click opens the app, right
// click opens this panel. The bar holds one copy per monitor; each copy
// only reads the file and launches commands, so they cannot desync.
Panel {
  id: root

  moduleName: "contra.verbatim"
  ipcTarget: "contra.verbatim"
  manageIpc: true

  readonly property string statePath: Quickshell.env("HOME") + "/.local/share/verbatim/state.json"
  property var st: Model.idle()
  property real nowMs: Date.now()
  // Set after a start/stop request so a double click does not undo it.
  property bool pending: false

  readonly property bool recording: st.state === "recording"
  readonly property bool processing: st.state === "processing"
  readonly property bool failed: st.state === "error"

  readonly property color fg: bar ? bar.barForeground : Color.bar.text
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color redColor: bar ? bar.urgent : Color.urgent
  readonly property color stateColor: recording ? redColor
                                    : processing ? Commons.Color.accent
                                    : failed ? Model.AMBER
                                    : fg

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function run(args) {
    Quickshell.execDetached(Model.verbatimCommand(args))
  }

  function markPending() {
    root.pending = true
    pendingTimer.restart()
  }

  function toggleRecording() {
    if (root.pending) return
    if (root.processing) {
      openApp()
      return
    }
    markPending()
    run(["toggle", "--no-open"])
  }

  function startRecording(title) {
    if (root.pending || root.recording || root.processing) return
    var t = String(title || "").trim()
    markPending()
    run(t !== "" ? ["start", t] : ["start"])
    titleField.text = ""
  }

  function stopRecording() {
    if (root.pending || !root.recording) return
    markPending()
    run(["stop", "--no-open"])
  }

  function openApp() {
    run(["app"])
    root.close()
  }

  onStChanged: {
    root.pending = false
    root.nowMs = Date.now()
  }

  onOpenedChanged: if (opened) {
    stateFile.reload()
    if (!root.recording && !root.processing)
      Qt.callLater(function() { titleField.forceActiveFocus() })
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onLoaded: root.st = Model.parse(text())
    onLoadFailed: root.st = Model.idle()
    onFileChanged: reload()
  }

  // Safety net: inotify loses a watch that starts on a missing file, so
  // re-read the small file every few seconds as well.
  Timer {
    interval: 3000
    running: true
    repeat: true
    onTriggered: stateFile.reload()
  }

  Timer {
    interval: 1000
    running: root.recording
    repeat: true
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  Timer {
    id: pendingTimer
    interval: 4000
    onTriggered: root.pending = false
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: Model.glyph(root.st)
    active: root.st.state !== "idle"
    activeColor: root.stateColor
    useActiveColor: true
    dimmed: root.st.state === "idle"
    labelText: Model.barLabel(root.st, root.nowMs)
    slotSize: Style.bar.iconSlot
    fixedWidth: labelText !== "" ? Math.ceil(labelMetrics.advanceWidth) + Style.space(4) + slotSize : slotSize
    tooltipText: Model.tooltip(root.st, root.nowMs)
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggle()
      else if (b === Qt.MiddleButton) root.openApp()
      else root.toggleRecording()
    }
  }

  TextMetrics {
    id: labelMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.bar.iconFont
    // Measure the widest form of the time so the bar does not jiggle.
    text: button.labelText.replace(/[0-9]/g, "0")
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: titleField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        Row {
          spacing: Style.space(8)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: Model.glyph(root.st)
            color: root.stateColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
          }
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Verbatim"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
        }

        Text {
          width: parent.width
          text: Model.statusLine(root.st, root.nowMs)
          color: root.st.state === "idle" ? root.fg : root.stateColor
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          width: parent.width
          visible: root.st.state !== "idle" && root.st.title !== ""
          text: root.st.title
          color: root.fg
          opacity: 0.7
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          width: parent.width
          visible: root.failed && root.st.message !== ""
          text: root.st.message
          color: Model.AMBER
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          width: parent.width
          visible: root.processing
          text: "You can start a new recording once this finishes."
          color: root.fg
          opacity: 0.6
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator { foreground: root.fg }

        TextField {
          id: titleField
          width: parent.width
          visible: !root.recording && !root.processing
          foreground: root.fg
          placeholderText: "Meeting title (optional)"
          onAccepted: root.startRecording(text)
          Keys.onEscapePressed: root.close()
        }

        Button {
          width: parent.width
          visible: !root.processing
          text: root.recording ? "Stop recording" : "Start recording"
          iconText: root.recording ? Model.GLYPH_REC : Model.GLYPH_MIC
          bordered: true
          foreground: root.recording ? root.redColor : root.fg
          fontFamily: root.fontFamily
          fontSize: Style.font.title
          verticalPadding: Style.space(10)
          onClicked: root.recording ? root.stopRecording() : root.startRecording(titleField.text)
        }

        Button {
          width: parent.width
          text: "Open Verbatim"
          iconText: String.fromCodePoint(0xF03CC)
          bordered: true
          leftAlign: true
          foreground: root.fg
          fontFamily: root.fontFamily
          onClicked: root.openApp()
        }
      }
    }
  }
}
