#!/bin/bash
# Stop running watcher processes
# Usage: ./scripts/stop.sh [component]
#   component: sandwich | jito | leader | all (default: all)
set -e

COMPONENT=${1:-all}

stop_component() {
    local name="$1"
    local pids
    pids=$(pgrep -f "watcher ${name}" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "Stopping watcher ${name} (PIDs: ${pids})..."
        pkill -f "watcher ${name}"
        echo "Stopped."
    else
        echo "No running watcher ${name} process found."
    fi
}

case "$COMPONENT" in
    sandwich|jito|leader)
        stop_component "$COMPONENT"
        ;;
    all)
        stop_component "sandwich"
        stop_component "jito"
        stop_component "leader"
        ;;
    *)
        echo "Unknown component: $COMPONENT"
        echo "Usage: $0 [sandwich|jito|leader|all]"
        exit 1
        ;;
esac
