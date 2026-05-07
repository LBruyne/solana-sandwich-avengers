#!/bin/bash
# Sync Solana slot leaders into ClickHouse. Runs in the background; logs appended to ./logs/.
# Usage: ./scripts/leader.sh [start_slot]
#   start_slot   starting slot (>= MIN_START_SLOT in config/config.go); default 400000000.
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-400000000}

mkdir -p ./logs
nohup ./sandwich-detector leader -s "${START_SLOT}" -t \
    >> "./logs/leader_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
echo "sandwich-detector leader started (PID: $!), start_slot=${START_SLOT}"
