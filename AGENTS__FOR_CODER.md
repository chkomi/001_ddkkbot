# AGENTS__FOR_CODER.md

업데이트: 2026-02-16  
대상: 소놀봇 코덱스 시스템을 직접 변경/확장하는 AI 및 개발자

## 1) 문서 목적

이 문서는 런타임 구조, 상태 파일, 스킬 연동, 변경 시 주의점, 검증 절차를 기술한다.  
운영 대화용 최소 지침은 `AGENTS.md`를 본다.

## 2) 현재 아키텍처 요약

- 실행 진입점: `mybot_autoexecutor.sh`
- 실제 데몬: `daemon_service.py`
- 새 메시지 수집: `quick_check.py` (skill 기반 polling + store 반영)
- 스킬 브리지: `skill_bridge.py`
- 기본 transport: `codex app-server` (단일 경로)
- 중간 진행 메시지: rewriter `codex app-server`가 사용자 친화 문장으로 재작성
- TASK 단위 원칙: `1 TASK = 1 thread_id`

기본 흐름:
1. 데몬 루프에서 `quick_check.py` 실행
2. pending 있으면 `codex app-server` 보장
3. chat별 `thread/start` 또는 `thread/resume`
4. `turn/start`로 처리 시작
5. 처리 중 새 메시지는 `turn/steer`로 주입(실패 시 큐 적재)
6. `turn/completed`에서 최종 답변 전송 + processed 처리
7. task memory를 `thread_{thread_id}` 단위로 동기화
8. `codex/event/agent_message`는 rewriter 경유 후 텔레그램으로 전달(내부 용어 제거)

## 3) 핵심 파일 역할

- `daemon_service.py`: 오케스트레이터(수집/턴제어/전송/태스크/세션메타/UI 버튼)
- `quick_check.py`: 텔레그램 polling + pending 판단(exit code 0/1/2)
- `skill_bridge.py`: 허용 스킬 로딩 + runtime/env 조립
- `scripts/task_commands.py`: TASK 목록/검색/활성화 CLI(JSON)
- `.codex/skills/sonolbot-tasks/scripts/task_memory.py`: TASK 메모리 생성/갱신/검색
- `process_pending.py`: 데몬 공통 사이클 재사용 드레인 스크립트(주 실행경로 아님)

## 4) TASK 메모리 최신 설계

기본 경로:
- chat 분리 ON: `tasks/chat_{chat_id}/thread_{thread_id}/`
- chat 분리 OFF: `tasks/thread_{thread_id}/`

레거시 호환:
- 기존 `msg_{message_id}` 폴더/인덱스 항목은 읽기 호환 유지
- 신규 쓰기는 thread 중심(`task_id=thread_{thread_id}`)으로 처리

`index.json` 핵심 필드:
- `task_id` (주 식별자: `thread_*` 우선)
- `thread_id`
- `message_id` (최근 반영 메시지)
- `latest_message_id`
- `source_message_ids`
- `instruction`, `result_summary`, `display_title`, `display_subtitle`
- `work_status`, `ops_status`, `timestamp`, `task_dir`, `related_task_ids`

중요:
- 신규 코드에서 TASK 식별은 `message_id`가 아니라 `task_id/thread_id`를 우선 사용한다.
- `codex_session.session_id`를 TASK 식별자로 추론해서는 안 된다(레거시 항목 병합 위험).
- 지침 파일명은 반드시 `INSTRUNCTION.md` 철자를 유지한다.

## 5) 데몬 UI/버튼 플로우

메인 버튼:
- `TASK 목록 보기(최근20)`
- `기존 TASK 이어하기`
- `새 TASK 시작하기`
- `컨텍스트 초기화`

선택 콜백:
- inline callback payload: `task_select:{task_id}`
- 수신 저장 텍스트: `__cb__:task_select:{task_id}`

동작 요약:
1. 목록 보기: 최근순 20개 카드 전송 + 각 카드별 `선택` 버튼
2. 기존 TASK 이어하기: 검색어 입력 -> 후보 선택 -> 해당 TASK thread로 전환
3. 새 TASK 시작하기: 새 thread 시작 + 이전 맥락 요약(설정 길이) 주입
4. 컨텍스트 초기화: 현재 컨텍스트 리셋(필요 시 turn interrupt)

운영/로그 탭:
- Codex 메인 모델/추론 + Rewriter 모델/추론 설정을 저장/적용
- 선택 봇 로그 보기 시 `메인 봇 로그` / `Rewriter 봇 로그` 2개 탭으로 분리 표시

