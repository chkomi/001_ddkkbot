#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-platform foreground-capable daemon backend for Sonolbot.

This process runs an infinite loop:
- quick check for pending Telegram messages
- start and maintain Codex app-server when new messages exist
- process Telegram turns via app-server thread/turn APIs
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
import shutil
import errno
from dotenv import load_dotenv
from skill_bridge import (
    build_telegram_runtime,
    build_messenger_runtime,
    get_task_skill,
    get_telegram_skill,
    get_discord_skill,
    get_messenger_skill,
)
import task_helpers
import chat_state as chat_state_module
from scripts.bot_config_store import (
    default_config_path,
    load_config as load_bots_config,
    migrate_legacy_env_if_needed,
    save_config as save_bots_config,
)

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

try:
    import fcntl  # type: ignore
except Exception:
    fcntl = None  # type: ignore

try:
    import msvcrt  # type: ignore
except Exception:
    msvcrt = None  # type: ignore

PROMPT_TEXT_BACKUP = """
sonolbot-telegram 스킬로 새메시지(RECEIVE_FROM_TELEGRAM_NEWMSG) 확인하면 먼저 텔레그램으로 확인했다는 메시지부터 보낼것(SEND_TO_TELEGRAM_MSG), 존재하는 경우 그 작업(RECEIVE_FROM_TELEGRAM_NEWMSG)을 처리할 것.
RECEIVE_FROM_TELEGRAM_NEWMSG은 사용자의 지시이니 잘듣고 따를것.
RECEIVE_FROM_TELEGRAM_NEWMSG를 처리하는 중 사용자가 꼭 알아야 한다고 느끼는 중요한 점 있으면 텔레그램으로 보낼 것(SEND_TO_TELEGRAM_MSG)

SEND_TO_TELEGRAM_MSG 시마다 텔레그램으로부터 RECEIVE_FROM_TELEGRAM_NEWMSG를 확인하여, 
RECEIVE_FROM_TELEGRAM_NEWMSG이 존재하면, 사용자의 추가 지시이니 잘듣고 따를 것
RECEIVE_FROM_TELEGRAM_NEWMSG에 따라 작업을 처음 시작할 때, sonolbot-tasks 스킬 원칙을 따라 tasks 폴더에 새 작업을 만들 것 (sonolbot-tasks 스킬 사용).

중요 실행 원칙:
1) sonolbot-telegram + sonolbot-tasks 스킬 사용 
2) 수신은 허용 사용자 필터를 적용하고, 텍스트/파일/이미지/위치/음성/비디오를 처리할 것.
3) 로그는 logs 폴더(환경변수 LOGS_DIR/TELEGRAM_LOGS_DIR/TASKS_LOGS_DIR 반영)로 기록하고 7일 보관을 유지할 것.
4) TASK 생성 또는 선택후, INSTRUNCTION.md를 먼저 읽으며, 작업 변경 사항은 즉시 동기화할 것 (sonolbot-task 스킬 참조).
5) SEND_TO_TELEGRAM_MSG는 양식이 정해져 있지 않으니, 사용자에게 필요한 사항을 너가 판단하여 보내되, 알기 쉽게 설명할 것
"""

PROMPT_TEXT = """
"""
SECURE_FILE_MODE = 0o600
SECURE_DIR_MODE = 0o700
DEFAULT_ACTIVITY_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_ACTIVITY_BACKUP_COUNT = 7
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"
DEFAULT_CODEX_REASONING_EFFORT = "high"
CODEX_CLI_VERSION_UNKNOWN = "unknown"
DEFAULT_FALLBACK_SEND_MAX_ATTEMPTS = 4
DEFAULT_FALLBACK_SEND_RETRY_DELAY_SEC = 1.0
DEFAULT_FALLBACK_SEND_RETRY_BACKOFF = 1.8
DEFAULT_CODEX_TRANSPORT_MODE = "app_server"
DEFAULT_APP_SERVER_LISTEN = "stdio://"
DEFAULT_APP_SERVER_PROGRESS_INTERVAL_SEC = 20.0
DEFAULT_APP_SERVER_STEER_BATCH_WINDOW_MS = 800
DEFAULT_APP_SERVER_TURN_TIMEOUT_SEC = 1800
DEFAULT_APP_SERVER_RESTART_BACKOFF_SEC = 3.0
DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SEC = 45.0
DEFAULT_APP_SERVER_APPROVAL_POLICY = "on-request"
DEFAULT_APP_SERVER_SANDBOX = "workspace-write"
DEFAULT_APP_SERVER_FORWARD_AGENT_MESSAGE = True
DEFAULT_TELEGRAM_FORCE_PARSE_MODE = True
DEFAULT_TELEGRAM_DEFAULT_PARSE_MODE = "HTML"
DEFAULT_TELEGRAM_PARSE_FALLBACK_RAW_ON_FAIL = True
DEFAULT_AGENT_REWRITER_ENABLED = True
DEFAULT_AGENT_REWRITER_TIMEOUT_SEC = 40.0
DEFAULT_AGENT_REWRITER_MAX_RETRY = 1
DEFAULT_AGENT_REWRITER_MODEL = "gpt-5.3-codex"
DEFAULT_AGENT_REWRITER_REASONING_EFFORT = "none"
DEFAULT_AGENT_REWRITER_REQUEST_TIMEOUT_SEC = 30.0
DEFAULT_AGENT_REWRITER_RESTART_BACKOFF_SEC = 2.0
DEFAULT_AGENT_REWRITER_TMP_ROOT = "/tmp/sonolbot-agent-rewriter"
DEFAULT_AGENT_REWRITER_CLEANUP_TMP = True
DEFAULT_AGENT_REWRITER_PROMPT = (
    "당신은 텔레그램 사용자에게 보여줄 중간 진행 안내문 재작성 전용 어시스턴트다.\n"
    "목표: 원문의 의미를 유지하면서 사용자 친화적인 한국어 안내문으로 바꿔라.\n"
    "출력 규칙:\n"
    "1) 1~3문장으로 작성하되, 사용자가 현재 무엇을 진행 중인지 이해할 수 있을 만큼 구체적으로 쓸 것.\n"
    "2) 내부 기술/구조/운영 용어를 절대 노출하지 말 것.\n"
    "   금지 예: thread, msg_번호, INSTRUNCTION.md, index.json, task_meta, 동기화, 세션, 백그라운드, 스크립트, 명령어.\n"
    "3) 시스템 파일/규칙/로그/프롬프트/도구 호출 사실을 언급하지 말 것.\n"
    "4) 결과는 설명문만 출력하고, 머리말/꼬리말/코드블록/불릿은 사용하지 말 것.\n"
    "5) 텔레그램 HTML 파싱 기준으로 작성하고, 강조가 필요하면 <b>와 <code>만 최소한으로 사용하라.\n"
    "   Markdown 문법(*, _, #, ``` 등)은 사용하지 말 것.\n"
)
DEFAULT_TASKS_PARTITION_BY_CHAT = True
DEFAULT_MULTI_BOT_MANAGER_ENABLED = True
DEFAULT_BOT_WORKSPACE_DIRNAME = "bots"
DEFAULT_WORKER_RESTART_BASE_SEC = 5.0
DEFAULT_WORKER_RESTART_MAX_SEC = 90.0
DEFAULT_WORKER_STABLE_RESET_SEC = 45.0
DEFAULT_CHAT_LEASE_TTL_SEC = 90.0
DEFAULT_CHAT_LEASE_HEARTBEAT_SEC = 20.0
DEFAULT_FILE_LOCK_WAIT_TIMEOUT_SEC = 1.0
DEFAULT_COMPLETED_MESSAGE_TTL_SEC = 180.0
DEFAULT_UI_MODE_TIMEOUT_SEC = 300.0
DEFAULT_NEW_TASK_SUMMARY_LINES = 50
DEFAULT_NEW_TASK_SUMMARY_MAX_CHARS = 12000
DEFAULT_RESUME_CHAT_SUMMARY_HOURS = 5
DEFAULT_RESUME_CHAT_SUMMARY_LINES = 30
DEFAULT_RESUME_CHAT_SUMMARY_MAX_CHARS = 12000
DEFAULT_TASK_GUIDE_TELEGRAM_CHUNK_CHARS = 500
DEFAULT_TASK_AGENTS_INSTRUCTIONS_MAX_CHARS = 12000
DEFAULT_TASK_SEARCH_LLM_ENABLED = True
DEFAULT_TASK_SEARCH_LLM_LIMIT = 5
DEFAULT_TASK_SEARCH_LLM_CANDIDATE_POOL_LIMIT = 80
DEFAULT_TASK_SEARCH_LLM_MIN_SCORE = 60
DEFAULT_TASK_SEARCH_LLM_TURN_TIMEOUT_SEC = 35.0
DEFAULT_TASK_SEARCH_LLM_REQUEST_TIMEOUT_SEC = 20.0

BUTTON_TASK_LIST_RECENT20 = "TASK 목록 보기(최근20)"
BUTTON_TASK_RESUME = "기존 TASK 이어하기"
BUTTON_TASK_NEW = "새 TASK 시작하기"
BUTTON_TASK_GUIDE_VIEW = "TASK 지침 보기"
BUTTON_BOT_RENAME = "봇이름 변경"
BUTTON_MENU_BACK = "메뉴로 돌아가기"

UI_MODE_IDLE = "idle"
UI_MODE_AWAITING_RESUME_QUERY = "awaiting_resume_query"
UI_MODE_AWAITING_RESUME_CHOICE = "awaiting_resume_choice"
UI_MODE_AWAITING_NEW_TASK_INPUT = "awaiting_new_task_input"
UI_MODE_AWAITING_TEMP_TASK_DECISION = "awaiting_temp_task_decision"
UI_MODE_AWAITING_TASK_GUIDE_EDIT = "awaiting_task_guide_edit"
UI_MODE_AWAITING_BOT_RENAME_ALIAS = "awaiting_bot_rename_alias"
CALLBACK_TASK_SELECT_PREFIX = "__cb__:task_select:"
INLINE_TASK_SELECT_CALLBACK_PREFIX = "task_select:"
LEGACY_TASK_THREAD_MAP_FILENAME = "legacy_task_thread_map.json"
INTERNAL_AGENT_TEXT_PATTERNS = (
    r"\bthread_[A-Za-z0-9._:-]+\b",
    r"\bmsg_\d+\b",
    r"\bINSTRUNCTION\.md\b",
    r"\bINSTRUCTIONS\.md\b",
    r"\bindex\.json\b",
    r"\btask_info\b",
    r"\btask_meta\b",
    r"\bcodex\b",
    r"\bapp-server\b",
    r"백그라운드",
    r"동기화",
    r"세션",
    r"메타",
    r"스크립트",
    r"명령어",
)

MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
REWRITER_AGENTS_FILENAME = "AGENTS.md"
TASK_AGENTS_FILENAME = "AGENTS.md"
TASK_GUIDE_TRIGGER_TEXT = "task 지침"
TASK_GUIDE_EDIT_KEYWORDS = (
    "만들",
    "생성",
    "추가",
    "변경",
    "수정",
    "업데이트",
    "반영",
    "적용",
    "작성",
    "넣어",
)

FALLBACK_DAEMON_LOG_FILE = Path("/tmp/sonolbot-daemon-fallback.log")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _ProcessFileLock:
    """Cross-platform process lock backed by a lock file and a pid file."""

    def __init__(self, lock_file: Path, pid_file: Path, owner_label: str) -> None:
        self.lock_file = lock_file.resolve()
        self.pid_file = pid_file.resolve()
        self.owner_label = owner_label
        self._fd: int | None = None

    def _try_lock_fd(self, fd: int) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise
        if msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        # Best-effort fallback if OS-level non-blocking lock APIs are unavailable.
        if self.pid_file.exists():
            return False
        return True

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_RDWR | os.O_CREAT, SECURE_FILE_MODE)
        if not self._try_lock_fd(fd):
            os.close(fd)
            existing = 0
            try:
                existing = int(self.pid_file.read_text(encoding="utf-8").strip())
            except Exception:
                existing = 0
            if _is_pid_alive(existing):
                raise RuntimeError(f"{self.owner_label} already running (pid={existing})")
            raise RuntimeError(f"{self.owner_label} lock is busy: {self.lock_file}")

        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            current_pid = 0
            try:
                current_pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            except Exception:
                current_pid = 0
            if current_pid == os.getpid() and self.pid_file.exists():
                self.pid_file.unlink()
        except OSError:
            pass

        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class _StdioRpcChannel:
    """JSON-RPC over stdio shared by codex sub-processes (app-server, agent-rewriter).

    Encapsulates the previously duplicated logic between `_app_*` and `_rewriter_*`
    method families. The owner DaemonService keeps the concrete `Popen` reference
    on its own attributes; this channel reads it via the supplied `get_proc`
    callable so it always reflects start/stop transitions.
    """

    def __init__(
        self,
        *,
        label: str,
        log_func: Callable[[str], None],
        write_log: Callable[[str, str], None],
        get_proc: Callable[[], "Optional[subprocess.Popen[str]]"],
        send_lock: threading.Lock,
        req_lock: threading.Lock,
        pending: "dict[int, queue.Queue[dict[str, Any]]]",
        event_queue: "queue.Queue[dict[str, Any]]",
        get_default_timeout: Callable[[], float],
        resolve_tool_user_input_answers: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.label = label
        self._log = log_func
        self._write_log = write_log
        self._get_proc = get_proc
        self._send_lock = send_lock
        self._req_lock = req_lock
        self._pending = pending
        self._event_queue = event_queue
        self._get_default_timeout = get_default_timeout
        self._resolve_user_input = resolve_tool_user_input_answers
        self._next_id = 1

    @property
    def proc(self) -> "Optional[subprocess.Popen[str]]":
        return self._get_proc()

    def is_running(self) -> bool:
        p = self.proc
        return p is not None and p.poll() is None

    def send_json(self, payload: dict[str, Any]) -> bool:
        p = self.proc
        if p is None or p.poll() is not None or p.stdin is None:
            return False
        rendered = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            try:
                p.stdin.write(rendered + "\n")
                p.stdin.flush()
                self._write_log("SEND", rendered)
                return True
            except Exception as exc:
                self._log(f"WARN: {self.label} send failed: {exc}")
                return False

    def notify(self, method: str, params: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        return self.send_json(payload)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.is_running():
            return None
        with self._req_lock:
            req_id = self._next_id
            self._next_id += 1
            response_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[req_id] = response_q

        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        if not self.send_json(payload):
            with self._req_lock:
                self._pending.pop(req_id, None)
            return None

        wait_sec = timeout_sec if timeout_sec is not None else self._get_default_timeout()
        try:
            reply = response_q.get(timeout=max(1.0, float(wait_sec)))
        except queue.Empty:
            self._log(f"WARN: {self.label} request timeout method={method} id={req_id}")
            with self._req_lock:
                self._pending.pop(req_id, None)
            return None

        if "error" in reply:
            self._log(
                f"WARN: {self.label} request error method={method} id={req_id} error={reply.get('error')}"
            )
            return None
        result = reply.get("result")
        if isinstance(result, dict):
            return result
        return {"value": result}

    def handle_server_request(self, request_obj: dict[str, Any]) -> None:
        req_id = request_obj.get("id")
        method = str(request_obj.get("method") or "")
        params = request_obj.get("params")

        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            payload = {"id": req_id, "result": {"decision": "accept"}}
        elif method == "item/tool/requestUserInput":
            payload = {
                "id": req_id,
                "result": self._resolve_user_input(params if isinstance(params, dict) else {}),
            }
        elif method == "item/tool/call":
            payload = {
                "id": req_id,
                "result": {
                    "success": False,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": "dynamic tool call is not supported by this daemon bridge",
                        }
                    ],
                },
            }
        elif method in ("execCommandApproval", "applyPatchApproval"):
            payload = {"id": req_id, "result": {"decision": "approved"}}
        else:
            payload = {"id": req_id, "result": {}}
            self._log(
                f"WARN: unhandled {self.label} request method={method}, replied with empty result"
            )

        if not self.send_json(payload):
            self._log(
                f"WARN: failed to send {self.label} request response method={method} id={req_id}"
            )

    def dispatch_incoming(self, line: str) -> None:
        self._write_log("RECV", line)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            self._log(f"WARN: non-json {self.label} output: {line[:180]}")
            return
        if not isinstance(obj, dict):
            return

        if "id" in obj and ("result" in obj or "error" in obj):
            req_id = obj.get("id")
            with self._req_lock:
                pending_q = self._pending.pop(req_id, None)
            if pending_q is not None:
                try:
                    pending_q.put_nowait(obj)
                except Exception:
                    pass
            return

        method = obj.get("method")
        if not isinstance(method, str):
            return

        if "id" in obj:
            self.handle_server_request(obj)
            return

        try:
            self._event_queue.put_nowait(obj)
        except Exception:
            self._log(f"WARN: {self.label} event queue full; dropping event")

    def stdout_reader(self) -> None:
        p = self.proc
        if p is None or p.stdout is None:
            return
        for raw in p.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            self.dispatch_incoming(line)

    def stderr_reader(self) -> None:
        p = self.proc
        if p is None or p.stderr is None:
            return
        for raw in p.stderr:
            line = raw.rstrip("\n")
            if not line:
                continue
            self._write_log("ERR", line)
            if "ERROR" in line or "WARN" in line:
                self._log(f"[{self.label}][stderr] {line}")


