"""verbatim.cli — command-line front-end for the Verbatim meeting recorder."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from . import core
from .core import VerbatimError


def _notify(title: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=Verbatim", title, body],
                       check=False)


def _err(msg: str) -> int:
    print(f"verbatim: {msg}", file=sys.stderr)
    return 1


# ── commands ────────────────────────────────────────────────────────────────
def cmd_start(a) -> int:
    if core.is_recording():
        return _err("already recording (use `verbatim stop`)")
    title = a.title or f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    path = core.start_meeting(title)
    print(f"● Recording: {title}")
    print(f"  audio → {path}")
    print("  Stop with:  verbatim stop")
    _notify("Recording started", title)
    return 0


def _engine(a) -> str:
    if getattr(a, "no_ai", False):
        return "none"
    if getattr(a, "claude", False):
        return "claude"
    if getattr(a, "local", False):
        return "local"
    return core.AI_ENGINE


def _engine_label(engine: str) -> str:
    return {"local": f"local AI analysis ({core.LOCAL_MODEL})",
            "claude": "Claude AI analysis",
            "ollama": "basic local summary",
            "none": "transcript only"}[engine]


def cmd_stop(a) -> int:
    """Stop the recorder. Transcription and the AI note run in a background
    job (watch `verbatim status`) unless --wait is given."""
    engine = _engine(a)
    if getattr(a, "wait", False):
        cap = core._capture()
        if not core.is_recording() or not cap:
            return _err("nothing is recording")
        path = core.stop_audio_capture()
        if not path:
            return _err("the recording was empty")
        cap["path"] = str(path)
        core.check_long_enough(cap)
        print("■ Stopped. Transcribing…")
        return _process(path, cap.get("title"), cap.get("started_at"), engine,
                        open_note=not a.no_open)
    try:
        cap = core.stop_meeting(engine=engine)
    except VerbatimError as e:
        _notify("Verbatim: not transcribed", str(e))
        return _err(str(e))
    print(f"■ Stopped: {cap.get('title')}")
    print(f"  audio → {cap['path']}")
    print("  Transcribing and summarising in the background "
          "(`verbatim status` shows progress).")
    _notify("Recording stopped", "Transcribing and summarising…")
    return 0


def _process(audio, title, started_at, engine, open_note=False) -> int:
    try:
        mid, path = core.process_recording(Path(audio), title, started_at, engine)
    except Exception as e:  # noqa: BLE001 — the bar must hear about any failure
        core.write_state("error", title=title, audio=str(audio),
                         message=f"Processing failed: {e}")
        _notify("⚠ Verbatim: processing failed",
                f"{e}\nThe audio is safe: {audio}")
        return _err(f"processing failed: {e}\n  the audio is safe: {audio}")
    m = core.get_meeting(mid)
    failure = core.transcript_failure(m) if m else None
    if failure:
        print(f"⚠  {failure}", file=sys.stderr)
        _notify("⚠ Verbatim: no speech found", str(path))
    else:
        _notify("Meeting note ready", m.title if m else str(path))
    print(f"✓ Saved note: {path}")
    if open_note:
        _open_file(str(path))
    return 0


def cmd_process(a) -> int:
    """Background half of `stop` (also usable by hand)."""
    return _process(a.audio, a.title, a.started, a.engine or core.AI_ENGINE)


def cmd_import(a) -> int:
    """Transcribe any recording and file it as a meeting."""
    engine = _engine(a)
    print(f"Transcribing {a.audio} …")
    return _process(a.audio, a.title, None, engine, open_note=not a.no_open)


def cmd_toggle(a) -> int:
    """One-key friendly: start if idle, stop (and process) if recording."""
    if core.is_recording():
        a.wait = False
        return cmd_stop(a)
    if core.read_state().get("state") == "processing":
        return _err("still processing the last recording")
    a.title = None
    return cmd_start(a)


def cmd_status(a) -> int:
    st = core.read_state()
    state = st.get("state", "idle")
    if state == "recording":
        mins = (int(datetime.now().timestamp()) - (st.get("started_at") or 0)) // 60
        print(f"● Recording: {st.get('title')} ({mins} min)\n  audio → {st.get('audio')}")
    elif state == "processing":
        print(f"… Processing: {st.get('title')} — {st.get('message')}")
    elif state == "error":
        print(f"⚠ {st.get('message')}\n  audio → {st.get('audio')}")
    else:
        print("Idle." + (f" Last: {st.get('message')}" if st.get("message") else ""))
    return 0


def cmd_state(a) -> int:
    print(json.dumps(core.read_state()))
    return 0


def _native_app() -> str | None:
    """The native Rust app (app/ in this repo), if it is installed."""
    found = shutil.which("verbatim-app")
    if found:
        return found
    local = Path.home() / ".local/bin/verbatim-app"
    return str(local) if os.access(local, os.X_OK) else None


def cmd_app(a) -> int:
    """Open Verbatim in its own app window (starting the backend if needed)."""
    url = "http://127.0.0.1:8777/"
    if not _gui_up(url):
        subprocess.run(["systemctl", "--user", "start", "verbatim.service"],
                       check=False)
        for _ in range(20):
            if _gui_up(url):
                break
            time.sleep(0.25)
    native = _native_app()
    if native:
        # The native window's app id is "verbatim", so a second launch
        # focuses the open window instead of starting another.
        cmd = (["omarchy-launch-or-focus", "^verbatim$", shlex.quote(native)]
               if shutil.which("omarchy-launch-or-focus") else [native])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return 0
    # Fallback: the browser page served by the backend.
    # Omarchy's launcher focuses the window if it is already open, matching
    # on the page title set in gui.INDEX.
    for cmd in (["omarchy-launch-or-focus-webapp", "Verbatim — Meeting Notes", url],
                ["chromium", f"--app={url}"]):
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return 0
    webbrowser.open(url)
    return 0


def _gui_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/status", timeout=1):
            return True
    except OSError:
        return False


def cmd_audio_dir(a) -> int:
    """Show or set where meeting audio is archived."""
    if a.path:
        core.write_config(audio_dir=str(Path(a.path).expanduser()))
    configured = (os.environ.get("VERBATIM_AUDIO_DIR")
                  or core.read_config().get("audio_dir"))
    try:
        active, warning = core.resolve_audio_dir()
    except VerbatimError as e:
        return _err(str(e))
    print(f"configured: {configured or '(none — using default)'}")
    print(f"active:     {active}")
    if warning:
        print(f"⚠  {warning}", file=sys.stderr)
    free = shutil.disk_usage(active).free / 1e9
    print(f"free space: {free:.1f} GB  (~{free * 1000 / 8:.0f} hours at 8 MB/hour)")
    return 0


def cmd_audio(a) -> int:
    """Print (or open) the archived audio for a meeting."""
    m = core.get_meeting(a.id)
    if not m:
        return _err(f"no meeting matching '{a.id}'")
    p = core.audio_path_for(m.id)
    if not p:
        return _err(f"no archived audio for '{m.title or m.short_id}'")
    print(p)
    if a.open:
        _open_file(str(p))
    return 0


def cmd_list(a) -> int:
    ms = core.list_meetings(limit=a.limit, query=a.query)
    if not ms:
        print("No meetings yet.")
        return 0
    for m in ms:
        print(f"{m.short_id}  {m.started_dt:%Y-%m-%d %H:%M}  "
              f"{m.duration_human:>8}  {m.status:<10}  {m.title or '(untitled)'}")
    return 0


def cmd_show(a) -> int:
    print(core.export_transcript(a.id))
    return 0


def cmd_ai(a) -> int:
    """Fireflies-style analysis via the configured AI engine."""
    engine = "ollama" if getattr(a, "ollama_summary", False) else _engine(a)
    print(f"✨ Analyzing ({_engine_label(engine)})…", file=sys.stderr)
    text = (core.summarize(a.id) if engine == "ollama"
            else core.analyze(a.id, engine=engine))
    print(text)
    return 0


def cmd_summary(a) -> int:  # kept as an alias of `ai`
    return cmd_ai(a)


def cmd_note(a) -> int:
    engine = _engine(a)
    print(f"  Building note ({_engine_label(engine)})…", file=sys.stderr)
    path, _ = core.build_note(a.id, engine=engine)
    print(f"✓ Saved note: {path}")
    if not a.no_open:
        _open_file(str(path))
    return 0


def cmd_label(a) -> int:
    print(core.label_speaker(a.id, a.speaker, a.name))
    return 0


def cmd_rename(a) -> int:
    core.rename_meeting(a.id, a.title)
    print(f"✓ Renamed to: {a.title}")
    return 0


def cmd_delete(a) -> int:
    print(core.delete_meeting(a.id))
    return 0


def cmd_gui(a) -> int:
    from . import gui
    gui.serve(port=a.port, open_browser=not a.no_browser)
    return 0


def cmd_open(a) -> int:
    m = core.get_meeting(a.id)
    if not m:
        return _err(f"no meeting matching '{a.id}'")
    p = core.note_path(m)
    if not p.exists():
        print("Note not built yet; building…")
        p, _ = core.build_note(m.id)
    _open_file(str(p))
    return 0


def _open_file(path: str) -> None:
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(f"file://{path}")


def _spawn_overlay() -> None:
    """Best-effort: pop the floating recording overlay if a display is present
    and it isn't already up (its own single-instance guard handles dupes)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        core.spawn_overlay()
    except Exception:
        pass


