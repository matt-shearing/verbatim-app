"""verbatim.cli — command-line front-end for the Verbatim meeting recorder."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime

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
        return _err("a meeting is already recording (use `verbatim stop`)")
    title = a.title or f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    diar = "ml" if a.ml else None
    print(f"● Starting meeting: {title}")
    print("  (loading model + opening mic/system capture…)")
    mid = core.start_meeting(title, diarization=diar, wait=not a.no_wait)
    _notify("Recording started", title)
    if mid:
        print(f"✓ Recording — id {mid[:8]}")
        print("  Stop with:  verbatim stop")
    else:
        print("✓ Start requested (not waiting for confirmation)")
    return 0


def _engine(a) -> str:
    if getattr(a, "no_ai", False):
        return "none"
    if getattr(a, "local", False):
        return "ollama"
    return "claude"


def cmd_stop(a) -> int:
    mid = core.is_recording()
    if not mid:
        # still allow finalizing 'latest' if the daemon already stopped it
        print("No meeting is recording; finalizing most recent meeting.")
    else:
        print("■ Stopping…")
        core.stop_meeting()
    engine = _engine(a)
    _notify("Recording stopped", "Transcribing & analyzing…")
    target = mid or "latest"
    label = {"claude": "Claude AI analysis", "ollama": "local summary",
             "none": "transcript only"}[engine]
    print(f"  Building note ({label})…")
    try:
        path, _ = core.build_note(target, engine=engine)
    except VerbatimError as e:
        return _err(str(e))
    print(f"✓ Saved note: {path}")
    _notify("Meeting note ready", str(path))
    if not a.no_open:
        _open_file(str(path))
    return 0


def cmd_toggle(a) -> int:
    """One-key friendly: start if idle, stop+save if recording."""
    if core.is_recording():
        return cmd_stop(a)
    a.title = None
    a.ml = False
    a.no_wait = False
    return cmd_start(a)


def cmd_status(a) -> int:
    print(core._vox("meeting", "status", timeout=15))
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
    """Fireflies-style analysis via Claude Code (or --local Ollama summary)."""
    print("✨ Analyzing with " + ("Ollama…" if a.local else "Claude…"),
          file=sys.stderr)
    text = core.summarize(a.id) if a.local else core.analyze(a.id)
    print(text)
    return 0


def cmd_summary(a) -> int:  # kept as an alias of `ai`
    return cmd_ai(a)


def cmd_note(a) -> int:
    print(f"  Building note ({'local summary' if a.local else 'Claude AI'})…",
          file=sys.stderr)
    path, _ = core.build_note(a.id, engine=_engine(a))
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


def cmd_identify(a) -> int:
    print("🧠 Identifying speakers with Claude…", file=sys.stderr)
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
        print(f"{s['pct']:>3}%  {s['name']:<24} {s['words']:>6} words  "
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
    s.add_argument("--ml", action="store_true",
                   help="ML multi-speaker diarization (default: You/Remote)")
    s.add_argument("--no-wait", action="store_true",
                   help="don't wait for the recording to confirm")
    s.set_defaults(func=cmd_start)

    def _ai_flags(sp):
        sp.add_argument("--local", action="store_true",
                        help="use offline Ollama summary instead of Claude")
        sp.add_argument("--no-ai", action="store_true",
                        help="transcript only, no AI analysis")

    for name in ("stop", "finish"):
        s = sub.add_parser(name, help="stop recording → transcribe → AI analysis → save note")
        _ai_flags(s)
        s.add_argument("--no-open", action="store_true", help="don't open the note after")
        s.set_defaults(func=cmd_stop)

    s = sub.add_parser("toggle", help="start if idle, else stop+save (for a hotkey)")
    _ai_flags(s)
    s.add_argument("--no-open", action="store_true", default=True)
    s.set_defaults(func=cmd_toggle)

    sub.add_parser("status", help="show current recording status").set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list past meetings")
    s.add_argument("query", nargs="?", help="filter by title text")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="print a meeting transcript")
    s.add_argument("id", help="meeting id, short id, or 'latest'")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("ai", help="Fireflies-style analysis via Claude (print)")
    s.add_argument("id", help="meeting id, short id, or 'latest'")
    s.add_argument("--local", action="store_true",
                   help="use offline Ollama summary instead of Claude")
    s.set_defaults(func=cmd_ai)

    s = sub.add_parser("summary", help="alias of `ai`")
    s.add_argument("id")
    s.add_argument("--local", action="store_true")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VerbatimError as e:
        return _err(str(e))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
