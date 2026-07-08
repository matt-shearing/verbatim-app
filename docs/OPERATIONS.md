# Verbatim — Operations & Deployment

Everything needed to run Verbatim as a persistent service, understand how
speaker names work (and fix them when they're wrong), and auto-archive notes.
The reusable units and scripts referenced here live in [`../deploy/`](../deploy/).

---

## 1. Run the web GUI as a service (autostart on boot)

By default `verbatim gui` runs only while you have a terminal open. To keep the
web app (http://127.0.0.1:8777) always available — including after a reboot,
before you log in — run it as a **systemd user service**.

Unit: [`deploy/systemd/verbatim.service`](../deploy/systemd/verbatim.service)

```bash
cp deploy/systemd/verbatim.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verbatim.service
# To survive reboots without an interactive login, enable lingering once:
loginctl enable-linger "$USER"
```

Manage / debug:

```bash
systemctl --user status verbatim.service
systemctl --user restart verbatim.service      # after updating the app source
journalctl --user -u verbatim.service -f
```

### ⚠️ The PATH gotcha (most common failure)

A systemd **service** does not inherit your interactive shell's `PATH`. Verbatim
shells out to the `claude` and `voxtype` CLIs, which typically live in
`~/.local/bin`. If that directory isn't on the service's `PATH`, AI analysis
fails with:

```
⚠ 'claude' (Claude Code CLI) not found — install it or use `--local` …
```

The shipped unit fixes this with:

```ini
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin
```

Adjust if your `claude`/`voxtype` binaries live elsewhere (`which claude`).

---

## 2. How speaker names work — and how to fix them

Speaker naming is **two layers**, which is the key thing to understand when a
transcript shows the wrong people.

1. **Base acoustic labels** — VoxType's diarization clusters the audio into
   speakers (`speaker_num → name`), stored in the meetings SQLite DB. For a
   clean 1:1 these are usually right; for a call where several people share the
   far end ("Remote"), acoustic clustering **cannot** split them — they get
   merged into one label.

2. **Attribution overrides** — an optional per-line map
   (`~/.local/share/verbatim/attribution/<meeting-id>.json`) produced by the
   **🧠 Identify speakers (AI)** pass. Claude reads the whole transcript and
   assigns *every line* to a named person from conversational context — this is
   what splits a merged "Remote" channel into real people.

The displayed transcript and the AI analysis use the **attribution layer if it
has any lines, otherwise the base labels**. The saved note's *Participants*
header is derived from the **base labels**.

### Failure modes & fixes

**(a) Base labels are already correct, but a bad AI attribution invented people**
(e.g. Claude invented a phantom "Sales Rep" for hundreds of lines and buried the
real speaker). Fix = drop the bad override so it falls back to the good base labels,
then regenerate:

```bash
rm ~/.local/share/verbatim/attribution/<full-meeting-id>.json
rm ~/.local/share/verbatim/analysis/<full-meeting-id>.md   # clear cached analysis
verbatim note <meeting-id>                                 # rebuild note + re-analyze
```

**(b) The far end is one merged voice that needs splitting into known people.**
Re-run the identify pass — and it works far better if you tell it who was on the
call. The built-in prompt is [`verbatim/prompts/identify.md`](../verbatim/prompts/identify.md);
prepending a short "KNOWN PARTICIPANTS: …" roster to it before the `{{NUMBERED}}`
transcript reliably splits merged speakers and prevents invented names. Save the
result with `core.save_attribution()`, then rebuild the note.

**(c) Simple rename / relabel** (names are just wrong, not mis-split):

```bash
verbatim label <meeting-id> <speaker_num> "Real Name"   # base acoustic label
verbatim rename <meeting-id> "New meeting title"
```

Or use the dropdowns in the GUI Transcript tab (per-block, add name, rename
everywhere).

> **Always confirm *which* meeting you're editing first** (`verbatim list`).
> Note files are named from the title, and a renamed meeting leaves an orphaned
> old note file behind — two files, same meeting id. Editing the wrong one is an
> easy and costly mistake.

---

## 3. Auto-archive notes (transcript + summary) to another folder

Verbatim writes one Markdown note per meeting (AI summary **and** full
transcript in a single file) to `~/Meetings/`. To automatically mirror those
into another location (e.g. a docs/context repo), use the sync tooling:

- Script: [`deploy/verbatim-sync-notes.sh`](../deploy/verbatim-sync-notes.sh)
- Trigger on each new note: [`deploy/systemd/verbatim-sync.path`](../deploy/systemd/verbatim-sync.path)
- Periodic safety net (catches in-place regenerations):
  [`deploy/systemd/verbatim-sync.timer`](../deploy/systemd/verbatim-sync.timer)
- Runner: [`deploy/systemd/verbatim-sync.service`](../deploy/systemd/verbatim-sync.service)

Install:

```bash
cp deploy/verbatim-sync-notes.sh ~/.local/bin/ && chmod +x ~/.local/bin/verbatim-sync-notes.sh
cp deploy/systemd/verbatim-sync.* ~/.config/systemd/user/
# Where notes are mirrored to (create it or point at an existing folder):
export VERBATIM_SYNC_DEST="$HOME/meeting-notes-archive"   # set persistently for the service
systemctl --user daemon-reload
systemctl --user enable --now verbatim-sync.path verbatim-sync.timer
```

Behaviour:

- Names mirrored files `<TitleSlug>-<id8-4>.md` (stable across regenerations).
- **Dedupes by meeting id** (newest note wins) and prunes stale same-id copies
  left behind by renames — so renames don't accumulate duplicates.
- Idempotent; only copies when content changed.
- **One-way** (`~/Meetings` → destination). Edit the *destination* copy at your
  own risk — the timer can overwrite it. Treat mirrored notes as read-only.

Set `VERBATIM_SYNC_DEST` in the environment the service sees (e.g. via a
`systemctl --user edit verbatim-sync.service` drop-in with
`Environment=VERBATIM_SYNC_DEST=/path`), or edit the default in the script.

---

## 4. State & file locations reference

| What | Path |
|------|------|
| App source (this repo) | `~/dev/verbatim-app` |
| CLI on PATH | `~/.local/bin/verbatim` → `bin/verbatim` |
| Saved notes (summary + transcript) | `~/Meetings/*.md` (`VERBATIM_NOTES_DIR`) |
| Meetings DB + models | `~/.local/share/verbatim/` |
| AI attribution overrides | `~/.local/share/verbatim/attribution/<id>.json` |
| Cached AI analysis | `~/.local/share/verbatim/analysis/<id>.md` |
| Recordings | `~/.verbatim/recordings/` |
| VoxType meeting config | `~/.config/voxtype/config.toml` (`[meeting]`) |
| systemd user units | `~/.config/systemd/user/verbatim*.{service,path,timer}` |
