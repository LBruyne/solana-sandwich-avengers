#!/bin/bash
# Run Jito bundle monitoring: fetch bundles and/or sync sandwich inBundle marks
# Usage: ./scripts/jito.sh [start_slot] [--fetch-bundle-only | --sync-in-bundle]
#   start_slot:          (Optional) starting slot number (>= 350000000)
#   --fetch-bundle-only: Only fetch/sync Jito bundles by slot
#   --sync-in-bundle:    Only sync sandwich inBundle marks
#   (default):           Run both tasks
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-362900000}
shift 2>/dev/null || true
EXTRA_FLAGS="$*"

echo "Starting Jito bundle monitoring from slot ${START_SLOT} ${EXTRA_FLAGS}..."
nohup ./watcher jito -s "${START_SLOT}" -t ${EXTRA_FLAGS} >> ./logs/jito_$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "Jito monitoring started (PID: $!). Logs: ./logs/"