def cmd_identify(a) -> int:
    print(f"🧠 Identifying speakers ({core.LOCAL_MODEL if core.AI_ENGINE == 'local' else 'Claude'})…", file=sys.stderr)
    attr = core.identify_speakers(a.id, refresh=a.refresh, roster=a.who)
    from collections import Counter
    for name, n in Counter(attr.get("lines", {}).values()).most_common():
        print(f"  {name:<24} {n} lines")
    print("✓ Speakers attributed. Rebuild the note with `verbatim note`.")
    return 0


def cmd_stats(a) -> int:
    rows = core.speaker_stats(a.id)
    if not rows:
        return _err("no transcript to measure")
    for s in rows:
        who = f"{s['name']} ({s['tag']})" if s.get("tag") else s["name"]
        print(f"{s['pct']:>3}%  {who:<26} {s['words']:>6} words  "
              f"{s['lines']:>4} lines")
    return 0


def cmd_search(a) -> int:
    hits = core.search_meetings(a.query, limit=a.limit)
    if not hits:
        print("No matches.")
        return 0
    for d in hits:
        print(f"{d['short_id']}  {d['started_human']}  [{d['match_in']}]  "
              f"{d['title'] or '(untitled)'}")
        if d.get("snippet"):
            print(f"        {d['snippet']}")
    return 0


