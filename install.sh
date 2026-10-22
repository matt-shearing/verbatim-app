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

# Offline transcriber: a venv on top of the system onnxruntime + numpy, adding
# onnx-asr (pinned) to run VoxType's Parakeet model on recorded files.
VENV="$HOME/.local/share/verbatim/venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -q --no-deps onnx-asr==0.12.0
"$VENV/bin/python" -c "import onnx_asr, onnxruntime, numpy" \
  && echo "✓ transcriber ready ($VENV)"
for m in parakeet-tdt-0.6b-v3 ecapa_tdnn.onnx; do
  [ -e "$HOME/.local/share/voxtype/models/$m" ] \
    || echo "⚠ missing VoxType model $m — download it with: voxtype setup model"
done

# App launcher (opens the GUI in its own window) + icon
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICONS"
cp "$HERE/assets/verbatim.svg" "$ICONS/verbatim.svg"
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" &>/dev/null || true
sed "s|@HERE@|$HERE|g" "$HERE/verbatim.desktop" > "$APPS/verbatim.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
echo "✓ installed the Verbatim app launcher"

# Omarchy bar plugin
PLUGINS="$HOME/.config/omarchy/plugins"
if [ -d "$PLUGINS" ]; then
  ln -sfn "$HERE/omarchy-plugin" "$PLUGINS/contra.verbatim"
  echo "✓ linked bar plugin contra.verbatim"
  grep -q '"contra.verbatim"' "$HOME/.config/omarchy/shell.json" 2>/dev/null \
    || echo "  add it to the bar with: omarchy bar put contra.verbatim"
fi

case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "⚠ $BIN is not on your PATH — add it to use 'verbatim' directly." ;;
esac
echo
echo "Done. Click the mic on the bar, or:  verbatim start \"Test call\"  …then…  verbatim stop"
echo "        open the app:  verbatim app"
