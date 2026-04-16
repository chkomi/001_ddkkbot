#!/bin/bash
# ddkkbot Setup Wizard
# macOS에서 더블클릭으로 실행하세요.

set -euo pipefail
export LANG=ko_KR.UTF-8
export LC_ALL=ko_KR.UTF-8

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 터미널 색상
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

divider() { echo -e "${BLUE}─────────────────────────────────────────${RESET}"; }
title()   { echo -e "\n${BOLD}${CYAN}$1${RESET}"; }
ok()      { echo -e "  ${GREEN}✓${RESET} $1"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
err()     { echo -e "  ${RED}✗${RESET} $1"; }
ask()     { echo -en "  ${BOLD}$1${RESET} "; }

clear
echo ""
echo -e "${BOLD}${CYAN}  ddkkbot 설정 마법사${RESET}"
echo -e "${CYAN}  ─────────────────────────────────────────${RESET}"
echo ""

# ── 1. Python 확인 ────────────────────────────────────────────────────────────
title "1. Python 환경 확인"

PYTHON_BIN=""
for bin in python3.12 python3.11 python3.10 python3; do
  if command -v "$bin" &>/dev/null; then
    VERSION=$($bin --version 2>&1 | awk '{print $2}')
    MAJOR=$(echo "$VERSION" | cut -d. -f1)
    MINOR=$(echo "$VERSION" | cut -d. -f2)
    if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 9 ]; then
      PYTHON_BIN="$bin"
      ok "Python $VERSION ($bin)"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  err "Python 3.9 이상을 찾을 수 없습니다."
  echo ""
  echo "  Homebrew로 설치: brew install python3"
  echo ""
  read -p "  Enter 키를 눌러 종료..." _
  exit 1
fi

# ── 2. 의존성 설치 ────────────────────────────────────────────────────────────
title "2. 의존성 패키지 설치"
echo ""
ask "필수 패키지를 설치하시겠습니까? (y/n) [y]: "
read -r INSTALL_DEPS
INSTALL_DEPS="${INSTALL_DEPS:-y}"

if [[ "$INSTALL_DEPS" =~ ^[Yy] ]]; then
  echo ""
  echo "  pip install -r requirements.txt ..."
  if "$PYTHON_BIN" -m pip install -r requirements.txt -q; then
    ok "패키지 설치 완료"
  else
    warn "일부 패키지 설치 실패. 계속 진행합니다."
  fi
else
  warn "패키지 설치를 건너뜁니다."
fi

# ── 3. .env 설정 ─────────────────────────────────────────────────────────────
title "3. 환경 설정 (.env)"
echo ""

ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok ".env 파일 생성됨 (.env.example에서 복사)"
  else
    touch "$ENV_FILE"
    ok ".env 파일 생성됨 (빈 파일)"
  fi
else
  ok ".env 파일이 이미 존재합니다."
fi

# 설정 읽기 헬퍼
get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r'
}

# 설정 쓰기 헬퍼
set_env() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    # macOS와 Linux 모두 호환
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
      sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    fi
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

divider
echo ""
echo -e "  ${BOLD}AI 프로바이더 선택${RESET}"
echo "  1) Codex (OpenAI Codex CLI)"
echo "  2) Claude (Anthropic Claude API)"
echo ""
ask "선택 [1/2] (기본: 1): "
read -r AI_CHOICE
AI_CHOICE="${AI_CHOICE:-1}"

if [ "$AI_CHOICE" = "2" ]; then
  set_env "SONOLBOT_AI_PROVIDER" "claude"
  ok "AI 프로바이더: Claude"
  echo ""

  CURRENT_KEY=$(get_env "ANTHROPIC_API_KEY")
  if [ -n "$CURRENT_KEY" ] && [ "$CURRENT_KEY" != "sk-ant-..." ]; then
    ok "ANTHROPIC_API_KEY 이미 설정됨"
  else
    ask "Anthropic API 키를 입력하세요 (sk-ant-...): "
    read -r API_KEY
    if [ -n "$API_KEY" ]; then
      set_env "ANTHROPIC_API_KEY" "$API_KEY"
      ok "ANTHROPIC_API_KEY 설정됨"
    else
      warn "ANTHROPIC_API_KEY를 나중에 .env에 직접 입력하세요."
    fi
  fi
else
  set_env "SONOLBOT_AI_PROVIDER" "codex"
  ok "AI 프로바이더: Codex"
fi

divider
echo ""
echo -e "  ${BOLD}Telegram 설정${RESET}"
echo ""

CURRENT_TG_TOKEN=$(get_env "TELEGRAM_BOT_TOKEN")
if [ -n "$CURRENT_TG_TOKEN" ] && [ "$CURRENT_TG_TOKEN" != "YOUR_BOT_TOKEN_HERE" ]; then
  ok "TELEGRAM_BOT_TOKEN 이미 설정됨"
else
  ask "Telegram 봇 토큰 (없으면 Enter 건너뜀): "
  read -r TG_TOKEN
  if [ -n "$TG_TOKEN" ]; then
    set_env "TELEGRAM_BOT_TOKEN" "$TG_TOKEN"
    ok "TELEGRAM_BOT_TOKEN 설정됨"
  else
    warn "Telegram 봇 토큰을 나중에 .env에 직접 입력하세요."
  fi
fi

CURRENT_TG_USERS=$(get_env "TELEGRAM_ALLOWED_USERS")
if [ -n "$CURRENT_TG_USERS" ] && [ "$CURRENT_TG_USERS" != "YOUR_USER_ID_HERE" ]; then
  ok "TELEGRAM_ALLOWED_USERS 이미 설정됨"