텔레그램 출력 스타일:
- 데몬 송신 레이어에서 `parse_mode`를 기본 강제(기본값 `HTML`)
- parse_mode 전송 실패 시 무파싱 1회 재시도 정책 사용
- 메인/rewriter 모두 동일 송신 정책을 공유

## 6) 스킬 연동 규칙

필수 스킬:
- `sonolbot-telegram`
- `sonolbot-tasks`

데몬 연동 지점(`daemon_service.py`):
- 턴 시작/steer 전:
  - `init_task_session(...)`
  - `read_instrunction_first(...)`
  - `build_compact_memory_packet(...)`
- 턴 완료/재전송 성공 시:
  - `record_task_change(...)`

## 7) 세션 메타/상태 파일

주요 파일:
- `logs/codex-session-current.json`
- `state/codex-app-session-state.json` (봇 워커 기준 경로)
- `logs/codex-app-server.log`
- `logs/codex-agent-rewriter.log`
- `state/codex-agent-rewriter.pid`
- `state/codex-agent-rewriter-state.json`
- `state/agent-rewriter.lock`

rewriter 작업폴더:
- 기본: `/tmp/sonolbot-agent-rewriter/{bot_id}`
- `DAEMON_AGENT_REWRITER_WORKSPACE`가 지정되면 해당 경로 우선
- `DAEMON_AGENT_REWRITER_CLEANUP_TMP=1`이면 app-server 종료 시 tmp 작업폴더 자동 정리
- rewriter 프롬프트는 패널 저장 파일(`.control_panel_rewriter_prompt.txt`) 경유로 주입
- rewriter 기동 직전에 작업폴더의 `AGENTS.md`를 해당 프롬프트로 자동 생성/갱신
- 새 메시지 처리 사이클에서는 app-server 기동 전에 rewriter 선기동을 시도

`codex-session-current.json`(app_server 기준):
- `run_id`, `mode=app_server`, `started_at`
- `codex_cli_version`, `model`, `reasoning_effort`
- `transport`, `listen`, `thread_id`, `thread_ids_by_chat`, `sessions(chat->thread)`
- `session_id`는 하위호환 별칭(= thread_id alias)
- `app_server_pid`, `updated_at`

환경변수 동기화:
- `SONOLBOT_CODEX_RUN_ID`
- `SONOLBOT_CODEX_MODE`
- `SONOLBOT_CODEX_STARTED_AT`
- `SONOLBOT_CODEX_RESUME_TARGET`
- `SONOLBOT_CODEX_SESSION_ID`
- `SONOLBOT_CODEX_CLI_VERSION`
- `SONOLBOT_CODEX_MODEL`
- `SONOLBOT_CODEX_REASONING_EFFORT`
- `SONOLBOT_CODEX_SESSION_META_FILE`

## 8) 다중 채팅방/분리 규칙

- `SONOLBOT_TASKS_PARTITION_BY_CHAT=1` 권장
- ON이면 chat별 TASK 경로 분리 (`tasks/chat_{chat_id}/...`)
- OFF이면 전역 tasks 경로를 공유하므로 충돌/혼선 가능성이 커진다

권장:
- 다중 채팅 운영에서는 반드시 chat 분리를 유지한다.
- 전역 검색이 필요하면 chat별 index + 전역 집계 인덱스를 분리한다.

## 9) 변경 통제

아래 파일은 런타임 핵심이므로 신중히 변경:
- `daemon_service.py`
- `quick_check.py`
- `skill_bridge.py`
- `scripts/task_commands.py`
- `.codex/skills/sonolbot-telegram/scripts/telegram_io.py`
- `.codex/skills/sonolbot-tasks/scripts/task_memory.py`

권장 절차:
1. 변경 이유/영향/리스크를 먼저 명시
2. 최소 범위 변경
3. 회귀 검증 수행

## 10) 주요 환경변수

