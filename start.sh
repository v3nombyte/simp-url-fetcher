#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

# Activate or create venv
if [ ! -d venv ]; then
    echo "No virtual environment found — run ./install.sh first"
    exit 1
fi

source venv/bin/activate

# Check for .env
if [ ! -f .env ]; then
    echo "WARNING: No .env file found — JDownloader integration will be unavailable."
    echo "  cp env.example .env  and fill in your credentials to enable it."
    echo ""
fi

echo "Starting Simp URL Fetcher on http://localhost:8080"
echo ""

python3 -c "from app import start_app; start_app()"
