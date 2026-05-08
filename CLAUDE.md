# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md
한국어로 대화할 것.

---

## 실행 명령

```bash
# 데몬 실행 (포그라운드)
bash mybot_autoexecutor.sh

# 새 메시지 수동 확인 (exit code: 0=없음, 1=있음, 2=오류)
python3 quick_check.py

# 웹 대시보드 (포트 8765)
python3 web_dashboard/server.py

# macOS 초기 설치
bash setup_macos.sh

# 문법 검사 (변경 후 필수)
python3 -m py_compile daemon_service.py quick_check.py skill_bridge.py scripts/task_commands.py
python3 -m py_compile .codex/skills/sonolbot-tasks/scripts/task_memory.py

# 문서-런타임 정합 검사
python3 scripts/check_docs_alignment.py

# 태스크 목록 (JSON)
python3 scripts/task_commands.py
```

## 아키텍처

### 실행 흐름

```
mybot_autoexecutor.sh
  └─ daemon_service.py  (오케스트레이터)
       ├─ quick_check.py         새 메시지 polling (sonolbot-telegram 스킬)
       ├─ codex app-server        AI 처리 엔진 (기본)
       │    └─ JSON Lines stdio   thread/turn API
       ├─ claude_app_server.py    Claude CLI 어댑터 (ai_provider=claude)
       └─ rewriter app-server     agent_message 사용자 친화 재작성
```

데몬 루프: pending 있으면 app-server 기동 → `thread/start` or `thread/resume` → `turn/start` → 처리 중 새 메시지는 `turn/steer` 주입(실패 시 큐) → `turn/completed` 후 텔레그램 최종 전송.

### AI 프로바이더

| 값 | 엔진 | 설명 |
|---|---|---|
| `codex` | `codex app-server` | 기본 |
| `claude` | `claude_app_server.py` | Claude Code CLI 래퍼 |
| `hybrid` | claude 주 + 이미지→codex | 이미지 키워드 감지 시 Codex 위임 |

### 멀티봇 구조

- 봇 설정: `.control_panel_telegram_bots.json` (`scripts/bot_config_store.py` 관리)
- 봇별 격리 경로: `bots/{bot_id}/logs/`, `bots/{bot_id}/state/`, `bots/{bot_id}/tasks/`
- 플랫폼: Telegram (`quick_check.py`), Discord (`discord_relay.py`), Slack (`slack_relay.py`)
- `bot_id` 도출: Telegram → `tg_{numeric_prefix}`, Discord → `discord_{user_id}`, Slack → `slack_{team}_{app}`

### 스킬 시스템

`skill_bridge.py`가 `.codex/skills/` 아래에서 허용 스킬 스크립트를 동적 로드:
- `sonolbot-telegram`: Telegram I/O (수신/송신/파일/위치/음성/버튼)
- `sonolbot-tasks`: TASK 메모리 생성·갱신·검색

허용 목록은 `.env`의 `SONOLBOT_ALLOWED_SKILLS`로 변경 가능 (기본: 위 2개).

### TASK 메모리

경로 (chat 분리 ON 권장):
```
tasks/chat_{chat_id}/thread_{thread_id}/
  INSTRUNCTION.md   (철자 그대로 유지)
  index.json
```

`index.json` 핵심 필드: `task_id`, `thread_id`, `message_id`, `work_status`, `ops_status`, `instruction`, `result_summary`, `display_title`, `task_dir`.

규칙:
- 태스크 식별은 `task_id/thread_id` 우선, `message_id`는 보조
- `INSTRUNCTION.md` 작업 시작 전 먼저 읽고, 변경 시 즉시 동기화
- 레거시 `msg_*` 경로는 읽기 호환만 유지, 신규 쓰기 금지

### 핵심 파일 역할

| 파일 | 역할 |
|---|---|
| `daemon_service.py` | 오케스트레이터 (수집/턴 제어/전송/태스크/UI 버튼) |
| `quick_check.py` | 텔레그램 polling + pending 판단 |
| `skill_bridge.py` | 허용 스킬 로딩 + runtime/env 조립 |
| `claude_app_server.py` | Claude CLI를 codex app-server 프로토콜로 에뮬레이션 |
| `chat_state.py` | 채팅별 상태 TypedDict 스키마 |
| `task_helpers.py` | task_id 정규화·인라인 키보드·가이드 편집 감지 (순수 함수) |
| `scripts/bot_config_store.py` | 멀티봇 설정 저장소 |
| `scripts/task_commands.py` | TASK 목록/검색/활성화 CLI |
| `web_dashboard/server.py` | FastAPI 대시보드 (포트 8765) |

### 세션 상태 파일

- `logs/codex-session-current.json`: 실행 중 app-server 세션 메타
- `state/codex-app-session-state.json`: 봇 워커 기준 상태
- `state/codex-agent-rewriter-state.json`, `state/agent-rewriter.lock`

### Telegram 송신 정책

`daemon_service.py` 송신 레이어: `parse_mode=HTML` 기본 강제 → 실패 시 무파싱 1회 재시도.

## 변경 후 검증 (필수)

1. `python3 -m py_compile` (핵심 파일 전부)
2. `python3 scripts/check_docs_alignment.py`
3. `logs/daemon-YYYY-MM-DD.log` 오류 확인
4. app-server 모드에서 `logs/codex-session-current.json` 갱신 확인

## 주요 환경변수 (.env)

```env
# 공통
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
SONOLBOT_ALLOWED_SKILLS=sonolbot-telegram,sonolbot-tasks

# AI 프로바이더
SONOLBOT_CODEX_MODEL=gpt-5.5
SONOLBOT_CODEX_REASONING_EFFORT=xhigh
SONOLBOT_CLAUDE_MODEL=...

# 데몬
DAEMON_POLL_INTERVAL_SEC=1
DAEMON_APP_SERVER_TURN_TIMEOUT_SEC=1800
DAEMON_APP_SERVER_PROGRESS_INTERVAL_SEC=20
DAEMON_AGENT_REWRITER_ENABLED=1

# 멀티봇/태스크
SONOLBOT_TASKS_PARTITION_BY_CHAT=1
WEB_DASHBOARD_ENABLED=1
WEB_DASHBOARD_PORT=8765
```

## 변경 통제

아래 파일은 런타임 핵심이므로 최소 범위만 수정:
- `daemon_service.py`, `quick_check.py`, `skill_bridge.py`
- `scripts/task_commands.py`
- `.codex/skills/sonolbot-telegram/scripts/telegram_io.py`
- `.codex/skills/sonolbot-tasks/scripts/task_memory.py`

구조 변경 전 `AGENTS__FOR_CODER.md`로 영향 범위 확인 필수.