텔레그램:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USERS`
- `TELEGRAM_USER_ID` (선택)
- `TELEGRAM_POLLING_INTERVAL` (기본 1)
- `TELEGRAM_API_TIMEOUT_SEC`
- `TELEGRAM_MESSAGE_RETENTION_DAYS`
- `TELEGRAM_MAX_FILE_BYTES`
- `TELEGRAM_MESSAGE_STORE`

데몬:
- `DAEMON_POLL_INTERVAL_SEC` (기본 1)
- `DAEMON_IDLE_TIMEOUT_SEC`
- `DAEMON_ACTIVITY_FILE`
- `DAEMON_ACTIVITY_MAX_BYTES`
- `DAEMON_ACTIVITY_BACKUP_COUNT`
- `DAEMON_ACTIVITY_RETENTION_DAYS`
- `LOG_RETENTION_DAYS`

app-server:
- `DAEMON_APP_SERVER_LISTEN`
- `DAEMON_APP_SERVER_PROGRESS_INTERVAL_SEC`
- `DAEMON_APP_SERVER_STEER_BATCH_WINDOW_MS`
- `DAEMON_APP_SERVER_TURN_TIMEOUT_SEC`
- `DAEMON_APP_SERVER_RESTART_BACKOFF_SEC`
- `DAEMON_APP_SERVER_REQUEST_TIMEOUT_SEC`
- `DAEMON_APP_SERVER_APPROVAL_POLICY`
- `DAEMON_APP_SERVER_SANDBOX`
- `DAEMON_APP_SERVER_STATE_FILE`
- `DAEMON_APP_SERVER_LOG_FILE`
- `DAEMON_TELEGRAM_FORCE_PARSE_MODE`
- `DAEMON_TELEGRAM_DEFAULT_PARSE_MODE`
- `DAEMON_TELEGRAM_PARSE_FALLBACK_RAW_ON_FAIL`

rewriter:
- `DAEMON_AGENT_REWRITER_ENABLED`
- `DAEMON_AGENT_REWRITER_MODEL`
- `DAEMON_AGENT_REWRITER_REASONING_EFFORT`
- `DAEMON_AGENT_REWRITER_TIMEOUT_SEC`
- `DAEMON_AGENT_REWRITER_REQUEST_TIMEOUT_SEC`
- `DAEMON_AGENT_REWRITER_MAX_RETRY`
- `DAEMON_AGENT_REWRITER_RESTART_BACKOFF_SEC`
- `DAEMON_AGENT_REWRITER_PROMPT_FILE` (패널이 자동 설정)
- `DAEMON_AGENT_REWRITER_PROMPT` (레거시/수동 실행 fallback)
- `DAEMON_AGENT_REWRITER_TMP_ROOT`
- `DAEMON_AGENT_REWRITER_CLEANUP_TMP`
- `DAEMON_AGENT_REWRITER_WORKSPACE`
- `DAEMON_AGENT_REWRITER_PID_FILE`
- `DAEMON_AGENT_REWRITER_STATE_FILE`
- `DAEMON_AGENT_REWRITER_LOG_FILE`
- `DAEMON_AGENT_REWRITER_LOCK_FILE`

모델:
- `SONOLBOT_CODEX_MODEL`
- `SONOLBOT_CODEX_REASONING_EFFORT`

경로:
- `TASKS_DIR`, `LOGS_DIR`, `TELEGRAM_TASKS_DIR`, `TELEGRAM_LOGS_DIR`, `TASKS_LOGS_DIR`

## 11) 변경 후 검증 체크리스트

필수:
1. `python3 -m py_compile daemon_service.py quick_check.py skill_bridge.py scripts/task_commands.py`
2. `python3 -m py_compile .codex/skills/sonolbot-tasks/scripts/task_memory.py`
3. `python3 scripts/check_docs_alignment.py`
4. `logs/daemon-YYYY-MM-DD.log` 오류 확인
5. app-server 모드에서 `logs/codex-session-current.json` 갱신 확인
6. TASK 변경 경로에서 `tasks/chat_*/thread_*/INSTRUNCTION.md` 갱신 확인

권장:
1. pending 메시지 1건으로 turn start/completed 왕복
2. 처리 중 메시지 추가 후 steer 동작 확인
3. 전송 실패 유도 시 deferred retry 동작 확인
4. `TASK 목록 보기(최근20)` 카드 + 선택 버튼 동작 확인

## 12) 알려진 리스크/주의점

1. 레거시 `msg_*` 데이터가 많은 경우, 검색/목록에서 thread 단위와 legacy 항목이 혼재될 수 있다.
2. `task_id` 정규화 규칙을 우회해 `message_id`만 직접 사용하는 신규 코드는 구조 드리프트를 만든다.
3. app-server에서 `session_id`는 별칭이므로 신규 구현은 `thread_id` 우선으로 다룬다.
4. 문서-런타임 정합은 `scripts/check_docs_alignment.py` 기준으로 유지한다.
5. 레거시 `msg_*`를 thread로 바꿀 때는 `thread_id` 또는 `legacy_task_thread_map.json` 기반의 확정값만 사용한다.
