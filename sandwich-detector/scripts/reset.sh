#!/bin/bash
# Drop all sandwich-detector tables in ClickHouse.
# WARNING: deletes all data. Tables are recreated on the next run.
set -e

cd "$(dirname "$0")/.."

echo "This will DROP all sandwich-detector tables in the configured database."
read -p "Type 'yes' to continue: " confirmation
if [ "$confirmation" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

./sandwich-detector reset
