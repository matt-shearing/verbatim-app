# Deploy — systemd units & notes sync

Reusable pieces for running Verbatim persistently. Full walkthrough in
[`../docs/OPERATIONS.md`](../docs/OPERATIONS.md).

| File | Purpose |
|------|---------|
| `systemd/verbatim.service` | Run the web GUI (`:8777`) as a user service, autostart on boot |
| `verbatim-sync-notes.sh` | Mirror saved notes into an archive folder (idempotent, dedupes by meeting id) |
| `systemd/verbatim-sync.service` | Oneshot runner for the sync script |
| `systemd/verbatim-sync.path` | Fires the sync the moment a new note is saved |
| `systemd/verbatim-sync.timer` | Periodic sync (safety net for regenerations) |

## Quick install

```bash
# 1) Web GUI as a service (autostart)
cp systemd/verbatim.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verbatim.service
loginctl enable-linger "$USER"          # start before login / survive reboot

# 2) Auto-archive notes (optional)
cp verbatim-sync-notes.sh ~/.local/bin/ && chmod +x ~/.local/bin/verbatim-sync-notes.sh
cp systemd/verbatim-sync.* ~/.config/systemd/user/
export VERBATIM_SYNC_DEST="$HOME/meeting-notes-archive"   # where notes are mirrored
systemctl --user enable --now verbatim-sync.path verbatim-sync.timer
```

## Notes

- **PATH:** `verbatim.service` sets `Environment=PATH=%h/.local/bin:...` so the
  `claude`/`voxtype` CLIs resolve inside the service. Without it, AI analysis
  fails with "'claude' not found". Adjust if your binaries live elsewhere.
- **`VERBATIM_SYNC_DEST`** defaults to `~/meeting-notes-archive`. Set it in the
  service environment (`systemctl --user edit verbatim-sync.service`) or edit
  the script default.
- All units are **user** units (`systemctl --user`), no root required.
