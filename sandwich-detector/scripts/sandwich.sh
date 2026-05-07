#!/bin/bash
# Start sandwich detection in the background. Logs are appended to ./logs/.
# Usage: ./scripts/sandwich.sh [start_slot]
#   start_slot   starting slot (>= MIN_START_SLOT in config/config.go); default 400000000.
set -e

cd "$(dirname "$0")/.."

START_SLOT=${1:-400000000}

mkdir -p ./logs
nohup ./sandwich-detector sandwich -s "${START_SLOT}" \
    >> "./logs/sandwich_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
echo "sandwich-detector sandwich started (PID: $!), start_slot=${START_SLOT}"
