#!/usr/bin/env bash
# install.sh — AutoMyFeishu dependency bootstrapper
# Usage: bash install.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "=== AutoMyFeishu Installer ==="
echo ""

# ── 1. Locate python3 ──────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
  echo "[ERROR] python3 not found. Please install Python 3.10+ and re-run."
  exit 1
fi

# ── 2. Verify Python >= 3.10 ───────────────────────────────────────────────────
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if ! "$PYTHON" -c "import sys; assert sys.version_info >= (3, 10), 'too old'" 2>/dev/null; then
  echo "[ERROR] Python 3.10+ required, but found Python ${PY_VER}."
  echo "        Please upgrade Python and re-run."
  exit 1
fi
echo "[OK] Python ${PY_VER}"

# ── 3. Create virtual environment ─────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "[...] Creating virtual environment at .venv/ ..."
  "$PYTHON" -m venv .venv
  echo "[OK] Virtual environment created"
else
  echo "[OK] Virtual environment already exists at .venv/"
fi

# ── 4. Install / update dependencies ──────────────────────────────────────────
echo "[...] Installing dependencies from requirements.txt ..."
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt
echo "[OK] Dependencies installed"

# ── 5. Ensure .env exists ─────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo ""
    echo "[NOTICE] .env has been created from .env.example."
    echo "         Please edit .env and fill in your real Feishu and LLM tokens:"
    echo "           nano .env"
  else
    echo "[WARNING] .env.example not found. Please create .env manually."
  fi
else
  echo "[OK] .env already exists — skipping copy"
fi

# ── 6. Create logs directory (required by systemd unit) ───────────────────────
mkdir -p logs
echo "[OK] logs/ directory ready"

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your credentials (if you haven't already):"
echo "       nano .env"
echo "  2. Register the systemd service (requires root):"
echo "       sudo bash manage.sh install"
echo "  3. Start the service:"
echo "       sudo bash manage.sh start"
echo "  4. Verify it is running:"
echo "       sudo bash manage.sh status"
echo ""
echo "For development / manual launch:"
echo "  bash launch.sh"
