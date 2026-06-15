#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "=== Simp URL Fetcher Install ==="
echo ""

# ── Virtual environment ──
if [ ! -d venv ]; then
    echo "[1/4] Creating Python virtual environment..."
    python3 -m venv venv
else
    echo "[1/4] Virtual environment already exists — skipping"
fi

source venv/bin/activate

# ── Dependencies ──
echo "[2/4] Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# ── Playwright browsers ──
echo "[3/4] Installing Playwright Chromium browser..."
python3 -m playwright install chromium 2>/dev/null || {
    echo "  └─ Playwright install had warnings (non-fatal)"
}

# ── Environment file ──
if [ ! -f .env ]; then
    echo "[4/4] Creating .env from env.example..."
    cp env.example .env
    echo "  └─ Edit .env with your JDownloader MyJDownloader credentials before running"
else
    echo "[4/4] .env already exists — skipping"
fi

echo ""
echo "Install complete."
echo "Run  ./start.sh  or  source venv/bin/activate && python -c \"from app import start_app; start_app()\""
