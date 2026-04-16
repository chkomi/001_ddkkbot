# 소놀봇 (mybot_ver2)

텔레그램 지시사항을 받아서 자동으로 처리하고 결과를 회신하는 봇입니다.

현재 기본 실행 환경은 **Windows + WSL + Codex CLI** 입니다.

## 주요 기능

- 텔레그램 명령 수집/처리/회신
- 여러 메시지 합산 처리
- 작업 중 진행률 보고
- 파일/이미지/위치 첨부 처리
- 작업 메모리(`tasks/chat_{chat_id}/thread_*`, 레거시 `msg_*` 읽기 호환) 및 인덱스(`tasks/index.json`) 유지
- 단일 포그라운드 데몬 실행 (스킬 기반)

## 요구사항

- Windows 10/11
- WSL2 (예: Ubuntu)
- WSL 내부 Python 3.8+
- WSL 내부 Codex CLI (`codex`)
- 텔레그램 봇 토큰 (BotFather)

## 빠른 시작

### 1) 텔레그램 준비

1. `@BotFather`에서 봇 생성 후 토큰 발급
2. `@userinfobot`에서 본인 사용자 ID 확인

또는 WSL에서:

```bash
python3 get_my_id.py
```

### 2) 설치

방법 A (권장, Windows에서 실행):

1. `setup.bat` 더블클릭
2. 내부적으로 WSL의 `setup_wsl.sh` 실행
3. 의존성 설치 + `.env` 생성

방법 B (WSL에서 직접):

```bash
chmod +x setup_wsl.sh
./setup_wsl.sh
```

`.env` 예시:

```env
TELEGRAM_BOT_TOKEN=당신의_봇_토큰
TELEGRAM_ALLOWED_USERS=당신의_사용자_ID
SONOLBOT_ALLOWED_SKILLS=sonolbot-telegram,sonolbot-tasks
TELEGRAM_POLLING_INTERVAL=1
TELEGRAM_MAX_FILE_BYTES=52428800
```

### macOS 빠른 시작

macOS에서는 WSL 없이 로컬에서 바로 실행할 수 있습니다.

1. `setup_macos.command` 실행 (또는 터미널에서 `bash setup_macos.sh`)
2. `control_panel_macos.command` 실행
3. 패널에서 허용 사용자 ID를 먼저 등록한 뒤, 봇 토큰을 추가/활성화하고 Start를 누릅니다.

### 3) 데몬 실행 (권장)

```bash
chmod +x mybot_autoexecutor.sh
./mybot_autoexecutor.sh
```

- 단일 포그라운드 데몬으로 계속 실행됩니다.
- 새 메시지가 있을 때만 Codex를 실행합니다.
- 기본 실행 모드: `codex app-server` 기반 턴/스레드 대화 루프
  - 최초 새 메시지 수신 시 app-server 시작
  - 메시지 처리 중 추가 메시지는 `turn/steer`로 즉시 주입 시도
  - steer 실패 시 큐에 보관 후 다음 턴에 병합 반영
  - 텔레그램에는 주기적 진행요약 + 최종답 전송

### 4) GUI 컨트롤 패널 (선택)

Windows에서 실행:

- `control_panel.exe` 더블클릭 (권장)
- 또는 `control_panel.bat`

- Start/Stop/Status 버튼으로 데몬 제어 가능
- GUI에서 시작하면 별도 터미널 창 없이 백그라운드로 실행
- 패널 UI는 Windows Python tkinter로 실행되고, 데몬 제어는 WSL 브리지 방식으로 동작
- 현재 상태에 맞춰 `Start/Stop` 버튼이 눌린 스타일로 표시
- `최근 로그`는 요약 1줄 표시, `Log Detail` 클릭 시 로그 종류를 고른 뒤 해당 종류의 최신 로그 파일을 전체 확인
- 컨트롤 패널은 단일 인스턴스로 동작하며, 이미 실행 중이면 중복 창을 띄우지 않음
- 창의 `X` 버튼은 최소화로 동작하며 작업표시줄에서 다시 열 수 있음
- 완전 종료는 패널 상단 우측 `완전종료(Exit App)` 버튼 사용
- (필요 시) `build_control_panel_exe.bat` 실행 후 `control_panel.exe` 재생성

macOS에서 실행:

- `control_panel_macos.command` 더블클릭
- (선택) `.app`로 빌드: `bash build_control_panel_app_macos.sh`

## 실행 구조

- `mybot_autoexecutor.sh`: 데몬 래퍼(실행 진입점)
  - UTF-8 강제 (`LANG/LC_ALL/PYTHONUTF8/PYTHONIOENCODING`)
  - 내부적으로 `daemon_service.py` 실행
