#!/usr/bin/env bash
# verbatim-autocommit.sh
# Keep the verbatim-app repo tracking the working tree: commit & push any local
# changes. Runs periodically (verbatim-autocommit.timer). No-op when clean.
#
# Config (env):
#   VERBATIM_REPO   path to the repo (default: ~/dev/verbatim-app)
set -euo pipefail

REPO="${VERBATIM_REPO:-$HOME/dev/verbatim-app}"
cd "$REPO"

# Nothing staged or unstaged? Done.
if [ -z "$(git status --porcelain)" ]; then
  echo "verbatim-autocommit: clean, nothing to do"
  exit 0
fi

git add -A
files="$(git diff --cached --name-only | tr '\n' ' ' | sed 's/ *$//')"
n="$(git diff --cached --name-only | wc -l | tr -d ' ')"
git commit -q -m "auto-sync: ${n} file(s) changed — ${files}"

# Push; tolerate transient failures (retried on the next run).
if git push -q origin HEAD; then
  echo "verbatim-autocommit: committed & pushed — ${files}"
else
  echo "verbatim-autocommit: committed locally; push failed, will retry next run" >&2
  exit 0
fi
