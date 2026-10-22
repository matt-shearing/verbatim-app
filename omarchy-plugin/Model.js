.pragma library

// Pure helpers for contra.verbatim. No QML, no side effects.

// One quote-bubble mark in every state, so Verbatim never looks like the
// other mic widgets on the bar; colour says which state it is in.
var GLYPH_MIC = String.fromCodePoint(0xF1022)        // nf-md-comment_quote_outline (idle)
var GLYPH_REC = String.fromCodePoint(0xF1021)        // nf-md-comment_quote (recording)
var GLYPH_BUSY = String.fromCodePoint(0xF1021)       // nf-md-comment_quote (processing)
var GLYPH_ERROR = String.fromCodePoint(0xF1022)      // nf-md-comment_quote_outline (error)

var AMBER = "#d29922"

var STATES = ["idle", "recording", "processing", "error"]

function idle() {
  return { state: "idle", title: "", startedAt: 0, message: "", meetingId: "", audio: "" }
}

function str(v) {
  return (typeof v === "string") ? v : ""
}

function num(v) {
  var n = Number(v)
  return (v === null || v === undefined || isNaN(n)) ? 0 : n
}

// Parse the text of state.json. Anything missing or malformed reads as idle.
function parse(text) {
  if (!text || String(text).trim() === "") return idle()
  var o
  try {
    o = JSON.parse(text)
  } catch (e) {
    return idle()
  }
  if (!o || typeof o !== "object") return idle()
  var s = str(o.state)
  if (STATES.indexOf(s) < 0) s = "idle"
  return {
    state: s,
    title: str(o.title),
    startedAt: num(o.started_at),
    message: str(o.message),
    meetingId: str(o.meeting_id),
    audio: str(o.audio)
  }
}

function pad2(n) {
  return n < 10 ? "0" + n : "" + n
}

// m:ss, or h:mm:ss past an hour.
function elapsed(startedAt, nowMs) {
  if (!startedAt) return "0:00"
  var secs = Math.max(0, Math.floor(nowMs / 1000 - startedAt))
  var h = Math.floor(secs / 3600)
  var m = Math.floor((secs % 3600) / 60)
  var s = secs % 60
  if (h > 0) return h + ":" + pad2(m) + ":" + pad2(s)
  return m + ":" + pad2(s)
}

function glyph(st) {
  if (st.state === "recording") return GLYPH_REC
  if (st.state === "processing") return GLYPH_BUSY
  if (st.state === "error") return GLYPH_ERROR
  return GLYPH_MIC
}

function shorten(s, max) {
  if (!s) return ""
  return s.length > max ? s.slice(0, max - 1) + "…" : s
}

function progressText(st) {
  return st.message !== "" ? st.message : "Transcribing…"
}

function barLabel(st, nowMs) {
  if (st.state === "recording") return elapsed(st.startedAt, nowMs)
  if (st.state === "processing") return shorten(progressText(st), 20)
  if (st.state === "error") return "Error"
  return ""
}

function statusLine(st, nowMs) {
  if (st.state === "recording") return "Recording, " + elapsed(st.startedAt, nowMs)
  if (st.state === "processing") return progressText(st)
  if (st.state === "error") return "Something went wrong"
  return "Not recording"
}

function tooltip(st, nowMs) {
  var lines = ["Verbatim: " + statusLine(st, nowMs)]
  if (st.title !== "" && st.state !== "idle") lines.push(st.title)
  if (st.state === "error" && st.message !== "") lines.push(st.message)
  if (st.state === "processing") lines.push("Click to open Verbatim")
  else if (st.state === "recording") lines.push("Click to stop · right click for more")
  else lines.push("Click to record · right click for more")
  return lines.join("\n")
}

// Launch a user helper by absolute path first, PATH second. The bar's PATH
// starts with $OMARCHY_PATH/bin and misses ~/.local/bin.
function verbatimCommand(args) {
  return ["bash", "-c",
          'if [ -x "$HOME/.local/bin/verbatim" ]; then exec "$HOME/.local/bin/verbatim" "$@"; else exec verbatim "$@"; fi',
          "verbatim"].concat(args)
}
