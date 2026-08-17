"""
verbatim.core — shared logic for the Verbatim meeting recorder.

Verbatim is a thin, opinionated layer over VoxType's `meeting` mode. VoxType
does the hard parts (dual mic+system-audio capture, echo cancellation, GPU
transcription, diarization, Ollama summaries); Verbatim adds a friendly
start/stop workflow, Fireflies-style saved notes, and a browser GUI.

Design rule: never parse VoxType's private on-disk transcript format. We drive
the stable `voxtype meeting` CLI for content and read the meetings SQLite DB
(read-only, documented columns) only for fast listing/metadata.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
HOME = Path.home()
VOXTYPE_DATA = Path(os.environ.get("VOXTYPE_DATA", HOME / ".local/share/voxtype"))
MEETINGS_DB = VOXTYPE_DATA / "meetings" / "index.db"
NOTES_DIR = Path(os.environ.get("VERBATIM_NOTES_DIR", HOME / "Meetings"))
_VERBATIM_STATE = Path(os.environ.get("VERBATIM_CACHE", HOME / ".local/share/verbatim"))
CACHE_DIR = _VERBATIM_STATE / "analysis"
ATTRIB_DIR = _VERBATIM_STATE / "attribution"
VOXTYPE_BIN = os.environ.get("VOXTYPE_BIN", "voxtype")
CLAUDE_BIN = os.environ.get("VERBATIM_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("VERBATIM_CLAUDE_MODEL", "sonnet")
# AI engine: "local" (an OpenAI-compatible server on this machine or the LAN —
# nothing leaves the network) or "claude" (Claude Code CLI, cloud).
AI_ENGINE = os.environ.get("VERBATIM_AI_ENGINE", "local")
# Default is the SSH tunnel to the GPU node (verbatim-ai-tunnel.service); point
# this straight at a LAN host (e.g. http://gpu-node:11434) if its firewall allows.
LOCAL_URL = os.environ.get("VERBATIM_LOCAL_URL", "http://127.0.0.1:11435").rstrip("/")
# gemma4:12b won head-to-head testing (2026-08): perfect speaker attribution
# and clean analysis with thinking off; qwen3.5:9b misattributed speakers
# without thinking and rambled unusably with it. Both are pulled on the GPU node.
LOCAL_MODEL = os.environ.get("VERBATIM_LOCAL_MODEL", "gemma4:12b")
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# ── VoxType streaming vs meeting mode ───────────────────────────────────────
# VoxType has ONE global `[parakeet] streaming` flag, and the two modes need
# opposite values:
#
#   streaming = true   dictation types live at the cursor (the reason it's on)
#   streaming = false  REQUIRED by meeting mode, which calls `transcribe_timed`
#                      for timestamped segments
#
# With streaming on, every meeting chunk fails with
#   "transcribe_timed is not supported in streaming mode"
# and the meeting is saved with ZERO segments. It is logged at ERROR but the
# recording still reports success, so it looks fine until you open the note.
# That silently destroyed every meeting from 2026-07-09 (when streaming was
# switched on) to 2026-08-07, including a 118-minute one - and `retain_audio`
# is plumbed but not implemented in 0.7.5, so there is no audio to recover.
#
# So Verbatim flips the daemon into a meeting-capable config for the duration
# of a recording and restores the dictation config afterwards. The marker file
# records the original values so an interrupted run self-heals on next use.
VOXTYPE_CONFIG = Path(
    os.environ.get("VOXTYPE_CONFIG", HOME / ".config/voxtype/config.toml")
)
VOXTYPE_UNIT = os.environ.get("VOXTYPE_UNIT", "voxtype.service")
_MODE_MARKER = _VERBATIM_STATE / "voxtype-mode-override.json"
# The cache-aware streaming model only works in streaming mode; meeting mode
# needs the full-context offline model.
MEETING_PARAKEET_MODEL = os.environ.get(
    "VERBATIM_MEETING_PARAKEET_MODEL", "parakeet-tdt-0.6b-v3"
)
AUTO_MODE_SWITCH = os.environ.get("VERBATIM_AUTO_MODE_SWITCH", "1") != "0"


# ── Meeting audio archive ───────────────────────────────────────────────────
# VoxType's `retain_audio` is plumbed but NOT implemented in 0.7.5 — nothing
# ever writes audio and `audio_retained` is always false. So four meetings that
# failed to transcribe were unrecoverable, including a 118-minute one. Verbatim
# therefore keeps its own archival copy, always.
#
# Where it goes, in priority order:
#   1. $VERBATIM_AUDIO_DIR
#   2. "audio_dir" in ~/.config/verbatim/config.json  (`verbatim audio-dir <path>`)
#   3. ~/Meetings/audio
#
# Mixed mic + system-output, mono 16 kHz Opus @24k ≈ 8 MB/hour, which is both
# cheap to keep forever and exactly what MOSS/Whisper want as input.
CONFIG_FILE = Path(
    os.environ.get("VERBATIM_CONFIG", HOME / ".config/verbatim/config.json"))
AUDIO_FALLBACK = NOTES_DIR / "audio"
AUDIO_BITRATE = os.environ.get("VERBATIM_AUDIO_BITRATE", "24k")
# Refuse a destination with less headroom than this; a meeting can run 3 hours.
AUDIO_MIN_FREE_MB = int(os.environ.get("VERBATIM_AUDIO_MIN_FREE_MB", "500"))
_CAPTURE_STATE = _VERBATIM_STATE / "audio-capture.json"


class VerbatimError(RuntimeError):
    pass


def read_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(**kv) -> dict:
    cfg = read_config()
    cfg.update({k: v for k, v in kv.items() if v is not None})
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def _dir_usable(d: Path) -> str | None:
    """Return a reason the directory is unusable, or None if it is fine.

    The important case is a removable/udisks mount such as
    /run/media/<user>/<label>: when the drive is absent the path may still be
    creatable, and we would quietly fill the root filesystem instead. So for
    those roots we require an actual mount point.
    """
    try:
        parts = d.resolve().parts
        for base in ("/run/media", "/media", "/mnt"):
            b = Path(base)
            if str(d.resolve()).startswith(base + "/"):
                # The mount point is the path element just under <base>[/<user>]
                depth = len(b.parts) + (2 if base == "/run/media" else 1)
                if len(parts) >= depth:
                    mp = Path(*parts[:depth])
                    if not os.path.ismount(mp):
                        return f"{mp} is not mounted"
                break
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".verbatim-write-test"
        probe.touch()
        probe.unlink()
        free_mb = shutil.disk_usage(d).free // (1024 * 1024)
        if free_mb < AUDIO_MIN_FREE_MB:
            return f"only {free_mb} MB free (need {AUDIO_MIN_FREE_MB} MB)"
    except OSError as e:
        return str(e)
    return None


def resolve_audio_dir() -> tuple[Path, str | None]:
    """Pick where to write meeting audio. Returns (dir, warning-or-None).

    Never returns "nowhere": if the configured location is unavailable (drive
    unplugged, full) it falls back to the local directory and warns loudly.
    Losing a meeting because a disk is absent would be far worse than storing
    it somewhere less convenient.
    """
    configured = os.environ.get("VERBATIM_AUDIO_DIR") or read_config().get("audio_dir")
    if configured:
        d = Path(configured).expanduser()
        why = _dir_usable(d)
        if not why:
            return d, None
        why2 = _dir_usable(AUDIO_FALLBACK)
        if why2:
            raise VerbatimError(
                f"nowhere to store meeting audio: {d} unusable ({why}) and "
                f"fallback {AUDIO_FALLBACK} unusable ({why2})")
        return AUDIO_FALLBACK, (
            f"audio location {d} is unusable ({why}) — recording to "
            f"{AUDIO_FALLBACK} instead")
    why = _dir_usable(AUDIO_FALLBACK)
    if why:
        raise VerbatimError(f"cannot use {AUDIO_FALLBACK} for audio: {why}")
    return AUDIO_FALLBACK, None


def _pulse_sources() -> tuple[str, str]:
    def pactl(*a):
        return subprocess.run(["pactl", *a], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    mic = os.environ.get("VERBATIM_AUDIO_MIC") or pactl("get-default-source")
    mon = os.environ.get("VERBATIM_AUDIO_MONITOR")
    if not mon:
        sink = pactl("get-default-sink")
        mon = f"{sink}.monitor" if sink else ""
    if not mic and not mon:
        raise VerbatimError("no PulseAudio/PipeWire sources found (is pactl working?)")
    return mic, mon


def audio_path_for(meeting_id: str) -> Path | None:
    """Where a meeting's archived audio ended up, if we recorded any."""
    try:
        idx = json.loads((_VERBATIM_STATE / "audio-index.json")
                         .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    p = idx.get(meeting_id)
    return Path(p) if p and Path(p).exists() else None


def _remember_audio(meeting_id: str, path: Path) -> None:
    f = _VERBATIM_STATE / "audio-index.json"
    try:
        idx = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        idx = {}
    idx[meeting_id] = str(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")


def start_audio_capture(title: str | None) -> tuple[Path, str | None]:
    """Begin the archival recording. Returns (path, warning-or-None)."""
    stop_audio_capture()  # never leave two ffmpegs fighting over the same sources
    dest, warning = resolve_audio_dir()
    mic, mon = _pulse_sources()
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    path = dest / f"{stamp} - {_fs_safe(title or 'meeting')}.opus"

    inputs, filt = [], ""
    if mic and mon:
        inputs = ["-f", "pulse", "-i", mic, "-f", "pulse", "-i", mon]
        filt = "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,aresample=16000[a]"
    else:
        inputs = ["-f", "pulse", "-i", mic or mon]
        filt = "[0:a]aresample=16000[a]"

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", filt, "-map", "[a]", "-ac", "1",
           "-c:a", "libopus", "-b:a", AUDIO_BITRATE, str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    time.sleep(1.2)
    if proc.poll() is not None:
        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        raise VerbatimError(f"audio capture failed to start: {err[:300]}")
    _CAPTURE_STATE.parent.mkdir(parents=True, exist_ok=True)
    _CAPTURE_STATE.write_text(json.dumps({"pid": proc.pid, "path": str(path)}),
                              encoding="utf-8")
    return path, warning


def stop_audio_capture(meeting_id: str | None = None) -> Path | None:
    """Finalize the recording. Returns the path if a usable file was written."""
    try:
        st = json.loads(_CAPTURE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid, path = st.get("pid"), Path(st.get("path", ""))
    if pid:
        try:
            # SIGINT, not SIGKILL: ffmpeg must flush and write the Ogg trailer,
            # otherwise the file has no duration and some players reject it.
            os.kill(pid, 2)
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    _CAPTURE_STATE.unlink(missing_ok=True)
    if path.is_file() and path.stat().st_size > 1024:
        if meeting_id:
            _remember_audio(meeting_id, path)
        return path
    return None


def spawn_overlay() -> None:
    """Launch the floating recording overlay in its own process.

    `python3 -m verbatim` only resolves when the checkout root is on sys.path.
    The `verbatim` launcher inserts it into ITS OWN sys.path, but a child
    process does not inherit sys.path — only PYTHONPATH. So spawning from the
    systemd-launched tray (CWD=/) died with "No module named verbatim" and,
    because output is discarded, showed nothing at all. Pass the root along.
    """
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
    subprocess.Popen([sys.executable, "-m", "verbatim", "overlay"],
                     start_new_session=True, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _parakeet_section(text: str) -> tuple[int, int]:
    """Return the (start, end) offsets of the [parakeet] table body.

    Edits must be confined to this slice: `model` also exists under [whisper],
    and a naive global substitution would rewrite the wrong engine's model.
    """
    m = re.search(r"^\[parakeet\][^\n]*\n", text, flags=re.M)
    if not m:
        raise VerbatimError(f"no [parakeet] section in {VOXTYPE_CONFIG}")
    start = m.end()
    nxt = re.search(r"^\[", text[start:], flags=re.M)
    return start, (start + nxt.start() if nxt else len(text))


def _read_parakeet(text: str) -> dict:
    s, e = _parakeet_section(text)
    body = text[s:e]
    out = {}
    for key, pat in (("streaming", r"^streaming\s*=\s*(\w+)"),
                     ("model", r'^model\s*=\s*"([^"]*)"')):
        m = re.search(pat, body, flags=re.M)
        if m:
            out[key] = m.group(1)
    return out


def _write_parakeet(streaming: bool, model: str | None) -> None:
    text = VOXTYPE_CONFIG.read_text(encoding="utf-8")
    s, e = _parakeet_section(text)
    body = text[s:e]
    body, n = re.subn(r"^streaming\s*=\s*\w+",
                      f"streaming = {str(streaming).lower()}", body, count=1, flags=re.M)
    if not n:
        body = f"streaming = {str(streaming).lower()}\n" + body
    if model:
        body, n = re.subn(r'^model\s*=\s*"[^"]*"',
                          f'model = "{model}"', body, count=1, flags=re.M)
        if not n:
            body = f'model = "{model}"\n' + body
    VOXTYPE_CONFIG.write_text(text[:s] + body + text[e:], encoding="utf-8")


def _restart_voxtype(settle: float = 6.0) -> None:
    """Restart the daemon so it re-reads config. Meeting start/stop are file
    triggers consumed by the RUNNING daemon, so the config must be live before
    the trigger is written."""
    r = subprocess.run(
        ["systemctl", "--user", "restart", VOXTYPE_UNIT],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise VerbatimError(
            f"could not restart {VOXTYPE_UNIT}: {r.stderr.strip() or 'unknown error'}"
        )
    # The daemon reloads the ASR model on boot; give it time before triggering.
    time.sleep(settle)


def enter_meeting_mode() -> bool:
    """Switch VoxType to a meeting-capable config. Returns True if it changed."""
    if not AUTO_MODE_SWITCH:
        return False
    text = VOXTYPE_CONFIG.read_text(encoding="utf-8")
    cur = _read_parakeet(text)
    if cur.get("streaming") != "true":
        return False  # already meeting-capable; leave it alone
    _MODE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _MODE_MARKER.write_text(json.dumps(cur), encoding="utf-8")
    _write_parakeet(streaming=False, model=MEETING_PARAKEET_MODEL)
    _restart_voxtype()
    return True


def exit_meeting_mode() -> bool:
    """Restore the dictation (streaming) config saved by enter_meeting_mode()."""
    if not _MODE_MARKER.exists():
        return False
    try:
        prev = json.loads(_MODE_MARKER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prev = {"streaming": "true"}
    _write_parakeet(
        streaming=str(prev.get("streaming", "true")).lower() == "true",
        model=prev.get("model"),
    )
    _MODE_MARKER.unlink(missing_ok=True)
    _restart_voxtype()
    return True


# ── VoxType CLI plumbing ────────────────────────────────────────────────────
def _vox(*args: str, timeout: int | None = 300) -> str:
    """Run `voxtype <args>`, return clean stdout, raise on failure."""
    try:
        proc = subprocess.run(
            [VOXTYPE_BIN, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise VerbatimError(f"'{VOXTYPE_BIN}' not found on PATH")
    except subprocess.TimeoutExpired:
        raise VerbatimError(f"voxtype {' '.join(args)} timed out after {timeout}s")
    out = _ANSI.sub("", proc.stdout).strip()
    if proc.returncode != 0:
        err = _ANSI.sub("", proc.stderr).strip() or out or "unknown error"
        raise VerbatimError(err.splitlines()[-1] if err else "voxtype failed")
    return out


# ── LLM plumbing (local server or Claude CLI) ───────────────────────────────
def _llm_post(url: str, payload: dict, timeout: int) -> dict:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            pass
        raise VerbatimError(
            f"local AI server error ({e.code}): {detail or e.reason} — "
            f"is model '{LOCAL_MODEL}' pulled? (ollama pull {LOCAL_MODEL})")
    except urllib.error.URLError as e:
        raise VerbatimError(
            f"local AI server unreachable at {LOCAL_URL} ({e.reason}) — "
            "start it (see docs/LOCAL-AI.md) or set VERBATIM_AI_ENGINE=claude")
    except TimeoutError:
        raise VerbatimError(f"local AI analysis timed out after {timeout}s")


def _llm_local(prompt: str, timeout: int = 600) -> str:
    """Run the prompt against the local model server at LOCAL_URL. Prefers
    Ollama's native /api/chat (so reasoning models can have thinking disabled —
    with it on, a meeting pass burns minutes and thousands of tokens musing);
    falls back to OpenAI-compatible /v1/chat/completions for llama.cpp, vLLM
    and friends. Stdlib only — no pip deps."""
    import urllib.error
    msgs = [{"role": "user", "content": prompt}]
    try:
        data = _llm_post(f"{LOCAL_URL}/api/chat", {
            "model": LOCAL_MODEL, "messages": msgs, "think": False,
            "stream": False, "options": {"temperature": 0.2},
        }, timeout)
        out = (data.get("message") or {}).get("content", "")
    except urllib.error.HTTPError:
        data = _llm_post(f"{LOCAL_URL}/v1/chat/completions", {
            "model": LOCAL_MODEL, "messages": msgs,
            "temperature": 0.2, "stream": False,
        }, timeout)
        try:
            out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise VerbatimError(
                f"unexpected response from local AI server: {str(data)[:200]}")
    # Strip a reasoning-model <think> block if the server left one inline.
    out = re.sub(r"^\s*<think>.*?</think>\s*", "", out or "", flags=re.S)
    if not out.strip():
        raise VerbatimError("local AI model returned empty output")
    return out.strip()


def _llm_claude(prompt: str, timeout: int = 600, model: str | None = None) -> str:
    """Run the prompt through the Claude Code CLI headless (cloud)."""
    if not shutil.which(CLAUDE_BIN):
        raise VerbatimError(f"'{CLAUDE_BIN}' (Claude Code CLI) not found")
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", model or CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            # Neutral cwd so Claude doesn't load a project's context/tools.
            cwd=str(NOTES_DIR if NOTES_DIR.exists() else Path.home()),
        )
    except FileNotFoundError:
        raise VerbatimError(f"'{CLAUDE_BIN}' not found on PATH")
    except subprocess.TimeoutExpired:
        raise VerbatimError(f"Claude analysis timed out after {timeout}s")
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or "").strip() or "Claude returned no output"
        raise VerbatimError(err.splitlines()[-1])
    return out


def run_llm(prompt: str, timeout: int = 600, engine: str | None = None,
            model: str | None = None) -> str:
    """Dispatch a prompt to the configured AI engine. There is deliberately no
    silent fallback from local to cloud — if the transcript must stay on the
    network, it stays on the network."""
    eng = engine or AI_ENGINE
    if eng == "local":
        return _llm_local(prompt, timeout=timeout)
    if eng == "claude":
        return _llm_claude(prompt, timeout=timeout, model=model)
    raise VerbatimError(f"unknown AI engine '{eng}' (use 'local' or 'claude')")


# ── Meeting metadata (read-only DB access) ──────────────────────────────────
@dataclass
class Meeting:
    id: str
    title: str
    started_at: int
    ended_at: int | None
    duration_secs: int | None
    status: str
    chunk_count: int
    model: str | None

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def started_dt(self) -> datetime:
        return datetime.fromtimestamp(self.started_at)

    @property
    def duration_human(self) -> str:
        s = self.duration_secs or 0
        return f"{s // 60}m {s % 60}s"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["short_id"] = self.short_id
        d["started_human"] = self.started_dt.strftime("%Y-%m-%d %H:%M")
        d["duration_human"] = self.duration_human
        return d


def _db() -> sqlite3.Connection:
    if not MEETINGS_DB.exists():
        raise VerbatimError(f"meetings DB not found at {MEETINGS_DB}")
    con = sqlite3.connect(f"file:{MEETINGS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_meetings(limit: int = 200, query: str | None = None) -> list[Meeting]:
    sql = ("select id,title,started_at,ended_at,duration_secs,status,"
           "chunk_count,model from meetings")
    params: list = []
    if query:
        sql += " where title like ?"
        params.append(f"%{query}%")
    sql += " order by started_at desc limit ?"
    params.append(limit)
    with _db() as con:
        rows = con.execute(sql, params).fetchall()
    return [Meeting(**{k: r[k] for k in r.keys()}) for r in rows]


def get_meeting(meeting_id: str) -> Meeting | None:
    if meeting_id == "latest":
        ms = list_meetings(limit=1)
        return ms[0] if ms else None
    with _db() as con:
        r = con.execute(
            "select id,title,started_at,ended_at,duration_secs,status,"
            "chunk_count,model from meetings where id=? or id like ?",
            (meeting_id, f"{meeting_id}%"),
        ).fetchone()
    return Meeting(**{k: r[k] for k in r.keys()}) if r else None


def speaker_labels(meeting_id: str) -> dict[int, str]:
    with _db() as con:
        rows = con.execute(
            "select speaker_num,label from speaker_labels where meeting_id=?",
            (meeting_id,),
        ).fetchall()
    return {r["speaker_num"]: r["label"] for r in rows}


# ── Content operations (via CLI) ────────────────────────────────────────────
def _resolve(meeting_id: str) -> str:
    """Turn 'latest' or a short id into a full id VoxType will accept."""
    if meeting_id == "latest":
        return "latest"
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    return m.id


def _speaker_num(header: str) -> int | None:
    """Map a transcript speaker header to its numeric id used by labels."""
    h = header.strip().lower()
    if h == "you":
        return 0
    if h == "remote":
        return 1
    m = re.match(r"speaker[_ ]0*(\d+)$", h)  # "Speaker 2", "SPEAKER_02"
    return int(m.group(1)) if m else None


def _apply_labels(md: str, labels: dict[int, str]) -> str:
    """Replace '### You' / '### Remote' / '### SPEAKER_0N' headers with the
    user's assigned names. VoxType only does this for ML diarization, so we do
    it ourselves for the simple You/Remote scheme too."""
    if not labels:
        return md

    def repl(m: re.Match) -> str:
        num = _speaker_num(m.group(1))
        if num is not None and num in labels:
            return "### " + labels[num]
        return m.group(0)

    return re.sub(r"^###[ \t]+(.+?)[ \t]*$", repl, md, flags=re.M)


def export_transcript(meeting_id: str, fmt: str = "markdown",
                      speakers: bool = True, timestamps: bool = True,
                      apply_labels: bool = True) -> str:
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    args = ["meeting", "export", m.id, "--format", fmt]
    if speakers:
        args.append("--speakers")
    if timestamps:
        args.append("--timestamps")
    out = _vox(*args)
    if apply_labels and fmt == "markdown":
        out = _apply_labels(out, speaker_labels(m.id))
    return out


# ── Speaker attribution (LLM-based, per-utterance) ──────────────────────────
# Acoustic diarization can't separate multiple people merged into "Remote".
# Claude can, from context. We store a per-line override map + a roster, layered
# on top of the base (labeled) transcript. Manual edits adjust individual lines.
_UTT_RE = re.compile(r"^\*\[([^\]]+)\]\*\s*(.*)$")
_HDR_RE = re.compile(r"^###\s+(.+?)\s*$")


def _speaker_seq(md: str) -> list[str]:
    """The speaker header in effect for each utterance line, in order."""
    seq, cur = [], "Speaker"
    for ln in md.splitlines():
        h = _HDR_RE.match(ln)
        if h:
            cur = h.group(1).strip()
            continue
        if _UTT_RE.match(ln.strip()):
            seq.append(cur)
    return seq


def utterances(meeting_id: str) -> list[dict]:
    """Parse the labeled transcript into ordered utterances:
    {i, t, base, text, raw} — base is the label/effective speaker header, raw is
    the underlying acoustic designation (SPEAKER_0N / You / Remote)."""
    md = export_transcript(meeting_id)  # label-applied markdown
    utts, cur, i = [], "Speaker", 0
    for ln in md.splitlines():
        h = _HDR_RE.match(ln)
        if h:
            cur = h.group(1).strip()
            continue
        u = _UTT_RE.match(ln.strip())
        if u:
            utts.append({"i": i, "t": u.group(1), "base": cur, "text": u.group(2)})
            i += 1
    try:
        raw = _speaker_seq(export_transcript(meeting_id, apply_labels=False))
        for u in utts:
            u["raw"] = raw[u["i"]] if u["i"] < len(raw) else u["base"]
    except Exception:
        for u in utts:
            u["raw"] = u["base"]
    return utts


def pretty_speaker(tag: str) -> str:
    """Friendly acoustic designation: 'SPEAKER_00' -> 'Speaker 0'."""
    m = re.match(r"speaker[_ ]0*(\d+)$", (tag or "").strip(), re.I)
    return f"Speaker {int(m.group(1))}" if m else (tag or "").strip()


def _is_raw_tag(name: str) -> bool:
    return bool(re.match(r"(speaker[_ ]0*\d+|remote|you)$",
                         (name or "").strip(), re.I))


def attribution_path(full_id: str) -> Path:
    return ATTRIB_DIR / f"{full_id}.json"


def get_attribution(meeting_id: str) -> dict:
    """Return {'lines': {idx:name}, 'roster': [...]} — empty if none."""
    m = get_meeting(meeting_id)
    if not m:
        return {"lines": {}, "roster": []}
    p = attribution_path(m.id)
    if not p.exists():
        return {"lines": {}, "roster": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("lines", {})
        d.setdefault("roster", [])
        return d
    except (json.JSONDecodeError, OSError):
        return {"lines": {}, "roster": []}


def save_attribution(meeting_id: str, data: dict) -> None:
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    ATTRIB_DIR.mkdir(parents=True, exist_ok=True)
    attribution_path(m.id).write_text(json.dumps(data, indent=1), encoding="utf-8")


def resolved_utterances(meeting_id: str) -> dict:
    """Utterances with the effective speaker (override > base) plus roster and a
    list of all names in use — everything the UI needs to render + edit."""
    utts = utterances(meeting_id)
    attr = get_attribution(meeting_id)
    lines = attr.get("lines", {})
    for u in utts:
        override = lines.get(str(u["i"]))
        u["speaker"] = override or u["base"]
        u["ai"] = bool(override)
    names = []
    for r in attr.get("roster", []):
        n = r.get("name") if isinstance(r, dict) else r
        if n and n not in names:
            names.append(n)
    for u in utts:
        if u["speaker"] not in names:
            names.append(u["speaker"])
    return {"utterances": utts, "roster": attr.get("roster", []), "names": names}


def identify_speakers(meeting_id: str, refresh: bool = False,
                      roster: str | None = None) -> dict:
    """Use Claude to attribute each utterance to a named speaker. Caches the
    result as the attribution override map. Returns the attribution dict.

    roster: optional comma/newline-separated list of who was on the call. When
    given, Claude is told to use ONLY those names — this reliably splits a
    merged far-end channel into the right people and stops it inventing
    speakers. The first name is treated as the recorder ("You")."""
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    if not refresh:
        existing = get_attribution(m.id)
        if existing.get("lines"):
            return existing

    utts = utterances(m.id)
    if not utts:
        raise VerbatimError("transcript is empty — nothing to attribute")
    numbered = "\n".join(f"{u['i'] + 1} [{u['t']}] ({u['base']}): {u['text']}"
                         for u in utts)
    tmpl = (PROMPT_DIR / "identify.md").read_text(encoding="utf-8")
    prompt = tmpl.replace("{{NUMBERED}}", numbered)
    if roster:
        names = [n.strip() for n in re.split(r"[,;\n]", roster) if n.strip()]
        if names:
            prompt = (
                "KNOWN PARTICIPANTS (ground truth — use ONLY these names, do "
                "not invent anyone else; the first is the recorder/\"You\"):\n"
                + "\n".join(f"- {n}" for n in names)
                + "\n\nSplit any merged far-end speaker apart into these people "
                  "from conversational context.\n\n" + prompt
            )

    out = run_llm(prompt, timeout=900)

    data = _extract_json(out)
    if not isinstance(data, dict) or "segments" not in data:
        raise VerbatimError("could not parse speaker attribution from the AI model")
    # Expand compact runs into a per-utterance map (prompt is 1-based → 0-based).
    lines: dict[str, str] = {}
    for seg in data.get("segments", []):
        try:
            a, b = int(seg["from"]) - 1, int(seg["to"]) - 1
            name = str(seg["speaker"]).strip()
        except (KeyError, ValueError, TypeError):
            continue
        for i in range(max(a, 0), min(b, len(utts) - 1) + 1):
            lines[str(i)] = name
    if not lines:
        raise VerbatimError("the AI model returned no usable speaker segments")
    attribution = {"lines": lines, "roster": data.get("roster", [])}
    save_attribution(m.id, attribution)
    return attribution


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced {...} JSON object out of a model response."""
    s = text.find("{")
    if s == -1:
        return None
    depth, instr, esc = 0, False, False
    for i in range(s, len(text)):
        c = text[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def set_line_speaker(meeting_id: str, line: int, name: str) -> None:
    attr = get_attribution(meeting_id)
    attr.setdefault("lines", {})[str(int(line))] = name
    if name and name not in [r.get("name") if isinstance(r, dict) else r
                             for r in attr.get("roster", [])]:
        attr.setdefault("roster", []).append({"name": name, "role": "", "confidence": "manual"})
    save_attribution(meeting_id, attr)


def rename_speaker(meeting_id: str, old: str, new: str) -> None:
    """Rename a person everywhere (line overrides + roster)."""
    attr = get_attribution(meeting_id)
    attr["lines"] = {k: (new if v == old else v) for k, v in attr.get("lines", {}).items()}
    for r in attr.get("roster", []):
        if isinstance(r, dict) and r.get("name") == old:
            r["name"] = new
    save_attribution(meeting_id, attr)


def attributed_transcript_md(meeting_id: str) -> str:
    """Rebuild the transcript grouped by the effective (attributed) speaker."""
    res = resolved_utterances(meeting_id)
    lines, cur = [], None
    for u in res["utterances"]:
        if u["speaker"] != cur:
            cur = u["speaker"]
            lines.append(f"\n### {cur}")
        lines.append(f"*[{u['t']}]* {u['text']}")
    return "\n".join(lines).strip()


def display_transcript(meeting_id: str) -> str:
    """Best-available transcript: attributed if present, else labeled export."""
    if get_attribution(meeting_id).get("lines"):
        return attributed_transcript_md(meeting_id)
    return export_transcript(meeting_id)


def summarize(meeting_id: str, fmt: str = "markdown") -> str:
    """Offline summary via VoxType's built-in Ollama path (loads a local model)."""
    return _vox("meeting", "summarize", _resolve(meeting_id),
                "--format", fmt, timeout=600)


def analysis_cache_path(full_id: str) -> Path:
    return CACHE_DIR / f"{full_id}.md"


def get_cached_analysis(meeting_id: str) -> str | None:
    m = get_meeting(meeting_id)
    if not m:
        return None
    p = analysis_cache_path(m.id)
    return p.read_text(encoding="utf-8") if p.exists() else None


def analyze(meeting_id: str, model: str | None = None, timeout: int = 600,
            refresh: bool = False, engine: str | None = None) -> str:
    """Rich 'Fireflies-style' analysis via the configured AI engine.

    Default engine is 'local' (an OpenAI-compatible server on this machine or
    the LAN — the transcript never leaves the network). 'claude' uses the
    Claude Code CLI instead. Results are cached per meeting; pass refresh=True
    to regenerate.
    """
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    if not refresh:
        cached = analysis_cache_path(m.id)
        if cached.exists():
            return cached.read_text(encoding="utf-8")
    transcript = display_transcript(m.id)
    if not _transcript_body(transcript).strip("_ \n") or \
            _transcript_body(transcript).startswith("_No speech"):
        raise VerbatimError("transcript is empty — nothing to analyze")

    tmpl = (PROMPT_DIR / "analysis.md").read_text(encoding="utf-8")
    ctx = (f"Title: {m.title or 'Untitled'}\n"
           f"Date: {m.started_dt:%A %d %B %Y, %H:%M}\n"
           f"Duration: {m.duration_human}")
    prompt = tmpl.replace("{{CONTEXT}}", ctx).replace("{{TRANSCRIPT}}", transcript)

    out = run_llm(prompt, timeout=timeout, engine=engine, model=model)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    analysis_cache_path(m.id).write_text(out, encoding="utf-8")
    return out


def label_speaker(meeting_id: str, speaker: str, label: str) -> str:
    return _vox("meeting", "label", _resolve(meeting_id), speaker, label)


def delete_meeting(meeting_id: str) -> str:
    return _vox("meeting", "delete", _resolve(meeting_id), "--force")


def rename_meeting(meeting_id: str, title: str) -> None:
    """Rename via direct DB update (VoxType has no rename CLI)."""
    full = get_meeting(meeting_id)
    if not full:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    con = sqlite3.connect(MEETINGS_DB)
    try:
        con.execute("update meetings set title=? where id=?", (title, full.id))
        con.commit()
    finally:
        con.close()


# ── Recording lifecycle ─────────────────────────────────────────────────────
def is_recording() -> str | None:
    """Return the active meeting id if one is recording, else None."""
    out = _vox("meeting", "status", timeout=15)
    m = re.search(r"Meeting ID:\s*([0-9a-f-]+)", out)
    if "recording" in out.lower() and m:
        return m.group(1)
    return None


def start_meeting(title: str | None = None, diarization: str | None = None,
                  wait: bool = True, wait_timeout: int = 20) -> str:
    """Start a meeting and (optionally) block until it is actually recording.

    VoxType loads the ASR model on start (~5s), so 'start' returns before the
    meeting is live; we poll status until it reports recording.
    """
    # Must happen BEFORE the start trigger: meeting mode fails on every chunk
    # if the daemon is running with streaming = true.
    enter_meeting_mode()
    args = ["meeting", "start"]
    if title:
        args += ["--title", title]
    if diarization:
        args += ["--diarization", diarization]
    try:
        _vox(*args, timeout=30)
    except Exception:
        exit_meeting_mode()  # don't strand the daemon out of dictation mode
        raise
    # Archival audio runs alongside VoxType's own capture. Start it after the
    # meeting is confirmed so a failed start doesn't leave an orphan ffmpeg.
    try:
        _, warn = start_audio_capture(title)
        if warn:
            print(f"verbatim: {warn}", file=sys.stderr)
    except (VerbatimError, OSError) as e:
        # Transcription is still running; say so rather than aborting the meeting.
        print(f"verbatim: WARNING - no audio archive for this meeting: {e}",
              file=sys.stderr)
    if not wait:
        return ""
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        mid = is_recording()
        if mid:
            return mid
        time.sleep(1)
    raise VerbatimError(
        "meeting did not reach 'recording' state in time — is meeting mode "
        "enabled and the daemon restarted? (`systemctl --user restart voxtype`)"
    )


def stop_meeting(settle_secs: int = 6) -> None:
    mid = is_recording()
    try:
        _vox("meeting", "stop", timeout=30)
        # give the daemon a moment to finalize the last chunk + persist
        time.sleep(settle_secs)
    finally:
        # Always finalize the archive and hand dictation back its streaming
        # config, even if the stop itself failed.
        try:
            stop_audio_capture(mid)
        except OSError:
            pass
        exit_meeting_mode()


# ── Saved notes (the Fireflies-style artifact) ──────────────────────────────
def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s-]+", "-", text) or "meeting"


def _fs_safe(text: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "-", text).strip() or "meeting"


def note_path(m: Meeting) -> Path:
    stamp = m.started_dt.strftime("%Y-%m-%d %H%M")
    title = _fs_safe(m.title or "Untitled meeting")
    return NOTES_DIR / f"{stamp} - {title}.md"


def _transcript_body(export_md: str) -> str:
    """Strip VoxType's own '# Title' / '## Transcript' headers so we can nest
    the segments under our own heading without duplication."""
    lines = export_md.splitlines()
    out, seen_transcript = [], False
    for ln in lines:
        s = ln.strip()
        if not seen_transcript:
            if s.lower() == "## transcript":
                seen_transcript = True
                continue
            if s.startswith("#") or s == "" or s.startswith("==="):
                continue
        out.append(ln)
    body = "\n".join(out).strip() if seen_transcript else export_md.strip()
    return body or "_No speech was captured._"


def analysis_section(meeting_id: str, engine: str | None = None) -> str:
    """Return the Markdown 'intelligence' section for a note.

    engine: 'local' (rich analysis via the local AI server), 'claude' (Claude
    Code CLI, cloud), 'ollama' (VoxType's basic built-in summary), 'none', or
    None to use the configured default (AI_ENGINE).
    """
    engine = engine or AI_ENGINE
    if engine == "none":
        return ""
    try:
        if engine == "ollama":
            return summarize(meeting_id)
        return analyze(meeting_id, engine=engine)  # local | claude
    except VerbatimError as e:
        return f"_AI analysis unavailable: {e}_"


def transcript_failure(m: "Meeting", transcript_body: str | None = None) -> str | None:
    """Detect a recording that captured audio but transcribed none of it.

    This is the generic alarm for the failure that silently destroyed four
    meetings (see enter_meeting_mode): chunks are captured, every one fails to
    transcribe, and the note is written saying "No speech was captured" — which
    reads like a microphone problem rather than a broken pipeline.

    Any cause produces the same signature: chunks captured, transcript empty.
    Returns a human-readable diagnosis, or None when the meeting looks fine.
    Deliberately NOT raising: the note should still be written, just loudly.

    `transcript_body` is the already-rendered body when the caller has one, so
    building a note doesn't export twice. We go through the export rather than
    reading VoxType's transcript.json — that file is its private format, and
    this module's rule is to drive the stable CLI instead.
    """
    if not m.chunk_count:
        return None  # genuinely nothing captured (mic muted, instant stop)
    try:
        body = (transcript_body if transcript_body is not None
                else _transcript_body(display_transcript(m.id)))
    except VerbatimError:
        return None
    if body.strip() and "_No speech was captured._" not in body:
        return None

    hint = ""
    try:
        cur = _read_parakeet(VOXTYPE_CONFIG.read_text(encoding="utf-8"))
        if cur.get("streaming") == "true":
            hint = ("\n  Likely cause: [parakeet] streaming = true, which makes "
                    "meeting mode reject every chunk.\n  Verbatim normally "
                    "switches this automatically — if you started the meeting "
                    "from a long-running\n  process (the web GUI or tray), "
                    "restart it so it picks up the current code:\n"
                    "    systemctl --user restart verbatim.service")
    except (OSError, VerbatimError):
        pass
    return (f"{m.chunk_count} audio chunks were captured but NONE were "
            f"transcribed — this recording has no content.{hint}\n"
            f"  Check: journalctl --user -u voxtype.service --since "
            f"'{m.started_dt:%Y-%m-%d %H:%M}' | grep -i error")


def build_note(meeting_id: str, engine: str | None = None) -> tuple[Path, str]:
    """Assemble a complete meeting note and write it to NOTES_DIR."""
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")

    transcript = _transcript_body(display_transcript(m.id))
    failure = transcript_failure(m, transcript)
    intelligence = analysis_section(m.id, engine)

    labels = speaker_labels(m.id)
    parts = [
        f"# {m.title or 'Untitled meeting'}",
        "",
        f"- **Date:** {m.started_dt.strftime('%A, %d %B %Y, %H:%M')}",
        f"- **Duration:** {m.duration_human}",
        f"- **Meeting ID:** `{m.id}`",
    ]
    if labels:
        who = ", ".join(f"{v} (speaker {k})" for k, v in sorted(labels.items()))
        parts.append(f"- **Participants:** {who}")
    if m.model:
        parts.append(f"- **Transcribed by:** {m.model}")
    audio = audio_path_for(m.id)
    if audio:
        # The recording is the thing that makes a failed transcript survivable,
        # so link it from the note rather than leaving it findable only by date.
        parts.append(f"- **Audio:** [`{audio.name}`]({audio.as_uri()})")
    stats = speaker_stats(m.id)
    if stats:
        parts.append("- **Talk time:** "
                     + ", ".join(f"{_fmt_speaker(s)} {s['pct']}%" for s in stats[:6]))
    if failure:
        # Put it at the top of the note too, so a broken recording is obvious
        # when the file is opened weeks later, not just in the terminal.
        parts += ["", "> [!WARNING] **This recording failed to transcribe.**",
                  "> " + failure.replace("\n", "\n> ")]
    if intelligence:
        parts += ["", intelligence]
    parts += ["", "---", "", "## Full Transcript", "", transcript, ""]

    text = "\n".join(parts)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = note_path(m)
    path.write_text(text, encoding="utf-8")
    return path, text


# ── Stats, search, and summary-only export ──────────────────────────────────
def _fmt_speaker(s: dict) -> str:
    """'Matt (Speaker 0)' when a tag adds info, else just the name."""
    return f"{s['name']} ({s['tag']})" if s.get("tag") else s["name"]


def speaker_stats(meeting_id: str) -> list[dict]:
    """Per-speaker talk-time (word-count proxy) from the effective transcript.
    Returns [{name, tag, words, lines, pct}] desc — `name` is the person's
    display name, `tag` is the acoustic designation (e.g. 'Speaker 0'), shown in
    brackets only when it adds information (i.e. the speaker has a real name)."""
    m = get_meeting(meeting_id)
    meeting_id = m.id if m else meeting_id          # resolve short id → full
    try:
        utts = resolved_utterances(meeting_id)["utterances"]
    except VerbatimError:
        return []
    agg: dict[str, dict] = {}
    total = 0
    for u in utts:
        name = u.get("speaker") or u.get("base") or "Unknown"
        raw = u.get("raw") or u.get("base") or name
        w = len((u.get("text") or "").split())
        d = agg.setdefault(name, {"name": name, "words": 0, "lines": 0, "tags": {}})
        d["words"] += w
        d["lines"] += 1
        d["tags"][raw] = d["tags"].get(raw, 0) + 1
        total += w
    labels = speaker_labels(meeting_id)          # {num: name} → recover Speaker N
    name_nums: dict[str, list[int]] = {}
    for num, nm in labels.items():
        name_nums.setdefault(nm, []).append(num)
    out = []
    for d in sorted(agg.values(), key=lambda x: x["words"], reverse=True):
        raw = max(d["tags"], key=d["tags"].get) if d["tags"] else d["name"]
        unlabeled = _is_raw_tag(d["name"])
        disp = pretty_speaker(d["name"]) if unlabeled else d["name"]
        praw = pretty_speaker(raw)
        tag = "" if unlabeled or disp == praw else praw
        if not tag and d["name"] in name_nums:   # labeled: reuse the acoustic id
            tag = "Speaker " + "/".join(str(n) for n in sorted(name_nums[d["name"]]))
        out.append({"name": disp, "tag": tag, "words": d["words"],
                    "lines": d["lines"],
                    "pct": round(100 * d["words"] / total) if total else 0})
    return out


def _search_snippet(text: str, idx: int, qlen: int, pad: int = 60) -> str:
    a = max(0, idx - pad)
    b = min(len(text), idx + qlen + pad)
    s = text[a:b].replace("\n", " ").strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def search_meetings(query: str, limit: int = 60) -> list[dict]:
    """Search titles, transcripts and cached analyses. Returns meeting dicts
    augmented with {match_in, snippet}, most-recent first."""
    q = (query or "").strip()
    if not q:
        return []
    ql = q.lower()
    results: list[dict] = []
    for m in list_meetings(limit=800):
        where, snippet = None, ""
        if ql in (m.title or "").lower():
            where = "title"
        else:
            for src in ("transcript", "analysis"):
                try:
                    text = (export_transcript(m.id) if src == "transcript"
                            else (get_cached_analysis(m.id) or ""))
                except Exception:
                    text = ""
                idx = text.lower().find(ql)
                if idx != -1:
                    where, snippet = src, _search_snippet(text, idx, len(q))
                    break
        if where:
            d = m.to_dict()
            d["match_in"] = where
            d["snippet"] = snippet
            d["analyzed"] = analysis_cache_path(m.id).exists()
            results.append(d)
            if len(results) >= limit:
                break
    return results


def summary_markdown(meeting_id: str) -> str:
    """A clean, shareable summary-only note: header + AI analysis, no raw
    transcript. Uses cached analysis; raises if none exists yet."""
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    analysis = get_cached_analysis(m.id)
    if not analysis or not analysis.strip():
        raise VerbatimError("no AI summary yet — analyze the meeting first")
    parts = [
        f"# {m.title or 'Untitled meeting'}",
        "",
        f"- **Date:** {m.started_dt.strftime('%A, %d %B %Y, %H:%M')}",
        f"- **Duration:** {m.duration_human}",
    ]
    stats = speaker_stats(m.id)
    if stats:
        parts.append("- **Talk time:** "
                     + ", ".join(f"{_fmt_speaker(s)} {s['pct']}%" for s in stats[:6]))
    parts += ["", analysis.strip(), ""]
    return "\n".join(parts)


def export_summary(meeting_id: str) -> Path:
    """Write the summary-only note into a `summaries/` subfolder and return its
    path. Kept out of the top-level notes dir on purpose so it doesn't collide
    with (or get mistaken for) the full note in any per-meeting sync/archive."""
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    text = summary_markdown(m.id)
    out_dir = NOTES_DIR / "summaries"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = m.started_dt.strftime("%Y-%m-%d %H%M")
    title = _fs_safe(m.title or "Untitled meeting")
    path = out_dir / f"{stamp} - {title} - summary.md"
    path.write_text(text, encoding="utf-8")
    return path
