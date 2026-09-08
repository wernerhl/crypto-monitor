#!/usr/bin/env bash
# Mode (b) collector (build prompt §3.2, §6): run the fetch jobs from a machine where every
# venue is reachable (GitHub-hosted runners are US addresses: Binance futures and Bybit
# answer 451/403 there — see docs/data_sources.md "Runner reachability"). The script pulls,
# fetches the requested job, computes, commits as the bot and pushes. Idempotent per bucket,
# so overlapping with the Actions jobs is safe (concurrency is on the git push, which rebases).
#
# Usage:  scripts/collector.sh hourly     (cron: 9 * * * *)
#         scripts/collector.sh daily      (cron: 40 1 * * *)
# Requires: uv, a clone with push rights (fine-grained token or SSH key), .env with keys.
set -euo pipefail
JOB="${1:-hourly}"
cd "$(dirname "$0")/.."
git pull -q --rebase origin main
export PYTHONPATH="$PWD/src"
set -a; [ -f .env ] && . ./.env; set +a
uv run --no-sync monitor fetch "$JOB"
uv run --no-sync monitor compute "$JOB"
[ "$JOB" = hourly ] && { set -a; . ./.env 2>/dev/null; set +a; uv run --no-sync monitor alerts --dry-run >/dev/null 2>&1 || true; }
find data site/data -name '* [0-9].*' -delete 2>/dev/null || true
git add data/ site/data/ site/alerts.xml 2>/dev/null || git add data/ site/data/
if ! git diff --cached --quiet; then
  uv run --no-sync python scripts/check_write_set.py "$JOB"
  git -c user.name=crypto-monitor-bot -c user.email=crypto-monitor-bot@users.noreply.github.com commit -qm "data: collector $JOB $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]"
  if ! git pull -q --rebase origin main; then
    echo "collector $JOB: rebase conflict, not auto-resolved: $(git diff --name-only --diff-filter=U | tr '\n' ' ')" >&2
    git rebase --abort; exit 1
  fi
  git push -q origin HEAD:main
fi
echo "collector $JOB done $(date -u +%FT%TZ)"
