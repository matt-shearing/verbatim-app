#!/usr/bin/env bash
# Verbatim installer — symlinks the CLI onto your PATH and installs a desktop
# launcher for the GUI. Idempotent; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$APPS"

chmod +x "$HERE/bin/verbatim"
ln -sf "$HERE/bin/verbatim" "$BIN/verbatim"
echo "✓ linked $BIN/verbatim"

# Desktop entry for the GUI
sed "s|@HERE@|$HERE|g" "$HERE/verbatim.desktop" > "$APPS/verbatim.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
echo "✓ installed GUI launcher (Verbatim) in your app menu"

# Ensure meeting mode is enabled + daemon has loaded it
if command -v voxtype >/dev/null; then
  cfg="$(voxtype config 2>/dev/null || true)"
  if ! printf '%s\n' "$cfg" | grep -A2 '\[meeting\]' | grep -q 'enabled = true'; then
    echo "⚠ VoxType meeting mode looks disabled — enable [meeting] in config.toml"
    echo "  then: systemctl --user restart voxtype.service   (see README)"
  fi
fi

case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "⚠ $BIN is not on your PATH — add it to use 'verbatim' directly." ;;
esac
echo
echo "Done. Try:  verbatim start \"Test call\"   …then…   verbatim stop"
echo "        or:  verbatim gui"
