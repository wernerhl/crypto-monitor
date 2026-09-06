#!/usr/bin/env bash
# Fails when tracked files plus the .git object store exceed LIMIT_MB (default 800).
# Excludes .venv and other untracked build artefacts on purpose.
set -euo pipefail
# sync-agent duplicate copies ("name 2.ext") must not be tracked
if git ls-files | grep -E " [0-9]\.[A-Za-z0-9]+$" | head -1 | grep -q .; then
  echo "::error::tracked files that look like sync duplicates ('name 2.ext'); delete them" >&2
  git ls-files | grep -E " [0-9]\.[A-Za-z0-9]+$" | head -20 >&2
  exit 1
fi
LIMIT_MB="${LIMIT_MB:-800}"
TRACKED_KB=$(git ls-files -z | xargs -0 du -ck 2>/dev/null | tail -1 | cut -f1)
GIT_KB=$(du -sk .git | cut -f1)
SIZE_MB=$(( (TRACKED_KB + GIT_KB) / 1024 ))
echo "tracked files: $((TRACKED_KB/1024)) MB, .git: $((GIT_KB/1024)) MB, total: ${SIZE_MB} MB (limit ${LIMIT_MB} MB)"
if [ "$SIZE_MB" -gt "$LIMIT_MB" ]; then
  echo "::error::Repository exceeds ${LIMIT_MB} MB. Run the raw-file rotation described in docs/runbook.md (section 'Rotating raw files')." >&2
  exit 1
fi
