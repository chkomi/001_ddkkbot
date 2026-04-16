#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "[ERROR] .venv가 없습니다. 먼저 setup_macos.command를 실행하세요."
  echo "Press Enter to close..."
  read -r _
  exit 1
fi

exec "$ROOT/.venv/bin/python" "$ROOT/daemon_control_panel.py"

