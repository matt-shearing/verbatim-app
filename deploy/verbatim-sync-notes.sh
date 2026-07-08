#!/usr/bin/env bash
# verbatim-sync-notes.sh
# Mirror Verbatim meeting notes (each note = AI summary + full transcript in one
# .md) from the Verbatim notes dir into an archive folder, using stable
# "<TitleSlug>-<id8-4>.md" filenames.
#
# Verbatim writes a NEW file when a meeting is renamed and orphans the old one,
# so a single meeting id can have several source files. We keep only the NEWEST
# note per meeting id, and prune older same-id copies from the destination so
# renames don't accumulate duplicates. Idempotent; safe to re-run.
#
# Config (env):
#   VERBATIM_NOTES_DIR  source notes dir      (default: ~/Meetings)
#   VERBATIM_SYNC_DEST  archive destination   (default: ~/meeting-notes-archive)
set -euo pipefail

SRC="${VERBATIM_NOTES_DIR:-$HOME/Meetings}"
DEST="${VERBATIM_SYNC_DEST:-$HOME/meeting-notes-archive}"
mkdir -p "$DEST"

uuid_re='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

declare -A best_file best_mtime
noid_files=()

shopt -s nullglob
for f in "$SRC"/*.md; do
  id="$(grep -m1 -oE "$uuid_re" "$f" 2>/dev/null || true)"
  if [ -z "$id" ]; then
    noid_files+=("$f")
    continue
  fi
  mt="$(stat -c %Y "$f")"
  if [ -z "${best_mtime[$id]:-}" ] || [ "$mt" -gt "${best_mtime[$id]}" ]; then
    best_file[$id]="$f"
    best_mtime[$id]="$mt"
  fi
done

slugify() { printf '%s' "$1" | tr -cs 'A-Za-z0-9' '-' | sed 's/^-*//; s/-*$//'; }

sync_one() {  # $1 = source note; $2 = short id ("" if none)
  local f="$1" short="$2" title slug out
  title="$(grep -m1 -E '^# ' "$f" 2>/dev/null | sed 's/^# //' || true)"
  [ -z "$title" ] && title="$(basename "$f" .md)"
  slug="$(slugify "$title")"; [ -z "$slug" ] && slug="meeting"

  if [ -n "$short" ]; then
    out="$DEST/${slug}-${short}.md"
    # Prune any older copy of this same meeting id under a different title.
    for old in "$DEST"/*-"${short}".md; do
      [ "$old" = "$out" ] || { rm -f -- "$old"; echo "pruned stale: $(basename "$old")"; }
    done
  else
    out="$DEST/${slug}.md"
  fi

  if [ ! -f "$out" ] || ! cmp -s "$f" "$out"; then
    cp -- "$f" "$out"
    echo "synced: $(basename "$out")"
    return 0
  fi
  return 1
}

changed=0
for id in "${!best_file[@]}"; do
  sync_one "${best_file[$id]}" "${id:0:13}" && changed=$((changed + 1)) || true
done
for f in "${noid_files[@]}"; do
  sync_one "$f" "" && changed=$((changed + 1)) || true
done

echo "verbatim-sync: $changed file(s) updated -> $DEST"
