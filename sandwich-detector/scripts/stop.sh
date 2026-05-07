#!/bin/bash
# Stop running sandwich-detector subcommands.
# Usage: ./scripts/stop.sh [sandwich|jito|leader|all]   (default: all)
set -e

COMPONENT=${1:-all}

stop_component() {
    local name="$1"
    local pids
    pids=$(pgrep -f "sandwich-detector ${name}" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "Stopping sandwich-detector ${name} (PIDs: ${pids})"
        pkill -f "sandwich-detector ${name}"
    else
        echo "No running sandwich-detector ${name} process."
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
        echo "Unknown component: $COMPONENT" >&2
        echo "Usage: $0 [sandwich|jito|leader|all]" >&2
        exit 1
        ;;
esac