- `daemon_service.py`: Python 데몬 서비스 백엔드 (quick_check + app-server 실행 루프)
- `daemon_control_panel.py`: 시작/중지/상태 GUI 패널
- `quick_check.py`: `skill_bridge.py` + `sonolbot-telegram` 기반 pending 체크

`skill_bridge.py`는 기본적으로 아래 2개 스킬만 로드를 허용합니다.

- `sonolbot-telegram`
- `sonolbot-tasks`

필요 시 `.env`의 `SONOLBOT_ALLOWED_SKILLS`로 변경할 수 있습니다.

## 수동 테스트

WSL에서:

```bash
# 빠른 메시지 확인
python3 quick_check.py

# 단일 데몬 실행(포그라운드)
bash mybot_autoexecutor.sh
```

## Codex 커스텀 커맨드

프로젝트 로컬 커맨드는 `./.codex/prompts/`에 정의되어 있습니다.

- `/task-list [limit|검색어]`
  - 전체 태스크 목록/상태를 요약 표시
- `/task-activate <task_id|thread_ID|msg_ID|검색어>`
  - 해당 태스크의 `INSTRUNCTION.md`(fallback: `INSTRUCTIONS.md`)를 읽고
  - 현재 상태/최근 변경을 요약한 뒤 다음 작업 지시를 요청

예시:

```text
/task-list
/task-list 20
/task-list 브라우저
/task-activate thread_019c615b-1215-79c3-9ca7-bff6633cf4e0
/task-activate msg_486
```

## 로그 확인

WSL:

```bash
tail -f logs/daemon-$(date +%F).log
tail -f logs/codex-app-server.log
```

`codex-app-server.log`는 기본적으로 크기 기반 로테이션이 적용됩니다.
멀티봇 모드에서는 각 봇 워커 로그가 `bots/{bot_id}/logs/` 아래에 저장됩니다.

- `DAEMON_ACTIVITY_MAX_BYTES` (기본 `10485760`, 10MB)
- `DAEMON_ACTIVITY_BACKUP_COUNT` (기본 `7`)
- `DAEMON_ACTIVITY_RETENTION_DAYS` (기본 `LOG_RETENTION_DAYS`와 동일)

app-server 관련 주요 환경변수:

- `DAEMON_APP_SERVER_LISTEN` (기본 `stdio://`)
- `DAEMON_APP_SERVER_PROGRESS_INTERVAL_SEC` (기본 `20`)
- `DAEMON_APP_SERVER_STEER_BATCH_WINDOW_MS` (기본 `800`)
- `DAEMON_APP_SERVER_TURN_TIMEOUT_SEC` (기본 `1800`)
- `DAEMON_APP_SERVER_RESTART_BACKOFF_SEC` (기본 `3`)

Windows PowerShell:

```powershell
Get-Content .\logs\daemon-$(Get-Date -Format yyyy-MM-dd).log -Tail 100
```

## 프로젝트 구조

```text
mybot_ver2/
├── setup.bat                 # Windows -> WSL setup 래퍼
├── setup_wsl.sh              # WSL 설치 스크립트
├── mybot_autoexecutor.sh     # 포그라운드 데몬 실행기
├── daemon_service.py         # Python 데몬 백엔드
├── daemon_control_panel.py   # 시작/중지/상태 GUI
├── quick_check.py            # 스킬 기반 새 메시지 체크
├── skill_bridge.py           # sonolbot 스킬 로더
├── requirements.txt
├── .env
├── tasks/
└── logs/
```

## 보안

- 토큰/허용 사용자 ID는 `.env`로만 관리
- `.env`, `tasks/`, 로그 파일은 Git 제외

## 문제 해결

### `wsl.exe not found`

관리자 PowerShell에서:

```powershell
wsl --install
```

### `codex CLI not found`

WSL 내부에서 Codex CLI 설치 후 `codex --version` 확인

### 봇이 응답하지 않음

1. `.env` 값 확인
2. `TELEGRAM_ALLOWED_USERS`에 본인 ID 등록 확인
3. `logs/daemon-YYYY-MM-DD.log`, `logs/codex-app-server.log` 확인

### WSL에서 파일 권한이 777로 보임

WSL `drvfs`에 `metadata`가 꺼져 있으면 `chmod 600/700`이 실효 적용되지 않습니다.

`/etc/wsl.conf`에 아래를 설정:

```ini
[automount]
options = "metadata,umask=077,fmask=177"
```

적용:

```powershell
wsl --shutdown
```

다시 WSL 진입 후 권한 확인:

```bash
stat -c '%a %n' .env telegram_messages.json logs
```

## 상세 문서

- `CLAUDE.md`: 전체 아키텍처/운영 규칙
- `TELEGRAM_BOT.md`: 텔레그램 시스템 설계
- `FILE_SUPPORT.md`: 첨부 파일 처리
- `LOCATION_SUPPORT.md`: 위치 처리
