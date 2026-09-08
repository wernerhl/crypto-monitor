#!/usr/bin/env bash
# Collector placement test (work order 3, item 4). Run on a candidate machine (a VPS in a
# region the venues serve) from a clone of the repository:
#
#     bash scripts/collector_placement_test.sh [minutes]      # default 60
#
# It runs the websocket liquidation collector for the given time, then prints, per venue,
# the number of raw messages received and the connected share, and exits 0 when Binance's
# futures `forceOrder` stream delivered rows. Decision rule from the work order: rows → move
# the collector here (no-sleep machine, coverage near 100 %); no rows → record the result in
# docs/changelog.md and stop trying. The `liq_source` label lists only venues with rows
# either way.
set -euo pipefail
MIN="${1:-60}"
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src" OMP_NUM_THREADS=1
uv sync --frozen -q 2>/dev/null || true
echo "collector placement test: $(hostname) $(curl -s https://ifconfig.me 2>/dev/null || echo 'ip unknown') for $MIN min"
timeout "$((MIN * 60))" uv run --no-sync python -m monitor.fetch.liq_ws || true
uv run --no-sync python - <<'PY'
import gzip, json, glob, sys
from collections import defaultdict
msgs, cov = defaultdict(int), defaultdict(float)
for f in sorted(glob.glob("data/raw/*/*/*/*_liquidations_ws_*.json.gz"))[-12:]:
    env = json.loads(gzip.decompress(open(f, "rb").read()))
    v = env["source"]
    msgs[v] += sum(len(r.get("body") or []) for r in env["records"])
    cov[v] += float((env.get("meta", {}).get("coverage") or {}).get("connected_seconds") or 0)
for v in sorted(set(msgs) | set(cov)):
    print(f"{v}: messages={msgs[v]} connected_seconds={cov[v]:.0f}")
ok = msgs.get("binance", 0) > 0
print("RESULT:", "binance forceOrder delivers rows here → move the collector to this machine" if ok else "binance forceOrder silent here → document and stop trying")
sys.exit(0 if ok else 3)
PY
