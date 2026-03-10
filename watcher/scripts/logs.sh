#!/bin/bash
# View logs for a specific component
# Usage: ./scripts/logs.sh [component] [lines]
#   component: sandwich | jito | leader (default: sandwich)
#   lines:     Number of tail lines to show (default: 50)
set -e

cd "$(dirname "$0")/.."

COMPONENT=${1:-sandwich}
LINES=${2:-50}

# Find the most recent log file for the component
LOG_FILE=$(ls -t ./logs/${COMPONENT}_*.log 2>/dev/null | head -1)

if [ -z "$LOG_FILE" ]; then
    echo "No log files found for component: ${COMPONENT}"
    echo "Available logs:"
    ls ./logs/*.log 2>/dev/null || echo "  (none)"
    exit 1
fi

echo "=== Latest log: ${LOG_FILE} ==="
echo "--- Last ${LINES} lines ---"
tail -n "${LINES}" "$LOG_FILE"