class DaemonService:
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent
        self.logs_dir = Path(os.getenv("LOGS_DIR", str(self.root / "logs"))).resolve()
        self.tasks_dir = Path(os.getenv("TASKS_DIR", str(self.root / "tasks"))).resolve()
        self.platform = os.getenv("SONOLBOT_PLATFORM", "telegram").strip().lower()
        _default_store = (
            "discord_messages.json" if self.platform == "discord" else "telegram_messages.json"
        )
        _store_env = "DISCORD_MESSAGE_STORE" if self.platform == "discord" else "TELEGRAM_MESSAGE_STORE"
        self.store_file = Path(os.getenv(_store_env, str(self.root / _default_store))).resolve()
        self.is_bot_worker = (os.getenv("DAEMON_BOT_WORKER", "0").strip() == "1")
        self.bot_id = (os.getenv("SONOLBOT_BOT_ID", "") or "").strip()
        self.bot_workspace = Path(
            os.getenv("SONOLBOT_BOT_WORKSPACE", str(self.root))
        ).resolve()
        self.codex_work_dir = (self.bot_workspace if self.is_bot_worker else self.root).resolve()
        self.bots_config_path = default_config_path(self.root)

        self.pid_file = Path(
            os.getenv("DAEMON_PID_FILE", str(self.root / ".daemon_service.pid"))
        ).resolve()
        self.lock_file = Path(
            os.getenv("DAEMON_LOCK_FILE", str(self.pid_file.with_suffix(".lock")))
        ).resolve()
        self.codex_pid_file = Path(
            os.getenv("CODEX_PID_FILE", str(self.root / ".codex_app_server.pid"))
        ).resolve()
        self.state_dir = self.codex_pid_file.parent.resolve()
        self.app_server_lock_file = Path(
            os.getenv(
                "DAEMON_APP_SERVER_LOCK_FILE",
                str(self.state_dir / "app-server.lock"),
            )
        ).resolve()
        self.chat_locks_dir = Path(
            os.getenv(
                "DAEMON_CHAT_LOCKS_DIR",
                str(self.state_dir / "chat_locks"),
            )
        ).resolve()
        self.activity_file = Path(
            os.getenv("DAEMON_ACTIVITY_FILE", str(self.logs_dir / "codex-app-server.log"))
        ).resolve()

        self.poll_interval_sec = int(os.getenv("DAEMON_POLL_INTERVAL_SEC", "1"))
        self.idle_timeout_sec = int(os.getenv("DAEMON_IDLE_TIMEOUT_SEC", "600"))
        self.log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "7"))
        self.activity_max_bytes = self._env_int("DAEMON_ACTIVITY_MAX_BYTES", DEFAULT_ACTIVITY_MAX_BYTES, minimum=1)
        self.activity_backup_count = self._env_int("DAEMON_ACTIVITY_BACKUP_COUNT", DEFAULT_ACTIVITY_BACKUP_COUNT, minimum=0)
        self.activity_retention_days = self._env_int(
            "DAEMON_ACTIVITY_RETENTION_DAYS",
            self.log_retention_days,
            minimum=1,
        )
        self.codex_model = (
            os.getenv("SONOLBOT_CODEX_MODEL", "").strip() or DEFAULT_CODEX_MODEL
        )
        self.codex_reasoning_effort = (
            os.getenv("SONOLBOT_CODEX_REASONING_EFFORT", "").strip()
            or DEFAULT_CODEX_REASONING_EFFORT
        )
        self.fallback_send_max_attempts = self._env_int(
            "DAEMON_FALLBACK_SEND_MAX_ATTEMPTS",
            DEFAULT_FALLBACK_SEND_MAX_ATTEMPTS,
            minimum=1,
        )
        self.fallback_send_retry_delay_sec = self._env_float(
            "DAEMON_FALLBACK_SEND_RETRY_DELAY_SEC",
            DEFAULT_FALLBACK_SEND_RETRY_DELAY_SEC,
            minimum=0.05,
        )
        self.fallback_send_retry_backoff = self._env_float(
            "DAEMON_FALLBACK_SEND_RETRY_BACKOFF",
            DEFAULT_FALLBACK_SEND_RETRY_BACKOFF,
            minimum=1.0,
        )
        self.codex_cli_version = CODEX_CLI_VERSION_UNKNOWN
        self.codex_session_meta_file = self.logs_dir / "codex-session-current.json"
        self.tasks_partition_by_chat = self._env_bool(
            "SONOLBOT_TASKS_PARTITION_BY_CHAT",
            DEFAULT_TASKS_PARTITION_BY_CHAT,
        )
        self.app_server_listen = (
            os.getenv("DAEMON_APP_SERVER_LISTEN", DEFAULT_APP_SERVER_LISTEN).strip()
            or DEFAULT_APP_SERVER_LISTEN
        )
        self.app_server_progress_interval_sec = self._env_float(
            "DAEMON_APP_SERVER_PROGRESS_INTERVAL_SEC",
            DEFAULT_APP_SERVER_PROGRESS_INTERVAL_SEC,
            minimum=5.0,
        )
        self.app_server_steer_batch_window_ms = self._env_int(
            "DAEMON_APP_SERVER_STEER_BATCH_WINDOW_MS",
            DEFAULT_APP_SERVER_STEER_BATCH_WINDOW_MS,
            minimum=100,
        )
        self.app_server_turn_timeout_sec = self._env_int(
            "DAEMON_APP_SERVER_TURN_TIMEOUT_SEC",
            DEFAULT_APP_SERVER_TURN_TIMEOUT_SEC,
            minimum=60,
        )
        self.app_server_restart_backoff_sec = self._env_float(
            "DAEMON_APP_SERVER_RESTART_BACKOFF_SEC",
            DEFAULT_APP_SERVER_RESTART_BACKOFF_SEC,
            minimum=0.5,
        )
        self.app_server_request_timeout_sec = self._env_float(
            "DAEMON_APP_SERVER_REQUEST_TIMEOUT_SEC",
            DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SEC,
            minimum=5.0,
        )
        self.app_server_approval_policy = (
            os.getenv("DAEMON_APP_SERVER_APPROVAL_POLICY", DEFAULT_APP_SERVER_APPROVAL_POLICY).strip()
            or DEFAULT_APP_SERVER_APPROVAL_POLICY
        )
        self.app_server_sandbox = (
            os.getenv("DAEMON_APP_SERVER_SANDBOX", DEFAULT_APP_SERVER_SANDBOX).strip()
            or DEFAULT_APP_SERVER_SANDBOX
        )
        self.app_server_forward_agent_message = self._env_bool(
            "DAEMON_APP_SERVER_FORWARD_AGENT_MESSAGE",
            DEFAULT_APP_SERVER_FORWARD_AGENT_MESSAGE,
        )
        self.telegram_force_parse_mode = self._env_bool(
            "DAEMON_TELEGRAM_FORCE_PARSE_MODE",
            DEFAULT_TELEGRAM_FORCE_PARSE_MODE,
        )
        telegram_default_parse_raw = os.getenv(
            "DAEMON_TELEGRAM_DEFAULT_PARSE_MODE",
            DEFAULT_TELEGRAM_DEFAULT_PARSE_MODE,
        ).strip()
        telegram_default_parse_mode = self._normalize_telegram_parse_mode(telegram_default_parse_raw)
        if not telegram_default_parse_mode:
            if telegram_default_parse_raw:
                self._log(
                    "WARN: invalid DAEMON_TELEGRAM_DEFAULT_PARSE_MODE="
                    f"{telegram_default_parse_raw!r}; fallback={DEFAULT_TELEGRAM_DEFAULT_PARSE_MODE}"
                )
            telegram_default_parse_mode = DEFAULT_TELEGRAM_DEFAULT_PARSE_MODE
        self.telegram_default_parse_mode = telegram_default_parse_mode
        self.telegram_parse_fallback_raw_on_fail = self._env_bool(
            "DAEMON_TELEGRAM_PARSE_FALLBACK_RAW_ON_FAIL",
            DEFAULT_TELEGRAM_PARSE_FALLBACK_RAW_ON_FAIL,
        )
        self.agent_rewriter_enabled = self._env_bool(
            "DAEMON_AGENT_REWRITER_ENABLED",
            DEFAULT_AGENT_REWRITER_ENABLED,
        )
        self.agent_rewriter_timeout_sec = self._env_float(
            "DAEMON_AGENT_REWRITER_TIMEOUT_SEC",
            DEFAULT_AGENT_REWRITER_TIMEOUT_SEC,
            minimum=2.0,
        )
        self.agent_rewriter_request_timeout_sec = self._env_float(
            "DAEMON_AGENT_REWRITER_REQUEST_TIMEOUT_SEC",
            DEFAULT_AGENT_REWRITER_REQUEST_TIMEOUT_SEC,
            minimum=3.0,
        )
        self.agent_rewriter_restart_backoff_sec = self._env_float(
            "DAEMON_AGENT_REWRITER_RESTART_BACKOFF_SEC",
            DEFAULT_AGENT_REWRITER_RESTART_BACKOFF_SEC,
            minimum=0.5,
        )
        self.agent_rewriter_max_retry = self._env_int(
            "DAEMON_AGENT_REWRITER_MAX_RETRY",
            DEFAULT_AGENT_REWRITER_MAX_RETRY,
            minimum=0,
        )
        self.agent_rewriter_model = (
            os.getenv("DAEMON_AGENT_REWRITER_MODEL", DEFAULT_AGENT_REWRITER_MODEL).strip()
            or self.codex_model
        )
        self.agent_rewriter_reasoning_effort = (
            os.getenv("DAEMON_AGENT_REWRITER_REASONING_EFFORT", DEFAULT_AGENT_REWRITER_REASONING_EFFORT).strip()
            or DEFAULT_AGENT_REWRITER_REASONING_EFFORT
        )
        self.agent_rewriter_tmp_root = Path(
            os.getenv(
                "DAEMON_AGENT_REWRITER_TMP_ROOT",
                DEFAULT_AGENT_REWRITER_TMP_ROOT,
            )
        ).expanduser().resolve()
        self.agent_rewriter_cleanup_tmp = self._env_bool(
            "DAEMON_AGENT_REWRITER_CLEANUP_TMP",
            DEFAULT_AGENT_REWRITER_CLEANUP_TMP,
        )
        self.agent_rewriter_prompt_file = os.getenv("DAEMON_AGENT_REWRITER_PROMPT_FILE", "").strip()
        prompt_from_file = ""
        if self.agent_rewriter_prompt_file:
            try:
                prompt_path = Path(self.agent_rewriter_prompt_file).expanduser().resolve()
                prompt_from_file = prompt_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                self._log(
                    f"WARN: failed to load DAEMON_AGENT_REWRITER_PROMPT_FILE={self.agent_rewriter_prompt_file}: {exc}"
                )
        if prompt_from_file:
            self.agent_rewriter_prompt = prompt_from_file
        else:
            prompt_raw = os.getenv("DAEMON_AGENT_REWRITER_PROMPT", DEFAULT_AGENT_REWRITER_PROMPT)
            self.agent_rewriter_prompt = str(prompt_raw or DEFAULT_AGENT_REWRITER_PROMPT).replace("\\n", "\n").strip()
        if not self.agent_rewriter_prompt:
            self.agent_rewriter_prompt = DEFAULT_AGENT_REWRITER_PROMPT
        self.chat_lease_ttl_sec = self._env_float(
            "DAEMON_CHAT_LEASE_TTL_SEC",
            DEFAULT_CHAT_LEASE_TTL_SEC,
            minimum=30.0,
        )
        self.chat_lease_heartbeat_sec = self._env_float(
            "DAEMON_CHAT_LEASE_HEARTBEAT_SEC",
            DEFAULT_CHAT_LEASE_HEARTBEAT_SEC,
            minimum=5.0,
        )
        if self.chat_lease_heartbeat_sec > self.chat_lease_ttl_sec:
            self.chat_lease_heartbeat_sec = max(5.0, self.chat_lease_ttl_sec / 2.0)
        self.file_lock_wait_timeout_sec = self._env_float(
            "DAEMON_FILE_LOCK_WAIT_TIMEOUT_SEC",
            DEFAULT_FILE_LOCK_WAIT_TIMEOUT_SEC,
            minimum=0.2,
        )
        self.completed_message_ttl_sec = self._env_float(
            "DAEMON_COMPLETED_MESSAGE_TTL_SEC",
            DEFAULT_COMPLETED_MESSAGE_TTL_SEC,
            minimum=30.0,
        )
        self.ui_mode_timeout_sec = self._env_float(
            "DAEMON_UI_MODE_TIMEOUT_SEC",
            DEFAULT_UI_MODE_TIMEOUT_SEC,
            minimum=30.0,
        )
        self.new_task_summary_lines = self._env_int(
            "DAEMON_NEW_TASK_SUMMARY_LINES",
            DEFAULT_NEW_TASK_SUMMARY_LINES,
            minimum=10,
        )
        self.new_task_summary_max_chars = self._env_int(
            "DAEMON_NEW_TASK_SUMMARY_MAX_CHARS",
            DEFAULT_NEW_TASK_SUMMARY_MAX_CHARS,
            minimum=1200,
        )
        self.always_inject_recent_summary = self._env_bool(
            "DAEMON_ALWAYS_INJECT_RECENT_SUMMARY",
            True,
        )
        self.always_inject_summary_hours = self._env_int(
            "DAEMON_ALWAYS_INJECT_SUMMARY_HOURS",
            12,
            minimum=1,
        )
        self.always_inject_summary_lines = self._env_int(
            "DAEMON_ALWAYS_INJECT_SUMMARY_LINES",
            20,
            minimum=10,
        )
        self.always_inject_summary_max_chars = self._env_int(
            "DAEMON_ALWAYS_INJECT_SUMMARY_MAX_CHARS",
            3500,
            minimum=1200,
        )
        self.result_summary_note_max_len = self._env_int(
            "DAEMON_RESULT_SUMMARY_NOTE_MAX_LEN",
            240,
            minimum=80,
        )
        self.task_search_llm_enabled = self._env_bool(
            "DAEMON_TASK_SEARCH_LLM_ENABLED",
            DEFAULT_TASK_SEARCH_LLM_ENABLED,
        )
        self.task_search_llm_limit = self._env_int(
            "DAEMON_TASK_SEARCH_LLM_LIMIT",
            DEFAULT_TASK_SEARCH_LLM_LIMIT,
            minimum=1,
        )
        self.task_search_llm_candidate_pool_limit = self._env_int(
            "DAEMON_TASK_SEARCH_LLM_CANDIDATE_POOL_LIMIT",
            DEFAULT_TASK_SEARCH_LLM_CANDIDATE_POOL_LIMIT,
            minimum=10,
        )
        self.task_search_llm_min_score = self._env_int(
            "DAEMON_TASK_SEARCH_LLM_MIN_SCORE",
            DEFAULT_TASK_SEARCH_LLM_MIN_SCORE,
            minimum=0,
        )
        if self.task_search_llm_min_score > 100:
            self.task_search_llm_min_score = 100
        self.task_search_llm_turn_timeout_sec = self._env_float(
            "DAEMON_TASK_SEARCH_LLM_TURN_TIMEOUT_SEC",
            DEFAULT_TASK_SEARCH_LLM_TURN_TIMEOUT_SEC,
            minimum=5.0,
        )
        self.task_search_llm_request_timeout_sec = self._env_float(
            "DAEMON_TASK_SEARCH_LLM_REQUEST_TIMEOUT_SEC",
            DEFAULT_TASK_SEARCH_LLM_REQUEST_TIMEOUT_SEC,
            minimum=5.0,
        )
        self.app_server_state_file = Path(
            os.getenv(
                "DAEMON_APP_SERVER_STATE_FILE",
                str(self.logs_dir / "codex-app-session-state.json"),
            )
        ).resolve()
        self.app_server_log_file = Path(
            os.getenv("DAEMON_APP_SERVER_LOG_FILE", str(self.logs_dir / "codex-app-server.log"))
        ).resolve()
        rewriter_workspace_env = os.getenv("DAEMON_AGENT_REWRITER_WORKSPACE", "").strip()
        if rewriter_workspace_env:
            rewriter_workspace_raw = rewriter_workspace_env
        elif self.is_bot_worker and self.bot_id:
            bot_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.bot_id).strip()) or "unknown"
            rewriter_workspace_raw = str(self.agent_rewriter_tmp_root / bot_key)
        else:
            rewriter_workspace_raw = str(self.state_dir / "agent-rewriter-workspace")
        self.agent_rewriter_workspace = Path(rewriter_workspace_raw).resolve()
        self.agent_rewriter_pid_file = Path(
            os.getenv(
                "DAEMON_AGENT_REWRITER_PID_FILE",
                str(self.state_dir / "codex-agent-rewriter.pid"),
            )
        ).resolve()
        self.agent_rewriter_state_file = Path(
            os.getenv(
                "DAEMON_AGENT_REWRITER_STATE_FILE",
                str(self.state_dir / "codex-agent-rewriter-state.json"),
            )
        ).resolve()
        self.agent_rewriter_log_file = Path(
            os.getenv(
                "DAEMON_AGENT_REWRITER_LOG_FILE",
                str(self.logs_dir / "codex-agent-rewriter.log"),
            )
        ).resolve()
        self.agent_rewriter_lock_file = Path(
            os.getenv(
                "DAEMON_AGENT_REWRITER_LOCK_FILE",
                str(self.state_dir / "agent-rewriter.lock"),
            )
        ).resolve()

        self.python_bin = self._detect_python_bin()
        self.codex_run_meta: Optional[dict[str, object]] = None
        self._telegram_runtime: Optional[dict[str, object]] = None
        self._telegram_skill = None
        self._task_skill = None
        self.stop_requested = False
        self.app_proc: Optional[subprocess.Popen[str]] = None
        self.app_proc_generation = 0
        self.app_json_send_lock = threading.Lock()
        self.app_req_lock = threading.Lock()
        self.app_pending_responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.app_event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._app_channel = _StdioRpcChannel(
            label="app-server",
            log_func=self._log,
            write_log=self._write_app_server_log,
            get_proc=lambda: self.app_proc,
            send_lock=self.app_json_send_lock,
            req_lock=self.app_req_lock,
            pending=self.app_pending_responses,
            event_queue=self.app_event_queue,
            get_default_timeout=lambda: self.app_server_request_timeout_sec,
            resolve_tool_user_input_answers=self._resolve_tool_user_input_answers,
        )
        self.app_chat_states: dict[int, dict[str, Any]] = {}
        self.app_thread_to_chat: dict[str, int] = {}
        self.app_turn_to_chat: dict[str, int] = {}
        self.app_aux_turn_results: dict[str, dict[str, Any]] = {}
        self.app_last_restart_try_epoch = 0.0
        self._process_lock: _ProcessFileLock | None = None
        self._app_server_lock_fd: int | None = None
        self._app_server_lock_busy_logged_at = 0.0
        self._owned_chat_leases: set[int] = set()
        self._chat_lease_busy_logged_at: dict[int, float] = {}
        self.completed_message_ids_recent: dict[int, float] = {}
        self._completed_requeue_log_ts: dict[int, float] = {}
        self.rewriter_proc: Optional[subprocess.Popen[str]] = None
        self.rewriter_json_send_lock = threading.Lock()
        self.rewriter_req_lock = threading.Lock()
        self.rewriter_pending_responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.rewriter_event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._rewriter_channel = _StdioRpcChannel(
            label="agent-rewriter",
            log_func=self._log,
            write_log=self._write_agent_rewriter_log,
            get_proc=lambda: self.rewriter_proc,
            send_lock=self.rewriter_json_send_lock,
            req_lock=self.rewriter_req_lock,
            pending=self.rewriter_pending_responses,
            event_queue=self.rewriter_event_queue,
            get_default_timeout=lambda: self.agent_rewriter_request_timeout_sec,
            resolve_tool_user_input_answers=self._resolve_tool_user_input_answers,
        )
        self.rewriter_last_restart_try_epoch = 0.0
        self.rewriter_chat_threads: dict[int, str] = {}
        self.rewriter_turn_results: dict[str, dict[str, Any]] = {}
        self._agent_rewriter_lock_fd: int | None = None
        self._agent_rewriter_lock_busy_logged_at = 0.0

        self.env = os.environ.copy()
        self.env.setdefault("LANG", "C.UTF-8")
        self.env.setdefault("LC_ALL", "C.UTF-8")
        self.env.setdefault("PYTHONUTF8", "1")
        self.env.setdefault("PYTHONIOENCODING", "UTF-8")
        self.env["SONOLBOT_GUI_SESSION"] = "1" if self._has_gui_session() else "0"

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.store_file.parent.mkdir(parents=True, exist_ok=True)
        self.store_file.touch(exist_ok=True)
        self.codex_work_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chat_locks_dir.mkdir(parents=True, exist_ok=True)
        self.agent_rewriter_workspace.mkdir(parents=True, exist_ok=True)
        self._harden_sensitive_permissions()
        self._load_app_server_state()
        self._load_agent_rewriter_state()
        self._cleanup_activity_logs()
        self._rotate_activity_log_if_needed(force=False)

    def _detect_python_bin(self) -> str:
        venv_py = self.root / ".venv" / "bin" / "python"
        if venv_py.exists():
            return str(venv_py)
        return sys.executable

    def _daily_log_path(self) -> Path:
        return self.logs_dir / f"daemon-{datetime.now().strftime('%Y-%m-%d')}.log"

    def _env_int(self, name: str, default: int, minimum: int = 0) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return max(minimum, default)
        try:
            return max(minimum, int(raw))
        except ValueError:
            return max(minimum, default)

    def _env_float(self, name: str, default: float, minimum: float = 0.0) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return max(minimum, default)
        try:
            return max(minimum, float(raw))
        except ValueError:
            return max(minimum, default)

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            return default
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _normalize_telegram_parse_mode(parse_mode: object) -> str:
        normalized = str(parse_mode or "").strip().lower()
        if not normalized:
            return ""
        if normalized == "html":
            return "HTML"
        if normalized == "markdownv2":
            return "MarkdownV2"
        if normalized == "markdown":
            return "Markdown"
        return ""

    def _resolve_telegram_parse_mode(self, parse_mode: str | None) -> str | None:
        requested = self._normalize_telegram_parse_mode(parse_mode)
        if requested:
            return requested
        if not self.telegram_force_parse_mode:
            return None
        fallback_mode = self._normalize_telegram_parse_mode(self.telegram_default_parse_mode)
        if fallback_mode:
            return fallback_mode
        return None

    @staticmethod
    def _sanitize_telegram_text_for_parse_mode(text: object, parse_mode: str | None) -> str:
        rendered = str(text or "")
        normalized_mode = DaemonService._normalize_telegram_parse_mode(parse_mode)
        if normalized_mode != "HTML":
            return rendered
        # Telegram HTML parse_mode does not support <br>; use literal newline.
        rendered = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", rendered)
        return rendered

    def _secure_file(self, path: Path) -> None:
        try:
            path.chmod(SECURE_FILE_MODE)
        except OSError:
            pass

    def _secure_dir(self, path: Path) -> None:
        try:
            path.chmod(SECURE_DIR_MODE)
        except OSError:
            pass

    def _resolve_codex_config_path(self) -> Path:
        raw = str(self.env.get("CODEX_CONFIG") or "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = (self.root / candidate).resolve()
            return candidate.resolve()
        home_dir = str(self.env.get("HOME") or "").strip()
        if home_dir:
            return (Path(home_dir).expanduser() / ".codex" / "config.toml").resolve()
        return (Path.home() / ".codex" / "config.toml").resolve()

    @staticmethod
    def _format_mcp_server_key_for_override(server_name: str) -> str:
        name = str(server_name or "").strip()
        if not name:
            return ""
        if MCP_SERVER_NAME_RE.fullmatch(name):
            return name
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _build_disable_mcp_overrides_from_codex_config(self) -> tuple[list[str], Path]:
        config_path = self._resolve_codex_config_path()
        # Keep MCP enabled by default; opt-in disable via env for troubleshooting.
        if not self._env_bool("SONOLBOT_DISABLE_MCP_IN_APP_SERVER", False):
            return [], config_path
        if not config_path.exists():
            return [], config_path
        try:
            raw = config_path.read_text(encoding="utf-8")
            parsed = tomllib.loads(raw)
        except Exception as exc:
            self._log(f"WARN: failed to parse codex config for MCP overrides path={config_path}: {exc}")
            return [], config_path
        servers = parsed.get("mcp_servers")
        if not isinstance(servers, dict):
            return [], config_path

        overrides: list[str] = []
        for key in servers.keys():
            key_text = self._format_mcp_server_key_for_override(str(key))
            if not key_text:
                continue
            overrides.append(f"mcp_servers.{key_text}.enabled=false")
        return overrides, config_path

    def _build_codex_app_server_cmd(self, role: str) -> list[str]:
        ai_provider = os.getenv("SONOLBOT_AI_PROVIDER", "codex").strip().lower()
        if ai_provider == "claude":
            claude_model = os.getenv("SONOLBOT_CLAUDE_MODEL", "claude-opus-4-6")
            if role == "app-server":
                self._log(f"[AI] provider=claude model={claude_model}")
            return [sys.executable, str(self.root / "claude_app_server.py"), "--listen", self.app_server_listen]
        # 기본: codex app-server
        cmd = ["codex", "app-server", "--listen", self.app_server_listen]
        overrides, config_path = self._build_disable_mcp_overrides_from_codex_config()
        for override in overrides:
            cmd.extend(["-c", override])
        if overrides:
            self._log(
                f"{role} will start with MCP disabled from config "
                f"path={config_path} servers={len(overrides)}"
            )
        return cmd

    def _harden_sensitive_permissions(self) -> None:
        self._secure_dir(self.logs_dir)
        self._secure_dir(self.state_dir)
        self._secure_dir(self.chat_locks_dir)
        self._secure_file(self.store_file)
        env_file = self.root / ".env"
        if env_file.exists():
            self._secure_file(env_file)
        if self.app_server_state_file.exists():
            self._secure_file(self.app_server_state_file)
        if self.app_server_log_file.exists():
            self._secure_file(self.app_server_log_file)
        if self.app_server_lock_file.exists():
            self._secure_file(self.app_server_lock_file)
        if self.agent_rewriter_state_file.exists():
            self._secure_file(self.agent_rewriter_state_file)
        if self.agent_rewriter_log_file.exists():
            self._secure_file(self.agent_rewriter_log_file)
        if self.agent_rewriter_lock_file.exists():
            self._secure_file(self.agent_rewriter_lock_file)
        if self.agent_rewriter_pid_file.exists():
            self._secure_file(self.agent_rewriter_pid_file)
        self._secure_dir(self.agent_rewriter_workspace)
        if self.codex_pid_file.exists():
            self._secure_file(self.codex_pid_file)
        for log_path in self.logs_dir.glob("*.log"):
            self._secure_file(log_path)

    def _new_chat_state(self) -> dict[str, Any]:
        return chat_state_module.create_chat_state(ui_mode_idle=UI_MODE_IDLE)

    def _load_app_server_state(self) -> None:
        self.app_chat_states = {}
        self.app_thread_to_chat = {}
        if not self.app_server_state_file.exists():
            return
        try:
            raw = json.loads(self.app_server_state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = raw.get("sessions", {})
        if not isinstance(sessions, dict):
            return
        for chat_key, payload in sessions.items():
            try:
                chat_id = int(chat_key)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            state = self._new_chat_state()
            thread_id = str(payload.get("thread_id") or "").strip()
            if thread_id:
                state["thread_id"] = thread_id
                self.app_thread_to_chat[thread_id] = chat_id
            self.app_chat_states[chat_id] = state

    def _save_app_server_state(self) -> None:
        data: dict[str, Any] = {"version": 1, "sessions": {}}
        for chat_id, state in self.app_chat_states.items():
            thread_id = str(state.get("thread_id") or "").strip()
            if not thread_id:
                continue
            data["sessions"][str(chat_id)] = {"thread_id": thread_id}
        try:
            self.app_server_state_file.parent.mkdir(parents=True, exist_ok=True)
            self.app_server_state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._secure_file(self.app_server_state_file)
        except OSError as exc:
            self._log(f"WARN: failed to save app-server state: {exc}")

    def _load_agent_rewriter_state(self) -> None:
        self.rewriter_chat_threads = {}
        if not self.agent_rewriter_state_file.exists():
            return
        try:
            raw = json.loads(self.agent_rewriter_state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = raw.get("sessions", {})
        if not isinstance(sessions, dict):
            return
        for chat_key, payload in sessions.items():
            try:
                chat_id = int(chat_key)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            thread_id = str(payload.get("thread_id") or "").strip()
            if not thread_id:
                continue
            self.rewriter_chat_threads[chat_id] = thread_id

    def _save_agent_rewriter_state(self) -> None:
        data: dict[str, Any] = {"version": 1, "sessions": {}}
        for chat_id, thread_id in self.rewriter_chat_threads.items():
            normalized_thread_id = str(thread_id or "").strip()
            if not normalized_thread_id:
                continue
            data["sessions"][str(chat_id)] = {"thread_id": normalized_thread_id}
        try:
            self.agent_rewriter_state_file.parent.mkdir(parents=True, exist_ok=True)
            self.agent_rewriter_state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._secure_file(self.agent_rewriter_state_file)
        except OSError as exc:
            self._log(f"WARN: failed to save agent-rewriter state: {exc}")

    def _write_app_server_log(self, prefix: str, line: str) -> None:
        text = line.rstrip("\n")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rendered = f"[{ts}] [{prefix}] {text}\n"
        try:
            self.app_server_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.app_server_log_file.open("a", encoding="utf-8") as f:
                f.write(rendered)
            self._secure_file(self.app_server_log_file)
        except OSError:
            pass

    def _write_agent_rewriter_log(self, prefix: str, line: str) -> None:
        text = line.rstrip("\n")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rendered = f"[{ts}] [{prefix}] {text}\n"
        try:
            self.agent_rewriter_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.agent_rewriter_log_file.open("a", encoding="utf-8") as f:
                f.write(rendered)
            self._secure_file(self.agent_rewriter_log_file)
        except OSError:
            pass

    def _get_chat_state(self, chat_id: int) -> dict[str, Any]:
        state = self.app_chat_states.get(chat_id)
        if state is None:
            state = self._new_chat_state()
            self.app_chat_states[chat_id] = state
        return state

    def _has_gui_session(self) -> bool:
        if os.name == "nt":
            return True
        return bool(self.env.get("DISPLAY") or self.env.get("WAYLAND_DISPLAY"))

    def _log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        log_path = self._daily_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
            self._secure_file(log_path)
            return
        except OSError as exc:
            fallback_line = line.rstrip("\n") + f" [fallback_reason={exc}]\n"
            try:
                FALLBACK_DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with FALLBACK_DAEMON_LOG_FILE.open("a", encoding="utf-8") as f:
                    f.write(fallback_line)
            except OSError:
                pass

    def _cleanup_logs(self) -> None:
        cutoff = datetime.now().date() - timedelta(days=max(1, self.log_retention_days) - 1)
        for path in self.logs_dir.glob("*.log"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _activity_backup_path(self, index: int) -> Path:
        return self.activity_file.with_name(f"{self.activity_file.name}.{index}")

    def _rotate_activity_log_if_needed(self, force: bool) -> None:
        if self.activity_backup_count <= 0 or self.activity_max_bytes <= 0:
            return
        if not self.activity_file.exists():
            return
        try:
            size = self.activity_file.stat().st_size
        except OSError:
            return
        if size <= self.activity_max_bytes and not force:
            return

        for idx in range(self.activity_backup_count, 1, -1):
            src = self._activity_backup_path(idx - 1)
            dst = self._activity_backup_path(idx)
            if not src.exists():
                continue
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
                self._secure_file(dst)
            except OSError:
                continue

        backup1 = self._activity_backup_path(1)
        try:
            if backup1.exists():
                backup1.unlink()
            shutil.copy2(self.activity_file, backup1)
            self._secure_file(backup1)

            # copytruncate style rotation so a running writer can keep appending safely.
            with self.activity_file.open("w", encoding="utf-8"):
                pass
            self._secure_file(self.activity_file)
            self._log(
                "Activity log rotated "
                f"file={self.activity_file} size={size} max={self.activity_max_bytes} "
                f"backups={self.activity_backup_count}"
            )
        except OSError as exc:
            self._log(f"WARN: failed to rotate activity log {self.activity_file}: {exc}")

    def _cleanup_activity_logs(self) -> None:
        cutoff_ts = time.time() - (max(1, self.activity_retention_days) * 86400)
        pattern = f"{self.activity_file.name}.*"
        candidates = [self.activity_file, *sorted(self.activity_file.parent.glob(pattern))]

        for path in candidates:
            if not path.exists() or not path.is_file():
                continue

            prefix = f"{self.activity_file.name}."
            suffix = path.name[len(prefix) :] if path.name.startswith(prefix) else ""
            if suffix.isdigit() and self.activity_backup_count >= 0 and int(suffix) > self.activity_backup_count:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue

            # Keep active file while app-server is running.
            if path == self.activity_file and self._app_is_running():
                continue

            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink()
            except OSError:
                pass

    def _write_codex_session_meta(self) -> None:
        if not self.codex_run_meta:
            return
        payload = dict(self.codex_run_meta)
        payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.codex_session_meta_file.parent.mkdir(parents=True, exist_ok=True)
            self.codex_session_meta_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._secure_file(self.codex_session_meta_file)
        except OSError as exc:
            self._log(f"WARN: failed to write codex session meta: {exc}")

    def _set_runtime_env(self, key: str, value: str) -> None:
        self.env[key] = value
        os.environ[key] = value

    def _sync_codex_runtime_env(
        self,
        *,
        run_id: str,
        mode: str,
        started_at: str,
        resume_target: str,
        session_id: str,
        thread_id: str = "",
    ) -> None:
        self._set_runtime_env("SONOLBOT_CODEX_RUN_ID", run_id)
        self._set_runtime_env("SONOLBOT_CODEX_MODE", mode)
        self._set_runtime_env("SONOLBOT_CODEX_STARTED_AT", started_at)
        self._set_runtime_env("SONOLBOT_CODEX_RESUME_TARGET", resume_target)
        self._set_runtime_env("SONOLBOT_CODEX_SESSION_ID", session_id)
        self._set_runtime_env("SONOLBOT_CODEX_THREAD_ID", thread_id)
        self._set_runtime_env("SONOLBOT_CODEX_CLI_VERSION", self.codex_cli_version)
        self._set_runtime_env("SONOLBOT_CODEX_MODEL", self.codex_model)
        self._set_runtime_env("SONOLBOT_CODEX_REASONING_EFFORT", self.codex_reasoning_effort)
        self._set_runtime_env("SONOLBOT_CODEX_SESSION_META_FILE", str(self.codex_session_meta_file))
        self.env.setdefault("SONOLBOT_STORE_CODEX_SESSION", "1")
        os.environ.setdefault("SONOLBOT_STORE_CODEX_SESSION", "1")

    def _sync_app_server_session_meta(self, active_chat_id: int | None = None) -> None:
        if not self.codex_run_meta:
            return
        if str(self.codex_run_meta.get("mode") or "").strip() != "app_server":
            return

        sessions: dict[str, dict[str, object]] = {}
        thread_ids_by_chat: dict[str, str] = {}
        first_thread = ""
        active_thread = ""
        for chat_id in sorted(self.app_chat_states.keys()):
            state = self._get_chat_state(chat_id)
            thread_id = str(state.get("thread_id") or "").strip()
            if not thread_id:
                continue
            active_turn_id = str(state.get("active_turn_id") or "").strip()
            payload: dict[str, object] = {"thread_id": thread_id}
            if active_turn_id:
                payload["active_turn_id"] = active_turn_id
            queued_messages = state.get("queued_messages") or []
            if isinstance(queued_messages, list) and queued_messages:
                payload["queued_count"] = len(queued_messages)
            sessions[str(chat_id)] = payload
            thread_ids_by_chat[str(chat_id)] = thread_id
            if not first_thread:
                first_thread = thread_id
            if active_chat_id is not None and chat_id == active_chat_id:
                active_thread = thread_id

        if active_chat_id is None:
            current_thread_id = str(self.codex_run_meta.get("current_thread_id") or "").strip()
        else:
            current_thread_id = active_thread

        self.codex_run_meta["transport"] = "app_server"
        self.codex_run_meta["listen"] = self.app_server_listen
        self.codex_run_meta["app_server_generation"] = self.app_proc_generation
        self.codex_run_meta["app_server_pid"] = self.app_proc.pid if self._app_is_running() and self.app_proc else 0
        self.codex_run_meta["thread_ids_by_chat"] = thread_ids_by_chat
        self.codex_run_meta["sessions"] = sessions
        if active_chat_id is not None:
            self.codex_run_meta["last_active_chat_id"] = active_chat_id
        if current_thread_id:
            self.codex_run_meta["current_thread_id"] = current_thread_id
            self.codex_run_meta["thread_id"] = current_thread_id
        elif self.codex_run_meta.get("current_thread_id"):
            self.codex_run_meta["current_thread_id"] = ""
            self.codex_run_meta["thread_id"] = ""

        fallback_session_id = current_thread_id or first_thread
        if fallback_session_id:
            # Backward-compatible alias: session_id == active thread id in app-server mode.
            self.codex_run_meta["session_id"] = fallback_session_id
            self.codex_run_meta["session_id_kind"] = "thread_id_alias"

        self._sync_codex_runtime_env(
            run_id=str(self.codex_run_meta.get("run_id") or ""),
            mode="app_server",
            started_at=str(self.codex_run_meta.get("started_at") or ""),
            resume_target="",
            session_id=str(self.codex_run_meta.get("session_id") or ""),
            thread_id=(current_thread_id or fallback_session_id),
        )
        self._write_codex_session_meta()

    def _acquire_lock(self) -> None:
        if self._process_lock is None:
            owner_label = (
                f"Daemon worker(bot_id={self.bot_id or '-'})"
                if self.is_bot_worker
                else "Daemon"
            )
            self._process_lock = _ProcessFileLock(
                lock_file=self.lock_file,
                pid_file=self.pid_file,
                owner_label=owner_label,
            )
        self._process_lock.acquire()

    def _release_lock(self) -> None:
        if self._process_lock is not None:
            self._process_lock.release()
            self._process_lock = None

    @staticmethod
    def _read_pid_file(path: Path) -> int:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    @staticmethod
    def _pid_cmdline(pid: int) -> str:
        if pid <= 0:
            return ""
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            try:
                raw = proc_cmdline.read_bytes()
                if raw:
                    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
            if proc.returncode == 0:
                return str(proc.stdout or "").strip()
        except Exception:
            pass
        return ""

    def _is_codex_app_server_pid(self, pid: int) -> bool:
        if pid <= 0 or not _is_pid_alive(pid):
            return False
        cmdline = self._pid_cmdline(pid).lower()
        if not cmdline:
            # When command-line probing fails but process is alive,
            # treat as occupied to avoid duplicate app-server starts.
            return True
        return ("codex" in cmdline) and ("app-server" in cmdline)

    def _try_lock_fd_nonblocking(self, fd: int) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise
        if msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        return True

    def _unlock_fd(self, fd: int) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    @contextmanager
    def _exclusive_file_lock(self, lock_path: Path, wait_timeout_sec: float | None = None):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_dir(lock_path.parent)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, SECURE_FILE_MODE)
        self._secure_file(lock_path)
        acquired = False
        timeout_sec = max(0.05, float(wait_timeout_sec or self.file_lock_wait_timeout_sec))
        deadline = time.time() + timeout_sec
        try:
            while True:
                if self._try_lock_fd_nonblocking(fd):
                    acquired = True
                    break
                if time.time() >= deadline:
                    raise TimeoutError(f"lock timeout: {lock_path}")
                time.sleep(0.05)
            yield
        finally:
            if acquired:
                self._unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    def _log_app_server_lock_busy(self) -> None:
        now_epoch = time.time()
        if (now_epoch - self._app_server_lock_busy_logged_at) < max(5.0, float(self.poll_interval_sec)):
            return
        self._app_server_lock_busy_logged_at = now_epoch
        pid_hint = self._read_pid_file(self.codex_pid_file)
        self._log(
            f"app_server_lock_busy path={self.app_server_lock_file} pid_hint={pid_hint or '-'}"
        )

    def _acquire_app_server_lock(self) -> bool:
        if self._app_server_lock_fd is not None:
            return True
        self.app_server_lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._secure_dir(self.app_server_lock_file.parent)
        fd = os.open(str(self.app_server_lock_file), os.O_RDWR | os.O_CREAT, SECURE_FILE_MODE)
        if not self._try_lock_fd_nonblocking(fd):
            try:
                os.close(fd)
            except OSError:
                pass
            self._log_app_server_lock_busy()
            return False
        self._app_server_lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass
        self._secure_file(self.app_server_lock_file)
        self._app_server_lock_busy_logged_at = 0.0
        self._log(f"app_server_lock_acquired path={self.app_server_lock_file}")
        return True

    def _release_app_server_lock(self) -> None:
        if self._app_server_lock_fd is None:
            return
        self._unlock_fd(self._app_server_lock_fd)
        try:
            os.close(self._app_server_lock_fd)
        except OSError:
            pass
        self._app_server_lock_fd = None
        self._log(f"app_server_lock_released path={self.app_server_lock_file}")

    def _log_agent_rewriter_lock_busy(self) -> None:
        now_epoch = time.time()
        if (now_epoch - self._agent_rewriter_lock_busy_logged_at) < max(5.0, float(self.poll_interval_sec)):
            return
        self._agent_rewriter_lock_busy_logged_at = now_epoch
        pid_hint = self._read_pid_file(self.agent_rewriter_pid_file)
        self._log(
            f"agent_rewriter_lock_busy path={self.agent_rewriter_lock_file} pid_hint={pid_hint or '-'}"
        )

    def _acquire_agent_rewriter_lock(self) -> bool:
        if self._agent_rewriter_lock_fd is not None:
            return True
        self.agent_rewriter_lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._secure_dir(self.agent_rewriter_lock_file.parent)
        fd = os.open(str(self.agent_rewriter_lock_file), os.O_RDWR | os.O_CREAT, SECURE_FILE_MODE)
        if not self._try_lock_fd_nonblocking(fd):
            try:
                os.close(fd)
            except OSError:
                pass
            self._log_agent_rewriter_lock_busy()
            return False
        self._agent_rewriter_lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass
        self._secure_file(self.agent_rewriter_lock_file)
        self._agent_rewriter_lock_busy_logged_at = 0.0
        self._log(f"agent_rewriter_lock_acquired path={self.agent_rewriter_lock_file}")
        return True

    def _release_agent_rewriter_lock(self) -> None:
        if self._agent_rewriter_lock_fd is None:
            return
        self._unlock_fd(self._agent_rewriter_lock_fd)
        try:
            os.close(self._agent_rewriter_lock_fd)
        except OSError:
            pass
        self._agent_rewriter_lock_fd = None
        self._log(f"agent_rewriter_lock_released path={self.agent_rewriter_lock_file}")

    def _chat_lease_path(self, chat_id: int) -> Path:
        return self.chat_locks_dir / f"chat_{int(chat_id)}.json"

    def _chat_lease_lock_path(self, chat_id: int) -> Path:
        return self.chat_locks_dir / f"chat_{int(chat_id)}.lock"

    def _load_chat_lease_unlocked(self, chat_id: int) -> dict[str, Any] | None:
        path = self._chat_lease_path(chat_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            return None
        return None

    def _save_chat_lease_unlocked(self, chat_id: int, payload: dict[str, Any]) -> None:
        path = self._chat_lease_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_dir(path.parent)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._secure_file(tmp)
            os.replace(tmp, path)
            self._secure_file(path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _delete_chat_lease_unlocked(self, chat_id: int) -> None:
        path = self._chat_lease_path(chat_id)
        if not path.exists():
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _chat_lease_status(self, lease: dict[str, Any], now_epoch: float) -> tuple[bool, str]:
        try:
            expires_at = float(lease.get("expires_at") or 0.0)
        except Exception:
            expires_at = 0.0
        owner_pid = int(lease.get("owner_pid") or 0)
        if expires_at <= now_epoch:
            return False, "expired"
        if owner_pid <= 0 or not _is_pid_alive(owner_pid):
            return False, "owner_dead"
        return True, "active"

    def _log_chat_lease_busy(self, chat_id: int, owner_pid: int, expires_at: float) -> None:
        now_epoch = time.time()
        last = float(self._chat_lease_busy_logged_at.get(chat_id) or 0.0)
        if (now_epoch - last) < max(5.0, float(self.poll_interval_sec)):
            return
        self._chat_lease_busy_logged_at[chat_id] = now_epoch
        remain = max(0.0, expires_at - now_epoch)
        self._log(
            f"chat_lease_busy chat_id={chat_id} owner_pid={owner_pid or '-'} "
            f"remaining={remain:.1f}s"
        )

    def _chat_lease_try_acquire(self, chat_id: int, message_ids: set[int]) -> bool:
        msg_ids = sorted(int(v) for v in message_ids if int(v) > 0)
        now_epoch = time.time()
        lock_path = self._chat_lease_lock_path(chat_id)
        try:
            with self._exclusive_file_lock(lock_path):
                lease = self._load_chat_lease_unlocked(chat_id)
                if isinstance(lease, dict):
                    valid, _ = self._chat_lease_status(lease, now_epoch)
                    owner_pid = int(lease.get("owner_pid") or 0)
                    expires_at = float(lease.get("expires_at") or 0.0)
                    if valid and owner_pid != os.getpid():
                        self._log_chat_lease_busy(chat_id, owner_pid, expires_at)
                        return False
                    if not valid:
                        self._delete_chat_lease_unlocked(chat_id)
                        self._log(
                            f"chat_lease_recovered chat_id={chat_id} stale_reason=expired_or_owner_dead"
                        )

                payload = {
                    "chat_id": int(chat_id),
                    "owner_pid": int(os.getpid()),
                    "app_server_pid": int(self.app_proc.pid) if self._app_is_running() and self.app_proc else 0,
                    "turn_id": "",
                    "message_ids": msg_ids,
                    "acquired_at": now_epoch,
                    "updated_at": now_epoch,
                    "expires_at": now_epoch + float(self.chat_lease_ttl_sec),
                }
                self._save_chat_lease_unlocked(chat_id, payload)
                self._owned_chat_leases.add(int(chat_id))
                self._chat_lease_busy_logged_at.pop(int(chat_id), None)
                self._log(f"chat_lease_acquired chat_id={chat_id} messages={len(msg_ids)}")
                return True
        except TimeoutError:
            self._log(f"WARN: chat_lease_lock_timeout chat_id={chat_id}")
            return False
        except Exception as exc:
            self._log(f"WARN: chat_lease_acquire_failed chat_id={chat_id}: {exc}")
            return False

    def _chat_lease_touch(
        self,
        chat_id: int,
        *,
        turn_id: str | None = None,
        message_ids: set[int] | None = None,
    ) -> bool:
        now_epoch = time.time()
        lock_path = self._chat_lease_lock_path(chat_id)
        try:
            with self._exclusive_file_lock(lock_path):
                lease = self._load_chat_lease_unlocked(chat_id)
                if not isinstance(lease, dict):
                    return False
                owner_pid = int(lease.get("owner_pid") or 0)
                if owner_pid != os.getpid():
                    return False
                lease["app_server_pid"] = int(self.app_proc.pid) if self._app_is_running() and self.app_proc else 0
                lease["updated_at"] = now_epoch
                lease["expires_at"] = now_epoch + float(self.chat_lease_ttl_sec)
                if turn_id is not None:
                    lease["turn_id"] = str(turn_id or "")
                if message_ids is not None:
                    lease["message_ids"] = sorted(int(v) for v in message_ids if int(v) > 0)
                self._save_chat_lease_unlocked(chat_id, lease)
                return True
        except TimeoutError:
            self._log(f"WARN: chat_lease_touch_timeout chat_id={chat_id}")
            return False
        except Exception as exc:
            self._log(f"WARN: chat_lease_touch_failed chat_id={chat_id}: {exc}")
            return False

    def _chat_lease_release(self, chat_id: int, reason: str) -> None:
        lock_path = self._chat_lease_lock_path(chat_id)
        released = False
        try:
            with self._exclusive_file_lock(lock_path):
                lease = self._load_chat_lease_unlocked(chat_id)
                if isinstance(lease, dict):
                    valid, _ = self._chat_lease_status(lease, time.time())
                    owner_pid = int(lease.get("owner_pid") or 0)
                    if valid and owner_pid != os.getpid():
                        return
                self._delete_chat_lease_unlocked(chat_id)
                released = True
        except TimeoutError:
            self._log(f"WARN: chat_lease_release_timeout chat_id={chat_id} reason={reason}")
            return
        except Exception as exc:
            self._log(f"WARN: chat_lease_release_failed chat_id={chat_id} reason={reason}: {exc}")
            return
        finally:
            self._owned_chat_leases.discard(int(chat_id))
            self._chat_lease_busy_logged_at.pop(int(chat_id), None)
        if released:
            self._log(f"chat_lease_released chat_id={chat_id} reason={reason}")

    def _release_owned_chat_leases(self, reason: str) -> None:
        for chat_id in sorted(self._owned_chat_leases.copy()):
            self._chat_lease_release(chat_id, reason=reason)

    def _has_any_active_chat_lease(self) -> bool:
        if not self.chat_locks_dir.exists():
            return False
        now_epoch = time.time()
        for path in self.chat_locks_dir.glob("chat_*.json"):
            chat_id = 0
            m = re.search(r"chat_(\d+)\.json$", path.name)
            if m:
                try:
                    chat_id = int(m.group(1))
                except Exception:
                    chat_id = 0
            try:
                lease = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(lease, dict):
                continue
            valid, _ = self._chat_lease_status(lease, now_epoch)
            if valid:
                return True
            if chat_id > 0:
                self._chat_lease_release(chat_id, reason="stale_cleanup")
        return False

    def _prune_completed_message_cache(self, now_epoch: float | None = None) -> None:
        if not self.completed_message_ids_recent:
            return
        now = float(now_epoch if now_epoch is not None else time.time())
        ttl = float(self.completed_message_ttl_sec)
        expired_ids = [
            msg_id
            for msg_id, completed_at in self.completed_message_ids_recent.items()
            if (now - float(completed_at)) > ttl
        ]
        for msg_id in expired_ids:
            self.completed_message_ids_recent.pop(msg_id, None)
            self._completed_requeue_log_ts.pop(msg_id, None)

    def _remember_completed_message_ids(self, message_ids: set[int]) -> None:
        if not message_ids:
            return
        now_epoch = time.time()
        self._prune_completed_message_cache(now_epoch)
        for value in message_ids:
            msg_id = int(value)
            if msg_id <= 0:
                continue
            self.completed_message_ids_recent[msg_id] = now_epoch

    def _recently_completed_message_age_sec(
        self,
        message_id: int,
        now_epoch: float | None = None,
    ) -> float:
        msg_id = int(message_id)
        if msg_id <= 0:
            return -1.0
        completed_at = self.completed_message_ids_recent.get(msg_id)
        if completed_at is None:
            return -1.0
        now = float(now_epoch if now_epoch is not None else time.time())
        age_sec = max(0.0, now - float(completed_at))
        if age_sec > float(self.completed_message_ttl_sec):
            self.completed_message_ids_recent.pop(msg_id, None)
            self._completed_requeue_log_ts.pop(msg_id, None)
            return -1.0
        return age_sec

    def _is_message_recently_completed(self, message_id: int, now_epoch: float | None = None) -> bool:
        return self._recently_completed_message_age_sec(message_id, now_epoch=now_epoch) >= 0.0

    def _log_recently_completed_drop(self, chat_id: int, message_id: int, age_sec: float) -> None:
        now_epoch = time.time()
        msg_id = int(message_id)
        last_logged = float(self._completed_requeue_log_ts.get(msg_id) or 0.0)
        if (now_epoch - last_logged) < 15.0:
            return
        self._completed_requeue_log_ts[msg_id] = now_epoch
        self._log(
            "drop_recently_completed_message "
            f"chat_id={chat_id} message_id={msg_id} age_sec={age_sec:.2f}"
        )

    def _run_quick_check(self) -> int:
        cmd = [self.python_bin, str(self.root / "quick_check.py")]
        proc = subprocess.run(
            cmd,
            cwd=str(self.root),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            for line in proc.stdout.strip().splitlines():
                self._log(f"[quick_check] {line}")
        if proc.stderr:
            for line in proc.stderr.strip().splitlines():
                self._log(f"[quick_check][stderr] {line}")
        return int(proc.returncode)

    def _detect_codex_cli_version(self) -> str:
        try:
            proc = subprocess.run(
                ["codex", "--version"],
                cwd=str(self.codex_work_dir),
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
                timeout=8,
            )
        except Exception:
            return CODEX_CLI_VERSION_UNKNOWN

        lines = []
        for raw in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
            line = raw.strip()
            if line:
                lines.append(line)
        if not lines:
            return CODEX_CLI_VERSION_UNKNOWN

        for line in reversed(lines):
            if "codex-cli" in line.lower():
                return line
        return lines[-1]

    def _get_telegram_runtime_skill(self) -> tuple[dict[str, object] | None, object | None]:
        if self._telegram_runtime is not None and self._telegram_skill is not None:
            return self._telegram_runtime, self._telegram_skill
        try:
            if self.platform == "discord":
                runtime = build_messenger_runtime()
                skill = get_discord_skill()
            else:
                runtime = build_telegram_runtime()
                skill = get_telegram_skill()
        except Exception as exc:
            self._log(f"WARN: {self.platform} runtime init failed: {exc}")
            return None, None
        self._telegram_runtime = runtime
        self._telegram_skill = skill
        return runtime, skill

    def _get_task_skill(self) -> object | None:
        if self._task_skill is not None:
            return self._task_skill
        try:
            skill = get_task_skill()
        except Exception as exc:
            self._log(f"WARN: task skill init failed: {exc}")
            return None
        self._task_skill = skill
        return skill

    @staticmethod
    def _compact_prompt_text(value: object, max_len: int = 240) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    @staticmethod
    def _escape_telegram_html(value: object) -> str:
        return html.escape(str(value or "").strip(), quote=True)

    @staticmethod
    def _render_user_work_status(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "waiting":
            return "대기"
        if normalized == "blocked":
            return "중단"
        if normalized == "updated":
            return "진행완료"
        return "진행중"

    @staticmethod
    def _render_user_work_status_badge(value: object) -> str:
        status = DaemonService._render_user_work_status(value)
        if status == "진행완료":
            return f"🟢[{status}]"
        if status == "대기":
            return f"🟠[{status}]"
        if status == "중단":
            return f"🔴[{status}]"
        return f"🔵[{status}]"

    @staticmethod
    def _strip_new_command_prefix(text: str) -> str:
        raw = str(text or "")
        # Remove leading /new command for prompt injection only.
        cleaned = re.sub(r"^\s*/new(?:\s+|$)", "", raw, flags=re.IGNORECASE)
        return cleaned.strip()

    @staticmethod
    def _normalize_ui_text(text: str) -> str:
        return task_helpers.normalize_ui_text(text)

    @staticmethod
    def _extract_msg_id_token(text: str) -> int:
        normalized = str(text or "").strip()
        m = re.fullmatch(r"(?:msg_)?(\d+)", normalized, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return 0
        m2 = re.search(r"\bmsg_(\d+)\b", normalized, flags=re.IGNORECASE)
        if not m2:
            return 0
        try:
            return int(m2.group(1))
        except Exception:
            return 0

    @staticmethod
    def _normalize_task_id_token(value: object) -> str:
        return task_helpers.normalize_task_id_token(value)

    def _normalize_thread_id_token(self, value: object) -> str:
        candidate = self._compact_prompt_text(value, max_len=220)
        if not candidate:
            return ""
        normalized_task_id = self._normalize_task_id_token(f"thread_{candidate}")
        if not normalized_task_id.startswith("thread_"):
            return ""
        return normalized_task_id[len("thread_") :]

    def _task_row_id(self, row: dict[str, Any]) -> str:
        task_id = self._normalize_task_id_token(row.get("task_id"))
        if task_id:
            return task_id
        thread_id = self._compact_prompt_text(row.get("thread_id", ""), max_len=120)
        if thread_id:
            normalized = self._normalize_task_id_token(f"thread_{thread_id}")
            if normalized:
                return normalized
        msg_id = int(row.get("message_id", 0) or 0)
        if msg_id > 0:
            return f"msg_{msg_id}"
        return ""

    def _main_menu_keyboard_rows(self) -> list[list[str]]:
        return [
            [BUTTON_TASK_LIST_RECENT20, BUTTON_TASK_RESUME],
            [BUTTON_TASK_NEW, BUTTON_TASK_GUIDE_VIEW],
            [BUTTON_BOT_RENAME],
        ]

    @staticmethod
    def _split_text_chunks(text: str, max_chars: int = DEFAULT_TASK_GUIDE_TELEGRAM_CHUNK_CHARS) -> list[str]:
        rendered = str(text or "")
        if not rendered:
            return []
        limit = max(500, int(max_chars))
        chunks: list[str] = []
        buffer = ""
        for line in rendered.splitlines(keepends=True):
            if len(line) > limit:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                start = 0
                while start < len(line):
                    chunks.append(line[start : start + limit])
                    start += limit
                continue
            if len(buffer) + len(line) > limit and buffer:
                chunks.append(buffer)
                buffer = line
            else:
                buffer += line
        if buffer:
            chunks.append(buffer)
        if not chunks:
            chunks.append(rendered[:limit])
        return chunks

    def _build_candidate_keyboard_rows(self, button_texts: list[str], per_row: int = 1) -> list[list[str]]:
        rows: list[list[str]] = []
        normalized = [self._normalize_ui_text(v) for v in button_texts if self._normalize_ui_text(v)]
        if per_row <= 0:
            per_row = 1
        for idx in range(0, len(normalized), per_row):
            chunk = normalized[idx : idx + per_row]
            rows.append(chunk)
        rows.extend(self._main_menu_keyboard_rows())
        return rows

    def _build_task_inline_select_keyboard(
        self,
        rows: list[dict[str, Any]],
        max_count: int = 20,
    ) -> list[list[dict[str, str]]]:
        inline_rows: list[list[dict[str, str]]] = []
        for idx, row in enumerate(rows[: max(1, int(max_count))], start=1):
            task_id = self._task_row_id(row)
            if not task_id:
                continue
            inline_rows.append(
                [
                    {
                        "text": "선택",
                        "callback_data": f"{INLINE_TASK_SELECT_CALLBACK_PREFIX}{task_id}",
                    }
                ]
            )
        return inline_rows

    @staticmethod
    def _build_single_task_inline_select_keyboard(task_id: str) -> list[list[dict[str, str]]]:
        return task_helpers.build_single_task_inline_select_keyboard(task_id)

    @staticmethod
    def _extract_callback_task_select_id(text: str) -> str:
        return task_helpers.extract_callback_task_select_id(text)

    def _set_ui_mode(self, state: dict[str, Any], mode: str) -> None:
        state["ui_mode"] = mode
        state["ui_mode_expires_at"] = time.time() + float(self.ui_mode_timeout_sec)

    def _build_resume_choice_payload(self, rows: list[dict[str, Any]], max_count: int = 20) -> tuple[list[str], list[str], dict[str, str]]:
        candidate_ids: list[str] = []
        candidate_buttons: list[str] = []
        candidate_map: dict[str, str] = {}
        seen_buttons: set[str] = set()
        for idx, row in enumerate(rows[: max(1, int(max_count))], start=1):
            task_id = self._task_row_id(row)
            if not task_id:
                continue
            title = self._compact_prompt_text(
                row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
                max_len=36,
            ) or task_id
            button = f"{idx}. {title}"
            base = button
            dedupe = 2
            while button in seen_buttons:
                button = self._compact_prompt_text(f"{base} #{dedupe}", max_len=40) or f"{idx}. {task_id}"
                dedupe += 1
            seen_buttons.add(button)
            normalized_button = self._normalize_ui_text(button)
            candidate_ids.append(task_id)
            candidate_buttons.append(button)
            candidate_map[normalized_button] = task_id
        return candidate_ids, candidate_buttons, candidate_map

    def _clear_ui_mode(self, state: dict[str, Any]) -> None:
        state["ui_mode"] = UI_MODE_IDLE
        state["ui_mode_expires_at"] = 0.0
        state["resume_choice_inline_only"] = False
        state["resume_candidates"] = []
        state["resume_candidate_buttons"] = []
        state["resume_candidate_map"] = {}
        state["bot_rename_base_name"] = ""

    @staticmethod
    def _strip_trailing_bot_alias_suffix(name: object) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        stripped = re.sub(r"\s*\([^()\n]{1,64}\)\s*$", "", text).strip()
        return stripped or text

    def _normalize_bot_alias(self, value: object, max_len: int = 32) -> str:
        text = str(value or "")
        text = text.replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        text = text.replace("(", "").replace(")", "").strip()
        if not text:
            return ""
        return self._compact_prompt_text(text, max_len=max(8, int(max_len)))

    def _telegram_get_me_name(self, request_max_attempts: int = 1) -> str:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None or not hasattr(telegram, "get_me"):
            return ""
        try:
            try:
                profile = telegram.get_me(
                    runtime,
                    request_max_attempts=request_max_attempts,
                )
            except TypeError:
                profile = telegram.get_me(runtime)
        except Exception as exc:
            self._log(f"WARN: get_me failed: {exc}")
            return ""
        if not isinstance(profile, dict):
            return ""
        return self._compact_prompt_text(profile.get("first_name", ""), max_len=80)

    def _telegram_get_my_name(self, request_max_attempts: int = 1) -> str:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None or not hasattr(telegram, "get_my_name"):
            return ""
        try:
            try:
                value = telegram.get_my_name(
                    runtime,
                    language_code="",
                    request_max_attempts=request_max_attempts,
                )
            except TypeError:
                value = telegram.get_my_name(runtime, language_code="")
        except Exception as exc:
            self._log(f"WARN: get_my_name failed: {exc}")
            return ""
        return self._compact_prompt_text(value, max_len=80)

    def _telegram_set_my_name(self, target_name: str, request_max_attempts: int = 1) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None or not hasattr(telegram, "set_my_name"):
            return False
        try:
            try:
                return bool(
                    telegram.set_my_name(
                        runtime,
                        name=str(target_name or ""),
                        language_code="",
                        request_max_attempts=request_max_attempts,
                    )
                )
            except TypeError:
                return bool(
                    telegram.set_my_name(
                        runtime,
                        name=str(target_name or ""),
                        language_code="",
                    )
                )
        except Exception as exc:
            self._log(f"WARN: set_my_name failed: {exc}")
            return False

    def _load_current_bot_row(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        cfg = load_bots_config(self.bots_config_path)
        bots = cfg.get("bots")
        if not isinstance(bots, list):
            return {}, None
        for row in bots:
            if not isinstance(row, dict):
                continue
            row_bot_id = str(row.get("bot_id") or "").strip()
            if self.bot_id and row_bot_id == self.bot_id:
                return cfg, row
        token = str(os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
        if token:
            for row in bots:
                if not isinstance(row, dict):
                    continue
                row_token = str(row.get("token") or "").strip()
                if row_token and row_token == token:
                    return cfg, row
        return cfg, None

    def _resolve_bot_base_name(self, preferred: object = "") -> str:
        preferred_text = self._compact_prompt_text(preferred, max_len=80)
        if preferred_text:
            return self._strip_trailing_bot_alias_suffix(preferred_text)
        cfg, row = self._load_current_bot_row()
        _ = cfg
        if isinstance(row, dict):
            row_name = self._compact_prompt_text(row.get("bot_name", ""), max_len=80)
            if row_name:
                return self._strip_trailing_bot_alias_suffix(row_name)
        my_name = self._telegram_get_my_name(request_max_attempts=1)
        if my_name:
            return self._strip_trailing_bot_alias_suffix(my_name)
        profile_name = self._telegram_get_me_name(request_max_attempts=1)
        if profile_name:
            return self._strip_trailing_bot_alias_suffix(profile_name)
        return ""

    def _save_bot_name_to_config(self, new_name: str) -> bool:
        normalized_new_name = self._compact_prompt_text(new_name, max_len=80)
        if not normalized_new_name:
            return False
        cfg, row = self._load_current_bot_row()
        if not isinstance(row, dict):
            return False
        row["bot_name"] = normalized_new_name
        row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            save_bots_config(self.bots_config_path, cfg)
            return True
        except Exception as exc:
            self._log(
                f"ERROR: failed to save bot_name bot_id={self.bot_id or '-'} "
                f"path={self.bots_config_path}: {exc}"
            )
            return False

    def _rename_bot_display_name(self, alias_text: str, base_name_hint: object = "") -> tuple[bool, str]:
        if not self.is_bot_worker or not self.bot_id:
            return False, "현재 실행 환경에서는 봇 이름 변경을 지원하지 않습니다."
        alias = self._normalize_bot_alias(alias_text, max_len=32)
        if not alias:
            return False, "별칭이 비어 있습니다. 1~32자 별칭을 입력해 주세요."
        previous_name = self._telegram_get_my_name(request_max_attempts=1)
        if not previous_name:
            previous_name = self._telegram_get_me_name(request_max_attempts=1)
        previous_name = self._compact_prompt_text(previous_name, max_len=80)
        base_name = self._resolve_bot_base_name(preferred=base_name_hint or previous_name)
        if not base_name:
            return False, "현재 봇 기본 이름을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."
        target_name = self._compact_prompt_text(f"{base_name}({alias})", max_len=80)
        if not target_name:
            return False, "변경할 이름을 만들지 못했습니다. 다시 입력해 주세요."
        if not self._telegram_set_my_name(target_name, request_max_attempts=1):
            return False, "텔레그램 이름 변경 요청이 실패했습니다. 잠시 후 다시 시도해 주세요."
        verified_name = self._telegram_get_my_name(request_max_attempts=1)
        if not verified_name:
            verified_name = self._telegram_get_me_name(request_max_attempts=1)
        verified_name = self._compact_prompt_text(verified_name, max_len=80)
        if not verified_name:
            return False, "텔레그램 이름 변경 확인에 실패했습니다. 잠시 후 다시 시도해 주세요."
        if verified_name != target_name:
            return (
                False,
                (
                    "텔레그램 이름 확인 결과가 요청값과 달라 적용을 중단했습니다.\n"
                    f"요청: <code>{self._escape_telegram_html(target_name)}</code>\n"
                    f"확인: <code>{self._escape_telegram_html(verified_name)}</code>"
                ),
            )
        if not self._save_bot_name_to_config(verified_name):
            rolled_back = False
            rollback_target = self._compact_prompt_text(previous_name, max_len=80)
            if rollback_target and rollback_target != verified_name:
                rolled_back = self._telegram_set_my_name(rollback_target, request_max_attempts=1)
            if rolled_back:
                return (
                    False,
                    "내부 설정 저장 실패로 변경을 취소했습니다. 잠시 후 다시 시도해 주세요.",
                )
            return (
                False,
                "내부 설정 저장 실패로 이름 변경을 확정하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            )
        return (
            True,
            (
                "<b>봇 이름 변경 완료</b>\n"
                f"적용 이름: <code>{self._escape_telegram_html(verified_name)}</code>"
            ),
        )

    @staticmethod
    def _clear_temp_task_seed(state: dict[str, Any]) -> None:
        task_helpers.clear_temp_task_seed(state)

    def _build_temp_task_seed_batch(
        self,
        chat_id: int,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        seed_text = str(state.get("temp_task_first_text") or "").strip()
        if not seed_text:
            return []
        try:
            seed_message_id = int(state.get("temp_task_first_message_id", 0) or 0)
        except Exception:
            seed_message_id = 0
        if seed_message_id <= 0:
            # Fallback pseudo id when the original id is unavailable.
            seed_message_id = max(1, int(time.time()))
        seed_timestamp = str(state.get("temp_task_first_timestamp") or "").strip()
        if not seed_timestamp:
            seed_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return [
            {
                "chat_id": int(chat_id),
                "message_id": int(seed_message_id),
                "type": "user",
                "text": seed_text,
                "files": [],
                "location": None,
                "timestamp": seed_timestamp,
                "processed": False,
            }
        ]

    @staticmethod
    def _parse_datetime_epoch(value: str) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return 0.0

    def _telegram_send_text(
        self,
        chat_id: int,
        text: str,
        request_max_attempts: int = 1,
        keyboard_rows: list[list[str]] | None = None,
        inline_keyboard_rows: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return False
        effective_parse_mode = self._resolve_telegram_parse_mode(parse_mode)
        normalized_text = self._sanitize_telegram_text_for_parse_mode(text, effective_parse_mode)

        def _send_once(current_parse_mode: str | None) -> bool:
            payload_text = self._sanitize_telegram_text_for_parse_mode(normalized_text, current_parse_mode)
            try:
                if inline_keyboard_rows and hasattr(telegram, "send_text_with_inline_keyboard"):
                    try:
                        return bool(
                            telegram.send_text_with_inline_keyboard(
                                runtime,
                                chat_id=int(chat_id),
                                text=payload_text,
                                inline_keyboard_rows=inline_keyboard_rows,
                                request_max_attempts=request_max_attempts,
                                parse_mode=current_parse_mode,
                            )
                        )
                    except TypeError:
                        return bool(
                            telegram.send_text_with_inline_keyboard(
                                runtime,
                                chat_id=int(chat_id),
                                text=payload_text,
                                inline_keyboard_rows=inline_keyboard_rows,
                                request_max_attempts=request_max_attempts,
                            )
                        )
                if keyboard_rows and hasattr(telegram, "send_text_with_keyboard"):
                    try:
                        return bool(
                            telegram.send_text_with_keyboard(
                                runtime,
                                chat_id=int(chat_id),
                                text=payload_text,
                                keyboard_rows=keyboard_rows,
                                resize_keyboard=True,
                                one_time_keyboard=False,
                                request_max_attempts=request_max_attempts,
                                parse_mode=current_parse_mode,
                            )
                        )
                    except TypeError:
                        return bool(
                            telegram.send_text_with_keyboard(
                                runtime,
                                chat_id=int(chat_id),
                                text=payload_text,
                                keyboard_rows=keyboard_rows,
                                resize_keyboard=True,
                                one_time_keyboard=False,
                                request_max_attempts=request_max_attempts,
                            )
                        )
                try:
                    return bool(
                        telegram.send_text_raw(
                            runtime,
                            chat_id=int(chat_id),
                            text=payload_text,
                            request_max_attempts=request_max_attempts,
                            parse_mode=current_parse_mode,
                        )
                    )
                except TypeError:
                    return bool(
                        telegram.send_text_raw(
                            runtime,
                            chat_id=int(chat_id),
                            text=payload_text,
                            request_max_attempts=request_max_attempts,
                        )
                    )
            except Exception as exc:
                self._log(f"WARN: telegram send failed chat_id={chat_id}: {exc}")
                return False

        sent = _send_once(effective_parse_mode)
        if sent:
            return True
        if effective_parse_mode and self.telegram_parse_fallback_raw_on_fail:
            last_err = None
            try:
                last_err = runtime.get("_telegram_last_error")  # type: ignore[attr-defined]
            except Exception:
                last_err = None
            kind = str(last_err.get("kind") if isinstance(last_err, dict) else "").strip().lower()
            is_network = kind == "network"
            is_parse = False
            if isinstance(last_err, dict) and kind == "http":
                try:
                    status_code = int(last_err.get("status_code") or 0)
                except Exception:
                    status_code = 0
                body = str(last_err.get("body") or "")
                if status_code == 400 and ("can't parse entities" in body or "Unsupported start tag" in body):
                    is_parse = True

            if is_network:
                self._log(
                    "WARN: telegram send retry with same parse_mode due to network error "
                    f"chat_id={chat_id} parse_mode={effective_parse_mode}"
                )
                return _send_once(effective_parse_mode)

            # Only drop parse_mode when it looks like Telegram rejected the HTML entities.
            if is_parse:
                self._log(
                    "WARN: telegram send retry without parse_mode due to parse error "
                    f"chat_id={chat_id} parse_mode={effective_parse_mode}"
                )
                return _send_once(None)
        return False

    def _telegram_edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard_rows: list[list[dict[str, str]]] | None = None,
        request_max_attempts: int = 1,
        parse_mode: str | None = None,
    ) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return False
        if not hasattr(telegram, "edit_message_text"):
            return False
        effective_parse_mode = self._resolve_telegram_parse_mode(parse_mode)
        normalized_text = self._sanitize_telegram_text_for_parse_mode(text, effective_parse_mode)

        def _edit_once(current_parse_mode: str | None) -> bool:
            payload_text = self._sanitize_telegram_text_for_parse_mode(normalized_text, current_parse_mode)
            try:
                try:
                    return bool(
                        telegram.edit_message_text(
                            runtime,
                            chat_id=int(chat_id),
                            message_id=int(message_id),
                            text=payload_text,
                            inline_keyboard_rows=inline_keyboard_rows,
                            request_max_attempts=request_max_attempts,
                            parse_mode=current_parse_mode,
                        )
                    )
                except TypeError:
                    return bool(
                        telegram.edit_message_text(
                            runtime,
                            chat_id=int(chat_id),
                            message_id=int(message_id),
                            text=payload_text,
                            inline_keyboard_rows=inline_keyboard_rows,
                            request_max_attempts=request_max_attempts,
                        )
                    )
            except Exception as exc:
                self._log(
                    f"WARN: telegram edit failed chat_id={chat_id} message_id={message_id}: {exc}"
                )
                return False

        edited = _edit_once(effective_parse_mode)
        if edited:
            return True
        if effective_parse_mode and self.telegram_parse_fallback_raw_on_fail:
            last_err = None
            try:
                last_err = runtime.get("_telegram_last_error")  # type: ignore[attr-defined]
            except Exception:
                last_err = None
            kind = str(last_err.get("kind") if isinstance(last_err, dict) else "").strip().lower()
            is_network = kind == "network"
            is_parse = False
            if isinstance(last_err, dict) and kind == "http":
                try:
                    status_code = int(last_err.get("status_code") or 0)
                except Exception:
                    status_code = 0
                body = str(last_err.get("body") or "")
                if status_code == 400 and ("can't parse entities" in body or "Unsupported start tag" in body):
                    is_parse = True

            if is_network:
                self._log(
                    "WARN: telegram edit retry with same parse_mode due to network error "
                    f"chat_id={chat_id} message_id={message_id} parse_mode={effective_parse_mode}"
                )
                return _edit_once(effective_parse_mode)

            if is_parse:
                self._log(
                    "WARN: telegram edit retry without parse_mode due to parse error "
                    f"chat_id={chat_id} message_id={message_id} parse_mode={effective_parse_mode}"
                )
                return _edit_once(None)
        return False

    def _finalize_control_message(self, chat_id: int, message_id: int, reply_text: str) -> None:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return
        try:
            telegram.save_bot_response(
                store_path=str(self.store_file),
                chat_id=int(chat_id),
                text=str(reply_text or ""),
                reply_to_message_ids=[int(message_id)],
            )
        except Exception as exc:
            self._log(f"WARN: control response save failed chat_id={chat_id} msg_id={message_id}: {exc}")

        try:
            changed = int(telegram.mark_messages_processed(str(self.store_file), [int(message_id)]))
        except Exception as exc:
            self._log(f"WARN: control mark processed failed chat_id={chat_id} msg_id={message_id}: {exc}")
            changed = 0
        if changed > 0:
            self._remember_completed_message_ids({int(message_id)})

    def _run_task_commands_json(self, args: list[str], timeout_sec: float = 25.0) -> dict[str, Any] | None:
        cmd = [self.python_bin, str(self.root / "scripts" / "task_commands.py"), *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.root),
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_sec,
            )
        except Exception as exc:
            self._log(f"WARN: task command failed args={args}: {exc}")
            return None

        stdout = str(proc.stdout or "").strip()
        stderr = str(proc.stderr or "").strip()
        if stderr:
            self._log(f"[task_commands][stderr] {stderr}")
        if not stdout:
            return None
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                return payload
            return None
        except Exception as exc:
            self._log(f"WARN: task command json parse failed args={args}: {exc}")
            return None

    def _load_task_row(self, chat_id: int, task_id: str, include_instrunction: bool = False) -> dict[str, Any] | None:
        normalized_task_id = self._normalize_task_id_token(task_id)
        if not normalized_task_id:
            return None
        task_root = self._task_root_for_chat(chat_id)
        args = [
            "activate",
            normalized_task_id,
            "--tasks-dir",
            str(task_root),
            "--json",
        ]
        if include_instrunction:
            args.append("--include-instrunction")
        payload = self._run_task_commands_json(args)
        if not payload or not bool(payload.get("ok")):
            return None
        row = payload.get("task")
        if not isinstance(row, dict):
            return None
        return row

    def _task_row_recency_epoch(self, row: dict[str, Any]) -> float:
        latest_change = str(row.get("latest_change") or "").strip()
        if latest_change:
            ts_prefix = latest_change.split("|", 1)[0].strip()
            parsed = self._parse_datetime_epoch(ts_prefix)
            if parsed > 0:
                return parsed
        ts = self._parse_datetime_epoch(str(row.get("timestamp") or ""))
        if ts > 0:
            return ts
        task_dir = str(row.get("task_dir") or "").strip()
        if not task_dir:
            return 0.0
        try:
            return Path(task_dir).stat().st_mtime
        except OSError:
            return 0.0

    def _resolve_task_agents_thread_id(self, state: dict[str, Any]) -> str:
        current_thread_id = self._normalize_thread_id_token(state.get("thread_id"))
        if current_thread_id:
            return current_thread_id
        return self._normalize_thread_id_token(state.get("resume_target_thread_id"))

    def _task_agents_path(self, chat_id: int, thread_id: str) -> Path:
        normalized_thread_id = self._normalize_thread_id_token(thread_id)
        if not normalized_thread_id:
            raise ValueError("thread_id is required for task AGENTS path")
        task_root = self._task_root_for_chat(chat_id)
        return (task_root / f"thread_{normalized_thread_id}" / TASK_AGENTS_FILENAME).resolve()

    def _task_agents_relative_path(self, chat_id: int, thread_id: str) -> str:
        normalized_thread_id = self._normalize_thread_id_token(thread_id)
        if not normalized_thread_id:
            return ""
        if self.tasks_partition_by_chat:
            return f"tasks/chat_{chat_id}/thread_{normalized_thread_id}/{TASK_AGENTS_FILENAME}"
        return f"tasks/thread_{normalized_thread_id}/{TASK_AGENTS_FILENAME}"

    def _load_task_agents_text(self, chat_id: int, thread_id: str) -> tuple[str, bool]:
        path = self._task_agents_path(chat_id=chat_id, thread_id=thread_id)
        if not path.exists():
            return "", False
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._log(f"WARN: failed to read task AGENTS.md path={path}: {exc}")
            return "", True
        return text, True

    def _build_task_agents_edit_request_text(self, target_path: Path, user_text: str) -> str:
        return (
            "다음 작업을 수행하세요.\n"
            f"- 대상 파일: {target_path}\n"
            "- 파일 이름은 반드시 AGENTS.md로 유지하세요.\n"
            "- 사용자의 변경 요청을 반영해 파일을 생성 또는 수정하세요.\n"
            "- 수정 후 어떤 항목을 바꿨는지 짧게 보고하세요.\n"
            "- 관련 없는 파일은 수정하지 마세요.\n\n"
            "[사용자 변경 요청]\n"
            f"{str(user_text or '').strip()}"
        )

    @staticmethod
    def _is_task_guide_edit_request_text(text: str) -> bool:
        return task_helpers.is_task_guide_edit_request_text(text)

    def _default_task_agents_template(self, thread_id: str) -> str:
        normalized_thread_id = self._normalize_thread_id_token(thread_id)
        return (
            "# AGENTS.md\n\n"
            f"- Task Folder: thread_{normalized_thread_id}\n"
            "- Last Updated: (auto)\n\n"
            "## Task Guidance\n"
            "- (여기에 사용자 전용 TASK 지침을 작성하세요)\n"
        )

    def _ensure_task_agents_file(self, chat_id: int, thread_id: str) -> bool:
        path = self._task_agents_path(chat_id=chat_id, thread_id=thread_id)
        if path.exists():
            return True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                self._default_task_agents_template(thread_id=thread_id),
                encoding="utf-8",
            )
            self._log(f"task_guide_created chat_id={chat_id} path={path}")
            return True
        except OSError as exc:
            self._log(f"WARN: failed to create task AGENTS.md path={path}: {exc}")
            return False

    def _forward_task_guide_edit_request(
        self,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        user_text: str,
    ) -> bool:
        target_thread_id = self._resolve_task_agents_thread_id(state)
        if not target_thread_id:
            self._clear_ui_mode(state)
            reply_text = (
                "현재 선택된 TASK가 없어 지침 변경 요청을 처리할 수 없습니다.\n"
                "먼저 `TASK 목록 보기(최근20)` 또는 `기존 TASK 이어하기`로 TASK를 선택해 주세요."
            )
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=self._main_menu_keyboard_rows(),
                request_max_attempts=1,
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        target_path = self._task_agents_path(chat_id=chat_id, thread_id=target_thread_id)
        if not target_path.exists():
            if not self._ensure_task_agents_file(chat_id=chat_id, thread_id=target_thread_id):
                self._clear_ui_mode(state)
                reply_text = (
                    "TASK 지침 파일을 생성하는 중 문제가 발생했어요.\n"
                    "잠시 후 다시 시도해 주세요."
                )
                sent = self._telegram_send_text(
                    chat_id=chat_id,
                    text=reply_text,
                    keyboard_rows=self._main_menu_keyboard_rows(),
                    request_max_attempts=1,
                )
                if sent:
                    self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
                return True

        rewritten_request = self._build_task_agents_edit_request_text(
            target_path=target_path,
            user_text=user_text,
        )
        item["text"] = rewritten_request
        self._clear_ui_mode(state)
        self._log(
            f"task_guide_edit_forwarded chat_id={chat_id} msg_id={msg_id} "
            f"target={target_path}"
        )
        return False

    def _load_task_agents_developer_instructions(self, chat_id: int, state: dict[str, Any]) -> str:
        thread_id = self._resolve_task_agents_thread_id(state)
        if not thread_id:
            return ""
        content, exists = self._load_task_agents_text(chat_id=chat_id, thread_id=thread_id)
        if not exists:
            path = self._task_agents_path(chat_id=chat_id, thread_id=thread_id)
            self._log(f"task_guide_missing_for_developer_instructions chat_id={chat_id} path={path}")
            return ""
        normalized = str(content or "").strip()
        if not normalized:
            return ""
        if len(normalized) > DEFAULT_TASK_AGENTS_INSTRUCTIONS_MAX_CHARS:
            self._log(
                "WARN: task AGENTS.md too long for developerInstructions; "
                f"truncating to {DEFAULT_TASK_AGENTS_INSTRUCTIONS_MAX_CHARS} chars"
            )
            normalized = normalized[:DEFAULT_TASK_AGENTS_INSTRUCTIONS_MAX_CHARS]
        return normalized

    def _list_recent_tasks(self, chat_id: int, limit: int = 20, source_limit: int = 200) -> list[dict[str, Any]]:
        task_root = self._task_root_for_chat(chat_id)
        payload = self._run_task_commands_json(
            [
                "list",
                "--tasks-dir",
                str(task_root),
                "--limit",
                str(max(limit, source_limit)),
                "--json",
            ]
        )
        if not payload:
            return []
        rows = payload.get("tasks", [])
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = [row for row in rows if isinstance(row, dict)]
        normalized.sort(
            key=lambda row: (
                self._task_row_recency_epoch(row),
                self._task_row_id(row),
            ),
            reverse=True,
        )
        return normalized[: max(1, int(limit))]

    def _recover_latest_thread_id_for_chat(self, chat_id: int) -> str:
        rows = self._list_recent_tasks(chat_id=chat_id, limit=20, source_limit=120)
        for row in rows:
            thread_id = self._compact_prompt_text(row.get("thread_id", ""), max_len=220)
            if not thread_id:
                task_id = self._task_row_id(row)
                if task_id.startswith("thread_"):
                    thread_id = task_id[len("thread_") :]
            if thread_id:
                return thread_id
        return ""

    def _render_task_list_text(self, rows: list[dict[str, Any]], limit: int = 20) -> str:
        lines = [
            f"<b>TASK 목록 (최근 {int(limit)}개)</b>",
            "<i>최근순으로 정렬됩니다.</i>",
            "",
        ]
        for idx, row in enumerate(rows, start=1):
            title = self._compact_prompt_text(
                row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
                max_len=44,
            ) or "(제목 없음)"
            subtitle = self._compact_prompt_text(
                row.get("display_subtitle", "") or row.get("result_summary_short", "") or row.get("instruction_short", ""),
                max_len=64,
            )
            work_status_badge = self._render_user_work_status_badge(row.get("work_status", "") or row.get("status", ""))
            recent_ts = self._compact_prompt_text(self._task_row_recent_timestamp(row), max_len=19) or "-"
            title_html = self._escape_telegram_html(title)
            subtitle_html = self._escape_telegram_html(subtitle) if subtitle else "-"
            work_html = self._escape_telegram_html(work_status_badge)
            recent_html = self._escape_telegram_html(recent_ts)
            lines.append(f"<b>{idx}. {title_html}</b>")
            lines.append(f"<b>요약</b>: {subtitle_html}")
            lines.append(f"<b>상태</b>: {work_html}")
            lines.append(f"<b>최근</b>: <code>{recent_html}</code>")
            lines.append("")
        return "\n".join(lines).strip()

    def _render_task_item_card_text(self, idx: int, row: dict[str, Any]) -> str:
        title = self._compact_prompt_text(
            row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
            max_len=44,
        ) or "(제목 없음)"
        subtitle = self._compact_prompt_text(
            row.get("display_subtitle", "") or row.get("result_summary_short", "") or row.get("instruction_short", ""),
            max_len=64,
        )
        work_status_badge = self._render_user_work_status_badge(row.get("work_status", "") or row.get("status", ""))
        recent_ts = self._compact_prompt_text(self._task_row_recent_timestamp(row), max_len=19) or "-"
        title_html = self._escape_telegram_html(title)
        subtitle_html = self._escape_telegram_html(subtitle) if subtitle else "-"
        work_html = self._escape_telegram_html(work_status_badge)
        recent_html = self._escape_telegram_html(recent_ts)
        lines = [
            f"<b>{int(idx)}. {title_html}</b>",
            f"<b>요약</b>: {subtitle_html}",
            f"<b>상태</b>: {work_html}",
            f"<b>최근</b>: <code>{recent_html}</code>",
        ]
        try:
            relevance_score = int(row.get("relevance_score", -1))
        except Exception:
            relevance_score = -1
        if relevance_score >= 0:
            lines.append(f"<b>연관도</b>: <code>{relevance_score}점</code>")
        return "\n".join(lines).strip()

    def _render_task_candidates_text(self, query: str, rows: list[dict[str, Any]]) -> str:
        query_html = self._escape_telegram_html(query)
        lines = [
            "<b>연관 TASK 후보</b>",
            f"검색어: <code>{query_html}</code>",
            "<i>번호를 입력하거나 아래 버튼에서 선택해 주세요.</i>",
            "",
        ]
        for idx, row in enumerate(rows, start=1):
            title = self._compact_prompt_text(
                row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
                max_len=44,
            ) or "(제목 없음)"
            subtitle = self._compact_prompt_text(
                row.get("display_subtitle", "") or row.get("result_summary_short", ""),
                max_len=52,
            )
            work_status_badge = self._render_user_work_status_badge(row.get("work_status", "") or row.get("status", ""))
            recent_ts = self._compact_prompt_text(self._task_row_recent_timestamp(row), max_len=19) or "-"
            try:
                relevance_score = int(row.get("relevance_score", -1))
            except Exception:
                relevance_score = -1
            title_html = self._escape_telegram_html(title)
            subtitle_html = self._escape_telegram_html(subtitle) if subtitle else "-"
            work_html = self._escape_telegram_html(work_status_badge)
            recent_html = self._escape_telegram_html(recent_ts)
            lines.append(f"<b>{idx}. {title_html}</b>")
            lines.append(f"<b>요약</b>: {subtitle_html}")
            lines.append(f"<b>상태</b>: {work_html}")
            lines.append(f"<b>최근</b>: <code>{recent_html}</code>")
            if relevance_score >= 0:
                lines.append(f"<b>연관도</b>: <code>{int(relevance_score)}점</code>")
            lines.append("")
        return "\n".join(lines).strip()

    def _task_row_recent_timestamp(self, row: dict[str, Any]) -> str:
        latest_change = str(row.get("latest_change") or "").strip()
        if latest_change:
            prefix = latest_change.split("|", 1)[0].strip()
            if self._parse_datetime_epoch(prefix) > 0:
                return prefix
        title_updated = str(row.get("title_updated_at") or "").strip()
        if title_updated and self._parse_datetime_epoch(title_updated) > 0:
            return title_updated
        ts = str(row.get("timestamp") or "").strip()
        if ts and self._parse_datetime_epoch(ts) > 0:
            return ts
        return ""

    @staticmethod
    def _parse_json_object_from_text(raw_text: str) -> dict[str, Any] | None:
        text = str(raw_text or "").strip()
        if not text:
            return None
        candidates: list[str] = [text]
        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fence_match:
            candidates.append(str(fence_match.group(1)).strip())
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            candidates.append(text[first_brace : last_brace + 1].strip())
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _build_task_search_llm_prompt(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        limit: int,
        min_score: int,
    ) -> str:
        compact_candidates: list[dict[str, Any]] = []
        for row in candidates:
            task_id = self._task_row_id(row)
            if not task_id:
                continue
            title = self._compact_prompt_text(
                row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
                max_len=70,
            ) or "(제목 없음)"
            summary = self._compact_prompt_text(
                row.get("display_subtitle", "") or row.get("result_summary_short", "") or row.get("instruction_short", ""),
                max_len=140,
            )
            compact_candidates.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "summary": summary,
                    "recent_at": self._task_row_recent_timestamp(row),
                    "status": self._render_user_work_status(row.get("work_status", "") or row.get("status", "")),
                }
            )
        request_payload = {
            "version": 1,
            "query": self._normalize_ui_text(query),
            "top_k": int(limit),
            "min_score": int(min_score),
            "candidates": compact_candidates,
        }
        instructions = [
            "당신은 TASK 검색 랭커다.",
            "목표: query와 가장 관련성 높은 TASK를 candidates 내부에서만 고른다.",
            "중요 규칙:",
            "1) candidates에 없는 task_id는 절대 출력하지 않는다.",
            "2) score는 0~100 정수로 부여한다.",
            "3) score 높은 순으로 정렬한다.",
            "4) 출력은 JSON 객체 하나만 출력한다. 코드블록/설명문/추가 텍스트 금지.",
            '5) 출력 스키마: {"version":1,"query":"...","results":[{"task_id":"...","score":87,"reason":"짧은 근거"}]}',
            "6) results는 top_k 이내로 제한한다.",
            "",
            "입력 JSON:",
            json.dumps(request_payload, ensure_ascii=False),
        ]
        return "\n".join(instructions).strip()

    def _app_run_aux_turn_for_json(self, prompt_text: str, timeout_sec: float) -> dict[str, Any] | None:
        if not str(prompt_text or "").strip():
            return None
        if not self._ensure_app_server():
            return None
        started_thread = self._app_request(
            "thread/start",
            {
                "cwd": str(self.codex_work_dir),
                "approvalPolicy": self.app_server_approval_policy,
                "sandbox": self.app_server_sandbox,
                "model": self.codex_model,
            },
            timeout_sec=self.task_search_llm_request_timeout_sec,
        )
        if started_thread is None:
            return None
        thread = started_thread.get("thread")
        if not isinstance(thread, dict):
            return None
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            return None

        started_turn = self._app_request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": str(prompt_text)}],
                "model": self.codex_model,
                "effort": self.codex_reasoning_effort,
                "approvalPolicy": self.app_server_approval_policy,
            },
            timeout_sec=self.task_search_llm_request_timeout_sec,
        )
        if started_turn is None:
            return None

        turn = started_turn.get("turn")
        turn_id = ""
        if isinstance(turn, dict):
            turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            turn_id = str(started_turn.get("turnId") or "").strip()
        if not turn_id:
            return None

        self.app_aux_turn_results[turn_id] = {
            "status": "started",
            "text": "",
            "thread_id": thread_id,
            "updated_at": time.time(),
        }
        deadline = time.time() + max(1.0, float(timeout_sec))
        while time.time() < deadline:
            self._app_drain_events(max_items=200)
            current = self.app_aux_turn_results.get(turn_id)
            if isinstance(current, dict):
                status = str(current.get("status") or "").strip().lower()
                text = str(current.get("text") or "").strip()
                if status in {"failed", "cancelled", "error", "errored"}:
                    self.app_aux_turn_results.pop(turn_id, None)
                    return None
                if status == "completed" and text:
                    self.app_aux_turn_results.pop(turn_id, None)
                    return self._parse_json_object_from_text(text)
            if not self._app_is_running():
                break
            time.sleep(0.05)
        self.app_aux_turn_results.pop(turn_id, None)
        return None

    def _search_task_candidates_via_llm(self, chat_id: int, query: str, limit: int = 5) -> list[dict[str, Any]]:
        normalized_query = self._normalize_ui_text(query)
        if not normalized_query:
            return []
        candidate_pool = self._list_recent_tasks(
            chat_id=chat_id,
            limit=max(limit, self.task_search_llm_candidate_pool_limit),
            source_limit=max(self.task_search_llm_candidate_pool_limit * 2, 120),
        )
        if not candidate_pool:
            return []

        deduped_candidates: list[dict[str, Any]] = []
        row_by_task_id: dict[str, dict[str, Any]] = {}
        for row in candidate_pool:
            task_id = self._task_row_id(row)
            if not task_id or task_id in row_by_task_id:
                continue
            row_by_task_id[task_id] = row
            deduped_candidates.append(row)
        if not deduped_candidates:
            return []

        prompt = self._build_task_search_llm_prompt(
            query=normalized_query,
            candidates=deduped_candidates,
            limit=max(1, int(limit)),
            min_score=int(self.task_search_llm_min_score),
        )
        parsed = self._app_run_aux_turn_for_json(
            prompt_text=prompt,
            timeout_sec=self.task_search_llm_turn_timeout_sec,
        )
        if not isinstance(parsed, dict):
            return []

        raw_results = parsed.get("results")
        if not isinstance(raw_results, list):
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            task_id = self._normalize_task_id_token(item.get("task_id"))
            if not task_id or task_id in seen or task_id not in row_by_task_id:
                continue
            score_raw = item.get("score")
            try:
                score = int(score_raw)
            except Exception:
                continue
            if score < 0:
                score = 0
            if score > 100:
                score = 100
            if score < int(self.task_search_llm_min_score):
                continue
            row = dict(row_by_task_id[task_id])
            row["relevance_score"] = score
            reason = self._compact_prompt_text(item.get("reason", ""), max_len=120)
            if reason:
                row["relevance_reason"] = reason
            out.append(row)
            seen.add(task_id)
            if len(out) >= max(1, int(limit)):
                break
        out.sort(
            key=lambda row: (
                int(row.get("relevance_score", 0) or 0),
                self._task_row_recency_epoch(row),
            ),
            reverse=True,
        )
        return out[: max(1, int(limit))]

    def _search_task_candidates_for_resume(self, chat_id: int, query: str, limit: int = 5) -> list[dict[str, Any]]:
        effective_limit = max(1, min(int(limit), int(self.task_search_llm_limit)))
        if self.task_search_llm_enabled:
            candidates = self._search_task_candidates_via_llm(
                chat_id=chat_id,
                query=query,
                limit=effective_limit,
            )
            if candidates:
                self._log(
                    f"resume_search llm_ok chat_id={chat_id} query={self._compact_prompt_text(query, max_len=80)!r} "
                    f"count={len(candidates)} threshold={self.task_search_llm_min_score}"
                )
                return candidates
            self._log(
                f"WARN: resume_search llm_empty_or_failed chat_id={chat_id} "
                f"query={self._compact_prompt_text(query, max_len=80)!r}; fallback=code"
            )
        return self._search_task_candidates(chat_id=chat_id, query=query, limit=effective_limit)

    def _search_task_candidates(self, chat_id: int, query: str, limit: int = 5) -> list[dict[str, Any]]:
        task_root = self._task_root_for_chat(chat_id)
        normalized_query = self._normalize_ui_text(query)
        if not normalized_query:
            return []

        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        task_skill = self._get_task_skill()
        if task_skill is not None and hasattr(task_skill, "find_relevant_tasks"):
            try:
                related = task_skill.find_relevant_tasks(
                    query=normalized_query,
                    tasks_dir=str(task_root),
                    limit=max(limit * 3, 10),
                    logs_dir=str(self.logs_dir),
                )
            except Exception as exc:
                self._log(f"WARN: relevant task search failed chat_id={chat_id}: {exc}")
                related = []
            if isinstance(related, list):
                for item in related:
                    if not isinstance(item, dict):
                        continue
                    candidate_task_id = self._normalize_task_id_token(item.get("task_id"))
                    if not candidate_task_id:
                        thread_id = self._compact_prompt_text(item.get("thread_id", ""), max_len=200)
                        if thread_id:
                            candidate_task_id = self._normalize_task_id_token(f"thread_{thread_id}")
                    if not candidate_task_id:
                        msg_id = int(item.get("message_id", 0) or 0)
                        if msg_id > 0:
                            candidate_task_id = f"msg_{msg_id}"
                    if not candidate_task_id or candidate_task_id in seen_ids:
                        continue
                    row = self._load_task_row(chat_id=chat_id, task_id=candidate_task_id, include_instrunction=False)
                    if not row:
                        continue
                    resolved_id = self._task_row_id(row) or candidate_task_id
                    seen_ids.add(resolved_id)
                    results.append(row)
                    if len(results) >= limit:
                        return results[:limit]

        payload = self._run_task_commands_json(
            [
                "list",
                "--tasks-dir",
                str(task_root),
                "--keyword",
                normalized_query,
                "--limit",
                str(max(limit * 4, 20)),
                "--json",
            ]
        )
        if not payload:
            return results[:limit]

        rows = payload.get("tasks", [])
        if not isinstance(rows, list):
            return results[:limit]
        for row in rows:
            if not isinstance(row, dict):
                continue
            task_id = self._task_row_id(row)
            if not task_id or task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            results.append(row)
            if len(results) >= limit:
                break
        return results[:limit]

    def _resolve_task_choice(self, text: str, candidates: list[str], candidate_map: dict[str, str] | None = None) -> str:
        normalized = self._normalize_ui_text(text)
        if not normalized:
            return ""
        candidate_ids = [self._normalize_task_id_token(v) for v in candidates]
        candidate_ids = [v for v in candidate_ids if v]
        map_raw = candidate_map if isinstance(candidate_map, dict) else {}
        normalized_map = {
            self._normalize_ui_text(str(k)): self._normalize_task_id_token(v)
            for k, v in map_raw.items()
        }

        mapped = normalized_map.get(normalized)
        if mapped and mapped in candidate_ids:
            return mapped
        idx_match = re.match(r"^(\d+)\s*[\).:\-]?", normalized)
        if idx_match:
            idx = int(idx_match.group(1))
            if 1 <= idx <= len(candidate_ids):
                return str(candidate_ids[idx - 1])
        direct_task_id = self._normalize_task_id_token(normalized)
        if direct_task_id and direct_task_id in candidate_ids:
            return direct_task_id
        direct_msg_id = self._extract_msg_id_token(normalized)
        if direct_msg_id > 0:
            direct_msg_task = f"msg_{direct_msg_id}"
            if direct_msg_task in candidate_ids:
                return direct_msg_task
        if normalized.isdigit():
            idx = int(normalized)
            if 1 <= idx <= len(candidate_ids):
                return str(candidate_ids[idx - 1])
        return ""

    def _set_selected_task_state(self, chat_id: int, state: dict[str, Any], row: dict[str, Any]) -> None:
        task_id = self._task_row_id(row)
        if not task_id:
            msg_id = int(row.get("message_id", 0) or 0)
            if msg_id > 0:
                task_id = f"msg_{msg_id}"
        status = self._compact_prompt_text(row.get("work_status", "") or row.get("status", ""), max_len=28) or "unknown"
        ops_status = self._compact_prompt_text(row.get("ops_status", ""), max_len=28) or "unknown"
        title = self._compact_prompt_text(
            row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
            max_len=120,
        ) or "(제목 없음)"
        subtitle = self._compact_prompt_text(
            row.get("display_subtitle", "") or row.get("result_summary_short", "") or row.get("instruction_short", ""),
            max_len=220,
        )
        instruction = self._compact_prompt_text(row.get("instruction", ""), max_len=220) or "(지시 없음)"
        latest_change = self._compact_prompt_text(row.get("latest_change", ""), max_len=240)
        recent_ts = self._compact_prompt_text(self._task_row_recent_timestamp(row), max_len=19)
        related_ids = row.get("related_task_ids", [])
        if not isinstance(related_ids, list):
            related_ids = []
        related_text = ", ".join(self._compact_prompt_text(v, max_len=100) for v in related_ids[:8] if str(v).strip())
        task_thread_id = self._compact_prompt_text(row.get("thread_id", ""), max_len=200)
        if not task_thread_id and task_id and task_id.startswith("thread_"):
            task_thread_id = task_id[len("thread_") :]
        if not task_thread_id and task_id:
            task_thread_id = self._lookup_mapped_thread_id(chat_id=chat_id, task_id=task_id)
        if task_thread_id and task_id:
            self._bind_task_thread_mapping(chat_id=chat_id, task_id=task_id, thread_id=task_thread_id)

        lines = [
            "[이어갈 TASK 컨텍스트]",
            f"- task: {task_id or '(unknown)'}",
            f"- title: {title}",
            f"- status: {status} (ops={ops_status})",
        ]
        if subtitle:
            lines.append(f"- summary: {subtitle}")
        if recent_ts:
            lines.append(f"- recent_at: {recent_ts}")
        if task_thread_id:
            lines.append(f"- thread_id: {task_thread_id}")
        lines += [
            f"- instruction: {instruction}",
        ]
        if latest_change:
            lines.append(f"- latest_change: {latest_change}")
        if related_text:
            lines.append(f"- related: {related_text}")
        lines.append("- 지시사항을 위 TASK 맥락으로 이어서 처리할 것.")
        packet = "\n".join(lines)
        if len(packet) > 1500:
            packet = packet[:1497] + "..."

        state["selected_task_id"] = task_id
        state["selected_task_packet"] = packet
        state["resume_target_thread_id"] = task_thread_id
        state["resume_thread_switch_pending"] = bool(task_id)

    def _clear_selected_task_state(self, state: dict[str, Any]) -> None:
        state["selected_task_id"] = ""
        state["selected_task_packet"] = ""
        state["resume_target_thread_id"] = ""
        state["resume_thread_switch_pending"] = False
        state["resume_recent_chat_summary_once"] = ""
        state["resume_context_inject_once"] = False

    def _build_new_task_carryover_summary(self, chat_id: int, state: dict[str, Any]) -> str:
        rows = self._list_recent_tasks(chat_id=chat_id, limit=20, source_limit=200)
        if not rows:
            return ""

        selected_task_id = self._normalize_task_id_token(state.get("selected_task_id"))
        anchor_task_id = ""
        if selected_task_id:
            for row in rows:
                row_task_id = self._task_row_id(row)
                if row_task_id == selected_task_id:
                    anchor_task_id = selected_task_id
                    break
        if not anchor_task_id and rows:
            anchor_task_id = self._task_row_id(rows[0])
        if not anchor_task_id:
            return ""

        anchor_row = self._load_task_row(chat_id=chat_id, task_id=anchor_task_id, include_instrunction=True) or {}
        instruction_text = str(anchor_row.get("instruction_text") or "").strip()
        instruction_short = self._compact_prompt_text(anchor_row.get("instruction", ""), max_len=220)
        anchor_title = self._compact_prompt_text(anchor_row.get("display_title", ""), max_len=120)
        anchor_subtitle = self._compact_prompt_text(anchor_row.get("display_subtitle", ""), max_len=160)
        latest_change = self._compact_prompt_text(anchor_row.get("latest_change", ""), max_len=220)

        target_lines = max(10, int(self.new_task_summary_lines))
        lines: list[str] = [
            "[이전 대화 핵심 요약(자동)]",
            f"- 기준 TASK: {anchor_task_id}",
        ]
        if anchor_title:
            lines.append(f"- 기준 제목: {anchor_title}")
        if anchor_subtitle:
            lines.append(f"- 기준 요약: {anchor_subtitle}")
        if instruction_short:
            lines.append(f"- 기준 지시: {instruction_short}")
        if latest_change:
            lines.append(f"- 최근 변경: {latest_change}")
        lines.append("")
        lines.append("[핵심 맥락]")

        if instruction_text:
            for raw in instruction_text.splitlines():
                compact = self._compact_prompt_text(raw, max_len=180)
                if not compact:
                    continue
                lines.append(compact)
                if len(lines) >= int(target_lines * 0.72):
                    break

        lines.append("")
        lines.append("[최근 TASK 흐름]")
        for row in rows:
            row_task_id = self._task_row_id(row) or "(unknown)"
            work_status = self._compact_prompt_text(row.get("work_status", "") or row.get("status", ""), max_len=24) or "unknown"
            ops_status = self._compact_prompt_text(row.get("ops_status", ""), max_len=24) or "unknown"
            title = self._compact_prompt_text(
                row.get("display_title", "") or row.get("instruction", "") or row.get("instruction_short", ""),
                max_len=70,
            ) or "(제목 없음)"
            subtitle = self._compact_prompt_text(
                row.get("display_subtitle", "") or row.get("result_summary_short", ""),
                max_len=56,
            )
            recent_ts = self._compact_prompt_text(self._task_row_recent_timestamp(row), max_len=19) or "-"
            if subtitle:
                lines.append(f"- {row_task_id} | {title} | {subtitle} | {work_status}/{ops_status} | {recent_ts}")
            else:
                lines.append(f"- {row_task_id} | {title} | {work_status}/{ops_status} | {recent_ts}")
            if len(lines) >= target_lines:
                break

        rendered = "\n".join(lines[:target_lines]).strip()
        max_chars = max(1200, int(self.new_task_summary_max_chars))
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 3] + "..."
        return rendered

    def _build_recent_chat_summary(
        self,
        chat_id: int,
        *,
        hours: int = DEFAULT_RESUME_CHAT_SUMMARY_HOURS,
        target_lines: int = DEFAULT_RESUME_CHAT_SUMMARY_LINES,
        max_chars: int = DEFAULT_RESUME_CHAT_SUMMARY_MAX_CHARS,
        exclude_message_id: int | None = None,
    ) -> str:
        now_epoch = time.time()
        window_hours = max(1, int(hours))
        cutoff_epoch = now_epoch - float(window_hours * 3600)
        target = max(10, int(target_lines))
        char_limit = max(1200, int(max_chars))

        lines: list[str] = [
            "[현재 챗 최근 대화 요약(자동)]",
            f"- 범위: 최근 {window_hours}시간",
            f"- 목표 줄수: 약 {target}줄 (메시지가 적으면 더 짧을 수 있음)",
            "",
            "[대화 흐름]",
        ]

        try:
            payload = json.loads(self.store_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log(f"WARN: recent chat summary load failed chat_id={chat_id}: {exc}")
            lines.append("- 메시지 저장소를 읽지 못해 요약을 생성하지 못했습니다.")
            return "\n".join(lines).strip()

        raw_messages = payload.get("messages", []) if isinstance(payload, dict) else []
        if not isinstance(raw_messages, list):
            raw_messages = []

        filtered: list[tuple[float, int, dict[str, Any]]] = []
        for idx, raw in enumerate(raw_messages):
            if not isinstance(raw, dict):
                continue
            try:
                row_chat_id = int(raw.get("chat_id"))
            except Exception:
                continue
            if row_chat_id != int(chat_id):
                continue

            msg_type = str(raw.get("type") or "").strip().lower() or "user"
            if exclude_message_id is not None and msg_type == "user":
                try:
                    if int(raw.get("message_id")) == int(exclude_message_id):
                        continue
                except Exception:
                    pass

            ts_text = str(raw.get("timestamp") or "").strip()
            ts_epoch = self._parse_datetime_epoch(ts_text)
            if ts_epoch <= 0 or ts_epoch < cutoff_epoch:
                continue
            filtered.append((ts_epoch, idx, raw))

        filtered.sort(key=lambda item: (item[0], item[1]))
        if not filtered:
            lines.append("- 최근 대화가 없습니다.")
            return "\n".join(lines).strip()

        max_entries = max(target * 3, 90)
        omitted = 0
        if len(filtered) > max_entries:
            omitted = len(filtered) - max_entries
            filtered = filtered[-max_entries:]
        lines.insert(3, f"- 포함 메시지: {len(filtered)}개")
        if omitted > 0:
            lines.insert(4, f"- 오래된 항목 생략: {omitted}개")

        for _, _, raw in filtered:
            msg_type = str(raw.get("type") or "").strip().lower() or "user"
            ts_text = self._compact_prompt_text(raw.get("timestamp", ""), max_len=19) or "-"
            text = self._compact_prompt_text(raw.get("text", ""), max_len=180)

            files = raw.get("files")
            file_count = len(files) if isinstance(files, list) else 0
            location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
            suffix_parts: list[str] = []
            if file_count > 0:
                suffix_parts.append(f"첨부 {file_count}개")
            if location:
                lat = location.get("latitude")
                lon = location.get("longitude")
                if lat is not None and lon is not None:
                    suffix_parts.append(f"위치 {lat},{lon}")
            suffix = f" [{' / '.join(suffix_parts)}]" if suffix_parts else ""

            if not text:
                text = "(텍스트 없음)"

            if msg_type == "bot":
                speaker = "BOT"
            else:
                name = self._compact_prompt_text(raw.get("first_name", ""), max_len=20) or self._compact_prompt_text(
                    raw.get("username", ""), max_len=20
                )
                speaker = f"USER({name})" if name else "USER"
            lines.append(f"- [{ts_text}] {speaker}: {text}{suffix}")

        rendered = "\n".join(lines).strip()
        if len(rendered) > char_limit:
            rendered = rendered[: char_limit - 3] + "..."
        return rendered

    def _apply_selected_task_thread_target(self, chat_id: int, state: dict[str, Any]) -> None:
        if not bool(state.get("resume_thread_switch_pending")):
            return

        target_thread_id = str(state.get("resume_target_thread_id") or "").strip()
        current_thread_id = str(state.get("thread_id") or "").strip()
        selected_task_id = self._normalize_task_id_token(state.get("selected_task_id"))
        state["resume_thread_switch_pending"] = False
        state["resume_target_thread_id"] = ""

        if not target_thread_id and selected_task_id:
            target_thread_id = self._lookup_mapped_thread_id(chat_id=chat_id, task_id=selected_task_id)

        if target_thread_id:
            if current_thread_id and current_thread_id != target_thread_id:
                self.app_thread_to_chat.pop(current_thread_id, None)
            state["thread_id"] = target_thread_id
            state["app_generation"] = 0
            state["force_new_thread_once"] = False
            self._save_app_server_state()
            self._sync_app_server_session_meta(active_chat_id=chat_id)
            if selected_task_id:
                self._bind_task_thread_mapping(
                    chat_id=chat_id,
                    task_id=selected_task_id,
                    thread_id=target_thread_id,
                )
            self._log(f"task thread target applied chat_id={chat_id} thread_id={target_thread_id}")
            return

        state["force_new_thread_once"] = True
        self._save_app_server_state()
        self._log(f"task thread target missing, will start new thread chat_id={chat_id}")

    def _process_chat_control_messages(
        self,
        chat_id: int,
        state: dict[str, Any],
        pending_chat_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not pending_chat_messages:
            return pending_chat_messages

        mode = str(state.get("ui_mode") or UI_MODE_IDLE)
        expires_at = float(state.get("ui_mode_expires_at") or 0.0)
        if mode != UI_MODE_IDLE and expires_at > 0 and time.time() > expires_at:
            if mode == UI_MODE_AWAITING_TEMP_TASK_DECISION:
                self._clear_temp_task_seed(state)
            self._clear_ui_mode(state)

        remaining: list[dict[str, Any]] = []
        for item in pending_chat_messages:
            if self._handle_single_control_message(chat_id=chat_id, state=state, item=item):
                continue
            remaining.append(item)
        return remaining

    def _handle_single_control_message(
        self,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
    ) -> bool:
        msg_id = int(item.get("message_id", 0) or 0)
        text = self._normalize_ui_text(str(item.get("text", "")))
        callback_selected_task_id = self._extract_callback_task_select_id(text)
        if msg_id <= 0 or not text:
            return False
        current_mode = str(state.get("ui_mode") or UI_MODE_IDLE)
        ctx = {
            "chat_id": chat_id,
            "state": state,
            "item": item,
            "msg_id": msg_id,
            "text": text,
            "callback_selected_task_id": callback_selected_task_id,
        }

        if text.startswith("__cb__:") and current_mode != UI_MODE_AWAITING_RESUME_CHOICE:
            return self._ui_handle_callback_expired(**ctx)
        if text == BUTTON_TASK_LIST_RECENT20:
            return self._ui_handle_task_list_recent(**ctx)
        if text == BUTTON_TASK_GUIDE_VIEW:
            return self._ui_handle_task_guide_view(**ctx)
        if self._is_task_guide_edit_request_text(text):
            return self._forward_task_guide_edit_request(
                chat_id=chat_id, state=state, item=item, msg_id=msg_id, user_text=text,
            )
        if text == BUTTON_BOT_RENAME:
            return self._ui_handle_bot_rename(**ctx)

        temp_mode_passthrough_buttons = {
            BUTTON_TASK_LIST_RECENT20,
            BUTTON_TASK_GUIDE_VIEW,
            BUTTON_BOT_RENAME,
            BUTTON_MENU_BACK,
        }
        if current_mode == UI_MODE_AWAITING_TEMP_TASK_DECISION and text not in temp_mode_passthrough_buttons:
            return self._ui_handle_temp_task_decision(**ctx)

        if text == BUTTON_TASK_RESUME:
            return self._ui_handle_task_resume_button(**ctx)
        if text == BUTTON_TASK_NEW:
            return self._ui_handle_task_new_button(**ctx)
        if text == BUTTON_MENU_BACK:
            return self._ui_handle_menu_back(**ctx)

        if current_mode == UI_MODE_AWAITING_RESUME_QUERY:
            return self._ui_handle_awaiting_resume_query(**ctx)
        if current_mode == UI_MODE_AWAITING_RESUME_CHOICE:
            return self._ui_handle_awaiting_resume_choice(**ctx)
        if current_mode == UI_MODE_AWAITING_TASK_GUIDE_EDIT:
            if callback_selected_task_id:
                return False
            return self._forward_task_guide_edit_request(
                chat_id=chat_id, state=state, item=item, msg_id=msg_id, user_text=text,
            )
        if current_mode == UI_MODE_AWAITING_BOT_RENAME_ALIAS:
            return self._ui_handle_awaiting_bot_rename_alias(**ctx)
        if current_mode == UI_MODE_AWAITING_NEW_TASK_INPUT:
            return self._ui_handle_awaiting_new_task_input(**ctx)
        if current_mode == UI_MODE_IDLE and not callback_selected_task_id:
            return self._ui_handle_idle(**ctx)

        return False

    # ------------------------------------------------------------------
    # UI control-message handlers (extracted from _handle_single_control_message)
    # ------------------------------------------------------------------

    def _ui_handle_callback_expired(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        reply_text = "선택 가능한 목록이 만료되었어요. `TASK 목록 보기(최근20)`를 다시 눌러 주세요."
        keyboard_rows = self._main_menu_keyboard_rows()
        sent = self._telegram_send_text(
            chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_task_list_recent(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        rows = self._list_recent_tasks(chat_id=chat_id, limit=20, source_limit=300)
        if not rows:
            self._clear_ui_mode(state)
            reply_text = "최근 TASK 20개를 보여드리려 했지만, 조회된 TASK가 없습니다."
            keyboard_rows = self._main_menu_keyboard_rows()
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=keyboard_rows,
                inline_keyboard_rows=None,
                request_max_attempts=1,
                parse_mode="HTML",
            )
        else:
            candidate_ids, candidate_buttons, candidate_map = self._build_resume_choice_payload(rows=rows, max_count=20)
            state["resume_choice_inline_only"] = True
            state["resume_candidates"] = candidate_ids
            state["resume_candidate_buttons"] = candidate_buttons
            state["resume_candidate_map"] = candidate_map
            self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_CHOICE)
            header_text = self._render_task_list_text(rows=[], limit=20)
            footer_text = "최근순으로 정렬됩니다. 특정 작업(TASK)을 이어 진행하시려면 하단의 선택 버튼을 눌러 주세요."
            reply_text = footer_text
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=header_text,
                keyboard_rows=None,
                inline_keyboard_rows=None,
                request_max_attempts=1,
                parse_mode="HTML",
            )
            for idx, row in enumerate(rows, start=1):
                row_task_id = self._task_row_id(row)
                if not row_task_id:
                    continue
                item_text = self._render_task_item_card_text(idx=idx, row=row)
                item_inline_keyboard = self._build_single_task_inline_select_keyboard(task_id=row_task_id)
                sent_item = self._telegram_send_text(
                    chat_id=chat_id,
                    text=item_text,
                    keyboard_rows=None,
                    inline_keyboard_rows=item_inline_keyboard,
                    request_max_attempts=1,
                    parse_mode="HTML",
                )
                sent = bool(sent or sent_item)
            sent_footer = self._telegram_send_text(
                chat_id=chat_id,
                text=footer_text,
                keyboard_rows=None,
                inline_keyboard_rows=None,
                request_max_attempts=1,
            )
            sent = bool(sent or sent_footer)
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_task_guide_view(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        guide_thread_id = self._resolve_task_agents_thread_id(state)
        if not guide_thread_id:
            self._clear_ui_mode(state)
            reply_text = (
                "현재 선택된 TASK가 없습니다.\n"
                "먼저 `TASK 목록 보기(최근20)` 또는 `기존 TASK 이어하기`로 TASK를 선택해 주세요."
            )
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=self._main_menu_keyboard_rows(),
                request_max_attempts=1,
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        relative_path = self._task_agents_relative_path(chat_id=chat_id, thread_id=guide_thread_id)
        guide_text, exists = self._load_task_agents_text(chat_id=chat_id, thread_id=guide_thread_id)
        self._set_ui_mode(state, UI_MODE_AWAITING_TASK_GUIDE_EDIT)
        sent = False
        if exists and guide_text.strip():
            header_text = (
                f"<b>TASK 지침 파일 보기</b>\n"
                f"- 파일: <code>{self._escape_telegram_html(relative_path)}</code>\n"
                "- 아래 내용 확인 후 변경 요청을 바로 보내주세요."
            )
            sent_header = self._telegram_send_text(
                chat_id=chat_id,
                text=header_text,
                keyboard_rows=None,
                request_max_attempts=1,
                parse_mode="HTML",
            )
            sent = bool(sent or sent_header)
            chunks = self._split_text_chunks(guide_text, max_chars=DEFAULT_TASK_GUIDE_TELEGRAM_CHUNK_CHARS)
            total_chunks = len(chunks)
            for idx, chunk in enumerate(chunks, start=1):
                chunk_label = f"TASK 지침 내용 ({idx}/{total_chunks})"
                body_text = (
                    f"<b>{self._escape_telegram_html(chunk_label)}</b>\n"
                    f"<pre>{self._escape_telegram_html(chunk)}</pre>"
                )
                sent_chunk = self._telegram_send_text(
                    chat_id=chat_id,
                    text=body_text,
                    keyboard_rows=None,
                    request_max_attempts=1,
                    parse_mode="HTML",
                )
                sent = bool(sent or sent_chunk)
            reply_text = (
                f"TASK 지침을 보여드렸어요. `{relative_path}` 변경 요청을 보내주시면 "
                "코덱스가 해당 파일을 직접 수정합니다."
            )
        elif exists:
            reply_text = (
                f"`{relative_path}` 파일은 존재하지만 현재 내용이 비어 있습니다.\n"
                "원하시는 지침 내용을 보내주시면 코덱스가 파일을 수정해 반영합니다."
            )
        else:
            reply_text = (
                f"현재 `{relative_path}` 파일이 없습니다.\n"
                "`TASK 지침 추가 ...` 또는 `TASK 지침 변경 ...`처럼 요청해주시면 "
                "해당 AGENTS.md를 생성한 뒤 바로 반영합니다."
            )
        sent_footer = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=self._main_menu_keyboard_rows(),
            request_max_attempts=1,
        )
        sent = bool(sent or sent_footer)
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_bot_rename(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        if not self.is_bot_worker or not self.bot_id:
            self._clear_ui_mode(state)
            reply_text = "현재 실행 환경에서는 봇 이름 변경을 지원하지 않습니다."
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=self._main_menu_keyboard_rows(),
                request_max_attempts=1,
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        base_name = self._resolve_bot_base_name()
        state["bot_rename_base_name"] = base_name
        self._set_ui_mode(state, UI_MODE_AWAITING_BOT_RENAME_ALIAS)
        shown_name = base_name if base_name else "(확인 실패)"
        reply_text = (
            "<b>봇 이름 변경</b>\n"
            f"현재 기본 이름: <code>{self._escape_telegram_html(shown_name)}</code>\n"
            "원하는 별칭을 입력해 주세요.\n"
            "적용 형식: <code>기존이름(별칭)</code>"
        )
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=self._main_menu_keyboard_rows(),
            request_max_attempts=1,
            parse_mode="HTML",
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_temp_task_decision(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        if text == BUTTON_TASK_NEW:
            state["pending_new_task_summary"] = self._build_new_task_carryover_summary(chat_id=chat_id, state=state)
            state["force_new_thread_once"] = True
            self._clear_selected_task_state(state)
            queued = list(state.get("queued_messages") or [])
            queued.extend(self._build_temp_task_seed_batch(chat_id=chat_id, state=state))
            deduped_queued: list[dict[str, Any]] = []
            seen_ids: set[int] = set()
            for queued_item in queued:
                try:
                    queued_msg_id = int(queued_item.get("message_id"))
                except Exception:
                    continue
                if queued_msg_id in seen_ids:
                    continue
                seen_ids.add(queued_msg_id)
                deduped_queued.append(queued_item)
            state["queued_messages"] = deduped_queued
            self._clear_temp_task_seed(state)
            self._clear_ui_mode(state)
            reply_text = (
                "새 TASK로 시작할게요.\n"
                "방금 보낸 내용을 첫 요청으로 이어서 처리합니다."
            )
            keyboard_rows = self._main_menu_keyboard_rows()
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=keyboard_rows,
                request_max_attempts=1,
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        if text == BUTTON_TASK_RESUME:
            seed_query = str(state.get("temp_task_first_text") or "").strip()
            if not seed_query:
                self._clear_temp_task_seed(state)
                self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_QUERY)
                reply_text = "원하시는 TASK를 검색하겠습니다. 검색어를 입력해주세요"
                keyboard_rows = self._main_menu_keyboard_rows()
                sent = self._telegram_send_text(
                    chat_id=chat_id,
                    text=reply_text,
                    keyboard_rows=keyboard_rows,
                    request_max_attempts=1,
                )
                if sent:
                    self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
                return True

            candidates = self._search_task_candidates_for_resume(
                chat_id=chat_id,
                query=seed_query,
                limit=self.task_search_llm_limit,
            )
            self._clear_temp_task_seed(state)
            if not candidates:
                self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_QUERY)
                reply_text = (
                    f"`{seed_query}`와 연관된 TASK를 찾지 못했습니다. "
                    "다른 키워드를 입력해 주세요."
                )
                keyboard_rows = self._main_menu_keyboard_rows()
                sent = self._telegram_send_text(
                    chat_id=chat_id,
                    text=reply_text,
                    keyboard_rows=keyboard_rows,
                    request_max_attempts=1,
                )
                if sent:
                    self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
                return True

            candidate_ids, candidate_buttons, candidate_map = self._build_resume_choice_payload(
                rows=candidates,
                max_count=self.task_search_llm_limit,
            )
            state["resume_choice_inline_only"] = True
            state["resume_candidates"] = candidate_ids
            state["resume_candidate_buttons"] = candidate_buttons
            state["resume_candidate_map"] = candidate_map
            self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_CHOICE)
            query_html = self._escape_telegram_html(seed_query)
            header_text = (
                "<b>연관 TASK 후보</b>\n"
                f"검색어: <code>{query_html}</code>\n"
                "<i>연관도 높은 순으로 정렬됩니다. 항목의 선택 버튼을 눌러 주세요.</i>"
            )
            reply_text = header_text
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=header_text,
                keyboard_rows=None,
                request_max_attempts=1,
                parse_mode="HTML",
            )
            for idx, row in enumerate(candidates, start=1):
                row_task_id = self._task_row_id(row)
                if not row_task_id:
                    continue
                item_text = self._render_task_item_card_text(idx=idx, row=row)
                item_inline_keyboard = self._build_single_task_inline_select_keyboard(task_id=row_task_id)
                sent_item = self._telegram_send_text(
                    chat_id=chat_id,
                    text=item_text,
                    keyboard_rows=None,
                    inline_keyboard_rows=item_inline_keyboard,
                    request_max_attempts=1,
                    parse_mode="HTML",
                )
                sent = bool(sent or sent_item)
            footer_text = "원하시는 TASK의 선택 버튼을 누르면 바로 이어서 진행합니다."
            sent_footer = self._telegram_send_text(
                chat_id=chat_id,
                text=footer_text,
                keyboard_rows=None,
                inline_keyboard_rows=None,
                request_max_attempts=1,
            )
            sent = bool(sent or sent_footer)
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        reply_text = "새 TASK로 시작할지, 기존 TASK를 이어갈지 버튼으로 선택해 주세요."
        keyboard_rows = [[BUTTON_TASK_NEW, BUTTON_TASK_RESUME]]
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=keyboard_rows,
            request_max_attempts=1,
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_task_resume_button(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_QUERY)
        state["resume_choice_inline_only"] = False
        state["resume_candidates"] = []
        state["resume_candidate_buttons"] = []
        state["resume_candidate_map"] = {}
        reply_text = "원하시는 TASK를 검색하겠습니다. 검색어를 입력해주세요"
        keyboard_rows = self._main_menu_keyboard_rows()
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=keyboard_rows,
            request_max_attempts=1,
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_task_new_button(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        self._set_ui_mode(state, UI_MODE_AWAITING_NEW_TASK_INPUT)
        state["resume_choice_inline_only"] = False
        state["resume_candidates"] = []
        state["resume_candidate_buttons"] = []
        state["resume_candidate_map"] = {}
        reply_text = "새 TASK로 시작할 지시를 입력해 주세요."
        keyboard_rows = self._main_menu_keyboard_rows()
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=keyboard_rows,
            request_max_attempts=1,
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_menu_back(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        self._clear_temp_task_seed(state)
        self._clear_ui_mode(state)
        reply_text = "메뉴로 돌아왔어요."
        keyboard_rows = self._main_menu_keyboard_rows()
        sent = self._telegram_send_text(
            chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_awaiting_resume_query(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        candidates = self._search_task_candidates_for_resume(
            chat_id=chat_id,
            query=text,
            limit=self.task_search_llm_limit,
        )
        if not candidates:
            reply_text = f"`{text}`와 연관된 TASK를 찾지 못했습니다. 다른 키워드를 입력해 주세요."
            keyboard_rows = self._main_menu_keyboard_rows()
            sent = self._telegram_send_text(
                chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        candidate_ids, candidate_buttons, candidate_map = self._build_resume_choice_payload(
            rows=candidates,
            max_count=self.task_search_llm_limit,
        )
        state["resume_choice_inline_only"] = True
        state["resume_candidates"] = candidate_ids
        state["resume_candidate_buttons"] = candidate_buttons
        state["resume_candidate_map"] = candidate_map
        self._set_ui_mode(state, UI_MODE_AWAITING_RESUME_CHOICE)
        query_html = self._escape_telegram_html(text)
        header_text = (
            "<b>연관 TASK 후보</b>\n"
            f"검색어: <code>{query_html}</code>\n"
            "<i>연관도 높은 순으로 정렬됩니다. 항목의 선택 버튼을 눌러 주세요.</i>"
        )
        reply_text = header_text
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=header_text,
            keyboard_rows=None,
            request_max_attempts=1,
            parse_mode="HTML",
        )
        for idx, row in enumerate(candidates, start=1):
            row_task_id = self._task_row_id(row)
            if not row_task_id:
                continue
            item_text = self._render_task_item_card_text(idx=idx, row=row)
            item_inline_keyboard = self._build_single_task_inline_select_keyboard(task_id=row_task_id)
            sent_item = self._telegram_send_text(
                chat_id=chat_id,
                text=item_text,
                keyboard_rows=None,
                inline_keyboard_rows=item_inline_keyboard,
                request_max_attempts=1,
                parse_mode="HTML",
            )
            sent = bool(sent or sent_item)
        footer_text = "원하시는 TASK의 선택 버튼을 누르면 바로 이어서 진행합니다."
        sent_footer = self._telegram_send_text(
            chat_id=chat_id,
            text=footer_text,
            keyboard_rows=None,
            inline_keyboard_rows=None,
            request_max_attempts=1,
        )
        sent = bool(sent or sent_footer)
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_awaiting_resume_choice(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        candidate_ids = [
            self._normalize_task_id_token(v)
            for v in (state.get("resume_candidates") or [])
        ]
        candidate_ids = [v for v in candidate_ids if v]
        candidate_buttons = [
            self._normalize_ui_text(v)
            for v in (state.get("resume_candidate_buttons") or [])
            if self._normalize_ui_text(v)
        ]
        candidate_map_raw = state.get("resume_candidate_map") if isinstance(state.get("resume_candidate_map"), dict) else {}
        inline_only = bool(state.get("resume_choice_inline_only"))
        if callback_selected_task_id:
            selected_task_id = callback_selected_task_id
        else:
            selected_task_id = self._resolve_task_choice(text=text, candidates=candidate_ids, candidate_map=candidate_map_raw)
        if callback_selected_task_id and candidate_ids and selected_task_id not in candidate_ids:
            reply_text = "선택 가능한 목록이 갱신되었습니다. `TASK 목록 보기(최근20)`를 다시 눌러 주세요."
            keyboard_rows = self._main_menu_keyboard_rows()
            sent = self._telegram_send_text(
                chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True
        if not selected_task_id:
            if inline_only:
                reply_text = "목록 항목의 `선택` 버튼을 누르거나, 번호(1,2,3...) 또는 TASK ID를 입력해 주세요."
                keyboard_rows = None
            else:
                reply_text = "후보 버튼을 누르거나, 번호(1,2,3...)를 입력해 주세요."
                keyboard_rows = self._build_candidate_keyboard_rows(candidate_buttons) if candidate_buttons else self._main_menu_keyboard_rows()
            sent = self._telegram_send_text(
                chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        row = self._load_task_row(chat_id=chat_id, task_id=selected_task_id, include_instrunction=False)
        if not row:
            reply_text = f"{selected_task_id} TASK를 찾지 못했습니다. 다시 선택해 주세요."
            keyboard_rows = None if inline_only else (self._build_candidate_keyboard_rows(candidate_buttons) if candidate_buttons else self._main_menu_keyboard_rows())
            sent = self._telegram_send_text(
                chat_id=chat_id, text=reply_text, keyboard_rows=keyboard_rows, request_max_attempts=1
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        self._set_selected_task_state(chat_id=chat_id, state=state, row=row)
        state["resume_recent_chat_summary_once"] = self._build_recent_chat_summary(
            chat_id=chat_id,
            hours=DEFAULT_RESUME_CHAT_SUMMARY_HOURS,
            target_lines=DEFAULT_RESUME_CHAT_SUMMARY_LINES,
            max_chars=DEFAULT_RESUME_CHAT_SUMMARY_MAX_CHARS,
            exclude_message_id=msg_id,
        )
        state["resume_context_inject_once"] = True
        if not str(state.get("active_turn_id") or "").strip():
            self._apply_selected_task_thread_target(chat_id=chat_id, state=state)
        self._clear_ui_mode(state)
        reply_text = (
            f"{selected_task_id} TASK로 이어서 진행할게요.\n"
            "이제 이어서 할 내용을 보내주시면 바로 처리합니다."
        )
        callback_source_message_id = int(item.get("callback_message_id", 0) or 0)
        keyboard_rows = self._main_menu_keyboard_rows()
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=keyboard_rows,
            request_max_attempts=1,
        )
        if sent:
            self._log(
                f"task_select_delivery=send_first chat_id={chat_id} task_id={selected_task_id}"
            )
        elif callback_selected_task_id and callback_source_message_id > 0:
            self._log(
                "WARN: task_select_delivery send_first_failed "
                f"chat_id={chat_id} task_id={selected_task_id} callback_message_id={callback_source_message_id}"
            )
            sent = self._telegram_edit_message_text(
                chat_id=chat_id,
                message_id=callback_source_message_id,
                text=reply_text,
                inline_keyboard_rows=[],
                request_max_attempts=1,
            )
            if sent:
                self._log(
                    f"task_select_delivery=fallback_edit chat_id={chat_id} task_id={selected_task_id} callback_message_id={callback_source_message_id}"
                )
        if sent:
            self._log(
                f"task_select_focus_mode=no_post_edit chat_id={chat_id} task_id={selected_task_id}"
            )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_awaiting_bot_rename_alias(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        if callback_selected_task_id:
            return False
        alias = self._normalize_bot_alias(text, max_len=32)
        if not alias:
            reply_text = "별칭이 비어 있습니다. 1~32자 별칭을 입력해 주세요."
            sent = self._telegram_send_text(
                chat_id=chat_id,
                text=reply_text,
                keyboard_rows=self._main_menu_keyboard_rows(),
                request_max_attempts=1,
            )
            if sent:
                self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
            return True

        ok, reply_text = self._rename_bot_display_name(
            alias_text=alias,
            base_name_hint=state.get("bot_rename_base_name", ""),
        )
        if ok:
            self._clear_ui_mode(state)
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=self._main_menu_keyboard_rows(),
            request_max_attempts=1,
            parse_mode="HTML",
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _ui_handle_awaiting_new_task_input(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        state["pending_new_task_summary"] = self._build_new_task_carryover_summary(chat_id=chat_id, state=state)
        state["force_new_thread_once"] = True
        self._clear_selected_task_state(state)
        self._clear_ui_mode(state)
        if str(state.get("active_turn_id") or "").strip():
            self._telegram_send_text(
                chat_id=chat_id,
                text="현재 진행 중인 응답이 끝나면 새 TASK로 시작합니다.",
                keyboard_rows=self._main_menu_keyboard_rows(),
                request_max_attempts=1,
            )
        return False

    def _ui_handle_idle(
        self,
        *,
        chat_id: int,
        state: dict[str, Any],
        item: dict[str, Any],
        msg_id: int,
        text: str,
        callback_selected_task_id: str,
    ) -> bool:
        has_thread = bool(str(state.get("thread_id") or "").strip())
        has_active_turn = bool(str(state.get("active_turn_id") or "").strip())
        self._log(
            f"[DBG] idle_check chat_id={chat_id} mode={UI_MODE_IDLE} has_thread={has_thread} "
            f"has_active={has_active_turn} force_new={bool(state.get('force_new_thread_once'))} platform={self.platform}"
        )
        if has_thread or has_active_turn or bool(state.get("force_new_thread_once")):
            return False
        recovered_thread_id = self._recover_latest_thread_id_for_chat(chat_id=chat_id)
        self._log(f"[DBG] recovered_thread={repr(recovered_thread_id)}")
        if recovered_thread_id:
            state["thread_id"] = recovered_thread_id
            state["app_generation"] = 0
            self._clear_temp_task_seed(state)
            self._save_app_server_state()
            self._sync_app_server_session_meta(active_chat_id=chat_id)
            self._log(
                f"cold_start_auto_resume_thread chat_id={chat_id} thread_id={recovered_thread_id} "
                f"msg_id={msg_id}"
            )
            return False

        if self.platform == "discord":
            state["force_new_thread_once"] = True
            self._clear_selected_task_state(state)
            self._clear_temp_task_seed(state)
            return False

        state["temp_task_first_text"] = text
        state["temp_task_first_message_id"] = msg_id
        state["temp_task_first_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._set_ui_mode(state, UI_MODE_AWAITING_TEMP_TASK_DECISION)
        prompt_seed = self._escape_telegram_html(self._compact_prompt_text(text, max_len=120))
        reply_text = (
            f"말씀하신 내용(<code>{prompt_seed}</code>)을 기준으로 시작할게요.\n"
            "새 TASK로 시작할지, 기존 TASK를 이어갈지 선택해 주세요."
        )
        keyboard_rows = [[BUTTON_TASK_NEW, BUTTON_TASK_RESUME]]
        sent = self._telegram_send_text(
            chat_id=chat_id,
            text=reply_text,
            keyboard_rows=keyboard_rows,
            request_max_attempts=1,
            parse_mode="HTML",
        )
        if sent:
            self._finalize_control_message(chat_id=chat_id, message_id=msg_id, reply_text=reply_text)
        return True

    def _snapshot_pending_messages(self) -> list[dict[str, object]]:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return []
        try:
            pending = telegram.get_pending_messages(str(self.store_file), include_bot=False)
        except Exception as exc:
            self._log(f"WARN: pending snapshot failed: {exc}")
            return []

        allowed_ids = set(int(v) for v in (runtime.get("allowed_user_ids") or []))
        messages: list[dict[str, object]] = []
        seen_ids: set[tuple[int, int]] = set()
        for msg in pending:
            try:
                msg_id = int(msg.get("message_id"))
                chat_id = int(msg.get("chat_id"))
                user_id = int(msg.get("user_id"))
            except Exception:
                continue
            if allowed_ids and user_id not in allowed_ids:
                continue
            dedupe_key = (chat_id, msg_id)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            files = msg.get("files") if isinstance(msg.get("files"), list) else []
            location = msg.get("location") if isinstance(msg.get("location"), dict) else None
            messages.append(
                {
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "text": self._compact_prompt_text(
                        self._strip_new_command_prefix(str(msg.get("text", ""))),
                        max_len=320,
                    ),
                    "files": files,
                    "location": location,
                }
            )
        messages.sort(key=lambda item: int(item.get("message_id", 0)))
        return messages

    def _task_root_for_chat(self, chat_id: int) -> Path:
        if not self.tasks_partition_by_chat:
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            return self.tasks_dir
        root = (self.tasks_dir / f"chat_{chat_id}").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _legacy_task_thread_map_path(self, chat_id: int) -> Path:
        return self._task_root_for_chat(chat_id) / LEGACY_TASK_THREAD_MAP_FILENAME

    def _load_legacy_task_thread_map(self, chat_id: int) -> dict[str, str]:
        path = self._legacy_task_thread_map_path(chat_id)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(loaded, dict):
            return {}
        out: dict[str, str] = {}
        for raw_task_id, raw_thread_id in loaded.items():
            task_id = self._normalize_task_id_token(raw_task_id)
            thread_id = self._compact_prompt_text(raw_thread_id, max_len=200)
            if not task_id or not thread_id:
                continue
            out[task_id] = thread_id
        return out

    def _save_legacy_task_thread_map(self, chat_id: int, mapping: dict[str, str]) -> None:
        path = self._legacy_task_thread_map_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized: dict[str, str] = {}
        for raw_task_id, raw_thread_id in (mapping or {}).items():
            task_id = self._normalize_task_id_token(raw_task_id)
            thread_id = self._compact_prompt_text(raw_thread_id, max_len=200)
            if not task_id or not thread_id:
                continue
            normalized[task_id] = thread_id
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _lookup_mapped_thread_id(self, chat_id: int, task_id: str) -> str:
        normalized_task_id = self._normalize_task_id_token(task_id)
        if not normalized_task_id:
            return ""
        mapping = self._load_legacy_task_thread_map(chat_id)
        return self._compact_prompt_text(mapping.get(normalized_task_id, ""), max_len=200)

    def _bind_task_thread_mapping(self, chat_id: int, task_id: str, thread_id: str) -> None:
        normalized_task_id = self._normalize_task_id_token(task_id)
        normalized_thread_id = self._compact_prompt_text(thread_id, max_len=200)
        if not normalized_task_id or not normalized_thread_id:
            return
        mapping = self._load_legacy_task_thread_map(chat_id)
        if mapping.get(normalized_task_id) == normalized_thread_id:
            return
        mapping[normalized_task_id] = normalized_thread_id
        self._save_legacy_task_thread_map(chat_id, mapping)

    def _task_path_hint_for_messages(self, pending_messages: list[dict[str, object]]) -> str:
        if not self.tasks_partition_by_chat:
            return "tasks/thread_{thread_id}/INSTRUNCTION.md"
        chat_ids: set[int] = set()
        for item in pending_messages:
            try:
                chat_ids.add(int(item.get("chat_id")))
            except Exception:
                continue
        if len(chat_ids) == 1:
            only_chat = next(iter(chat_ids))
            return f"tasks/chat_{only_chat}/thread_{{thread_id}}/INSTRUNCTION.md"
        return "tasks/chat_{chat_id}/thread_{thread_id}/INSTRUNCTION.md"

    def _build_dynamic_request_line(self, pending_messages: list[dict[str, object]]) -> str:
        if not pending_messages:
            rendered_refs = "없음"
            rendered_requests = "새메시지가 없습니다."
        else:
            request_entries: list[str] = []
            ref_entries: list[str] = []
            for item in pending_messages:
                msg_id = int(item.get("message_id", 0))
                text = self._compact_prompt_text(item.get("text", ""), max_len=320)
                if not text:
                    text = "(텍스트 없음, 첨부/위치 정보 참고)"
                request_entries.append(f"[msg_{msg_id}] {text}")

                files_raw = item.get("files")
                file_types: list[str] = []
                if isinstance(files_raw, list):
                    for f in files_raw:
                        if isinstance(f, dict):
                            file_type = self._compact_prompt_text(f.get("type", ""), max_len=30)
                            if file_type:
                                file_types.append(file_type)
                file_types = sorted(set(file_types))
                file_info = (
                    f"{len(files_raw)}개[{','.join(file_types)}]"
                    if isinstance(files_raw, list) and files_raw
                    else "없음"
                )

                location_info = "없음"
                location_raw = item.get("location")
                if isinstance(location_raw, dict):
                    lat = location_raw.get("latitude")
                    lon = location_raw.get("longitude")
                    if lat is not None and lon is not None:
                        location_info = f"{lat},{lon}"

                if file_info != "없음" or location_info != "없음":
                    ref_entries.append(f"msg_{msg_id}: files={file_info}, location={location_info}")

            rendered_refs = " | ".join(ref_entries) if ref_entries else "없음"
            rendered_requests = " | ".join(request_entries)
        task_path_hint = self._task_path_hint_for_messages(pending_messages)

        return (
            f"참조사항: {rendered_refs}\n"
            "작업 메모리는 sonolbot-tasks 스킬 규칙을 따를 것 "
            f"({task_path_hint} 선읽기 및 변경 즉시 동기화).\n"
            "요청사항을 처리한 후, 사용자에게 전달할 최종 답변 본문만 작성할 것 "
            "(결과에는 지침 준수/백그라운드 동작 언급 없이 요청사항에 대한 직접적인 답변만 포함할 것. "
            "친절하고 이해하기 쉽게 답하되 꼭 알아야 할 사항을 빠뜨리지 말것)\n"
            "최종 답변은 텔레그램 HTML 파싱 기준으로 작성할 것 "
            "(필요시 <b>, <code> 최소 사용, Markdown 문법은 사용하지 말 것).\n"
            f"요청사항: {rendered_requests}"
        )

    def _build_codex_prompt(self, pending_messages: list[dict[str, object]]) -> str:
        request_line = self._build_dynamic_request_line(pending_messages)
        # Keep the injected request as the very last line.
        return PROMPT_TEXT.strip() + "\n" + request_line

    def _task_prepare_batch(
        self,
        chat_id: int,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        thread_id: str,
    ) -> str:
        if not messages or not str(thread_id or "").strip():
            return ""
        task_skill = self._get_task_skill()
        if task_skill is None:
            return ""
        task_root = self._task_root_for_chat(chat_id)
        query_tokens: list[str] = []
        source_message_ids: set[int] = set()
        for item in messages:
            try:
                msg_id = int(item.get("message_id"))
            except Exception:
                continue
            if msg_id > 0:
                source_message_ids.add(msg_id)
            instruction = self._compact_prompt_text(item.get("text", ""), max_len=1200)
            if not instruction:
                instruction = "(텍스트 없음, 첨부/위치 정보 참고)"
            query_tokens.append(instruction)

        if not query_tokens:
            return ""
        lead_instruction = query_tokens[-1]
        task_id = f"thread_{thread_id}"
        try:
            session = task_skill.init_task_session(
                tasks_dir=str(task_root),
                task_id=task_id,
                thread_id=thread_id,
                instruction=lead_instruction,
                message_id=max(source_message_ids) if source_message_ids else 0,
                source_message_ids=sorted(source_message_ids),
                chat_id=chat_id,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                logs_dir=str(self.logs_dir),
            )
            task_dir = str(session.get("task_dir") or "").strip()
            if task_dir:
                task_skill.read_instrunction_first(task_dir=task_dir, logs_dir=str(self.logs_dir))
            active_task_ids: set[str] = state.get("active_task_ids") or set()
            active_task_ids.add(task_id)
            state["active_task_ids"] = active_task_ids
            selected_task_id = self._normalize_task_id_token(state.get("selected_task_id"))
            if selected_task_id:
                self._bind_task_thread_mapping(
                    chat_id=chat_id,
                    task_id=selected_task_id,
                    thread_id=thread_id,
                )
        except Exception as exc:
            self._log(f"WARN: task init/read failed chat_id={chat_id} task_id={task_id}: {exc}")
            return ""

        try:
            packet = str(
                task_skill.build_compact_memory_packet(
                    query=" | ".join(query_tokens),
                    tasks_dir=str(task_root),
                    limit=3,
                    max_chars=1000,
                    logs_dir=str(self.logs_dir),
                )
            ).strip()
        except Exception as exc:
            self._log(f"WARN: compact task memory build failed chat_id={chat_id}: {exc}")
            return ""
        if not packet or packet == "관련 TASK를 찾지 못했습니다.":
            return ""
        return packet

    def _task_record_batch_change(
        self,
        *,
        chat_id: int,
        task_ids: set[str],
        message_ids: set[int],
        status: str,
        result_text: str,
        sent_ok: bool,
    ) -> None:
        normalized_task_ids = {
            self._normalize_task_id_token(v)
            for v in (task_ids or set())
            if self._normalize_task_id_token(v)
        }
        if not normalized_task_ids:
            return
        task_skill = self._get_task_skill()
        if task_skill is None:
            return
        task_root = self._task_root_for_chat(chat_id)
        summary = self._compact_prompt_text(result_text, max_len=500)
        if status == "completed":
            if sent_ok:
                note = "app-server turn 완료 후 텔레그램 최종 답변 전송"
            else:
                note = "app-server turn 완료, 텔레그램 전송 지연으로 재시도 대기"
        else:
            note = f"app-server turn 종료(status={status})"

        note_summary = self._compact_prompt_text(summary, max_len=self.result_summary_note_max_len)
        if note_summary:
            note = f"{note} | 응답요약: {note_summary}"

        for task_id in sorted(normalized_task_ids):
            try:
                task_skill.record_task_change(
                    tasks_dir=str(task_root),
                    task_id=task_id,
                    thread_id=(task_id[len("thread_") :] if task_id.startswith("thread_") else ""),
                    message_id=(max(message_ids) if message_ids else 0),
                    source_message_ids=sorted(int(v) for v in message_ids if int(v) > 0),
                    change_note=note,
                    result_summary=summary or "(응답 텍스트 없음)",
                    sent_files=[],
                    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    logs_dir=str(self.logs_dir),
                )
            except Exception as exc:
                self._log(f"WARN: task record failed chat_id={chat_id} task_id={task_id}: {exc}")

    def _contains_internal_agent_text(self, text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        for pattern in INTERNAL_AGENT_TEXT_PATTERNS:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return True
        return False

    def _load_latest_user_hint(self, chat_id: int, state: dict[str, Any]) -> str:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None or not hasattr(telegram, "load_message_store"):
            return ""
        try:
            store = telegram.load_message_store(str(self.store_file))
        except Exception:
            return ""
        messages = store.get("messages", [])
        if not isinstance(messages, list):
            return ""

        active_ids = {int(v) for v in (state.get("active_message_ids") or set()) if int(v) > 0}
        normalized_items: list[dict[str, Any]] = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            msg_type = str(raw.get("type") or "").strip().lower()
            if msg_type != "user":
                continue
            try:
                row_chat_id = int(raw.get("chat_id"))
            except Exception:
                continue
            if row_chat_id != int(chat_id):
                continue
            msg_text = self._compact_prompt_text(self._strip_new_command_prefix(str(raw.get("text", ""))), max_len=220)
            if not msg_text:
                continue
            msg_id = raw.get("message_id")
            msg_id_int = int(msg_id) if isinstance(msg_id, int) else 0
            normalized_items.append({"message_id": msg_id_int, "text": msg_text})

        if not normalized_items:
            return ""
        if active_ids:
            for row in reversed(normalized_items):
                msg_id_int = int(row.get("message_id") or 0)
                if msg_id_int > 0 and msg_id_int in active_ids:
                    return str(row.get("text") or "")
        return str(normalized_items[-1].get("text") or "")

    def _build_agent_rewriter_input(self, chat_id: int, state: dict[str, Any], raw_text: str) -> str:
        user_hint = self._load_latest_user_hint(chat_id=chat_id, state=state)
        selected_task_id = self._normalize_task_id_token(state.get("selected_task_id"))
        lines = [
            "아래 원문 안내를 지침에 맞는 사용자 진행 안내문으로 재작성하라.",
            f"- 최근 사용자 요청: {user_hint or '(없음)'}",
            f"- 선택된 TASK 식별자: {selected_task_id or '(없음)'}",
            "- 출력 형식: 텔레그램 HTML 파싱 기준(필요 시 <b>, <code>만 최소 사용, Markdown 문법 금지)",
            "- 원문 안내:",
            self._compact_prompt_text(raw_text, max_len=1200) or "(비어 있음)",
            "",
            "결과 문장만 출력하라.",
        ]
        return "\n".join(lines).strip()

    def _build_agent_rewriter_fallback(self, chat_id: int, state: dict[str, Any], raw_text: str) -> str:
        user_hint = self._compact_prompt_text(self._load_latest_user_hint(chat_id=chat_id, state=state), max_len=56)
        if not user_hint:
            raw_compact = self._compact_prompt_text(raw_text, max_len=56)
            if raw_compact and not self._contains_internal_agent_text(raw_compact):
                user_hint = raw_compact
        if user_hint:
            return (
                f"요청하신 '{user_hint}' 내용을 더 정확히 설명드리기 위해 관련 내용을 확인하고 핵심을 정리하는 중입니다. "
                "잠시만 기다려 주시면 이해하기 쉽게 이어서 안내드릴게요."
            )
        return (
            "요청하신 내용을 정확하게 설명드리기 위해 관련 내용을 확인하고 핵심을 정리하는 중입니다. "
            "잠시만 기다려 주시면 이해하기 쉽게 이어서 안내드릴게요."
        )

    def _normalize_agent_rewriter_output(self, text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        normalized = re.sub(r"^\s*(재작성 결과[:：]\s*)", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+\n", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return self._compact_prompt_text(normalized, max_len=700)

    def _rewriter_is_running(self) -> bool:
        return self._rewriter_channel.is_running()

    def _rewriter_send_json(self, payload: dict[str, Any]) -> bool:
        return self._rewriter_channel.send_json(payload)

    def _rewriter_notify(self, method: str, params: dict[str, Any] | None = None) -> bool:
        return self._rewriter_channel.notify(method, params)

    def _rewriter_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any] | None:
        return self._rewriter_channel.request(method, params, timeout_sec)

    def _rewriter_handle_server_request(self, request_obj: dict[str, Any]) -> None:
        self._rewriter_channel.handle_server_request(request_obj)

    def _rewriter_dispatch_incoming(self, line: str) -> None:
        self._rewriter_channel.dispatch_incoming(line)

    def _rewriter_stdout_reader(self) -> None:
        self._rewriter_channel.stdout_reader()

    def _rewriter_stderr_reader(self) -> None:
        self._rewriter_channel.stderr_reader()

    def _rewriter_process_notification(self, event: dict[str, Any]) -> None:
        method = str(event.get("method") or "")
        params = event.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "codex/event/task_complete":
            msg = params.get("msg")
            if not isinstance(msg, dict):
                return
            turn_id = str(msg.get("turn_id") or params.get("id") or "").strip()
            if not turn_id:
                return
            last_text = str(msg.get("last_agent_message") or "").strip()
            self.rewriter_turn_results[turn_id] = {
                "status": "completed",
                "text": last_text,
                "updated_at": time.time(),
            }
            return

        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            turn_id = str(turn.get("id") or "").strip()
            if not turn_id:
                return
            status = str(turn.get("status") or "").strip().lower() or "completed"
            current = self.rewriter_turn_results.get(turn_id, {})
            if not isinstance(current, dict):
                current = {}
            current.setdefault("text", "")
            current["status"] = status
            current["updated_at"] = time.time()
            self.rewriter_turn_results[turn_id] = current
            return

    def _rewriter_drain_events(self, max_items: int = 200) -> None:
        for _ in range(max_items):
            try:
                event = self.rewriter_event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._rewriter_process_notification(event)
            except Exception as exc:
                self._log(f"WARN: agent-rewriter event handling failed: {exc}")

    def _rewriter_wait_turn_result(self, turn_id: str, timeout_sec: float) -> dict[str, Any] | None:
        if not turn_id:
            return None
        deadline = time.time() + max(0.5, float(timeout_sec))
        while time.time() < deadline:
            self._rewriter_drain_events(max_items=200)
            current = self.rewriter_turn_results.get(turn_id)
            if isinstance(current, dict):
                status = str(current.get("status") or "").strip().lower()
                if status == "completed":
                    self.rewriter_turn_results.pop(turn_id, None)
                    return current
            if not self._rewriter_is_running():
                break
            time.sleep(0.05)
        return None

    def _stop_agent_rewriter(self, reason: str) -> None:
        if self.rewriter_proc is not None:
            self._log(f"Stopping agent-rewriter (reason={reason}, pid={self.rewriter_proc.pid})")
            try:
                self.rewriter_proc.terminate()
                self.rewriter_proc.wait(timeout=3)
            except Exception:
                try:
                    self.rewriter_proc.kill()
                except Exception:
                    pass
        self.rewriter_proc = None
        with self.rewriter_req_lock:
            self.rewriter_pending_responses.clear()
        self.rewriter_turn_results = {}
        self.rewriter_chat_threads = {}
        self._save_agent_rewriter_state()
        try:
            if self.agent_rewriter_pid_file.exists():
                self.agent_rewriter_pid_file.unlink()
        except OSError:
            pass
        self._release_agent_rewriter_lock()
        self._cleanup_agent_rewriter_workspace(reason=reason)

    def _cleanup_agent_rewriter_workspace(self, reason: str) -> None:
        if not self.agent_rewriter_cleanup_tmp:
            return
        workspace = self.agent_rewriter_workspace
        try:
            workspace_resolved = workspace.resolve()
            tmp_root_resolved = self.agent_rewriter_tmp_root.resolve()
            workspace_resolved.relative_to(tmp_root_resolved)
        except Exception:
            return
        if not workspace_resolved.exists():
            return
        try:
            shutil.rmtree(workspace_resolved)
            self._log(
                f"agent-rewriter workspace cleaned reason={reason} path={workspace_resolved}"
            )
        except OSError as exc:
            self._log(
                f"WARN: failed to clean agent-rewriter workspace "
                f"reason={reason} path={workspace_resolved}: {exc}"
            )

    def _sync_agent_rewriter_agents_file(self) -> bool:
        prompt_text = str(self.agent_rewriter_prompt or "").strip()
        if not prompt_text:
            prompt_text = DEFAULT_AGENT_REWRITER_PROMPT
        agents_path = self.agent_rewriter_workspace / REWRITER_AGENTS_FILENAME
        try:
            self.agent_rewriter_workspace.mkdir(parents=True, exist_ok=True)
            normalized = prompt_text.replace("\r\n", "\n").strip()
            agents_path.write_text(normalized + "\n", encoding="utf-8")
            self._secure_file(agents_path)
            return True
        except OSError as exc:
            self._log(f"ERROR: failed to write rewriter AGENTS.md path={agents_path}: {exc}")
            return False

    def _ensure_agent_rewriter(self) -> bool:
        if not self.agent_rewriter_enabled:
            return False
        if self._rewriter_is_running():
            return True
        if self.rewriter_proc is not None and self.rewriter_proc.poll() is not None:
            self._stop_agent_rewriter("agent_rewriter_exited")
        now_epoch = time.time()
        if (now_epoch - self.rewriter_last_restart_try_epoch) < self.agent_rewriter_restart_backoff_sec:
            return False
        self.rewriter_last_restart_try_epoch = now_epoch

        existing_pid = self._read_pid_file(self.agent_rewriter_pid_file)
        if existing_pid > 0 and (self.rewriter_proc is None or existing_pid != int(self.rewriter_proc.pid)):
            if _is_pid_alive(existing_pid):
                if self._is_codex_app_server_pid(existing_pid):
                    self._log(f"agent_rewriter_existing_pid_running pid={existing_pid}; skip duplicate start")
                    return False
                self._log(
                    f"WARN: stale rewriter pid file points to non app-server process pid={existing_pid}; clearing"
                )
            try:
                self.agent_rewriter_pid_file.unlink()
            except OSError:
                pass

        if not self._acquire_agent_rewriter_lock():
            return False

        cmd = self._build_codex_app_server_cmd(role="agent-rewriter")
        rewriter_env = self.env.copy()
        rewriter_env["SONOLBOT_AGENT_REWRITER"] = "1"
        rewriter_env["WORK_DIR"] = str(self.agent_rewriter_workspace)
        rewriter_env["SONOLBOT_ALLOWED_SKILLS"] = ""
        if not self._sync_agent_rewriter_agents_file():
            self._release_agent_rewriter_lock()
            return False
        try:
            self.rewriter_proc = subprocess.Popen(
                cmd,
                cwd=str(self.agent_rewriter_workspace),
                env=rewriter_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except Exception as exc:
            self.rewriter_proc = None
            self._release_agent_rewriter_lock()
            self._log(f"ERROR: failed to start agent-rewriter: {exc}")
            return False

        threading.Thread(target=self._rewriter_stdout_reader, daemon=True).start()
        threading.Thread(target=self._rewriter_stderr_reader, daemon=True).start()
        try:
            self.agent_rewriter_pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.agent_rewriter_pid_file.write_text(str(self.rewriter_proc.pid), encoding="utf-8")
            self._secure_file(self.agent_rewriter_pid_file)
        except OSError:
            pass

        init_result = self._rewriter_request(
            "initialize",
            {"clientInfo": {"name": "sonolbot-agent-rewriter", "version": "1.0"}, "capabilities": {}},
            timeout_sec=15.0,
        )
        if init_result is None:
            self._log("ERROR: agent-rewriter initialize failed")
            self._stop_agent_rewriter("initialize_failed")
            return False
        self._rewriter_notify("initialized")
        self._log(f"agent-rewriter started pid={self.rewriter_proc.pid} listen={self.app_server_listen}")
        return True

    def _agent_rewriter_attach_or_create_thread(self, chat_id: int) -> str:
        thread_id = str(self.rewriter_chat_threads.get(int(chat_id)) or "").strip()
        if thread_id:
            resumed = self._rewriter_request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "cwd": str(self.agent_rewriter_workspace),
                    "approvalPolicy": self.app_server_approval_policy,
                    "sandbox": self.app_server_sandbox,
                    "model": self.agent_rewriter_model,
                },
                timeout_sec=12.0,
            )
            if resumed is not None:
                return thread_id
            self.rewriter_chat_threads.pop(int(chat_id), None)
            self._save_agent_rewriter_state()

        started = self._rewriter_request(
            "thread/start",
            {
                "cwd": str(self.agent_rewriter_workspace),
                "approvalPolicy": self.app_server_approval_policy,
                "sandbox": self.app_server_sandbox,
                "model": self.agent_rewriter_model,
            },
            timeout_sec=12.0,
        )
        if started is None:
            return ""
        thread = started.get("thread")
        if not isinstance(thread, dict):
            return ""
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            return ""
        self.rewriter_chat_threads[int(chat_id)] = thread_id
        self._save_agent_rewriter_state()
        self._log(f"agent-rewriter thread started chat_id={chat_id} thread_id={thread_id}")
        return thread_id

    def _rewrite_agent_message(
        self,
        chat_id: int,
        state: dict[str, Any],
        raw_text: str,
        fallback_to_raw: bool = False,
    ) -> str:
        raw = str(raw_text or "").strip()
        if not raw:
            return ""
        if not self.agent_rewriter_enabled:
            return raw

        attempts = max(1, int(self.agent_rewriter_max_retry) + 1)
        for attempt in range(1, attempts + 1):
            if not self._ensure_agent_rewriter():
                continue
            thread_id = self._agent_rewriter_attach_or_create_thread(chat_id=chat_id)
            if not thread_id:
                continue

            payload = {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": self._build_agent_rewriter_input(chat_id=chat_id, state=state, raw_text=raw),
                    }
                ],
                "model": self.agent_rewriter_model,
                "effort": self.agent_rewriter_reasoning_effort,
                "approvalPolicy": self.app_server_approval_policy,
            }
            started = self._rewriter_request("turn/start", payload, timeout_sec=self.agent_rewriter_request_timeout_sec)
            if started is None:
                continue
            turn = started.get("turn")
            turn_id = ""
            if isinstance(turn, dict):
                turn_id = str(turn.get("id") or "").strip()
            if not turn_id:
                turn_id = str(started.get("turnId") or "").strip()
            if not turn_id:
                continue

            result = self._rewriter_wait_turn_result(turn_id=turn_id, timeout_sec=self.agent_rewriter_timeout_sec)
            if not isinstance(result, dict):
                continue
            rewritten = self._normalize_agent_rewriter_output(str(result.get("text") or ""))
            if not rewritten:
                continue
            if self._contains_internal_agent_text(rewritten):
                self._log(
                    f"WARN: agent-rewriter output contained internal terms chat_id={chat_id} "
                    f"attempt={attempt}/{attempts}"
                )
                continue
            return rewritten

        if fallback_to_raw:
            return raw
        return self._build_agent_rewriter_fallback(chat_id=chat_id, state=state, raw_text=raw)

    def _app_is_running(self) -> bool:
        return self._app_channel.is_running()

    def _app_send_json(self, payload: dict[str, Any]) -> bool:
        return self._app_channel.send_json(payload)

    def _app_notify(self, method: str, params: dict[str, Any] | None = None) -> bool:
        return self._app_channel.notify(method, params)

    def _app_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any] | None:
        return self._app_channel.request(method, params, timeout_sec)

    def _resolve_tool_user_input_answers(self, params: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        questions = params.get("questions")
        if isinstance(questions, list):
            for q in questions:
                if not isinstance(q, dict):
                    continue
                qid = str(q.get("id") or "").strip()
                if not qid:
                    continue
                selected = ""
                opts = q.get("options")
                if isinstance(opts, list) and opts:
                    first = opts[0]
                    if isinstance(first, dict):
                        selected = str(first.get("label") or "").strip()
                    else:
                        selected = str(first).strip()
                if not selected:
                    selected = "확인"
                answers[qid] = {"answers": [selected]}
        return {"answers": answers}

    def _app_handle_server_request(self, request_obj: dict[str, Any]) -> None:
        self._app_channel.handle_server_request(request_obj)

    def _app_dispatch_incoming(self, line: str) -> None:
        self._app_channel.dispatch_incoming(line)

    def _app_stdout_reader(self) -> None:
        self._app_channel.stdout_reader()

    def _app_stderr_reader(self) -> None:
        self._app_channel.stderr_reader()

    def _stop_app_server(self, reason: str) -> None:
        if self.app_proc is not None:
            self._log(f"Stopping app-server (reason={reason}, pid={self.app_proc.pid})")
            try:
                self.app_proc.terminate()
                self.app_proc.wait(timeout=3)
            except Exception:
                try:
                    self.app_proc.kill()
                except Exception:
                    pass
        self.app_proc = None
        with self.app_req_lock:
            self.app_pending_responses.clear()
        self.app_turn_to_chat.clear()
        self.app_aux_turn_results = {}
        for state in self.app_chat_states.values():
            state["active_turn_id"] = ""
            state["active_message_ids"] = set()
            state["active_task_ids"] = set()
            state["delta_text"] = ""
            state["final_text"] = ""
            state["last_agent_message_sent"] = ""
            state["last_agent_message_raw"] = ""
            state["last_progress_len"] = 0
            state["last_progress_sent_at"] = 0.0
            state["last_lease_heartbeat_at"] = 0.0
        self._release_owned_chat_leases(reason=f"app_server_stop:{reason}")
        try:
            if self.codex_pid_file.exists():
                self.codex_pid_file.unlink()
        except OSError:
            pass
        self._release_app_server_lock()
        self._stop_agent_rewriter(f"app_server_stop:{reason}")
        if self.codex_run_meta and str(self.codex_run_meta.get("mode") or "").strip() == "app_server":
            self.codex_run_meta["app_server_pid"] = 0
            self.codex_run_meta["current_thread_id"] = ""
            self.codex_run_meta["thread_id"] = ""
            self.codex_run_meta["session_id"] = ""
            self._sync_app_server_session_meta()

    def _ensure_app_server(self) -> bool:
        if self._app_is_running():
            return True
        if self.app_proc is not None and self.app_proc.poll() is not None:
            self._stop_app_server("app_server_exited")
        now_epoch = time.time()
        if (now_epoch - self.app_last_restart_try_epoch) < self.app_server_restart_backoff_sec:
            return False
        self.app_last_restart_try_epoch = now_epoch

        existing_pid = self._read_pid_file(self.codex_pid_file)
        if existing_pid > 0 and (self.app_proc is None or existing_pid != int(self.app_proc.pid)):
            if _is_pid_alive(existing_pid):
                if self._is_codex_app_server_pid(existing_pid):
                    self._log(f"app_server_existing_pid_running pid={existing_pid}; skip duplicate start")
                    return False
                self._log(
                    f"WARN: stale codex pid file points to non app-server process pid={existing_pid}; clearing"
                )
            try:
                self.codex_pid_file.unlink()
            except OSError:
                pass

        if not self._acquire_app_server_lock():
            return False

        cmd = self._build_codex_app_server_cmd(role="app-server")
        try:
            self.app_proc = subprocess.Popen(
                cmd,
                cwd=str(self.codex_work_dir),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except Exception as exc:
            self.app_proc = None
            self._release_app_server_lock()
            self._log(f"ERROR: failed to start app-server: {exc}")
            return False

        self.app_proc_generation += 1
        threading.Thread(target=self._app_stdout_reader, daemon=True).start()
        threading.Thread(target=self._app_stderr_reader, daemon=True).start()
        try:
            self.codex_pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.codex_pid_file.write_text(str(self.app_proc.pid), encoding="utf-8")
            self._secure_file(self.codex_pid_file)
        except OSError:
            pass

        init_result = self._app_request(
            "initialize",
            {"clientInfo": {"name": "sonolbot-daemon", "version": "1.0"}, "capabilities": {}},
            timeout_sec=20.0,
        )
        if init_result is None:
            self._log("ERROR: app-server initialize failed")
            self._stop_app_server("initialize_failed")
            return False
        self._app_notify("initialized")
        self.codex_cli_version = self._detect_codex_cli_version()
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-app_server"
        self.codex_run_meta = {
            "run_id": run_id,
            "mode": "app_server",
            "started_at": started_at,
            "started_epoch": time.time(),
            "codex_cli_version": self.codex_cli_version,
            "model": self.codex_model,
            "reasoning_effort": self.codex_reasoning_effort,
            "resume_target": "",
            "session_id": "",
            "session_id_kind": "thread_id_alias",
            "thread_id": "",
            "transport": "app_server",
            "listen": self.app_server_listen,
            "app_server_generation": self.app_proc_generation,
            "app_server_pid": self.app_proc.pid if self.app_proc else 0,
            "sessions": {},
            "thread_ids_by_chat": {},
        }
        self._sync_codex_runtime_env(
            run_id=run_id,
            mode="app_server",
            started_at=started_at,
            resume_target="",
            session_id="",
        )
        self._sync_app_server_session_meta()
        self._log(f"app-server started pid={self.app_proc.pid} listen={self.app_server_listen}")
        return True

    def _app_attach_or_create_thread(self, chat_id: int) -> str:
        state = self._get_chat_state(chat_id)
        thread_id = str(state.get("thread_id") or "").strip()
        if bool(state.get("force_new_thread_once")) and thread_id:
            self.app_thread_to_chat.pop(thread_id, None)
            state["thread_id"] = ""
            thread_id = ""
            self._save_app_server_state()
        if not thread_id and not bool(state.get("force_new_thread_once")):
            recovered_thread_id = self._recover_latest_thread_id_for_chat(chat_id=chat_id)
            if recovered_thread_id:
                state["thread_id"] = recovered_thread_id
                state["app_generation"] = 0
                thread_id = recovered_thread_id
                self._save_app_server_state()
                self._sync_app_server_session_meta(active_chat_id=chat_id)
                self._log(
                    f"app-server thread recovered chat_id={chat_id} thread_id={recovered_thread_id}"
                )
        attached_generation = int(state.get("app_generation") or 0)
        needs_attach = attached_generation != self.app_proc_generation
        task_agents_instructions = self._load_task_agents_developer_instructions(chat_id=chat_id, state=state)

        if thread_id and needs_attach:
            resume_payload: dict[str, Any] = {
                "threadId": thread_id,
                "cwd": str(self.codex_work_dir),
                "approvalPolicy": self.app_server_approval_policy,
                "sandbox": self.app_server_sandbox,
                "model": self.codex_model,
            }
            if task_agents_instructions:
                resume_payload["developerInstructions"] = task_agents_instructions
            resumed = self._app_request(
                "thread/resume",
                resume_payload,
            )
            if resumed is None:
                thread_id = ""
                state["thread_id"] = ""
                self._save_app_server_state()
            else:
                state["app_generation"] = self.app_proc_generation
                self.app_thread_to_chat[thread_id] = chat_id
                self._save_app_server_state()
                self._sync_app_server_session_meta(active_chat_id=chat_id)
                return thread_id

        if not thread_id:
            start_payload: dict[str, Any] = {
                "cwd": str(self.codex_work_dir),
                "approvalPolicy": self.app_server_approval_policy,
                "sandbox": self.app_server_sandbox,
                "model": self.codex_model,
            }
            if task_agents_instructions:
                start_payload["developerInstructions"] = task_agents_instructions
            started = self._app_request(
                "thread/start",
                start_payload,
            )
            if started is None:
                return ""
            thread = started.get("thread")
            if not isinstance(thread, dict):
                return ""
            thread_id = str(thread.get("id") or "").strip()
            if not thread_id:
                return ""
            state["thread_id"] = thread_id
            state["app_generation"] = self.app_proc_generation
            self.app_thread_to_chat[thread_id] = chat_id
            self._save_app_server_state()
            self._sync_app_server_session_meta(active_chat_id=chat_id)
            self._log(f"app-server thread started chat_id={chat_id} thread_id={thread_id}")
        return thread_id

    def _group_pending_by_chat(self, messages: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in messages:
            try:
                chat_id = int(item.get("chat_id"))
                msg_id = int(item.get("message_id"))
            except Exception:
                continue
            normalized = dict(item)
            normalized["chat_id"] = chat_id
            normalized["message_id"] = msg_id
            grouped.setdefault(chat_id, []).append(normalized)
        for chat_id in grouped:
            grouped[chat_id].sort(key=lambda row: int(row.get("message_id", 0)))
        return grouped

    def _build_turn_text(
        self,
        messages: list[dict[str, Any]],
        steering: bool,
        task_packet: str = "",
        selected_task_packet: str = "",
        resume_recent_chat_summary: str = "",
        carryover_summary: str = "",
    ) -> str:
        parts: list[str] = []
        if steering:
            parts.append("추가 지시사항:")
        if carryover_summary:
            parts.append("이전 대화 핵심 요약:\n" + carryover_summary)
        if selected_task_packet:
            parts.append(selected_task_packet)
        if resume_recent_chat_summary:
            parts.append("현재 챗 최근 대화 요약:\n" + resume_recent_chat_summary)
        body = self._build_dynamic_request_line(messages)
        if task_packet:
            body = body + "\n\n작업 메모리 요약:\n" + task_packet
        parts.append(body)
        return "\n\n".join(part for part in parts if str(part).strip())

    def _collect_new_messages_for_chat(
        self,
        chat_id: int,
        state: dict[str, Any],
        pending_chat_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        active_ids: set[int] = state.get("active_message_ids") or set()
        queued_messages: list[dict[str, Any]] = state.get("queued_messages") or []
        queued_ids = {int(item.get("message_id")) for item in queued_messages if isinstance(item, dict)}
        failed_ids: set[int] = state.get("failed_reply_ids") or set()
        now_epoch = time.time()

        new_items: list[dict[str, Any]] = []
        for item in pending_chat_messages:
            msg_id = int(item.get("message_id", 0))
            if msg_id in active_ids or msg_id in queued_ids or msg_id in failed_ids:
                continue
            if self._is_message_recently_completed(msg_id, now_epoch=now_epoch):
                age_sec = self._recently_completed_message_age_sec(msg_id, now_epoch=now_epoch)
                if age_sec >= 0.0:
                    self._log_recently_completed_drop(chat_id, msg_id, age_sec)
                continue
            new_items.append(item)
        return new_items

    def _app_start_turn_for_chat(self, chat_id: int, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return False
        state = self._get_chat_state(chat_id)
        now_epoch = time.time()
        filtered_batch: list[dict[str, Any]] = []
        dropped_recent_count = 0
        for item in batch:
            msg_id = int(item.get("message_id", 0))
            if msg_id <= 0:
                continue
            if self._is_message_recently_completed(msg_id, now_epoch=now_epoch):
                age_sec = self._recently_completed_message_age_sec(msg_id, now_epoch=now_epoch)
                if age_sec >= 0.0:
                    self._log_recently_completed_drop(chat_id, msg_id, age_sec)
                dropped_recent_count += 1
                continue
            filtered_batch.append(item)
        if dropped_recent_count > 0:
            self._log(
                "turn_start_completed_cache_filter "
                f"chat_id={chat_id} dropped={dropped_recent_count} total={len(batch)}"
            )
        if not filtered_batch:
            return False
        batch = filtered_batch
        batch_message_ids = {
            int(item.get("message_id", 0))
            for item in batch
            if int(item.get("message_id", 0)) > 0
        }
        if not batch_message_ids:
            return False
        if not self._chat_lease_try_acquire(chat_id=chat_id, message_ids=batch_message_ids):
            self._log(f"chat_turn_start_skipped_due_to_lease chat_id={chat_id} batch={len(batch)}")
            return False

        started = False
        self._apply_selected_task_thread_target(chat_id=chat_id, state=state)
        thread_id = self._app_attach_or_create_thread(chat_id)
        if not thread_id:
            self._chat_lease_release(chat_id, reason="start_failed_thread")
            return False
        task_packet = self._task_prepare_batch(chat_id=chat_id, state=state, messages=batch, thread_id=thread_id)
        selected_task_packet = str(state.get("selected_task_packet") or "").strip()
        resume_recent_chat_summary = ""
        if bool(state.get("resume_context_inject_once")):
            resume_recent_chat_summary = str(state.get("resume_recent_chat_summary_once") or "").strip()
        elif self.always_inject_recent_summary:
            exclude_msg_id = max(batch_message_ids) if batch_message_ids else None
            try:
                resume_recent_chat_summary = self._build_recent_chat_summary(
                    chat_id=chat_id,
                    hours=self.always_inject_summary_hours,
                    target_lines=self.always_inject_summary_lines,
                    max_chars=self.always_inject_summary_max_chars,
                    exclude_message_id=exclude_msg_id,
                )
            except Exception as exc:
                self._log(f"WARN: auto recent chat summary failed chat_id={chat_id}: {exc}")
                resume_recent_chat_summary = ""
        carryover_summary = str(state.get("pending_new_task_summary") or "").strip()

        payload = {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": self._build_turn_text(
                        batch,
                        steering=False,
                        task_packet=task_packet,
                        selected_task_packet=selected_task_packet,
                        resume_recent_chat_summary=resume_recent_chat_summary,
                        carryover_summary=carryover_summary,
                    ),
                }
            ],
            "model": self.codex_model,
            "effort": self.codex_reasoning_effort,
            "approvalPolicy": self.app_server_approval_policy,
        }
        result = self._app_request("turn/start", payload)
        if result is None:
            self._chat_lease_release(chat_id, reason="start_failed_request")
            return False
        turn = result.get("turn")
        turn_id = ""
        if isinstance(turn, dict):
            turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            turn_id = str(result.get("turnId") or "").strip()
        if not turn_id:
            self._chat_lease_release(chat_id, reason="start_failed_missing_turn")
            return False

        state["active_turn_id"] = turn_id
        state["active_message_ids"] = batch_message_ids
        state["delta_text"] = ""
        state["final_text"] = ""
        state["last_agent_message_sent"] = ""
        state["last_agent_message_raw"] = ""
        state["last_progress_sent_at"] = 0.0
        state["last_progress_len"] = 0
        state["last_turn_started_at"] = time.time()
        state["last_lease_heartbeat_at"] = time.time()
        state["queued_messages"] = []
        state["pending_new_task_summary"] = ""
        state["resume_recent_chat_summary_once"] = ""
        state["resume_context_inject_once"] = False
        state["force_new_thread_once"] = False
        self._clear_temp_task_seed(state)
        self.app_turn_to_chat[turn_id] = chat_id
        self._chat_lease_touch(
            chat_id=chat_id,
            turn_id=turn_id,
            message_ids=batch_message_ids,
        )
        self._sync_app_server_session_meta(active_chat_id=chat_id)
        self._log(
            f"app-server turn started chat_id={chat_id} thread_id={thread_id} "
            f"turn_id={turn_id} batch={len(batch)}"
        )
        started = True
        return started

    def _app_steer_turn_for_chat(self, chat_id: int, new_items: list[dict[str, Any]]) -> bool:
        if not new_items:
            return True
        state = self._get_chat_state(chat_id)
        thread_id = str(state.get("thread_id") or "").strip()
        turn_id = str(state.get("active_turn_id") or "").strip()
        if not thread_id or not turn_id:
            return False
        task_packet = self._task_prepare_batch(chat_id=chat_id, state=state, messages=new_items, thread_id=thread_id)
        selected_task_packet = str(state.get("selected_task_packet") or "").strip()
        payload = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [
                {
                    "type": "text",
                    "text": self._build_turn_text(
                        new_items,
                        steering=True,
                        task_packet=task_packet,
                        selected_task_packet=selected_task_packet,
                    ),
                }
            ],
        }
        result = self._app_request("turn/steer", payload, timeout_sec=20.0)
        if result is None:
            return False
        ack_turn = str(result.get("turnId") or "").strip()
        if ack_turn and ack_turn != turn_id:
            return False
        active_ids: set[int] = state.get("active_message_ids") or set()
        for item in new_items:
            try:
                active_ids.add(int(item.get("message_id")))
            except Exception:
                continue
        state["active_message_ids"] = active_ids
        self._chat_lease_touch(
            chat_id=chat_id,
            turn_id=turn_id,
            message_ids=active_ids,
        )
        self._sync_app_server_session_meta(active_chat_id=chat_id)
        self._log(f"app-server steer accepted chat_id={chat_id} turn_id={turn_id} messages={len(new_items)}")
        return True

    def _app_try_send_final_reply(self, chat_id: int, message_ids: set[int], text: str) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return False
        ordered_ids = sorted(int(v) for v in message_ids if int(v) > 0)
        if not ordered_ids:
            return False

        ok = False
        for attempt in range(1, self.fallback_send_max_attempts + 1):
            try:
                ok = bool(
                    self._telegram_send_text(
                        chat_id=chat_id,
                        text=text,
                        request_max_attempts=1,
                        keyboard_rows=self._main_menu_keyboard_rows(),
                    )
                )
            except Exception as exc:
                self._log(f"WARN: final reply send exception chat={chat_id} attempt={attempt}: {exc}")
                ok = False
            if ok:
                break
            if attempt < self.fallback_send_max_attempts:
                sleep_sec = self.fallback_send_retry_delay_sec * (
                    self.fallback_send_retry_backoff ** (attempt - 1)
                )
                time.sleep(sleep_sec)
        if not ok:
            return False

        try:
            telegram.save_bot_response(
                store_path=str(self.store_file),
                chat_id=chat_id,
                text=text,
                reply_to_message_ids=ordered_ids,
            )
        except Exception as exc:
            self._log(f"WARN: failed to save bot response chat={chat_id}: {exc}")

        try:
            changed = int(telegram.mark_messages_processed(str(self.store_file), ordered_ids))
        except Exception as exc:
            self._log(f"WARN: failed to mark processed chat={chat_id}: {exc}")
            changed = 0
        self._log(
            f"Final reply sent chat_id={chat_id} message_count={len(ordered_ids)} marked={changed}"
        )
        return True

    def _app_try_send_progress(self, chat_id: int, state: dict[str, Any]) -> None:
        if self.app_server_forward_agent_message:
            # Progress snippet mode is disabled when forwarding raw agent_message stream.
            return
        turn_id = str(state.get("active_turn_id") or "").strip()
        if not turn_id:
            return
        delta_text = str(state.get("delta_text") or "")
        if not delta_text.strip():
            return
        now_epoch = time.time()
        last_sent = float(state.get("last_progress_sent_at") or 0.0)
        if (now_epoch - last_sent) < self.app_server_progress_interval_sec:
            return
        last_len = int(state.get("last_progress_len") or 0)
        if len(delta_text) <= last_len:
            return
        active_ids: set[int] = state.get("active_message_ids") or set()
        if not active_ids:
            return
        active_task_ids: set[str] = state.get("active_task_ids") or set()
        if active_task_ids:
            task_prefix = sorted(active_task_ids)[0]
        else:
            thread_id = str(state.get("thread_id") or "").strip()
            if thread_id:
                task_prefix = f"thread_{thread_id}"
            else:
                task_prefix = f"msg_{min(active_ids)}"
        snippet = delta_text[-220:].strip()
        progress_text = f"[진행중] {task_prefix}\n{snippet}"

        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return
        try:
            ok = bool(
                self._telegram_send_text(
                    chat_id=chat_id,
                    text=progress_text,
                    request_max_attempts=1,
                    keyboard_rows=self._main_menu_keyboard_rows(),
                )
            )
        except Exception as exc:
            self._log(f"WARN: progress send failed chat_id={chat_id}: {exc}")
            ok = False
        if ok:
            state["last_progress_sent_at"] = now_epoch
            state["last_progress_len"] = len(delta_text)

    def _app_try_send_agent_message(self, chat_id: int, text: str) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return False
        try:
            return bool(
                self._telegram_send_text(
                    chat_id=chat_id,
                    text=text,
                    request_max_attempts=1,
                    keyboard_rows=self._main_menu_keyboard_rows(),
                )
            )
        except Exception as exc:
            self._log(f"WARN: intermediate agent_message send failed chat_id={chat_id}: {exc}")
            return False

    def _app_finalize_reply_without_resend(self, chat_id: int, message_ids: set[int], text: str) -> bool:
        runtime, telegram = self._get_telegram_runtime_skill()
        if runtime is None or telegram is None:
            return False
        ordered_ids = sorted(int(v) for v in message_ids if int(v) > 0)
        if not ordered_ids:
            return False

        try:
            telegram.save_bot_response(
                store_path=str(self.store_file),
                chat_id=chat_id,
                text=text,
                reply_to_message_ids=ordered_ids,
            )
        except Exception as exc:
            self._log(f"WARN: failed to save streamed bot response chat={chat_id}: {exc}")

        try:
            changed = int(telegram.mark_messages_processed(str(self.store_file), ordered_ids))
        except Exception as exc:
            self._log(f"WARN: failed to mark streamed reply processed chat={chat_id}: {exc}")
            return False
        self._log(
            f"Final reply already streamed chat_id={chat_id} message_count={len(ordered_ids)} marked={changed}"
        )
        return True

    def _app_on_turn_completed(self, thread_id: str, turn: dict[str, Any]) -> None:
        chat_id = self.app_thread_to_chat.get(thread_id)
        if chat_id is None:
            return
        state = self._get_chat_state(chat_id)
        turn_id = str(turn.get("id") or "").strip()
        if turn_id and str(state.get("active_turn_id") or "").strip() and turn_id != str(state.get("active_turn_id")):
            self._log(f"WARN: completed turn mismatch chat_id={chat_id} completed={turn_id} active={state.get('active_turn_id')}")

        status = str(turn.get("status") or "").strip().lower() or "completed"
        final_text = str(state.get("final_text") or "").strip()
        message_ids: set[int] = set(state.get("active_message_ids") or set())
        task_ids: set[str] = set(state.get("active_task_ids") or set())
        if not task_ids and thread_id:
            task_ids = {f"thread_{thread_id}"}
        last_agent_message_sent = str(state.get("last_agent_message_sent") or "")

        sent_ok = False
        if status == "completed" and final_text and message_ids:
            if last_agent_message_sent and last_agent_message_sent == final_text:
                sent_ok = self._app_finalize_reply_without_resend(
                    chat_id=chat_id,
                    message_ids=message_ids,
                    text=final_text,
                )
            else:
                sent_ok = self._app_try_send_final_reply(chat_id=chat_id, message_ids=message_ids, text=final_text)
            if not sent_ok:
                state["failed_reply_text"] = final_text
                state["failed_reply_ids"] = set(message_ids)
                self._log(
                    f"WARN: final reply send deferred chat_id={chat_id} "
                    f"messages={len(message_ids)}"
                )
            else:
                state["failed_reply_text"] = ""
                state["failed_reply_ids"] = set()
        elif status == "completed" and not final_text and message_ids:
            self._log(
                f"WARN: turn completed without task_complete final message chat_id={chat_id} "
                f"messages={len(message_ids)}"
            )
        elif status != "completed":
            self._log(f"WARN: turn completed with non-success status chat_id={chat_id} status={status}")

        self._task_record_batch_change(
            chat_id=chat_id,
            task_ids=task_ids,
            message_ids=message_ids,
            status=status,
            result_text=final_text,
            sent_ok=sent_ok,
        )
        selected_task_id = self._normalize_task_id_token(state.get("selected_task_id"))
        if selected_task_id and thread_id:
            self._bind_task_thread_mapping(
                chat_id=chat_id,
                task_id=selected_task_id,
                thread_id=thread_id,
            )
        if status == "completed" and message_ids:
            self._remember_completed_message_ids(message_ids)
        self._chat_lease_release(chat_id, reason=f"turn_completed:{status or 'completed'}")

        state["active_turn_id"] = ""
        state["active_message_ids"] = set()
        state["active_task_ids"] = set()
        state["delta_text"] = ""
        state["final_text"] = ""
        state["last_agent_message_sent"] = ""
        state["last_agent_message_raw"] = ""
        state["last_progress_len"] = 0
        state["last_progress_sent_at"] = 0.0
        state["last_lease_heartbeat_at"] = 0.0
        if turn_id:
            self.app_turn_to_chat.pop(turn_id, None)
        self._sync_app_server_session_meta(active_chat_id=chat_id)

    def _app_process_notification(self, event: dict[str, Any]) -> None:
        method = str(event.get("method") or "")
        params = event.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "turn/started":
            thread_id = str(params.get("threadId") or "").strip()
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            turn_id = str(turn.get("id") or "").strip()
            chat_id = self.app_thread_to_chat.get(thread_id)
            if chat_id is None:
                return
            state = self._get_chat_state(chat_id)
            if turn_id:
                state["active_turn_id"] = turn_id
                self.app_turn_to_chat[turn_id] = chat_id
                state["last_turn_started_at"] = time.time()
                state["last_lease_heartbeat_at"] = time.time()
                self._chat_lease_touch(
                    chat_id=chat_id,
                    turn_id=turn_id,
                    message_ids=set(state.get("active_message_ids") or set()),
                )
                self._sync_app_server_session_meta(active_chat_id=chat_id)
            return

        if method == "item/agentMessage/delta":
            thread_id = str(params.get("threadId") or "").strip()
            chat_id = self.app_thread_to_chat.get(thread_id)
            if chat_id is None:
                return
            delta = str(params.get("delta") or "")
            if not delta:
                return
            state = self._get_chat_state(chat_id)
            state["delta_text"] = str(state.get("delta_text") or "") + delta
            return

        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                return
            item_type = str(item.get("type") or "").strip().lower()
            if item_type != "agentmessage":
                return
            return

        if method == "codex/event/agent_message":
            if not self.app_server_forward_agent_message:
                return
            msg = params.get("msg")
            if not isinstance(msg, dict):
                return
            raw_message_text = str(msg.get("message") or "")
            if not raw_message_text.strip():
                return
            thread_id = str(params.get("conversationId") or "").strip()
            if not thread_id:
                thread_id = str(msg.get("thread_id") or "").strip()
            chat_id = self.app_thread_to_chat.get(thread_id)
            if chat_id is None:
                turn_id = str(params.get("id") or "").strip()
                if turn_id:
                    chat_id = self.app_turn_to_chat.get(turn_id)
            if chat_id is None:
                return
            state = self._get_chat_state(chat_id)
            if str(state.get("last_agent_message_raw") or "") == raw_message_text:
                return
            message_text = self._rewrite_agent_message(
                chat_id=chat_id,
                state=state,
                raw_text=raw_message_text,
                fallback_to_raw=False,
            )
            if not str(message_text or "").strip():
                return
            sent = self._app_try_send_agent_message(chat_id=chat_id, text=message_text)
            if sent:
                state["last_agent_message_raw"] = raw_message_text
                state["last_agent_message_sent"] = message_text
            return

        if method == "codex/event/task_complete":
            msg = params.get("msg")
            if not isinstance(msg, dict):
                return
            raw_final_text = str(msg.get("last_agent_message") or "").strip()
            if not raw_final_text:
                return
            turn_id = str(msg.get("turn_id") or params.get("id") or "").strip()
            if turn_id:
                aux_state = self.app_aux_turn_results.get(turn_id)
                if isinstance(aux_state, dict):
                    aux_state["status"] = "completed"
                    aux_state["text"] = raw_final_text
                    aux_state["updated_at"] = time.time()
                    self.app_aux_turn_results[turn_id] = aux_state
                    return
            thread_id = str(params.get("conversationId") or "").strip()
            if not thread_id:
                thread_id = str(msg.get("thread_id") or "").strip()
            chat_id = self.app_thread_to_chat.get(thread_id)
            if chat_id is None:
                if turn_id:
                    chat_id = self.app_turn_to_chat.get(turn_id)
            if chat_id is None:
                return
            state = self._get_chat_state(chat_id)
            state["final_text"] = raw_final_text
            return

        if method == "turn/completed":
            thread_id = str(params.get("threadId") or "").strip()
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            aux_turn_id = str(turn.get("id") or "").strip()
            if aux_turn_id:
                aux_state = self.app_aux_turn_results.get(aux_turn_id)
                if isinstance(aux_state, dict):
                    aux_state["status"] = str(turn.get("status") or "").strip().lower() or "completed"
                    aux_state["updated_at"] = time.time()
                    self.app_aux_turn_results[aux_turn_id] = aux_state
                    return
            self._app_on_turn_completed(thread_id, turn)
            return

    def _app_drain_events(self, max_items: int = 400) -> None:
        for _ in range(max_items):
            try:
                event = self.app_event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._app_process_notification(event)
            except Exception as exc:
                self._log(f"WARN: app-server event handling failed: {exc}")

    def _app_retry_failed_replies(self) -> None:
        for chat_id, state in self.app_chat_states.items():
            failed_text = str(state.get("failed_reply_text") or "").strip()
            failed_ids: set[int] = state.get("failed_reply_ids") or set()
            if not failed_text or not failed_ids:
                continue
            ok = self._app_try_send_final_reply(chat_id=chat_id, message_ids=failed_ids, text=failed_text)
            if ok:
                thread_id = str(state.get("thread_id") or "").strip()
                retry_task_ids: set[str] = {f"thread_{thread_id}"} if thread_id else set()
                self._task_record_batch_change(
                    chat_id=chat_id,
                    task_ids=retry_task_ids,
                    message_ids=set(failed_ids),
                    status="completed",
                    result_text=failed_text,
                    sent_ok=True,
                )
                state["failed_reply_text"] = ""
                state["failed_reply_ids"] = set()

    def _app_process_cycle(self) -> None:
        self._prune_completed_message_cache()
        pending_messages = self._snapshot_pending_messages()
        has_stateful_work = self._has_app_stateful_work()
        if not pending_messages and not has_stateful_work and not self._app_is_running():
            return

        if not pending_messages and not has_stateful_work and self._app_is_running():
            if self._is_bot_workspace_idle():
                if self._has_any_active_chat_lease():
                    self._log("idle_shutdown_skipped_active_lease")
                else:
                    self._stop_app_server(f"workspace_idle>{self.idle_timeout_sec}s")
            return

        if self.agent_rewriter_enabled:
            self._ensure_agent_rewriter()
        if not self._ensure_app_server():
            return
        self._app_drain_events()
        self._app_retry_failed_replies()
        # turn/completed 처리에서 processed 마킹이 반영될 수 있으므로
        # pending 스냅샷을 갱신해 같은 메시지의 동일 사이클 재투입을 방지한다.
        pending_messages = self._snapshot_pending_messages()

        grouped = self._group_pending_by_chat(pending_messages)
        chat_ids = set(grouped.keys())
        for chat_id, state in self.app_chat_states.items():
            if (
                str(state.get("active_turn_id") or "").strip()
                or state.get("queued_messages")
                or str(state.get("failed_reply_text") or "").strip()
            ):
                chat_ids.add(chat_id)

        for chat_id in sorted(chat_ids):
            pending_chat_messages = grouped.get(chat_id, [])
            state = self._get_chat_state(chat_id)
            pending_chat_messages = self._process_chat_control_messages(
                chat_id=chat_id,
                state=state,
                pending_chat_messages=pending_chat_messages,
            )
            # Turn timeout guard.
            active_turn = str(state.get("active_turn_id") or "").strip()
            if active_turn:
                started_epoch = float(state.get("last_turn_started_at") or 0.0)
                if started_epoch > 0 and (time.time() - started_epoch) > self.app_server_turn_timeout_sec:
                    thread_id = str(state.get("thread_id") or "").strip()
                    if thread_id:
                        self._log(
                            f"WARN: interrupting stale turn chat_id={chat_id} turn_id={active_turn} "
                            f"timeout={self.app_server_turn_timeout_sec}s"
                        )
                        self._app_request(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": active_turn},
                            timeout_sec=10.0,
                        )
                    state["active_turn_id"] = ""
                    state["active_message_ids"] = set()
                    state["active_task_ids"] = set()
                    state["delta_text"] = ""
                    state["final_text"] = ""
                    state["last_agent_message_sent"] = ""
                    state["last_agent_message_raw"] = ""
                    state["last_lease_heartbeat_at"] = 0.0
                    self._chat_lease_release(chat_id, reason="turn_timeout_interrupt")
                    self._sync_app_server_session_meta(active_chat_id=chat_id)
                    active_turn = ""

            new_items = self._collect_new_messages_for_chat(chat_id, state, pending_chat_messages)
            if str(state.get("active_turn_id") or "").strip():
                last_lease_heartbeat = float(state.get("last_lease_heartbeat_at") or 0.0)
                if (time.time() - last_lease_heartbeat) >= float(self.chat_lease_heartbeat_sec):
                    touched = self._chat_lease_touch(
                        chat_id=chat_id,
                        turn_id=str(state.get("active_turn_id") or ""),
                        message_ids=set(state.get("active_message_ids") or set()),
                    )
                    if touched:
                        state["last_lease_heartbeat_at"] = time.time()
                if not new_items:
                    self._app_try_send_progress(chat_id, state)
                    continue
                if bool(state.get("force_new_thread_once")):
                    queued = list(state.get("queued_messages") or [])
                    queued.extend(new_items)
                    deduped_waiting: list[dict[str, Any]] = []
                    seen_waiting: set[int] = set()
                    for item in queued:
                        try:
                            msg_id = int(item.get("message_id"))
                        except Exception:
                            continue
                        if msg_id in seen_waiting:
                            continue
                        seen_waiting.add(msg_id)
                        deduped_waiting.append(item)
                    state["queued_messages"] = deduped_waiting
                    self._app_try_send_progress(chat_id, state)
                    continue
                # Small coalescing window before steer to reduce call count.
                time.sleep(self.app_server_steer_batch_window_ms / 1000.0)
                steer_ok = self._app_steer_turn_for_chat(chat_id, new_items)
                if not steer_ok:
                    queued = list(state.get("queued_messages") or [])
                    queued.extend(new_items)
                    # de-duplicate queued rows by message id while keeping order.
                    deduped: list[dict[str, Any]] = []
                    seen: set[int] = set()
                    for item in queued:
                        try:
                            msg_id = int(item.get("message_id"))
                        except Exception:
                            continue
                        if msg_id in seen:
                            continue
                        seen.add(msg_id)
                        deduped.append(item)
                    state["queued_messages"] = deduped
                self._app_try_send_progress(chat_id, state)
                continue

            batch = list(state.get("queued_messages") or [])
            if not batch and not new_items:
                continue
            if not batch:
                batch = new_items
            else:
                merged = batch + new_items
                deduped_batch: list[dict[str, Any]] = []
                seen_batch: set[int] = set()
                for item in merged:
                    try:
                        msg_id = int(item.get("message_id"))
                    except Exception:
                        continue
                    if msg_id in seen_batch:
                        continue
                    seen_batch.add(msg_id)
                    deduped_batch.append(item)
                batch = deduped_batch

            started = self._app_start_turn_for_chat(chat_id, batch)
            if not started:
                now_epoch = time.time()
                retry_batch: list[dict[str, Any]] = []
                for item in batch:
                    msg_id = int(item.get("message_id", 0))
                    if msg_id <= 0:
                        continue
                    if self._is_message_recently_completed(msg_id, now_epoch=now_epoch):
                        age_sec = self._recently_completed_message_age_sec(msg_id, now_epoch=now_epoch)
                        if age_sec >= 0.0:
                            self._log_recently_completed_drop(chat_id, msg_id, age_sec)
                        continue
                    retry_batch.append(item)
                state["queued_messages"] = retry_batch
            else:
                state["queued_messages"] = []

    def _handle_signal(self, signum: int, _frame: object) -> None:
        self._log(f"Signal received: {signum}")
        self.stop_requested = True

    def _has_app_stateful_work(self) -> bool:
        return any(
            bool(str(state.get("active_turn_id") or "").strip())
            or bool(state.get("queued_messages"))
            or bool(str(state.get("failed_reply_text") or "").strip())
            for state in self.app_chat_states.values()
        )

    def _workspace_latest_mtime(self) -> float:
        results_dir = Path(
            os.getenv("SONOLBOT_RESULTS_DIR", str(self.bot_workspace / "results"))
        ).resolve()
        roots = [
            self.tasks_dir,
            self.app_server_state_file,
            self.app_server_log_file,
            self.codex_session_meta_file,
            self.activity_file,
            results_dir,
        ]

        latest = 0.0
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            if not root.exists():
                continue
            try:
                latest = max(latest, root.stat().st_mtime)
            except OSError:
                continue
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file() and path.parent == self.logs_dir and path.name.startswith("daemon-"):
                        # Exclude daemon heartbeat logs from idle detector.
                        continue
                    if path == self.store_file or path.name == "telegram_messages.json":
                        # Telegram store can be rewritten by periodic polling even with no real work.
                        continue
                    try:
                        latest = max(latest, path.stat().st_mtime)
                    except OSError:
                        continue
        return latest

    def _is_bot_workspace_idle(self) -> bool:
        if int(self.idle_timeout_sec) <= 0:
            return False
        latest = self._workspace_latest_mtime()
        if latest <= 0:
            return False
        idle_sec = time.time() - latest
        return idle_sec >= float(self.idle_timeout_sec)

    def _run_doc_runtime_check(self) -> None:
        script_path = self.root / "scripts" / "check_docs_alignment.py"
        if not script_path.exists():
            self._log(f"WARN: docs alignment checker missing: {script_path}")
            return
        try:
            proc = subprocess.run(
                [self.python_bin, str(script_path)],
                cwd=str(self.root),
                env=self.env,
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
        except Exception as exc:
            self._log(f"WARN: docs alignment checker execution failed: {exc}")
            return
        if proc.returncode == 0:
            return
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            for line in stdout.splitlines():
                self._log(f"[docs-check] {line}")
        if stderr:
            for line in stderr.splitlines():
                self._log(f"[docs-check][stderr] {line}")

    def _run_main_cycle(self) -> int:
        self._cleanup_logs()
        self._cleanup_activity_logs()
        self._rotate_activity_log_if_needed(force=False)
        rc = self._run_quick_check()
        if rc not in (0, 1):
            self._log(f"quick_check failed rc={rc}")
            return rc

        self._app_process_cycle()
        return rc

    def drain_pending_once(self, max_cycles: int = 120, sleep_sec: float = 1.0, use_lock: bool = True) -> int:
        if not shutil.which("codex"):
            self._log("ERROR: codex CLI not found in PATH")
            return 1

        locked = False
        if use_lock:
            try:
                self._acquire_lock()
                locked = True
            except Exception as exc:
                self._log(f"ERROR: cannot acquire daemon lock for drain mode: {exc}")
                return 1

        try:
            self._run_doc_runtime_check()
            cycles = max(1, int(max_cycles))
            pause = max(0.2, float(sleep_sec))
            for _ in range(cycles):
                rc = self._run_main_cycle()
                if rc not in (0, 1):
                    return rc

                pending = bool(self._snapshot_pending_messages())
                stateful = self._has_app_stateful_work()
                if not pending and not stateful:
                    return 0

                time.sleep(pause)

            self._log(f"WARN: drain mode reached max_cycles={cycles} before idle")
            return 1
        finally:
            self._stop_app_server("drain_mode_done")
            if locked:
                self._release_lock()

    def _start_discord_relay(self) -> None:
        """Discord relay 프로세스를 시작합니다 (SONOLBOT_PLATFORM=discord 시 호출)."""
        relay_script = self.root / "discord_relay.py"
        if not relay_script.exists():
            self._log("WARN: discord_relay.py not found, Discord relay not started")
            return
        discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not discord_token:
            self._log("WARN: DISCORD_BOT_TOKEN not set, Discord relay not started")
            return
        try:
            proc = subprocess.Popen(
                [sys.executable, str(relay_script)],
                cwd=str(self.root),
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
            self._discord_relay_proc = proc
            self._log(f"Discord relay started pid={proc.pid}")
        except Exception as exc:
            self._log(f"WARN: failed to start Discord relay: {exc}")

    def _stop_discord_relay(self) -> None:
        """Discord relay 프로세스를 종료합니다."""
        proc = getattr(self, "_discord_relay_proc", None)
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._discord_relay_proc = None
        self._log("Discord relay stopped")

    def run(self) -> int:
        ai_provider = os.getenv("SONOLBOT_AI_PROVIDER", "codex").strip().lower()
        if ai_provider != "claude" and not shutil.which("codex"):
            self._log("ERROR: codex CLI not found in PATH (set SONOLBOT_AI_PROVIDER=claude to use Claude instead)")
            return 1

        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self._acquire_lock()
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            return 1
        self._log(
            "Daemon started "
            f"pid={os.getpid()} poll={self.poll_interval_sec}s idle_timeout={self.idle_timeout_sec}s "
            f"worker={self.is_bot_worker} bot_id={self.bot_id or '-'} "
            f"platform={self.platform} ai_provider={ai_provider} "
            f"gui_session={self._has_gui_session()} "
            f"transport={DEFAULT_CODEX_TRANSPORT_MODE} "
            f"forward_agent_message={self.app_server_forward_agent_message} "
            f"telegram_parse_mode={self.telegram_default_parse_mode} "
            f"telegram_force_parse={self.telegram_force_parse_mode} "
            f"telegram_parse_fallback_raw={self.telegram_parse_fallback_raw_on_fail} "
            f"codex_model={self.codex_model} reasoning={self.codex_reasoning_effort} "
            f"rewriter_enabled={self.agent_rewriter_enabled} "
            f"rewriter_model={self.agent_rewriter_model} "
            f"rewriter_reasoning={self.agent_rewriter_reasoning_effort} "
            f"rewriter_workspace={self.agent_rewriter_workspace} "
            f"rewriter_prompt_file={self.agent_rewriter_prompt_file or '-'} "
            f"rewriter_cleanup_tmp={self.agent_rewriter_cleanup_tmp} "
            f"activity_max={self.activity_max_bytes} activity_backups={self.activity_backup_count} "
            f"activity_retention={self.activity_retention_days}d "
            f"tasks_partition_by_chat={self.tasks_partition_by_chat}"
        )
        self._run_doc_runtime_check()

        # Discord 플랫폼이면 relay 프로세스 시작
        if self.platform == "discord":
            self._start_discord_relay()

        try:
            while not self.stop_requested:
                self._run_main_cycle()
                time.sleep(max(1, self.poll_interval_sec))
        finally:
            self._stop_app_server("daemon_shutdown")
            if self.platform == "discord":
                self._stop_discord_relay()
            self._release_lock()
            self._log("Daemon stopped")
        return 0


class MultiBotManager:
    """Root daemon manager that spawns one bot worker per configured token."""

    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent
        self.logs_dir = Path(os.getenv("LOGS_DIR", str(self.root / "logs"))).resolve()
        self.pid_file = Path(
            os.getenv("DAEMON_PID_FILE", str(self.root / ".daemon_service.pid"))
        ).resolve()
        self.lock_file = Path(
            os.getenv("DAEMON_LOCK_FILE", str(self.pid_file.with_suffix(".lock")))
        ).resolve()
        self.poll_interval_sec = max(1, int(os.getenv("DAEMON_POLL_INTERVAL_SEC", "1")))
        self.log_retention_days = max(1, int(os.getenv("LOG_RETENTION_DAYS", "7")))
        self.workspace_root = Path(
            os.getenv("SONOLBOT_BOT_WORKSPACES_DIR", str(self.root / DEFAULT_BOT_WORKSPACE_DIRNAME))
        ).resolve()
        self.config_path = default_config_path(self.root)
        self.python_bin = self._detect_python_bin()
        self.stop_requested = False
        self.workers: dict[str, dict[str, Any]] = {}
        self.worker_restart_state: dict[str, dict[str, Any]] = {}
        self.worker_restart_base_sec = self._env_float(
            "DAEMON_WORKER_RESTART_BASE_SEC",
            DEFAULT_WORKER_RESTART_BASE_SEC,
            minimum=1.0,
        )
        self.worker_restart_max_sec = self._env_float(
            "DAEMON_WORKER_RESTART_MAX_SEC",
            DEFAULT_WORKER_RESTART_MAX_SEC,
            minimum=5.0,
        )
        self.worker_stable_reset_sec = self._env_float(
            "DAEMON_WORKER_STABLE_RESET_SEC",
            DEFAULT_WORKER_STABLE_RESET_SEC,
            minimum=5.0,
        )
        self._process_lock: _ProcessFileLock | None = None
        self.env = os.environ.copy()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def _detect_python_bin(self) -> str:
        venv_py = self.root / ".venv" / "bin" / "python"
        if venv_py.exists():
            return str(venv_py)
        return sys.executable

    def _daily_log_path(self) -> Path:
        return self.logs_dir / f"daemon-{datetime.now().strftime('%Y-%m-%d')}.log"

    def _env_float(self, name: str, default: float, minimum: float = 0.0) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return max(minimum, default)
        try:
            return max(minimum, float(raw))
        except ValueError:
            return max(minimum, default)

    def _log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [manager] {message}\n"
        log_path = self._daily_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _cleanup_logs(self) -> None:
        cutoff = datetime.now().date() - timedelta(days=self.log_retention_days - 1)
        for path in self.logs_dir.glob("*.log"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _acquire_lock(self) -> None:
        if self._process_lock is None:
            self._process_lock = _ProcessFileLock(
                lock_file=self.lock_file,
                pid_file=self.pid_file,
                owner_label="Daemon manager",
            )
        self._process_lock.acquire()

    def _release_lock(self) -> None:
        if self._process_lock is not None:
            self._process_lock.release()
            self._process_lock = None

    def _handle_signal(self, signum: int, _frame: object) -> None:
        self._log(f"Signal received: {signum}")
        self.stop_requested = True

    @staticmethod
    def _safe_bot_key(bot_id: str) -> str:
        val = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(bot_id or "").strip())
        return val or "unknown"

    def _workspace_for_bot(self, bot_id: str) -> Path:
        return (self.workspace_root / self._safe_bot_key(bot_id)).resolve()

    def _load_active_bots(self) -> list[dict[str, Any]]:
        cfg = load_bots_config(self.config_path)
        allowed_users = cfg.get("allowed_users_global")
        if not isinstance(allowed_users, list):
            allowed_users = []
        normalized_allowed = []
        for item in allowed_users:
            try:
                uid = int(item)
            except Exception:
                continue
            if uid > 0:
                normalized_allowed.append(uid)
        out: list[dict[str, Any]] = []
        bots = cfg.get("bots")
        if not isinstance(bots, list):
            return out
        for row in bots:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("active", False)):
                continue
            token = str(row.get("token") or "").strip()
            bot_id = str(row.get("bot_id") or "").strip()
            if not token or not bot_id:
                continue
            if not normalized_allowed:
                # Global allowed-users is required by current telegram skill contract.
                continue
            out.append(
                {
                    "bot_id": bot_id,
                    "token": token,
                    "bot_username": str(row.get("bot_username") or "").strip(),
                    "bot_name": str(row.get("bot_name") or "").strip(),
                    "allowed_users_global": normalized_allowed,
                }
            )
        return out

    def _worker_env(self, bot: dict[str, Any], workspace: Path) -> dict[str, str]:
        env = self.env.copy()
        allowed_users = bot.get("allowed_users_global") or []
        allowed_raw = ",".join(str(int(v)) for v in allowed_users if int(v) > 0)

        logs_dir = workspace / "logs"
        tasks_dir = workspace / "tasks"
        messages_dir = workspace / "messages"
        state_dir = workspace / "state"
        results_dir = workspace / "results"
        for path in (logs_dir, tasks_dir, messages_dir, state_dir, results_dir):
            path.mkdir(parents=True, exist_ok=True)
        rewriter_tmp_root = Path(
            os.getenv("DAEMON_AGENT_REWRITER_TMP_ROOT", DEFAULT_AGENT_REWRITER_TMP_ROOT)
        ).expanduser().resolve()
        rewriter_workspace = rewriter_tmp_root / self._safe_bot_key(str(bot["bot_id"]))
        try:
            rewriter_workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(
                f"WARN: failed to create tmp rewriter workspace bot_id={bot.get('bot_id')} "
                f"path={rewriter_workspace}: {exc}; fallback to state dir"
            )
            rewriter_workspace = state_dir / "agent-rewriter-workspace"
            rewriter_workspace.mkdir(parents=True, exist_ok=True)

        env["DAEMON_BOT_WORKER"] = "1"
        env["SONOLBOT_MULTI_BOT_MANAGER"] = "0"
        env["SONOLBOT_BOT_ID"] = str(bot["bot_id"])
        env["SONOLBOT_BOT_WORKSPACE"] = str(workspace)
        env["SONOLBOT_BOTS_CONFIG"] = str(self.config_path)

        # 플랫폼 및 AI 프로바이더 전파
        env["SONOLBOT_PLATFORM"] = str(bot.get("platform", "telegram"))
        if bot.get("platform") == "discord":
            env["DISCORD_BOT_TOKEN"] = str(bot.get("discord_token", ""))
            env["DISCORD_ALLOWED_USERS"] = str(bot.get("discord_allowed_users", allowed_raw))
            env["DISCORD_MESSAGE_STORE"] = str(messages_dir / "discord_messages.json")
            env.pop("TELEGRAM_BOT_TOKEN", None)
        else:
            env["TELEGRAM_BOT_TOKEN"] = str(bot["token"])
        env["TELEGRAM_ALLOWED_USERS"] = allowed_raw
        env["TELEGRAM_USER_ID"] = str(allowed_users[0]) if allowed_users else ""

        env["WORK_DIR"] = str(workspace)
        env["LOGS_DIR"] = str(logs_dir)
        env["TASKS_DIR"] = str(tasks_dir)
        env["TELEGRAM_TASKS_DIR"] = str(tasks_dir)
        env["TELEGRAM_LOGS_DIR"] = str(logs_dir)
        env["TASKS_LOGS_DIR"] = str(logs_dir)
        env["TELEGRAM_MESSAGE_STORE"] = str(messages_dir / "telegram_messages.json")
        env["DAEMON_ACTIVITY_FILE"] = str(logs_dir / "codex-app-server.log")
        env["DAEMON_APP_SERVER_LOG_FILE"] = str(logs_dir / "codex-app-server.log")
        env["DAEMON_APP_SERVER_STATE_FILE"] = str(state_dir / "codex-app-session-state.json")
        env["DAEMON_AGENT_REWRITER_TMP_ROOT"] = str(rewriter_tmp_root)
        env["DAEMON_AGENT_REWRITER_WORKSPACE"] = str(rewriter_workspace)
        env["DAEMON_AGENT_REWRITER_PID_FILE"] = str(state_dir / "codex-agent-rewriter.pid")
        env["DAEMON_AGENT_REWRITER_STATE_FILE"] = str(state_dir / "codex-agent-rewriter-state.json")
        env["DAEMON_AGENT_REWRITER_LOG_FILE"] = str(logs_dir / "codex-agent-rewriter.log")
        env["DAEMON_AGENT_REWRITER_LOCK_FILE"] = str(state_dir / "agent-rewriter.lock")
        env["DAEMON_PID_FILE"] = str(state_dir / "daemon-worker.pid")
        env["DAEMON_LOCK_FILE"] = str(state_dir / "daemon-worker.lock")
        env["CODEX_PID_FILE"] = str(state_dir / "codex-app-server.pid")
        env["SONOLBOT_TASKS_PARTITION_BY_CHAT"] = "1"
        return env

    def _spawn_worker(self, bot: dict[str, Any]) -> None:
        bot_id = str(bot["bot_id"])
        workspace = self._workspace_for_bot(bot_id)
        env = self._worker_env(bot, workspace)
        cmd = [self.python_bin, str(self.root / "daemon_service.py")]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=(os.name != "nt"),
            )
        except Exception as exc:
            self._log(f"ERROR: failed to spawn worker bot_id={bot_id}: {exc}")
            return
        self.workers[bot_id] = {
            "proc": proc,
            "workspace": workspace,
            "token": str(bot["token"]),
            "started_at": time.time(),
        }
        state = self.worker_restart_state.setdefault(bot_id, {})
        state["last_spawn_at"] = time.time()
        self._log(f"worker started bot_id={bot_id} pid={proc.pid} workspace={workspace}")

    def _stop_worker(self, bot_id: str, reason: str) -> None:
        slot = self.workers.pop(bot_id, None)
        if not slot:
            return
        proc = slot.get("proc")
        if not isinstance(proc, subprocess.Popen):
            return
        self._log(f"worker stopping bot_id={bot_id} pid={proc.pid} reason={reason}")
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _register_worker_exit(self, bot_id: str, rc: int, runtime_sec: float) -> None:
        state = self.worker_restart_state.setdefault(bot_id, {})
        fail_count = int(state.get("fail_count") or 0)
        should_reset = (rc == 0) and (runtime_sec >= self.worker_stable_reset_sec)
        if should_reset:
            fail_count = 0
            next_start_at = 0.0
            backoff_sec = 0.0
        else:
            fail_count += 1
            backoff_sec = min(
                self.worker_restart_max_sec,
                self.worker_restart_base_sec * (2 ** max(0, fail_count - 1)),
            )
            next_start_at = time.time() + backoff_sec

        state["fail_count"] = fail_count
        state["next_start_at"] = next_start_at
        state["last_exit_rc"] = int(rc)
        state["last_exit_at"] = time.time()
        state["last_skip_log_at"] = 0.0
        if backoff_sec > 0:
            self._log(
                f"worker restart delayed bot_id={bot_id} rc={rc} runtime={runtime_sec:.1f}s "
                f"backoff={backoff_sec:.1f}s failures={fail_count}"
            )

    def _can_start_worker_now(self, bot_id: str) -> bool:
        state = self.worker_restart_state.get(bot_id) or {}
        next_start_at = float(state.get("next_start_at") or 0.0)
        now = time.time()
        if next_start_at <= 0 or now >= next_start_at:
            return True

        remaining = max(0.0, next_start_at - now)
        last_skip_log_at = float(state.get("last_skip_log_at") or 0.0)
        if (now - last_skip_log_at) >= max(5.0, float(self.poll_interval_sec)):
            state["last_skip_log_at"] = now
            self.worker_restart_state[bot_id] = state
            self._log(
                f"worker start skipped due to backoff bot_id={bot_id} "
                f"remaining={remaining:.1f}s"
            )
        return False

    def _sync_workers(self) -> None:
        active = self._load_active_bots()
        active_map = {str(row["bot_id"]): row for row in active}

        # Stop removed/inactive workers.
        for bot_id in list(self.workers.keys()):
            if bot_id not in active_map:
                self._stop_worker(bot_id, "inactive_or_removed")
                self.worker_restart_state.pop(bot_id, None)

        # Reap dead workers.
        for bot_id, slot in list(self.workers.items()):
            proc = slot.get("proc")
            if not isinstance(proc, subprocess.Popen):
                continue
            rc = proc.poll()
            if rc is None:
                started_at = float(slot.get("started_at") or 0.0)
                if started_at > 0 and (time.time() - started_at) >= self.worker_stable_reset_sec:
                    state = self.worker_restart_state.get(bot_id)
                    if state and int(state.get("fail_count") or 0) > 0:
                        state["fail_count"] = 0
                        state["next_start_at"] = 0.0
                        self.worker_restart_state[bot_id] = state
                continue
            self.workers.pop(bot_id, None)
            started_at = float(slot.get("started_at") or 0.0)
            runtime_sec = max(0.0, time.time() - started_at) if started_at > 0 else 0.0
            self._log(f"worker exited bot_id={bot_id} rc={rc} runtime={runtime_sec:.1f}s")
            self._register_worker_exit(bot_id=bot_id, rc=int(rc), runtime_sec=runtime_sec)

        # Start missing workers.
        for bot_id, bot in active_map.items():
            if bot_id in self.workers:
                # Token changed: restart worker with new env.
                old_token = str(self.workers[bot_id].get("token") or "")
                new_token = str(bot.get("token") or "")
                if old_token != new_token:
                    self._stop_worker(bot_id, "token_changed")
                    if not self._can_start_worker_now(bot_id):
                        continue
                    self._spawn_worker(bot)
                continue
            if not self._can_start_worker_now(bot_id):
                continue
            self._spawn_worker(bot)

    def run(self) -> int:
        ai_provider = os.getenv("SONOLBOT_AI_PROVIDER", "codex").strip().lower()
        if ai_provider != "claude" and not shutil.which("codex"):
            self._log("ERROR: codex CLI not found in PATH (set SONOLBOT_AI_PROVIDER=claude to use Claude instead)")
            return 1
        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self._acquire_lock()
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            return 1

        migrated, detail = migrate_legacy_env_if_needed(self.root, self.config_path)
        if migrated:
            self._log(f"legacy config migrated: {detail}")
        else:
            self._log(f"legacy migration skipped: {detail}")

        self._log(
            f"manager started pid={os.getpid()} poll={self.poll_interval_sec}s "
            f"config={self.config_path} workspace_root={self.workspace_root}"
        )
        try:
            while not self.stop_requested:
                self._cleanup_logs()
                self._sync_workers()
                time.sleep(self.poll_interval_sec)
        finally:
            for bot_id in list(self.workers.keys()):
                self._stop_worker(bot_id, "manager_shutdown")
            self._release_lock()
            self._log("manager stopped")
        return 0


def main() -> int:
    if os.getenv("DAEMON_BOT_WORKER", "0").strip() == "1":
        service = DaemonService()
        return service.run()
    manager_enabled_raw = os.getenv("SONOLBOT_MULTI_BOT_MANAGER", "")
    if manager_enabled_raw.strip():
        manager_enabled = manager_enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        manager_enabled = DEFAULT_MULTI_BOT_MANAGER_ENABLED
    if manager_enabled:
        return MultiBotManager().run()
    return DaemonService().run()


if __name__ == "__main__":
    raise SystemExit(main())
