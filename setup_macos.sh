#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
VENV_DIR="$ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

msg() { echo "$@"; }

ensure_cmd() {
  local cmd="$1"
  local hint="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    return 0
  fi
  msg "[ERROR] '$cmd' 명령이 없습니다."
  msg "        $hint"
  exit 1
}

ensure_python() {
  ensure_cmd "python3" "python3 설치 후 다시 실행하세요. (예: brew install python)"
}

create_venv() {
  if [[ -x "$VENV_PYTHON" ]]; then
    msg "[OK] venv already exists: $VENV_DIR"
    return 0
  fi
  msg "[RUN] python3 -m venv .venv"
  python3 -m venv "$VENV_DIR"
}

install_deps() {
  msg "[RUN] pip install -r requirements.txt"
  "$VENV_PYTHON" -m pip install -U pip setuptools wheel >/dev/null
  "$VENV_PYTHON" -m pip install -r "$ROOT/requirements.txt"
}

check_tk() {
  if "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import tkinter  # noqa: F401
PY
  then
    msg "[OK] tkinter available"
    return 0
  fi
  msg "[WARN] tkinter를 import하지 못했습니다."
  msg "       macOS에서는 보통 기본 포함이지만, 환경에 따라 Python 배포판에 Tk가 빠질 수 있습니다."
  msg "       패널 실행이 안 되면 python.org 배포판 또는 brew python + tk 설치를 확인하세요."
}

write_env_minimal() {
  if [[ -f "$ENV_FILE" ]]; then
    msg "[OK] .env exists: $ENV_FILE"
    return 0
  fi
  msg "[RUN] create .env (multi-bot 기본값, 토큰은 패널에서 등록 권장)"
  cat >"$ENV_FILE" <<'EOF'
# Sonolbot env (macOS)
# - 멀티봇 운영은 Control Panel에서 봇 토큰/허용 사용자를 등록합니다.
# - 단일봇(직접 실행)으로 쓰려면 TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS를 추가로 설정하세요.

SONOLBOT_ALLOWED_SKILLS=sonolbot-telegram,sonolbot-tasks

# 텔레그램 메시지 파싱/스타일
DAEMON_TELEGRAM_FORCE_PARSE_MODE=1
DAEMON_TELEGRAM_DEFAULT_PARSE_MODE=HTML
DAEMON_TELEGRAM_PARSE_FALLBACK_RAW_ON_FAIL=1

# Agent Message Rewriter
DAEMON_AGENT_REWRITER_ENABLED=1
DAEMON_AGENT_REWRITER_MODEL=gpt-5.3-codex
DAEMON_AGENT_REWRITER_REASONING_EFFORT=none
DAEMON_AGENT_REWRITER_TMP_ROOT=/tmp/sonolbot-agent-rewriter
DAEMON_AGENT_REWRITER_CLEANUP_TMP=1
EOF
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

main() {
  msg "========================================"
  msg "Sonolbot setup (macOS)"
  msg "========================================"
  ensure_python
  create_venv
  install_deps
  check_tk
  write_env_minimal
  msg ""
  msg "[DONE] 설치가 완료되었습니다."
  msg "다음 단계:"
  msg "  1) ./control_panel_macos.command 실행"
  msg "  2) 패널에서 허용 사용자/봇 토큰 등록 후 Start"
}

main "$@"

