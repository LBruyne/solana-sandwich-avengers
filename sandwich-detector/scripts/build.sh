#!/bin/bash
# Build the sandwich-detector binary.
set -e

cd "$(dirname "$0")/.."

go build -o sandwich-detector .
echo "Built ./sandwich-detector"