def cmd_export(a) -> int:
    path = core.export_summary(a.id)
    print(f"✓ Saved summary: {path}")
    if not a.no_open:
        _open_file(str(path))
    return 0


def cmd_overlay(a) -> int:
    from . import overlay
    overlay.run()
    return 0


def cmd_tray(a) -> int:
    from . import tray
    tray.run()
    return 0


# ── parser ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verbatim",
        description="Local meeting recorder & note-taker (a Fireflies for your "
                    "own machine, powered by VoxType).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start recording a meeting")
    s.add_argument("title", nargs="?", help="meeting title")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("audio-dir",
                       help="show or set where meeting audio is archived")
    s.add_argument("path", nargs="?",
                   help="directory to store recordings (e.g. /mnt/archive/verbatim)")
    s.set_defaults(func=cmd_audio_dir)

    s = sub.add_parser("audio", help="path to a meeting's archived audio")
    s.add_argument("id", nargs="?", default="latest")
    s.add_argument("--open", action="store_true", help="open it in a player")
    s.set_defaults(func=cmd_audio)

    def _ai_flags(sp):
        sp.add_argument("--local", action="store_true",
                        help="force the local AI engine (the default unless "
                             "VERBATIM_AI_ENGINE=claude)")
        sp.add_argument("--claude", action="store_true",
                        help="force cloud analysis via the Claude Code CLI")
        sp.add_argument("--no-ai", action="store_true",
                        help="transcript only, no AI analysis")

    for name in ("stop", "finish"):
        s = sub.add_parser(name, help="stop recording; transcribe + summarise in the background")
        _ai_flags(s)
        s.add_argument("--wait", action="store_true",
                       help="process in the foreground and open the note")
        s.add_argument("--no-open", action="store_true", help="don't open the note after")
        s.set_defaults(func=cmd_stop)

    s = sub.add_parser("toggle", help="start if idle, else stop (for a hotkey or the bar)")
    _ai_flags(s)
    s.add_argument("--no-open", action="store_true", default=True)
    s.set_defaults(func=cmd_toggle)

    s = sub.add_parser("process", help=argparse.SUPPRESS)
    s.add_argument("audio")
    s.add_argument("--title")
    s.add_argument("--started", type=int)
    s.add_argument("--engine")
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("import", help="transcribe an audio file and file it as a meeting")
    s.add_argument("audio")
    s.add_argument("--title")
    _ai_flags(s)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_import)

    sub.add_parser("state", help="machine-readable status (JSON)").set_defaults(func=cmd_state)
    sub.add_parser("app", help="open Verbatim in its own window").set_defaults(func=cmd_app)

    sub.add_parser("status", help="show current recording status").set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list past meetings")
    s.add_argument("query", nargs="?", help="filter by title text")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="print a meeting transcript")
    s.add_argument("id", help="meeting id, short id, or 'latest'")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("ai", help="Fireflies-style analysis (local AI by default)")
    s.add_argument("id", help="meeting id, short id, or 'latest'")
    _ai_flags(s)
    s.add_argument("--ollama-summary", dest="ollama_summary", action="store_true",
                   help="VoxType's basic built-in Ollama summary instead")
    s.set_defaults(func=cmd_ai)

    s = sub.add_parser("summary", help="alias of `ai`")
    s.add_argument("id")
    _ai_flags(s)
    s.add_argument("--ollama-summary", dest="ollama_summary", action="store_true")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("note", help="(re)build the saved markdown note")
    s.add_argument("id")
    _ai_flags(s)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("label", help="name a speaker (e.g. label latest 0 Alice)")
    s.add_argument("id"); s.add_argument("speaker"); s.add_argument("name")
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("rename", help="rename a meeting")
    s.add_argument("id"); s.add_argument("title")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("delete", help="delete a meeting")
    s.add_argument("id")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("open", help="open a meeting's saved note")
    s.add_argument("id")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("gui", help="launch the browser GUI")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("identify", help="AI: attribute each line to a named speaker")
    s.add_argument("id")
    s.add_argument("--who", help='known participants, comma-separated (e.g. "Me, Kevin")')
    s.add_argument("--refresh", action="store_true", help="re-run even if cached")
    s.set_defaults(func=cmd_identify)

    s = sub.add_parser("stats", help="speaker talk-time breakdown")
    s.add_argument("id")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("search", help="search titles, transcripts & summaries")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("export", help="save a shareable summary-only .md (no transcript)")
    s.add_argument("id")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("overlay", help="floating 'recording' overlay (needs PySide6)")
    s.set_defaults(func=cmd_overlay)

    s = sub.add_parser("tray", help="system tray icon (needs PySide6)")
    s.set_defaults(func=cmd_tray)
    return p


def _heal_voxtype_mode() -> None:
    """Undo a stranded meeting-mode config left by a crashed/killed run.

    enter_meeting_mode() turns VoxType's streaming off so meeting mode works;
    if the process dies before stop, dictation silently loses live-typing until
    someone notices. Restore it whenever the marker outlives the recording.
    """
    try:
        if core._MODE_MARKER.exists() and not core.is_recording():
            if core.exit_meeting_mode():
                print("↻ restored VoxType dictation (streaming) config",
                      file=sys.stderr)
    except Exception:
        pass  # never block a command on housekeeping


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _heal_voxtype_mode()
    try:
        return args.func(args)
    except VerbatimError as e:
        return _err(str(e))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
