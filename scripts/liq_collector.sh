#!/usr/bin/env bash
# Resident websocket liquidation collector (work order B1): Binance `!forceOrder@arr` and Bybit
# `allLiquidation.*` into hourly raw envelopes under data/raw. launchd keeps it alive
# (scripts/launchd/com.wernerhl.crypto-monitor.liq.plist, installed by
# scripts/install_collector_macos.sh). The hourly collector run commits the raw files and
# parses them into the `liquidations` table. Restarts merge into the open hour's envelope.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
exec uv run --no-sync python -m monitor.fetch.liq_ws