else
  ask "허용 Telegram 사용자 ID (없으면 Enter 건너뜀): "
  read -r TG_USERS
  if [ -n "$TG_USERS" ]; then
    set_env "TELEGRAM_ALLOWED_USERS" "$TG_USERS"
    set_env "TELEGRAM_USER_ID" "${TG_USERS%%,*}"
    ok "TELEGRAM_ALLOWED_USERS 설정됨"
  fi
fi

divider
echo ""
ask "Discord 봇도 설정하시겠습니까? (y/n) [n]: "
read -r SETUP_DISCORD
SETUP_DISCORD="${SETUP_DISCORD:-n}"

if [[ "$SETUP_DISCORD" =~ ^[Yy] ]]; then
  echo ""
  echo -e "  ${BOLD}Discord 설정${RESET}"
  echo ""
  ask "Discord 봇 토큰: "
  read -r DC_TOKEN
  if [ -n "$DC_TOKEN" ]; then
    set_env "DISCORD_BOT_TOKEN" "$DC_TOKEN"
    ok "DISCORD_BOT_TOKEN 설정됨"
  fi

  ask "Discord 허용 채널 ID (없으면 Enter 건너뜀): "
  read -r DC_CHANNELS
  if [ -n "$DC_CHANNELS" ]; then
    set_env "DISCORD_ALLOWED_CHANNELS" "$DC_CHANNELS"
    ok "DISCORD_ALLOWED_CHANNELS 설정됨"
  fi

  ask "멘션 없이도 모든 메시지 처리? (y/n) [n]: "
  read -r DC_NO_MENTION
  DC_NO_MENTION="${DC_NO_MENTION:-n}"
  if [[ "$DC_NO_MENTION" =~ ^[Yy] ]]; then
    set_env "DISCORD_RESPOND_WITHOUT_MENTION" "1"
  fi
fi

divider
echo ""
ask "웹 대시보드를 활성화하시겠습니까? (y/n) [y]: "
read -r ENABLE_DASH
ENABLE_DASH="${ENABLE_DASH:-y}"

DASHBOARD_PORT=$(get_env "WEB_DASHBOARD_PORT")
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"

if [[ "$ENABLE_DASH" =~ ^[Yy] ]]; then
  set_env "WEB_DASHBOARD_ENABLED" "1"
  ask "대시보드 포트 [${DASHBOARD_PORT}]: "
  read -r DASH_PORT_INPUT
  DASHBOARD_PORT="${DASH_PORT_INPUT:-$DASHBOARD_PORT}"
  set_env "WEB_DASHBOARD_PORT" "$DASHBOARD_PORT"
  ok "웹 대시보드 활성화: http://localhost:$DASHBOARD_PORT"
fi

# ── 4. SONOLBOT_ALLOWED_SKILLS 업데이트 ───────────────────────────────────────
CURRENT_SKILLS=$(get_env "SONOLBOT_ALLOWED_SKILLS")
if [ -z "$CURRENT_SKILLS" ]; then
  CURRENT_SKILLS="sonolbot-telegram,sonolbot-tasks"
fi

DC_TOKEN_CHECK=$(get_env "DISCORD_BOT_TOKEN")
if [ -n "$DC_TOKEN_CHECK" ] && ! echo "$CURRENT_SKILLS" | grep -q "sonolbot-discord"; then
  NEW_SKILLS="${CURRENT_SKILLS},sonolbot-discord"
  set_env "SONOLBOT_ALLOWED_SKILLS" "$NEW_SKILLS"
  ok "SONOLBOT_ALLOWED_SKILLS에 sonolbot-discord 추가됨"
fi

# ── 5. 웹 대시보드 시작 ───────────────────────────────────────────────────────
title "4. 완료"
echo ""
ok "설정 완료. .env 파일을 확인하세요."
echo ""

ENABLE_DASH_CHECK=$(get_env "WEB_DASHBOARD_ENABLED")
if [ "$ENABLE_DASH_CHECK" = "1" ]; then
  ask "지금 웹 대시보드를 시작하시겠습니까? (y/n) [y]: "
  read -r START_DASH
  START_DASH="${START_DASH:-y}"

  if [[ "$START_DASH" =~ ^[Yy] ]]; then
    echo ""
    echo "  웹 대시보드 시작 중..."
    "$PYTHON_BIN" web_dashboard/server.py &
    DASH_PID=$!
    sleep 2

    if kill -0 "$DASH_PID" 2>/dev/null; then
      ok "대시보드 실행 중 (PID=$DASH_PID)"
      echo ""
      echo -e "  브라우저에서 여세요: ${CYAN}http://localhost:${DASHBOARD_PORT}${RESET}"
      open "http://localhost:${DASHBOARD_PORT}" 2>/dev/null || true
    else
      err "대시보드 시작 실패. python3 web_dashboard/server.py 로 직접 실행하세요."
    fi
  fi
fi

echo ""
divider
echo ""
echo -e "  ${BOLD}봇 실행 방법:${RESET}"
echo "  bash mybot_autoexecutor.sh"
echo ""
echo -e "  ${BOLD}웹 대시보드 실행:${RESET}"
echo "  python3 web_dashboard/server.py"
echo ""
echo "  설정 변경은 .env 파일을 직접 편집하거나 웹 대시보드에서 할 수 있습니다."
echo ""
divider
echo ""
read -p "  Enter 키를 눌러 종료..." _
