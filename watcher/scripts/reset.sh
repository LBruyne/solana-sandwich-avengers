#!/bin/bash
# Reset the database: drop all tables and recreate them
# WARNING: This will delete ALL data. Use with caution!
set -e

cd "$(dirname "$0")/.."

echo "⚠️  This will DELETE all data in the database!"
read -p "Are you sure you want to continue? (yes/no): " confirmation
if [ "$confirmation" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo "Resetting the database..."
./watcher reset
echo "Database reset complete."
