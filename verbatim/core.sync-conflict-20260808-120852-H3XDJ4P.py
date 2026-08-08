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
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class VerbatimError(RuntimeError):
    pass


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
    if not shutil.which(CLAUDE_BIN):
        raise VerbatimError(f"'{CLAUDE_BIN}' (Claude Code CLI) not found")

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

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=900,
            cwd=str(NOTES_DIR if NOTES_DIR.exists() else Path.home()),
        )
    except subprocess.TimeoutExpired:
        raise VerbatimError("speaker identification timed out")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise VerbatimError((proc.stderr or "Claude returned no output").splitlines()[-1])

    data = _extract_json(proc.stdout)
    if not isinstance(data, dict) or "segments" not in data:
        raise VerbatimError("could not parse speaker attribution from Claude")
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
        raise VerbatimError("Claude returned no usable speaker segments")
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
            refresh: bool = False) -> str:
    """Rich 'Fireflies-style' analysis via the Claude Code CLI.

    Runs `claude -p` headless with the transcript on stdin. Uses no local model
    (unlike summarize()), so it's light on RAM — the fix for machines where the
    18 GB Ollama model thrashes swap. Results are cached per meeting; pass
    refresh=True to regenerate.
    """
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")
    if not refresh:
        cached = analysis_cache_path(m.id)
        if cached.exists():
            return cached.read_text(encoding="utf-8")
    if not shutil.which(CLAUDE_BIN):
        raise VerbatimError(
            f"'{CLAUDE_BIN}' (Claude Code CLI) not found — install it or use "
            "`--local` for the offline Ollama summary")
    transcript = display_transcript(m.id)
    if not _transcript_body(transcript).strip("_ \n") or \
            _transcript_body(transcript).startswith("_No speech"):
        raise VerbatimError("transcript is empty — nothing to analyze")

    tmpl = (PROMPT_DIR / "analysis.md").read_text(encoding="utf-8")
    ctx = (f"Title: {m.title or 'Untitled'}\n"
           f"Date: {m.started_dt:%A %d %B %Y, %H:%M}\n"
           f"Duration: {m.duration_human}")
    prompt = tmpl.replace("{{CONTEXT}}", ctx).replace("{{TRANSCRIPT}}", transcript)

    # Run in a neutral cwd so Claude doesn't load this project's context/tools.
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", model or CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=str(NOTES_DIR if NOTES_DIR.exists() else Path.home()),
        )
    except FileNotFoundError:
        raise VerbatimError(f"'{CLAUDE_BIN}' not found on PATH")
    except subprocess.TimeoutExpired:
        raise VerbatimError(f"Claude analysis timed out after {timeout}s")
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or "").strip() or "Claude returned no output"
        raise VerbatimError(err.splitlines()[-1])
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
    args = ["meeting", "start"]
    if title:
        args += ["--title", title]
    if diarization:
        args += ["--diarization", diarization]
    _vox(*args, timeout=30)
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
    _vox("meeting", "stop", timeout=30)
    # give the daemon a moment to finalize the last chunk + persist
    time.sleep(settle_secs)


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


def analysis_section(meeting_id: str, engine: str = "claude") -> str:
    """Return the Markdown 'intelligence' section for a note.

    engine: 'claude' (Claude Code CLI, rich, no local RAM), 'ollama' (VoxType's
    local summary), or 'none'.
    """
    if engine == "none":
        return ""
    try:
        if engine == "ollama":
            return summarize(meeting_id)
        return analyze(meeting_id)  # claude
    except VerbatimError as e:
        return f"_AI analysis unavailable: {e}_"


def build_note(meeting_id: str, engine: str = "claude") -> tuple[Path, str]:
    """Assemble a complete meeting note and write it to NOTES_DIR."""
    m = get_meeting(meeting_id)
    if not m:
        raise VerbatimError(f"no meeting matching '{meeting_id}'")

    transcript = _transcript_body(display_transcript(m.id))
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
    stats = speaker_stats(m.id)
    if stats:
        parts.append("- **Talk time:** "
                     + ", ".join(f"{_fmt_speaker(s)} {s['pct']}%" for s in stats[:6]))
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
