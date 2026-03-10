#!/bin/bash
# Run slot leader synchronization: fetch and store slot leader information
# Usage: ./scripts/leader.sh [start_slot]
#   start_slot: (Optional) starting slot number (>= 350000000)
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-360000000}

echo "Starting slot leader sync from slot ${START_SLOT}..."
nohup ./watcher leader -s "${START_SLOT}" -t >> ./logs/leader_$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "Leader sync started (PID: $!). Logs: ./logs/"
