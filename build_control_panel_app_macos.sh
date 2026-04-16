#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "[ERROR] .venv가 없습니다. 먼저 setup_macos.command를 실행하세요."
  exit 1
fi

PY="$ROOT/.venv/bin/python"

echo "========================================"
echo "Build SonolbotControlPanel.app (macOS)"
echo "========================================"

"$PY" -m pip install -U pyinstaller

# NOTE:
# This builds a GUI app bundle. For best results, keep the .app under this repo
# so it can locate daemon_service.py and other files via root detection.
"$PY" -m PyInstaller \
  --windowed \
  --name SonolbotControlPanel \
  daemon_control_panel.py

echo ""
echo "[DONE] dist/SonolbotControlPanel.app created."

