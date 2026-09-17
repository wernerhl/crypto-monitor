#!/usr/bin/env bash
# Mode (b) collector (build prompt §3.2; work orders 6/7): fetch the raw envelopes that
# GitHub-hosted runners cannot get (Binance and Bybit answer 451/403 from US runner IPs — see
# docs/data_sources.md "Runner reachability") and commit ONLY those raw files. The runner's
# hourly/daily job parses them on its next run. The collector deliberately does NOT compute or
# commit any processed table: two jobs writing the same tables conflicted on 2026-09-10, and
# leaving the working tree dirty stalled `git pull --rebase` on 2026-09-16 (work order 7).
#
# Usage:  scripts/collector.sh hourly     (launchd :09)
#         scripts/collector.sh daily      (launchd 01:45 local)
# Requires: uv, a clone with push rights, .env with keys.
set -euo pipefail
JOB="${1:-hourly}"
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
set -a; [ -f .env ] && . ./.env; set +a

# never carry a dirty working tree into the rebase: discard any local processed/site changes
# (the collector owns none of them) and drop sync-agent duplicate copies
git checkout -q -- data/processed data/archive site 2>/dev/null || true
find data site/data -name '* [0-9].*' -delete 2>/dev/null || true
git pull -q --rebase origin main

# fetch writes raw envelopes under data/raw; ignore a non-zero exit (per-dataset failures are
# isolated) so one blocked venue does not stop the commit of the others
uv run --no-sync monitor fetch "$JOB" || echo "collector $JOB: fetch returned non-zero (per-dataset isolation)" >&2

# stage ONLY the raw envelopes the runners cannot fetch
git add $(git ls-files --others --exclude-standard --modified data/raw | grep -E '/(binance|bybit)_|liquidations_ws' || true) 2>/dev/null || true
# discard anything else the fetch or compute-on-import may have touched
git checkout -q -- data/processed data/archive site 2>/dev/null || true

if ! git diff --cached --quiet; then
  uv run --no-sync python scripts/check_write_set.py collector
  git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "data: collector raw $JOB $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]"
  if ! git pull -q --rebase origin main; then
    echo "collector $JOB: rebase conflict, not auto-resolved: $(git diff --name-only --diff-filter=U | tr '\n' ' ')" >&2
    git rebase --abort; exit 1
  fi
  git push -q origin HEAD:main
fi
echo "collector $JOB done $(date -u +%FT%TZ)"
