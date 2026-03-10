#!/bin/bash
# Build the watcher binary
set -e

cd "$(dirname "$0")/.."

echo "Building watcher..."
go build -o watcher .
echo "Watcher built successfully."
