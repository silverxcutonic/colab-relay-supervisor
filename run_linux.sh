#!/usr/bin/env bash
# ==============================================================================
# Google Colab 24/7 Autonomous Relay Runner for Linux & WSL2
# - Interactive [1-3] Starting Account Selection
# - 3-Account 8-Hour Round-Robin Shift Loop (24/7 Continuous)
# - In-VM Telegram Bot Supervisor & Mobile Emergency Backup Button
# - Safe Smart Hydration & 50s Ping-Pong Keepalive Duty Cycle
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================"
echo "      Google Colab 24/7 Autonomous Relay Engine (Linux / WSL2)        "
echo "======================================================================"
echo ""

# Prefer .colab-venv if available in workspace, else system python3
PYTHON_BIN="python3"
if [ -x "/home/runner/workspace/.colab-venv/bin/python" ]; then
    PYTHON_BIN="/home/runner/workspace/.colab-venv/bin/python"
elif [ -x "$SCRIPT_DIR/.colab-venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.colab-venv/bin/python"
fi

if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "Installing python3 and pip..."
    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
    PYTHON_BIN="python3"
fi

# Verify core Python dependencies
if ! "$PYTHON_BIN" -c "import click, filelock, requests, jupyter_kernel_client, pydantic, typer, websocket" &>/dev/null; then
    echo "Installing required Python packages from requirements.txt..."
    "$PYTHON_BIN" -m pip install -r requirements.txt --break-system-packages 2>/dev/null || "$PYTHON_BIN" -m pip install -r requirements.txt
fi

exec "$PYTHON_BIN" run_relay_linux.py --bootstrap "$@"
