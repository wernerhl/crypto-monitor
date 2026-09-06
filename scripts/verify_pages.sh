#!/usr/bin/env bash
# Usage: verify_pages.sh <url> <expected git sha>. Retries for up to ~5 minutes
# because Pages propagation lags the deploy step.
set -euo pipefail
URL="$1"; SHA="$2"
for i in $(seq 1 20); do
  BODY=$(curl -fsSL --max-time 30 "$URL" || true)
  if grep -q "data-build-sha=\"${SHA}\"" <<<"$BODY"; then
    echo "live page serves build ${SHA}"
    exit 0
  fi
  echo "attempt $i: live page does not yet carry ${SHA}; sleeping 15s"
  sleep 15
done
echo "::error::Pages URL ${URL} does not serve build ${SHA} after 5 minutes" >&2
exit 1
