#!/usr/bin/env bash
# Fails when tracked files plus the .git object store exceed LIMIT_MB (default 2000).
# Excludes .venv and other untracked build artefacts on purpose.
#
# The limit was raised from 800 to 2000 MB on 2026-09-14 (owner's decision, work order 6
# follow-up). The size is almost entirely .git history, not the working tree (tracked ~50 MB,
# .git ~860 MB): every hourly bot commit rewrites ~300 processed and archive parquet files, so
# the pack grows ~30 MB/day and raising the ceiling only defers the wall, it does not remove it.
# The durable fix is to stop versioning the derived parquet churn (squash the data history onto
# a fresh branch, or move data/processed and data/archive out of git into a release/branch/LFS
# store). GitHub tolerates repos to roughly 5 GB before it intervenes, so 2000 MB keeps the
# guardrail meaningful while buying about a month at the current rate.
set -euo pipefail
# sync-agent duplicate copies ("name 2.ext") must not be tracked
if git ls-files | grep -E " [0-9]\.[A-Za-z0-9]+$" | head -1 | grep -q .; then
  echo "::error::tracked files that look like sync duplicates ('name 2.ext'); delete them" >&2
  git ls-files | grep -E " [0-9]\.[A-Za-z0-9]+$" | head -20 >&2
  exit 1
fi
LIMIT_MB="${LIMIT_MB:-2000}"
TRACKED_KB=$(git ls-files -z | xargs -0 du -ck 2>/dev/null | tail -1 | cut -f1)
GIT_KB=$(du -sk .git | cut -f1)
SIZE_MB=$(( (TRACKED_KB + GIT_KB) / 1024 ))
echo "tracked files: $((TRACKED_KB/1024)) MB, .git: $((GIT_KB/1024)) MB, total: ${SIZE_MB} MB (limit ${LIMIT_MB} MB)"
if [ "$SIZE_MB" -gt "$LIMIT_MB" ]; then
  echo "::error::Repository exceeds ${LIMIT_MB} MB (mostly .git history from parquet churn, not raw files). Rotating raw files will not help much; squash the data history or move the derived parquet out of git (docs/runbook.md, 'Repository size')." >&2
  exit 1
fi
