# Verbatim

A **local, private "Fireflies"** for your own machine. Verbatim records your
meetings *off your desktop* — both your microphone **and** the other side's
audio (system output) — then transcribes, diarizes, summarizes, and files a
tidy Markdown note. Nothing leaves your computer. No bot joins your call.

It works on any web-based conferencing tool (Gather, Meet, Zoom-in-browser,
Teams, Discord, …) because it captures the audio at the OS level, not inside
the meeting app.

## How it works

Verbatim is a thin, opinionated layer over **[VoxType](https://voxtype.io)**'s
built-in `meeting` mode, which already does the heavy lifting:

- **Dual capture** — your mic (`You`) + the system-output loopback (`Remote`),
  with GTCRN neural **echo cancellation** so your voice doesn't double.
- **Local GPU transcription** — Parakeet/Whisper via ONNX (MIGraphX on this
  AMD Radeon box; ~15× real-time).
- **Diarization** — `simple` (You/Remote, great for 1:1) or `ml` (ECAPA
  multi-speaker embeddings).
- **Diarization** default is `ml` (ECAPA embeddings) so multiple people are
  separated, not just You vs the merged "Remote" side.

Verbatim adds the friendly workflow on top: a one-command start/stop, a
Fireflies-style saved note (`~/Meetings/YYYY-MM-DD HHMM - Title.md`), and a
localhost browser GUI to name, browse, search, analyze, and label speakers.

### AI analysis (the "Fireflies brain")

The `stop`/`note`/`ai` commands run an AI pass over the transcript that
produces a TL;DR, participant identification (real names inferred from
context), topic-grouped notes with timestamps, decisions, an owner-attributed
action-item table, open questions/risks, notable quotes, and a ready-to-send
follow-up email draft.

**By default this runs on a local model on our own hardware** — an
OpenAI-compatible server (Ollama on the gpu-node GPU node, reached over an SSH
tunnel), so **transcripts never leave the network**. See
**[`docs/LOCAL-AI.md`](docs/LOCAL-AI.md)** for the architecture, model choice
(`qwen3.5:9b` primary / `gemma4:12b` fallback) and configuration. The former
cloud path via the Claude Code CLI is still available with `--claude` or
`VERBATIM_AI_ENGINE=claude`. The analysis prompt lives in
`verbatim/prompts/analysis.md` (edit to taste).

```
┌────────────┐   mic + system loopback   ┌───────────────┐   markdown note
│  verbatim  │ ────────────────────────▶ │  VoxType       │ ─────────────────▶ ~/Meetings/*.md
│  CLI / GUI │   start/stop/export/...    │  meeting mode  │   + Ollama summary
└────────────┘                            └───────────────┘   (SQLite index)
```

## Install

```bash
./install.sh          # symlinks `verbatim` into ~/.local/bin + adds a GUI launcher
```

### Dependencies

Verbatim's own code is **pure Python standard library** (no pip packages) — it
orchestrates external tools rather than embedding them. You need:

| Dependency | Why | Notes |
|------------|-----|-------|
| **Python 3.11+** | runs the CLI + GUI | stdlib only |
| **[VoxType](https://voxtype.io)** with `[meeting]` mode enabled | audio capture, transcription, diarization | the engine Verbatim drives; runs as `voxtype.service` |
| **PipeWire** (`parec`) | system-audio loopback capture | VoxType uses it |
| **Local AI server** (Ollama on gpu-node) | AI analysis + speaker identification | OpenAI-compatible endpoint; see [`docs/LOCAL-AI.md`](docs/LOCAL-AI.md) |
| **[Claude Code CLI](https://claude.com/claude-code)** (`claude`) (optional) | cloud AI path via `--claude` | only if you opt out of local processing |
| **PySide6** (optional) | the desktop overlay + tray icon | `pip install PySide6`; not needed for the CLI or web UI |
| GPU transcription (optional) | ~15× real-time | ONNX; MIGraphX on AMD ROCm here, CPU otherwise |

GPU/ROCm and the ECAPA/Parakeet models are all handled by VoxType, not by
Verbatim.

## Deployment (autostart + notes archiving)

To keep the web GUI always running (autostart on boot) and optionally
auto-archive every note to another folder, see **[`docs/OPERATIONS.md`](docs/OPERATIONS.md)**
and the ready-to-use units in **[`deploy/`](deploy/)**. That doc also explains
the **speaker-naming model** (base acoustic labels vs. AI attribution overrides)
and how to correct wrong names — the thing you'll reach for most.

## Usage

```bash
verbatim start "Weekly sync"     # begins recording (waits until capture is live)
verbatim stop                    # stop → transcribe → Claude analysis → save & open note
verbatim toggle                  # start if idle, else stop+save  (bind to a hotkey)

verbatim ai latest               # print Fireflies-style analysis (local model)
verbatim ai latest --claude      # opt into cloud analysis via Claude Code
verbatim list                    # past meetings
verbatim show latest             # print a transcript
verbatim label latest 1 "Alice"  # name a speaker
verbatim rename latest "Retro"   # rename
verbatim note latest             # rebuild the saved .md note (transcript + AI)
verbatim note latest --no-ai     # transcript-only note (instant)
verbatim gui                     # open the browser UI (http://127.0.0.1:8777)

verbatim identify latest --who "Me, Kevin"   # AI: split & name speakers (roster optional)
verbatim stats latest            # talk-time breakdown per speaker
verbatim search "pricing"        # search titles, transcripts & summaries
verbatim export latest           # shareable summary-only .md (→ ~/Meetings/summaries)
verbatim overlay                 # floating "recording" overlay (needs PySide6)
verbatim tray                    # system-tray icon + auto overlay (needs PySide6)
```

Notes are written to `~/Meetings` (override with `VERBATIM_NOTES_DIR`). The
local model defaults to `qwen3.5:9b` (override with `VERBATIM_LOCAL_MODEL`);
the opt-in Claude model defaults to `sonnet` (`VERBATIM_CLAUDE_MODEL`).

## The GUI (`verbatim gui`)

A localhost single-page app (stdlib only, bound to `127.0.0.1:8777`). It has a
**light/dark toggle** (top-right, remembered), a per-meeting **talk-time** bar,
**full-text search** across transcripts & summaries (with snippets), and a
one-click **summary-only export** for sharing:

- **Sidebar** — searchable list of meetings, with an **✨ analyzed** badge.
- **Start/Stop** recording from the header, with a live recording indicator.
- **Meeting view** with two tabs:
  - **AI Summary** — the Claude "Fireflies brain" rendered as structured
    sections: TL;DR, participants (as chips), topic notes, decisions, an
    **action-items table**, risks, quotes, and the follow-up email. Cached, so
    it reloads instantly. **Analyze / Re-analyze** runs as a **background job**
    with polling, so long meetings (which can take minutes) won't stall the page
    or drop if you navigate away.
  - **Transcript** — the full diarized transcript with speaker headers, showing
    your assigned names.
- **🧠 Identify speakers (AI)** — the big one for multi-person calls. Acoustic
  diarization can't split several people merged into one "Remote" channel;
  Claude can, from conversational context. This runs an attribution pass that
  assigns **every utterance to a named person**, then rewrites the transcript
  accordingly. In the Transcript tab each speaker heading becomes a **dropdown**
  — correct any block, add a new name, or rename a person everywhere. It uses a
  compact "speaker-runs" format so it scales to hour-long meetings, and runs as
  a background job. Attributed names flow into the saved note and re-analyses.
- **Label speakers** — a simpler click-only editor for the acoustic labels
  (You→your name, Remote→…, or `ml` SPEAKER_0/1/2…). Verbatim applies these to
  `simple` transcripts itself, since VoxType only does that for `ml` diarization.
- **Copy summary**, **Save .md note**, inline **rename**, and **delete** (also
  clears the cached analysis).

> Restart the server after upgrading (`Ctrl-C` then `verbatim gui`) — the page is
> served from the running process, so a stale server shows the old UI.

## Desktop extras — overlay & tray (optional, needs PySide6)

So you never forget a recording is running:

- **Recording overlay** (`verbatim overlay`) — a tiny frameless, always-on-top,
  **draggable** pill showing ● REC, an elapsed timer and animated level bars,
  with one-click start/stop. It remembers where you put it, auto-pops when a
  recording starts, and gets out of the way otherwise.
- **Tray icon** (`verbatim tray`) — a system-tray icon (grey idle → red while
  recording) with Start/Stop, Show overlay, and Open Verbatim (web). It watches
  the recording state and **auto-shows the overlay** whenever recording begins,
  whoever started it (CLI, tray or web).

Start the tray on login:

```bash
cp deploy/verbatim-tray.desktop ~/.config/autostart/   # set Exec= to an absolute path if ~/.local/bin isn't on the session PATH
verbatim tray &                                        # …or just run it now
```

Both need `PySide6` and a display; they use XWayland (`QT_QPA_PLATFORM=xcb`) for
reliable positioning + always-on-top on KDE Wayland.

## KDE global shortcut (one-key start/stop)

System Settings → **Shortcuts** → **Custom Shortcuts** → *Edit → New → Global
Shortcut → Command/URL*:

- **Trigger:** e.g. `Meta+R`
- **Action:** `~/.local/bin/verbatim toggle`

Now one keypress starts a meeting; the next stops it and files the note. Desktop
notifications confirm start/stop.

## Configuration

Meeting behaviour lives in `~/.config/voxtype/config.toml` under `[meeting]`,
`[meeting.audio]`, `[meeting.diarization]`, `[meeting.summary]`:

- `[meeting.audio] loopback_device = "auto"` picks your **default sink's**
  monitor. If you switch output devices mid-session, `auto` follows the default
  at *meeting start* time.
- `[meeting.diarization] backend` — `"ml"` (default here; separates multiple
  speakers via ECAPA embeddings) or `"simple"` (You/Remote only, best for 1:1).
  The ECAPA model auto-downloads on the first `ml` meeting.
- `[meeting.summary]` (VoxType's own Ollama summary) is now only used by
  `verbatim ai --ollama-summary`. The default AI path is Verbatim's own local
  engine (see `docs/LOCAL-AI.md`).

> ⚠️ **VoxType caches config at daemon startup.** After editing `config.toml`
> run `systemctl --user restart voxtype.service`, or meeting mode won't see the
> change (symptom: `voxtype meeting start` reports "No meeting in progress").

## Design notes

- Verbatim never parses VoxType's private transcript storage — it drives the
  stable `voxtype meeting` CLI for content and reads the meetings SQLite DB
  (read-only) only for fast listing/metadata.
- The GUI is Python-stdlib only (no pip deps) and binds to `127.0.0.1`.

## Files

| Path | What |
|------|------|
| `bin/verbatim` | CLI launcher (no install needed) |
| `verbatim/core.py` | shared logic (capture, export, summarize, notes) |
| `verbatim/cli.py` | command-line interface |
| `verbatim/gui.py` | localhost web GUI + JSON API |
| `verbatim.desktop` | app-menu launcher for the GUI |
| `install.sh` | symlink + desktop-entry installer |
