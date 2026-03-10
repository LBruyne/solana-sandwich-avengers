#!/bin/bash
# Run sandwich detection: fetch blocks and detect in-block / cross-block sandwiches
# Usage: ./scripts/sandwich.sh [start_slot]
#   start_slot: (Optional) starting slot number (>= 350000000)
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-370453692}

echo "Starting sandwich detection from slot ${START_SLOT}..."
nohup ./watcher sandwich -s "${START_SLOT}" >> ./logs/sandwich_$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "Sandwich detection started (PID: $!). Logs: ./logs/"
