#!/bin/bash
# Fetch Jito bundles by slot, then mark sandwich_txs.inBundle.
# Runs both tasks by default; pass a flag to run only one.
# Usage: ./scripts/jito.sh [start_slot] [--fetch-bundle-only | --sync-in-bundle]
#   start_slot             starting slot (>= MIN_START_SLOT in config/config.go); default 400000000.
#   --fetch-bundle-only    fetch bundles only, skip inBundle marking
#   --sync-in-bundle       mark inBundle only, skip bundle fetching
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-400000000}
shift 2>/dev/null || true
EXTRA_FLAGS="$*"

mkdir -p ./logs
nohup ./sandwich-detector jito -s "${START_SLOT}" -t ${EXTRA_FLAGS} \
    >> "./logs/jito_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
echo "sandwich-detector jito started (PID: $!), start_slot=${START_SLOT} ${EXTRA_FLAGS}"
