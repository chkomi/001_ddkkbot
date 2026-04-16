#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-platform GUI control panel for Sonolbot daemon."""

from __future__ import annotations

import argparse
import functools
import json
import os
import locale
import re
import signal
import shlex
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import load_dotenv
from scripts.bot_config_store import (
    default_config_path,
    load_config as load_bots_config,
    remove_bot as remove_bot_config,
    save_config as save_bots_config,
    set_allowed_users_global as set_allowed_users_config,
    upsert_bot as upsert_bot_config,
)
from scripts.telegram_validation import (
    fetch_bot_profile,
    validate_bot_token_format,
    validate_user_id_format,
    validate_user_id_live,
)


def _detect_root_dir() -> Path:
    """Best-effort repo root detector.

    Supports:
    - running from repo (`python daemon_control_panel.py`)
    - running from a different cwd
    - (optional) frozen apps that are placed under repo folders (dist/ etc.)
    """
    override = (os.getenv("SONOLBOT_ROOT") or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.exists():
            return p.resolve()

    signatures = ("daemon_service.py", "requirements.txt", "skill_bridge.py")

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
    try:
        candidates.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    candidates.append(Path.cwd())

    def looks_like_root(p: Path) -> bool:
        return all((p / name).exists() for name in signatures)

    for base in candidates:
        try:
            base = base.resolve()
        except Exception:
            continue
        for up in [base, *base.parents][:6]:
            if looks_like_root(up):
                return up

    # Fallback: keep current behavior.
    return candidates[0].resolve() if candidates else Path.cwd().resolve()


ROOT = _detect_root_dir()
load_dotenv(ROOT / ".env", override=False)
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(ROOT / "logs"))).resolve()
PID_FILE = ROOT / ".daemon_service.pid"
PANEL_PID_FILE = ROOT / ".control_panel.pid"
SERVICE_SCRIPT = ROOT / "daemon_service.py"
AUTOSTART_PROMPT_FLAG = ROOT / ".control_panel_autostart_prompted"
IS_WINDOWS_NATIVE = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
WSL_EXE = "wsl.exe"
if IS_WINDOWS_NATIVE:
    REG_EXE = Path(r"C:\Windows\System32\reg.exe")
    CMD_EXE = Path(r"C:\Windows\System32\cmd.exe")
else:
    REG_EXE = Path("/mnt/c/Windows/System32/reg.exe")
    CMD_EXE = Path("/mnt/c/Windows/System32/cmd.exe")
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SonolbotControlPanel"
MACOS_LAUNCH_AGENT_LABEL = "com.sonolbot.daemon"
DIAG_LOG_PREFIX = "control-panel-diag"
DEFAULT_LOG_RETENTION_DAYS = 7
_LAST_DIAG_LOG_TS: dict[str, float] = {}
STARTUP_FILE_NAME = "SonolbotControlPanel.cmd"
_SKIP_WINDOWS_USER_DIRS = {
    "all users",
    "default",
    "default user",
    "defaultaccount",
    "public",
    "wdagutilityaccount",
}
LOG_TYPE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("daemon", "daemon-*.log", "log_type_daemon"),
    ("setup", "setup-run-*.log", "log_type_setup"),
    ("panel_run", "control-panel-run-*.log", "log_type_panel_run"),
    ("panel_launcher", "control-panel-launcher-*.log", "log_type_panel_launcher"),
    ("panel_diag", "control-panel-diag-*.log", "log_type_panel_diag"),
)
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"
DEFAULT_CODEX_REASONING_EFFORT = "high"
DEFAULT_REWRITER_CODEX_MODEL = "gpt-5.3-codex"
DEFAULT_REWRITER_CODEX_REASONING_EFFORT = "none"
CODEX_MODEL_CHOICES: tuple[str, ...] = (
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5-codex",
    "codex-mini-latest",
)
CODEX_REASONING_CHOICES: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
PANEL_CODEX_PREFS_FILE = ROOT / ".control_panel_codex_prefs.toml"
PANEL_REWRITER_PROMPT_FILE = ROOT / ".control_panel_rewriter_prompt.txt"
DEFAULT_REWRITER_PROMPT_TEXT = (
    "당신은 텔레그램 사용자에게 보여줄 중간 진행 안내문 재작성 전용 어시스턴트다.\n"
    "목표: 원문의 의미를 유지하면서 사용자 친화적인 한국어 안내문으로 바꿔라.\n"
    "출력 규칙:\n"
    "1) 1~3문장으로 작성하되, 사용자가 현재 무엇을 진행 중인지 이해할 수 있을 만큼 구체적으로 쓸 것.\n"
    "2) 내부 기술/구조/운영 용어를 절대 노출하지 말 것.\n"
    "   금지 예: thread, msg_번호, INSTRUNCTION.md, index.json, task_meta, 동기화, 세션, 백그라운드, 스크립트, 명령어.\n"
    "3) 시스템 파일/규칙/로그/프롬프트/도구 호출 사실을 언급하지 말 것.\n"
    "4) 결과는 설명문만 출력하고, 머리말/꼬리말/코드블록/불릿은 사용하지 말 것.\n"
    "5) 텔레그램 HTML 파싱 기준으로 작성하고, 강조가 필요하면 <b>와 <code>만 최소한으로 사용하라.\n"
    "   Markdown 문법(*, _, #, ``` 등)은 사용하지 말 것."
)
BOTS_CONFIG_FILE = default_config_path(ROOT)
BOT_WORKSPACES_DIR = Path(
    os.getenv("SONOLBOT_BOT_WORKSPACES_DIR", str(ROOT / "bots"))
).resolve()

def _detect_ui_lang() -> str:
    env_lang = (os.getenv("SONOLBOT_UI_LANG", "") or "").strip().lower()
    if env_lang in ("ko", "en"):
        return env_lang
    try:
        loc = locale.getdefaultlocale()[0] or ""
    except Exception:
        loc = ""
    return "ko" if loc.lower().startswith("ko") else "en"


UI_LANG = _detect_ui_lang()
TEXT = {
    "window_title": {"ko": "Sonolbot 데몬 제어", "en": "Sonolbot Daemon Control"},
    "status_running": {"ko": "실행 중", "en": "running"},
    "status_stopped": {"ko": "중지", "en": "stopped"},
    "status_gui_available": {"ko": "사용 가능", "en": "available"},
    "status_gui_not_detected": {"ko": "미감지", "en": "not-detected"},
    "status_autostart_enabled": {"ko": "활성화", "en": "enabled"},
    "status_autostart_disabled": {"ko": "비활성화", "en": "disabled"},
    "label_service_status": {"ko": "서비스 상태", "en": "Service Status"},
    "label_daemon_pid": {"ko": "데몬 PID", "en": "Daemon PID"},
    "label_codex_pid": {"ko": "Codex PID", "en": "Codex PID"},
    "label_gui_session": {"ko": "GUI 세션", "en": "GUI Session"},
    "label_autostart": {"ko": "자동 시작", "en": "AutoStart"},
    "label_autostart_macos": {"ko": "로그인 시 데몬 자동 시작", "en": "Login AutoStart (Daemon)"},
    "label_codex_prefs": {"ko": "Codex 모델 설정", "en": "Codex Model Settings"},
    "label_codex_model": {"ko": "모델", "en": "Model"},
    "label_codex_reasoning": {"ko": "추론 강도", "en": "Reasoning Effort"},
    "label_rewriter_model": {"ko": "Rewriter 모델", "en": "Rewriter Model"},
    "label_rewriter_reasoning": {"ko": "Rewriter 추론", "en": "Rewriter Reasoning"},
    "label_rewriter_prompt": {"ko": "Rewriter 프롬프트", "en": "Rewriter Prompt"},
    "label_rewriter_prompt_path": {"ko": "프롬프트 파일", "en": "Prompt File"},
    "label_codex_config_path": {"ko": "패널 설정 파일", "en": "Panel Config File"},
    "label_latest_log": {"ko": "최근 로그", "en": "Latest Log"},
    "btn_start": {"ko": "시작", "en": "Start"},
    "btn_stop": {"ko": "중지", "en": "Stop"},
    "btn_refresh": {"ko": "새로고침", "en": "Refresh"},
    "btn_open_logs": {"ko": "로그 열기", "en": "Open Logs"},
    "btn_autostart": {"ko": "자동시작", "en": "AutoStart"},
    "btn_log_detail": {"ko": "로그 상세", "en": "Log Detail"},
    "btn_bot_log_view": {"ko": "로그 보기", "en": "View Logs"},
    "btn_bot_detail_view": {"ko": "상세 보기", "en": "View Details"},
    "btn_apply_codex_prefs": {"ko": "설정 적용", "en": "Apply"},
    "btn_save_rewriter_prompt": {"ko": "프롬프트 저장", "en": "Save Prompt"},
    "btn_exit": {"ko": "완전종료", "en": "Exit App"},
    "msg_info": {"ko": "안내", "en": "Info"},
    "msg_error": {"ko": "오류", "en": "Error"},
    "msg_daemon_running": {"ko": "데몬이 이미 실행 중입니다 (pid={pid})", "en": "Daemon already running (pid={pid})"},
    "msg_missing_file": {"ko": "파일이 없습니다: {path}", "en": "Missing file: {path}"},
    "msg_daemon_not_running": {"ko": "데몬이 실행 중이 아닙니다.", "en": "Daemon is not running."},
    "msg_open_logs_failed": {"ko": "로그 폴더를 열 수 없습니다: {exc}", "en": "Cannot open logs dir: {exc}"},
    "msg_bot_add_save_failed": {
        "ko": "봇 설정 저장에 실패했습니다.\n{detail}",
        "en": "Failed to save bot configuration.\n{detail}",
    },
    "msg_bot_add_verify_failed": {
        "ko": "저장 검증에 실패했습니다. 봇 등록이 반영되지 않았습니다.\nconfig={config}\nbot_id={bot_id}",
        "en": "Post-save verification failed. Bot registration was not applied.\nconfig={config}\nbot_id={bot_id}",
    },
    "msg_bot_add_done": {
        "ko": "봇 등록 완료 (활성화됨)\nbot_id={bot_id}\nusername=@{bot_username}\n데몬 실행 중이면 워커가 곧 시작됩니다.",
        "en": "Bot registered (activated)\nbot_id={bot_id}\nusername=@{bot_username}\nIf daemon is running, worker should start shortly.",
    },
    "msg_bot_add_warn_no_allowed_users": {
        "ko": "허용 사용자 ID가 비어 있어 메시지 처리가 시작되지 않습니다.\nTelegram 설정에서 허용 사용자 ID를 1개 이상 추가하세요.",
        "en": "Global allowed user IDs are empty, so message processing will not start.\nAdd at least one allowed user ID in Telegram settings.",
    },
    "msg_codex_prefs_saved": {
        "ko": "Codex 설정을 저장했습니다.\nmodel={model}\nreasoning={reasoning}\nrewriter_model={rewriter_model}\nrewriter_reasoning={rewriter_reasoning}\npath={path}\nprompt_path={prompt_path}",
        "en": "Saved Codex settings.\nmodel={model}\nreasoning={reasoning}\nrewriter_model={rewriter_model}\nrewriter_reasoning={rewriter_reasoning}\npath={path}\nprompt_path={prompt_path}",
    },
    "msg_codex_prefs_save_failed": {
        "ko": "Codex 설정 저장에 실패했습니다.\n{detail}",
        "en": "Failed to save Codex settings.\n{detail}",
    },
    "msg_rewriter_prompt_saved": {
        "ko": "Rewriter 프롬프트를 저장했습니다.\nprompt_path={prompt_path}",
        "en": "Saved rewriter prompt.\nprompt_path={prompt_path}",
    },
    "msg_rewriter_prompt_save_failed": {
        "ko": "Rewriter 프롬프트 저장에 실패했습니다.\n{detail}",
        "en": "Failed to save rewriter prompt.\n{detail}",
    },
    "msg_wsl_path_not_resolved": {
        "ko": "WSL 프로젝트 경로를 확인할 수 없습니다.",
        "en": "WSL project path could not be resolved.",
    },
    "msg_daemon_start_failed": {
        "ko": "WSL에서 데몬 시작에 실패했습니다.\n{detail}",
        "en": "Failed to start daemon in WSL.\n{detail}",
    },
    "msg_autostart_title": {"ko": "자동 시작", "en": "AutoStart"},
    "msg_autostart_prompt": {
        "ko": "Windows 로그인 시 Control Panel을 자동 실행하도록 등록할까요?\n(재부팅 후 패널이 자동으로 열리고, 데몬도 자동 시작됩니다.)",
        "en": "Register Control Panel to auto-run at Windows login?\n(After reboot, the panel opens automatically and daemon starts automatically.)",
    },
    "msg_autostart_prompt_macos": {
        "ko": "macOS 로그인 시 데몬을 자동 실행하도록 등록할까요?\n(로그인 후 백그라운드로 데몬이 실행됩니다. 패널은 필요할 때만 실행하면 됩니다.)",
        "en": "Register daemon to auto-run at macOS login?\n(The daemon runs in background after login. Open the panel only when needed.)",
    },
    "msg_autostart_registered": {"ko": "자동 시작이 등록되었습니다.", "en": "AutoStart registered."},
    "msg_autostart_register_failed": {"ko": "자동 시작 등록에 실패했습니다.", "en": "Failed to register AutoStart."},
    "msg_autostart_enabled": {"ko": "자동 시작을 활성화했습니다.", "en": "AutoStart enabled."},
    "msg_autostart_disabled": {"ko": "자동 시작을 해제했습니다.", "en": "AutoStart disabled."},
    "msg_autostart_toggle_failed": {"ko": "자동 시작 설정 변경에 실패했습니다.", "en": "Failed to change AutoStart setting."},
    "msg_log_detail_title": {"ko": "로그 상세", "en": "Log Detail"},
    "msg_log_picker_title": {"ko": "로그 종류 선택", "en": "Select Log Type"},
    "msg_log_picker_desc": {"ko": "확인할 로그를 선택하세요.", "en": "Choose a log to open."},
    "btn_open": {"ko": "열기", "en": "Open"},
    "btn_cancel": {"ko": "취소", "en": "Cancel"},
    "msg_log_detail_not_found": {
        "ko": "선택한 로그가 없습니다.\n종류: {label}",
        "en": "No log file found for selected type.\nType: {label}",
    },
    "msg_select_bot_first": {
        "ko": "먼저 봇을 선택하세요.",
        "en": "Select a bot first.",
    },
    "msg_bot_codex_log_not_found": {
        "ko": "선택한 봇의 코덱스 실행 로그가 없습니다.\nbot_id={bot_id}",
        "en": "No Codex execution log for selected bot.\nbot_id={bot_id}",
    },
    "tab_bot_log_main": {"ko": "메인 봇 로그", "en": "Main Bot Log"},
    "tab_bot_log_rewriter": {"ko": "Rewriter 봇 로그", "en": "Rewriter Bot Log"},
    "msg_bot_main_log_empty": {
        "ko": "메인 봇 로그가 없습니다.\nbot_id={bot_id}",
        "en": "No main bot log found.\nbot_id={bot_id}",
    },
    "msg_bot_rewriter_log_empty": {
        "ko": "rewriter 로그가 없습니다.\nbot_id={bot_id}",
        "en": "No rewriter log found.\nbot_id={bot_id}",
    },
    "msg_bot_detail_title": {
        "ko": "봇 상세 정보",
        "en": "Bot Details",
    },
    "log_type_daemon": {"ko": "데몬 (봇 실행 상태)", "en": "Daemon (bot runtime)"},
    "log_type_setup": {"ko": "설정 (setup 실행)", "en": "Setup (installer run)"},
    "log_type_panel_run": {"ko": "패널 실행 (창 실행 과정)", "en": "Panel Run (window launch)"},
    "log_type_panel_launcher": {"ko": "패널 런처 (exe 시작)", "en": "Panel Launcher (exe start)"},
    "log_type_panel_diag": {"ko": "패널 진단 (자동시작/점검)", "en": "Panel Diag (autostart/check)"},
    "msg_panel_running_title": {"ko": "컨트롤 패널", "en": "Control Panel"},
    "msg_panel_already_running": {
        "ko": "컨트롤 패널이 이미 실행 중입니다. 기존 창(작업표시줄)을 사용하세요.",
        "en": "Control panel is already running. Use the existing window in taskbar.",
    },
}


def tr(key: str, **kwargs: object) -> str:
    entry = TEXT.get(key)
    if not entry:
        return key
    msg = entry.get(UI_LANG, entry.get("en", key))
    if kwargs:
        try:
            return msg.format(**kwargs)
        except Exception:
            return msg
    return msg


def _is_windows_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    line = (proc.stdout or "").strip().lower()
    return bool(line and "no tasks are running" not in line and "정보: 지정된" not in line)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS_NATIVE:
        return _is_windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wsl_base_cmd() -> list[str]:
    distro = os.getenv("WSL_DISTRO", "").strip()
    cmd: list[str] = [WSL_EXE]
    if distro:
        cmd.extend(["-d", distro])
    cmd.append("-e")
    return cmd


def _run_wsl(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    cmd = _wsl_base_cmd() + argv
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def _run_wsl_bash(script: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return _run_wsl(["/bin/bash", "-lc", script], timeout=timeout)


def _resolve_codex_config_path() -> str:
    return str(PANEL_CODEX_PREFS_FILE.resolve())


def _read_codex_config_text(path: str) -> str:
    if not path.strip():
        return ""
    p = Path(path).expanduser()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_codex_config_text(path: str, text: str) -> tuple[bool, str]:
    if not path.strip():
        return False, "empty config path"
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True, str(p)
    except OSError as exc:
        return False, str(exc)


def _resolve_rewriter_prompt_path() -> str:
    return str(PANEL_REWRITER_PROMPT_FILE.resolve())


def _load_rewriter_prompt() -> tuple[str, str]:
    path = _resolve_rewriter_prompt_path()
    p = Path(path)
    if not p.exists():
        return DEFAULT_REWRITER_PROMPT_TEXT, path
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_REWRITER_PROMPT_TEXT, path
    if not text:
        return DEFAULT_REWRITER_PROMPT_TEXT, path
    return text, path


def _save_rewriter_prompt(text: str) -> tuple[bool, str]:
    path = _resolve_rewriter_prompt_path()
    normalized = str(text or "").replace("\r\n", "\n").strip()
    if not normalized:
        normalized = DEFAULT_REWRITER_PROMPT_TEXT
    return _write_codex_config_text(path, normalized + "\n")


def _extract_root_toml_string(text: str, key: str) -> str:
    in_root = True
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*\"([^\"]*)\"\s*$")
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_root = False
        if not in_root:
            continue
        m = pattern.match(raw)
        if m:
            return m.group(1).strip()
    return ""


def _upsert_root_toml_string(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_root = True
    inserted = False
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new_line = f'{key} = "{value}"'

    for raw in lines:
        stripped = raw.strip()
        if in_root and stripped.startswith("[") and stripped.endswith("]"):
            if not inserted:
                out.append(new_line)
                inserted = True
            in_root = False
            out.append(raw)
            continue
        if in_root and key_pattern.match(raw):
            if not inserted:
                out.append(new_line)
                inserted = True
            continue
        out.append(raw)

    if not inserted:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)

    return "\n".join(out).rstrip() + "\n"


def _load_codex_preferences() -> tuple[str, str, str, str, str]:
    path = _resolve_codex_config_path()
    text = _read_codex_config_text(path)
    model = _extract_root_toml_string(text, "model") or DEFAULT_CODEX_MODEL
    reasoning = (
        _extract_root_toml_string(text, "model_reasoning_effort")
        or DEFAULT_CODEX_REASONING_EFFORT
    )
    rewriter_model = (
        _extract_root_toml_string(text, "rewriter_model")
        or DEFAULT_REWRITER_CODEX_MODEL
    )
    rewriter_reasoning = (
        _extract_root_toml_string(text, "rewriter_model_reasoning_effort")
        or DEFAULT_REWRITER_CODEX_REASONING_EFFORT
    )
    return model, reasoning, rewriter_model, rewriter_reasoning, path


def _save_codex_preferences(
    model: str,
    reasoning: str,
    rewriter_model: str,
    rewriter_reasoning: str,
) -> tuple[bool, str]:
    path = _resolve_codex_config_path()
    cur = _read_codex_config_text(path)
    next_text = _upsert_root_toml_string(cur, "model", model)
    next_text = _upsert_root_toml_string(next_text, "model_reasoning_effort", reasoning)
    next_text = _upsert_root_toml_string(next_text, "rewriter_model", rewriter_model)
    next_text = _upsert_root_toml_string(next_text, "rewriter_model_reasoning_effort", rewriter_reasoning)
    return _write_codex_config_text(path, next_text)


def _resolve_wsl_project_path() -> str:
    if not IS_WINDOWS_NATIVE:
        return str(ROOT)

    root_win = str(ROOT)
    commands = (
        [WSL_EXE, "-e", "wslpath", "-a", root_win],
        [WSL_EXE, "wslpath", "-a", root_win],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except OSError as exc:
            _diag_log(
                f"WARN wsl_path_resolve command_failed cmd={cmd!r} exc={exc!r}",
                throttle_key="wsl_path_resolve_command_failed",
                min_interval_sec=30,
            )
            continue

        resolved = (proc.stdout or "").strip()
        if proc.returncode == 0 and resolved:
            return resolved

        err = (proc.stderr or proc.stdout or "").strip()
        _diag_log(
            f"WARN wsl_path_resolve nonzero_rc={proc.returncode} cmd={cmd!r} err={err[:240]!r}",
            throttle_key="wsl_path_resolve_nonzero_rc",
            min_interval_sec=30,
        )

    # Fallback: convert Windows path directly, e.g. E:\foo -> /mnt/e/foo
    # Do not call WSL again here; if wslpath is flaky, let start path validation
    # fail later with a clearer daemon-start error instead of path-unresolved.
    normalized = root_win.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).lstrip("/")
        fallback = f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
        _diag_log(f"WARN wsl_path_resolve fallback_assumed path={fallback}")
        return fallback

    _diag_log(f"ERROR wsl_path_resolve failed root={root_win}")
    return ""


def _wsl_path_bundle() -> tuple[str, str]:
    wsl_root = _resolve_wsl_project_path()
    if not wsl_root:
        return "", ""
    return wsl_root, f"{wsl_root}/daemon_service.py"


def _is_wsl_pid_alive(pid: int, expected_token: str = "") -> bool:
    if pid <= 0:
        return False
    token = expected_token.strip()
    if token:
        escaped = shlex.quote(token)
        check_script = (
            f"pid={pid}; "
            "kill -0 \"$pid\" 2>/dev/null || exit 1; "
            "cmdline=$(tr '\\0' ' ' </proc/$pid/cmdline 2>/dev/null || true); "
            f"printf '%s' \"$cmdline\" | grep -F -- {escaped} >/dev/null"
        )
    else:
        check_script = f"pid={pid}; kill -0 \"$pid\" 2>/dev/null"
    proc = _run_wsl_bash(check_script, timeout=8)
    return proc.returncode == 0


def _is_service_pid_alive(pid: int, expected_token: str) -> bool:
    if pid <= 0:
        return False
    if not IS_WINDOWS_NATIVE:
        return _is_pid_alive(pid)
    return _is_wsl_pid_alive(pid, expected_token=expected_token)


def _acquire_panel_lock() -> int:
    if PANEL_PID_FILE.exists():
        existing = _read_pid(PANEL_PID_FILE)
        if existing > 0 and _is_pid_alive(existing):
            return existing
        try:
            PANEL_PID_FILE.unlink()
        except OSError:
            pass

    try:
        PANEL_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return 0
    return 0


def _release_panel_lock() -> None:
    try:
        if PANEL_PID_FILE.exists():
            PANEL_PID_FILE.unlink()
    except OSError:
        pass


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass

    deadline = time.time() + 5
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return
        time.sleep(0.2)

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _stop_wsl_pid(pid: int, expected_token: str = "") -> bool:
    if pid <= 0:
        return False
    token = expected_token.strip()
    token_check = ""
    if token:
        token_check = (
            "cmdline=$(tr '\\0' ' ' </proc/$pid/cmdline 2>/dev/null || true); "
            f"printf '%s' \"$cmdline\" | grep -F -- {shlex.quote(token)} >/dev/null || exit 2; "
        )
    script = (
        f"pid={pid}; "
        "kill -0 \"$pid\" 2>/dev/null || exit 0; "
        + token_check
        + "kill -TERM \"$pid\" 2>/dev/null || true; "
        "for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do "
        "  kill -0 \"$pid\" 2>/dev/null || exit 0; "
        "  sleep 0.2; "
        "done; "
        "kill -KILL \"$pid\" 2>/dev/null || true; "
        "kill -0 \"$pid\" 2>/dev/null && exit 1 || exit 0"
    )
    proc = _run_wsl_bash(script, timeout=12)
    return proc.returncode == 0


def _runtime_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS_NATIVE:
        return _is_wsl_pid_alive(pid)
    return _is_pid_alive(pid)


@functools.lru_cache(maxsize=1)
def _project_process_token() -> str:
    if IS_WINDOWS_NATIVE:
        return _to_wsl_path(ROOT).replace("\\", "/")
    return str(ROOT.resolve()).replace("\\", "/")


def _parse_ps_rows(output: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        args = parts[2].strip()
        if pid <= 0 or not args:
            continue
        rows.append({"pid": pid, "ppid": ppid, "args": args})
    return rows


def _list_runtime_process_rows() -> list[dict[str, object]]:
    if IS_WINDOWS_NATIVE:
        proc = _run_wsl_bash("ps -eo pid=,ppid=,args=", timeout=10)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            _diag_log(f"WARN runtime_ps_query_failed rc={proc.returncode} detail={detail[:220]!r}")
            return []
        return _parse_ps_rows(proc.stdout or "")
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError as exc:
        _diag_log(f"WARN runtime_ps_query_failed exc={exc!r}")
        return []
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        _diag_log(f"WARN runtime_ps_query_failed rc={proc.returncode} detail={detail[:220]!r}")
        return []
    return _parse_ps_rows(proc.stdout or "")


def _is_daemon_service_cmd(args: str) -> bool:
    norm = str(args or "").replace("\\", "/")
    if "daemon_service.py" not in norm:
        return False
    return _project_process_token() in norm


def _is_codex_app_server_cmd(args: str) -> bool:
    norm = str(args or "").lower()
    return ("codex" in norm) and ("app-server" in norm)


def _pid_has_ancestor(pid: int, roots: set[int], parent_by_pid: dict[int, int], max_depth: int = 16) -> bool:
    cur = int(pid)
    depth = 0
    while depth < max_depth and cur > 1:
        parent = int(parent_by_pid.get(cur, 0))
        if parent in roots:
            return True
        if parent <= 1 or parent == cur:
            break
        cur = parent
        depth += 1
    return False


def _iter_bot_state_dirs() -> list[Path]:
    out: list[Path] = []
    if not BOT_WORKSPACES_DIR.exists():
        return out
    try:
        for path in BOT_WORKSPACES_DIR.iterdir():
            if not path.is_dir():
                continue
            state_dir = path / "state"
            if state_dir.is_dir():
                out.append(state_dir)
    except OSError:
        pass
    return out


def _collect_codex_pid_hints() -> set[int]:
    hints: set[int] = set()
    state_dirs = _iter_bot_state_dirs()
    for state_dir in state_dirs:
        for name in ("codex-app-server.pid", "codex-runner.pid"):
            pid = _read_pid(state_dir / name)
            if pid > 0:
                hints.add(pid)
    return hints


def _collect_shutdown_targets() -> tuple[list[int], list[int]]:
    rows = _list_runtime_process_rows()
    if not rows:
        return [], []

    parent_by_pid: dict[int, int] = {}
    daemon_pids: set[int] = set()
    app_candidates: set[int] = set()
    for row in rows:
        pid = int(row.get("pid") or 0)
        ppid = int(row.get("ppid") or 0)
        args = str(row.get("args") or "")
        if pid <= 0:
            continue
        parent_by_pid[pid] = ppid
        if _is_daemon_service_cmd(args):
            daemon_pids.add(pid)
        if _is_codex_app_server_cmd(args):
            app_candidates.add(pid)

    app_hints = _collect_codex_pid_hints()
    app_pids: set[int] = set()
    for pid in app_candidates:
        if pid in app_hints or _pid_has_ancestor(pid, daemon_pids, parent_by_pid):
            app_pids.add(pid)

    return sorted(daemon_pids), sorted(app_pids)


def _stop_pid_target(pid: int, expected_token: str) -> bool:
    if pid <= 0:
        return True
    if IS_WINDOWS_NATIVE:
        return _stop_wsl_pid(pid, expected_token=expected_token)
    _kill_pid(pid)
    return not _runtime_pid_alive(pid)


def _cleanup_pid_file_if_stale(path: Path) -> None:
    pid = _read_pid(path)
    if pid > 0 and _runtime_pid_alive(pid):
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _cleanup_runtime_pid_files() -> None:
    _cleanup_pid_file_if_stale(PID_FILE)
    for state_dir in _iter_bot_state_dirs():
        for name in ("daemon-worker.pid", "codex-app-server.pid", "codex-runner.pid"):
            _cleanup_pid_file_if_stale(state_dir / name)


def _stop_all_runtime_processes() -> dict[str, object]:
    daemon_pids, app_pids = _collect_shutdown_targets()
    _diag_log(
        f"INFO stop_all_runtime start daemon_targets={len(daemon_pids)} app_server_targets={len(app_pids)} "
        f"daemon_pids={daemon_pids} app_pids={app_pids}"
    )

    failed_daemons: list[int] = []
    failed_apps: list[int] = []

    for pid in daemon_pids:
        ok = _stop_pid_target(pid, expected_token="daemon_service.py")
        if not ok and _runtime_pid_alive(pid):
            failed_daemons.append(pid)
            _diag_log(f"WARN stop_all_runtime daemon_stop_failed pid={pid}")

    for pid in app_pids:
        ok = _stop_pid_target(pid, expected_token="app-server")
        if not ok and _runtime_pid_alive(pid):
            failed_apps.append(pid)
            _diag_log(f"WARN stop_all_runtime app_server_stop_failed pid={pid}")

    _cleanup_runtime_pid_files()

    summary = {
        "daemon_targets": daemon_pids,
        "app_server_targets": app_pids,
        "failed_daemons": failed_daemons,
        "failed_app_servers": failed_apps,
    }
    _diag_log(
        "INFO stop_all_runtime done "
        f"failed_daemons={failed_daemons} failed_app_servers={failed_apps}"
    )
    return summary


def _latest_daemon_log_line() -> str:
    today = LOGS_DIR / f"daemon-{time.strftime('%Y-%m-%d')}.log"
    if not today.exists():
        return "-"
    try:
        lines = today.read_text(encoding="utf-8").strip().splitlines()
        return lines[-1] if lines else "-"
    except Exception:
        return "-"


def _read_tail_lines(path: Path, max_lines: int = 8) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _compact_log_line(line: str, max_chars: int = 120) -> str:
    one_line = " ".join((line or "-").split())
    if len(one_line) <= max_chars:
        return one_line
    return f"{one_line[: max_chars - 3]}..."


def _latest_log_file_by_pattern(pattern: str) -> Path | None:
    candidates = [p for p in LOGS_DIR.glob(pattern) if p.is_file()]
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return sorted(candidates)[-1]


def _read_log_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _diag_log(message: str, throttle_key: str | None = None, min_interval_sec: int = 0) -> None:
    try:
        if throttle_key and min_interval_sec > 0:
            now_ts = time.time()
            last_ts = _LAST_DIAG_LOG_TS.get(throttle_key, 0.0)
            if now_ts - last_ts < min_interval_sec:
                return
            _LAST_DIAG_LOG_TS[throttle_key] = now_ts

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        retention_raw = os.getenv("LOG_RETENTION_DAYS", str(DEFAULT_LOG_RETENTION_DAYS)).strip()
        try:
            retention_days = max(1, int(retention_raw))
        except ValueError:
            retention_days = DEFAULT_LOG_RETENTION_DAYS

        cutoff = datetime.now().date() - timedelta(days=retention_days - 1)
        for path in LOGS_DIR.glob(f"{DIAG_LOG_PREFIX}-*.log"):
            day_str = path.stem.replace(f"{DIAG_LOG_PREFIX}-", "", 1)
            try:
                day = datetime.strptime(day_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

        log_path = LOGS_DIR / f"{DIAG_LOG_PREFIX}-{time.strftime('%Y-%m-%d')}.log"
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _to_windows_path(path: Path) -> str:
    if IS_WINDOWS_NATIVE:
        return str(path)
    try:
        out = subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
        return out
    except Exception:
        return str(path)


def _to_wsl_path(path: Path) -> str:
    if not IS_WINDOWS_NATIVE:
        return str(path)
    raw = str(path)
    commands = (
        [WSL_EXE, "-e", "wslpath", "-a", raw],
        [WSL_EXE, "wslpath", "-a", raw],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        except OSError:
            continue
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return out

    normalized = raw.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return raw


def _augment_path_for_macos(env: dict[str, str]) -> None:
    """LaunchAgent / GUI-launched processes often have a limited PATH on macOS."""
    if sys.platform != "darwin":
        return
    current = (env.get("PATH") or "").strip()
    prefixes = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    parts: list[str] = []
    seen: set[str] = set()
    for p in prefixes + ([current] if current else []):
        for item in str(p).split(":"):
            item = item.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            parts.append(item)
    env["PATH"] = ":".join(parts)


def _autostart_command() -> str:
    exe_path = ROOT / "control_panel.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"Missing file: {exe_path}")
    return f"\"{_to_windows_path(exe_path)}\" --autostart-start-daemon"


def _startup_dirs() -> list[Path]:
    out: list[Path] = []

    if IS_WINDOWS_NATIVE:
        appdata = (os.getenv("APPDATA") or "").strip()
        programdata = (os.getenv("ProgramData") or "").strip()
        if appdata:
            out.append(Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup")
        if programdata:
            out.append(Path(programdata) / "Microsoft/Windows/Start Menu/Programs/StartUp")
        return out

    users_root = Path("/mnt/c/Users")
    if users_root.exists():
        try:
            for user_dir in sorted(users_root.iterdir(), key=lambda p: p.name.lower()):
                if not user_dir.is_dir():
                    continue
                if user_dir.name.lower() in _SKIP_WINDOWS_USER_DIRS:
                    continue
                out.append(user_dir / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup")
        except OSError:
            pass

    out.append(Path("/mnt/c/ProgramData/Microsoft/Windows/Start Menu/Programs/StartUp"))

    unique: list[Path] = []
    seen: set[str] = set()
    for path in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _startup_file_paths() -> list[Path]:
    return [d / STARTUP_FILE_NAME for d in _startup_dirs()]


def _startup_file_content() -> str:
    exe_path = _to_windows_path(ROOT / "control_panel.exe")
    return f'@echo off\r\nstart "" "{exe_path}" --autostart-start-daemon\r\n'


def _enable_autostart_via_startup_file() -> bool:
    content = _startup_file_content()
    for startup_file in _startup_file_paths():
        try:
            startup_file.parent.mkdir(parents=True, exist_ok=True)
            startup_file.write_text(content, encoding="utf-8")
            _diag_log(f"INFO autostart_enable startup_file_created path={startup_file}")
            return True
        except OSError as exc:
            _diag_log(
                f"WARN autostart_enable startup_file_write_failed path={startup_file} exc={exc!r}",
                throttle_key=f"autostart_enable_startup_file_write_failed:{startup_file}",
                min_interval_sec=60,
            )
            continue
    return False


def _disable_autostart_via_startup_file() -> bool:
    removed = False
    for startup_file in _startup_file_paths():
        if not startup_file.exists():
            continue
        try:
            startup_file.unlink()
            removed = True
            _diag_log(f"INFO autostart_disable startup_file_removed path={startup_file}")
        except OSError as exc:
            _diag_log(
                f"WARN autostart_disable startup_file_remove_failed path={startup_file} exc={exc!r}",
                throttle_key=f"autostart_disable_startup_file_remove_failed:{startup_file}",
                min_interval_sec=60,
            )
    return removed


def _is_autostart_enabled_via_startup_file() -> bool:
    for startup_file in _startup_file_paths():
        if startup_file.exists():
            return True
    return False


def _run_windows_reg(args: list[str]) -> subprocess.CompletedProcess[str]:
    candidates: list[list[str]] = []
    if os.name == "nt":
        candidates.append(["reg", *args])
    else:
        # Prefer Windows command gateway from WSL.
        candidates.append(["cmd.exe", "/c", "reg", *args])
        if CMD_EXE.exists():
            candidates.append([str(CMD_EXE), "/c", "reg", *args])
        candidates.append(["reg.exe", *args])
        if REG_EXE.exists():
            candidates.append([str(REG_EXE), *args])

    last_exc: OSError | None = None
    for cmd in candidates:
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
            )
        except OSError as exc:
            last_exc = exc
            continue

    if last_exc is not None:
        raise last_exc
    raise OSError("Windows registry command backend is unavailable")


def _macos_launch_agents_dir() -> Path:
    return (Path.home() / "Library" / "LaunchAgents").resolve()


def _macos_launch_agent_plist_path() -> Path:
    return _macos_launch_agents_dir() / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"


def _macos_launch_agent_program_args() -> list[str]:
    venv_py = (ROOT / ".venv" / "bin" / "python").resolve()
    if venv_py.exists():
        return ["/usr/bin/env", str(venv_py), str(SERVICE_SCRIPT)]
    # Fallback: rely on PATH inside plist EnvironmentVariables.
    return ["/usr/bin/env", "python3", str(SERVICE_SCRIPT)]


def _macos_launch_agent_plist_content(prompt_path: str) -> str:
    out_log = (LOGS_DIR / "daemon-autostart.out.log").resolve()
    err_log = (LOGS_DIR / "daemon-autostart.err.log").resolve()
    bots_config = (ROOT / ".control_panel_telegram_bots.json").resolve()
    bot_workspaces = (ROOT / "bots").resolve()
    _augment_path_for_macos(os.environ)  # best-effort for deriving PATH default
    path_val = (os.environ.get("PATH") or "").strip()

    # Minimal env needed for "manager -> workers" mode.
    env_vars: dict[str, str] = {
        "SONOLBOT_MULTI_BOT_MANAGER": "1",
        "SONOLBOT_BOTS_CONFIG": str(bots_config),
        "SONOLBOT_BOT_WORKSPACES_DIR": str(bot_workspaces),
        "DAEMON_AGENT_REWRITER_PROMPT_FILE": str(Path(prompt_path).resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "UTF-8",
    }
    if path_val:
        env_vars["PATH"] = path_val

    def xml_escape(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    program_args = _macos_launch_agent_program_args()
    program_args_xml = "\n".join(
        f"        <string>{xml_escape(arg)}</string>" for arg in program_args
    )
    env_xml = "\n".join(
        f"        <key>{xml_escape(k)}</key>\n        <string>{xml_escape(v)}</string>"
        for k, v in sorted(env_vars.items())
    )

    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        f"  <key>Label</key>\n  <string>{xml_escape(MACOS_LAUNCH_AGENT_LABEL)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{program_args_xml}\n"
        "  </array>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{xml_escape(str(ROOT.resolve()))}</string>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <false/>\n"
        f"  <key>StandardOutPath</key>\n  <string>{xml_escape(str(out_log))}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{xml_escape(str(err_log))}</string>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_xml}\n"
        "  </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _run_launchctl(args: list[str], timeout: int = 12) -> subprocess.CompletedProcess[str]:
    cmd = ["launchctl", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def _macos_is_autostart_enabled() -> bool:
    plist = _macos_launch_agent_plist_path()
    if not plist.exists():
        return False
    uid = os.getuid()
    # Best-effort: `print` succeeds when loaded.
    proc = _run_launchctl(["print", f"gui/{uid}/{MACOS_LAUNCH_AGENT_LABEL}"], timeout=6)
    if proc.returncode == 0:
        return True
    return True  # plist exists; treat as enabled even if launchctl print is blocked.


def _macos_set_autostart(enabled: bool, prompt_path: str) -> bool:
    plist = _macos_launch_agent_plist_path()
    uid = os.getuid()
    target_domain = f"gui/{uid}"

    if enabled:
        try:
            plist.parent.mkdir(parents=True, exist_ok=True)
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            content = _macos_launch_agent_plist_content(prompt_path=prompt_path)
            plist.write_text(content, encoding="utf-8")
        except OSError as exc:
            _diag_log(f"WARN macos_autostart_enable write_failed path={plist} exc={exc!r}")
            return False

        # Try modern launchctl first; ignore "not loaded" errors on bootout.
        _run_launchctl(["bootout", target_domain, str(plist)], timeout=8)
        proc = _run_launchctl(["bootstrap", target_domain, str(plist)], timeout=12)
        if proc.returncode == 0:
            return True

        # Fallback for older setups.
        proc2 = _run_launchctl(["load", "-w", str(plist)], timeout=12)
        return proc2.returncode == 0

    # disable
    if plist.exists():
        _run_launchctl(["bootout", target_domain, str(plist)], timeout=10)
        _run_launchctl(["unload", "-w", str(plist)], timeout=10)
        try:
            plist.unlink()
        except OSError:
            pass
        return True
    return True


def _is_autostart_enabled() -> bool:
    if sys.platform == "darwin":
        return _macos_is_autostart_enabled()

    reg_available = True
    try:
        proc = _run_windows_reg(["query", RUN_KEY, "/v", RUN_VALUE])
    except OSError as exc:
        reg_available = False
        _diag_log(
            f"WARN autostart_query backend_failed exc={exc!r}",
            throttle_key="autostart_query_backend_failed",
            min_interval_sec=30,
        )
    else:
        if proc.returncode == 0:
            return True
        if proc.returncode not in (0, 1):
            _diag_log(
                f"WARN autostart_query unexpected_rc={proc.returncode}",
                throttle_key="autostart_query_unexpected_rc",
                min_interval_sec=30,
            )

    # Fallback for environments where WSL->Windows reg interop is blocked.
    enabled_by_file = _is_autostart_enabled_via_startup_file()
    if enabled_by_file and not reg_available:
        _diag_log(
            "INFO autostart_query fallback startup_file_detected",
            throttle_key="autostart_query_startup_file_detected",
            min_interval_sec=30,
        )
    return enabled_by_file


def _set_autostart(enabled: bool) -> bool:
    if sys.platform == "darwin":
        prompt_text, prompt_path = _load_rewriter_prompt()
        _ = prompt_text
        return _macos_set_autostart(enabled, prompt_path)

    if enabled:
        try:
            cmd = _autostart_command()
        except FileNotFoundError as exc:
            _diag_log(f"WARN autostart_enable command_build_failed exc={exc!r}")
            return False
        try:
            proc = _run_windows_reg(
                [
                    "add",
                    RUN_KEY,
                    "/v",
                    RUN_VALUE,
                    "/t",
                    "REG_SZ",
                    "/d",
                    cmd,
                    "/f",
                ]
            )
        except OSError as exc:
            _diag_log(f"WARN autostart_enable backend_failed exc={exc!r}")
            return _enable_autostart_via_startup_file()
        if proc.returncode == 0:
            _disable_autostart_via_startup_file()
            return True
        _diag_log(
            f"WARN autostart_enable reg_nonzero_rc={proc.returncode}",
            throttle_key="autostart_enable_reg_nonzero_rc",
            min_interval_sec=30,
        )
        return _enable_autostart_via_startup_file()

    try:
        proc = _run_windows_reg(["delete", RUN_KEY, "/v", RUN_VALUE, "/f"])
    except OSError as exc:
        _diag_log(f"WARN autostart_disable backend_failed exc={exc!r}")
        return _disable_autostart_via_startup_file()
    if proc.returncode == 0:
        _disable_autostart_via_startup_file()
        return True
    _diag_log(
        f"WARN autostart_disable reg_nonzero_rc={proc.returncode}",
        throttle_key="autostart_disable_reg_nonzero_rc",
        min_interval_sec=30,
    )
    return _disable_autostart_via_startup_file()


def _parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--autostart-start-daemon", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


class AddBotDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, existing_tokens_by_bot_id: dict[str, str]) -> None:
        super().__init__(parent)
        self.title("봇 추가")
        self.geometry("620x300")
        self.minsize(560, 270)
        self.resizable(False, False)
        self.transient(parent)

        self.existing_tokens_by_bot_id = existing_tokens_by_bot_id
        self.result: dict[str, str] | None = None
        self._validated: dict[str, str] | None = None

        self.token_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="1) 토큰 입력 2) 토큰 검증 3) 등록")

        frame = tk.Frame(self, padx=14, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="텔레그램 봇 토큰").grid(row=0, column=0, sticky="w")
        token_entry = tk.Entry(frame, textvariable=self.token_var, width=54, show="*")
        token_entry.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 8))
        token_entry.focus_set()

        tk.Button(frame, text="토큰 검증", width=12, command=self._validate_token).grid(
            row=2, column=0, sticky="w"
        )
        self.register_btn = tk.Button(
            frame,
            text="등록",
            width=12,
            command=self._commit_register,
            state=tk.DISABLED,
        )
        self.register_btn.grid(row=2, column=1, sticky="w", padx=(6, 0))
        tk.Button(frame, text="취소", width=10, command=self._cancel).grid(row=2, column=2, sticky="e")

        tk.Label(frame, textvariable=self.status_var, justify="left", fg="#245c2a").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        tk.Label(
            frame,
            text="주의: 등록된 bot_id의 토큰은 변경할 수 없습니다. 변경하려면 삭제 후 재등록하세요.",
            justify="left",
            fg="#6d2a2a",
            wraplength=560,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.bind("<Return>", lambda _e: self._validate_token())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.update_idletasks()
        self._center_on_parent(parent)
        self.lift()
        self.focus_force()
        self.grab_set()

    def _center_on_parent(self, parent: tk.Tk) -> None:
        try:
            parent.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            ww = self.winfo_reqwidth()
            wh = self.winfo_reqheight()
            x = px + max(0, (pw - ww) // 2)
            y = py + max(0, (ph - wh) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            return

    def _validate_token(self) -> None:
        token = (self.token_var.get() or "").strip()
        ok, err = validate_bot_token_format(token)
        if not ok:
            messagebox.showerror(tr("msg_error"), err, parent=self)
            return
        ok_profile, profile, detail = fetch_bot_profile(token, timeout_sec=8.0)
        if not ok_profile:
            messagebox.showerror(tr("msg_error"), f"토큰 라이브 검증 실패\n{detail}", parent=self)
            return

        bot_id = str(profile.get("id") or "").strip()
        if not bot_id:
            messagebox.showerror(tr("msg_error"), "bot_id를 확인하지 못했습니다.", parent=self)
            return
        old_token = self.existing_tokens_by_bot_id.get(bot_id, "")
        if old_token and old_token != token:
            messagebox.showerror(
                tr("msg_error"),
                (
                    f"이미 등록된 bot_id={bot_id} 입니다.\n"
                    "보안 정책상 기존 봇의 토큰은 수정할 수 없습니다.\n"
                    "해당 봇을 삭제한 뒤 새 토큰으로 다시 등록하세요."
                ),
                parent=self,
            )
            return

        bot_username = str(profile.get("username") or "").strip()
        bot_name = str(profile.get("first_name") or "").strip()
        self._validated = {
            "token": token,
            "bot_id": bot_id,
            "bot_username": bot_username,
            "bot_name": bot_name,
        }
        self.register_btn.config(state=tk.NORMAL)
        self.status_var.set(
            f"검증 완료: bot_id={bot_id}, username=@{bot_username or '-'}\n'등록' 버튼을 누르면 저장됩니다."
        )

    def _commit_register(self) -> None:
        validated = self._validated
        if not isinstance(validated, dict):
            messagebox.showerror(tr("msg_error"), "먼저 토큰 검증을 실행하세요.", parent=self)
            return
        current_token = (self.token_var.get() or "").strip()
        if current_token != str(validated.get("token") or ""):
            messagebox.showerror(
                tr("msg_error"),
                "검증 후 토큰이 변경되었습니다. 다시 토큰 검증을 실행하세요.",
                parent=self,
            )
            self.register_btn.config(state=tk.DISABLED)
            return
        self.result = dict(validated)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class ControlPanel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(tr("window_title"))
        self.geometry("1180x860")
        self.minsize(1080, 780)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)

        self.status_var = tk.StringVar(value="unknown")
        self.daemon_state_var = tk.StringVar(value="데몬 상태: 확인 중")
        self.pid_var = tk.StringVar(value="-")
        self.codex_var = tk.StringVar(value="-")
        self.gui_var = tk.StringVar(value="-")
        self.autostart_var = tk.StringVar(value="-")
        self.runtime_daemon_var = tk.StringVar(value="DAEMON PID: -")
        self.runtime_codex_var = tk.StringVar(value="CODEX PID: -")
        self.runtime_gui_var = tk.StringVar(value="GUI: -")
        self.runtime_autostart_var = tk.StringVar(value="AUTOSTART: -")
        self.runtime_daemon_chip: tk.Label | None = None
        self.runtime_codex_chip: tk.Label | None = None
        self.runtime_gui_chip: tk.Label | None = None
        self.runtime_autostart_chip: tk.Label | None = None
        self.daemon_state_chip: tk.Label | None = None
        self.codex_model_var = tk.StringVar(value=DEFAULT_CODEX_MODEL)
        self.codex_reasoning_var = tk.StringVar(value=DEFAULT_CODEX_REASONING_EFFORT)
        self.rewriter_model_var = tk.StringVar(value=DEFAULT_REWRITER_CODEX_MODEL)
        self.rewriter_reasoning_var = tk.StringVar(value=DEFAULT_REWRITER_CODEX_REASONING_EFFORT)
        self.rewriter_prompt_path_var = tk.StringVar(value="-")
        self.codex_config_path_var = tk.StringVar(value="-")
        self.log_var = tk.StringVar(value="-")
        self.btn_start: tk.Button | None = None
        self.btn_stop: tk.Button | None = None
        self.bots_config_path_var = tk.StringVar(value=str(BOTS_CONFIG_FILE))
        self._bot_rows_cache: list[dict[str, object]] = []
        self._bot_rows_page_cache: list[dict[str, object]] = []
        self.bot_tree: ttk.Treeview | None = None
        self.bot_page_index = 0
        self.bot_page_size = 10
        self.bot_page_var = tk.StringVar(value="0 / 0")
        self.bot_page_prev_btn: tk.Button | None = None
        self.bot_page_next_btn: tk.Button | None = None
        self.bot_log_view_btn: tk.Button | None = None
        self.bot_detail_view_btn: tk.Button | None = None
        self.allowed_users_listbox: tk.Listbox | None = None
        self.allowed_user_entry: tk.Entry | None = None
        self.bot_empty_hint_var = tk.StringVar(value="")
        self.bot_detail_bot_id_var = tk.StringVar(value="-")
        self.bot_detail_username_var = tk.StringVar(value="-")
        self.bot_detail_name_var = tk.StringVar(value="-")
        self.bot_detail_alias_var = tk.StringVar(value="")
        self.bot_detail_memo_var = tk.StringVar(value="")
        self.bot_detail_active_var = tk.BooleanVar(value=False)
        self._suspend_bot_detail_autosave = False
        self._bot_detail_autosave_job: str | None = None
        self._bots_config_mtime: float | None = None
        self.bot_detail_alias_var.trace_add("write", self._on_bot_detail_var_changed)
        self.bot_detail_memo_var.trace_add("write", self._on_bot_detail_var_changed)
        self.bot_detail_active_var.trace_add("write", self._on_bot_detail_var_changed)
        self.rewriter_prompt_text: tk.Text | None = None

        frame = tk.Frame(self, padx=14, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)

        top_row = tk.Frame(frame)
        top_row.pack(fill=tk.X, pady=(0, 6))
        tk.Label(
            top_row,
            text="Sonolbot Control Panel",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT)
        self.daemon_state_chip = tk.Label(
            top_row,
            textvariable=self.daemon_state_var,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=3,
            bg="#fee2e2",
            fg="#7f1d1d",
        )
        self.daemon_state_chip.pack(side=tk.LEFT, padx=(10, 0))

        top_actions = tk.Frame(top_row)
        top_actions.pack(side=tk.RIGHT)
        tk.Button(top_actions, text=tr("btn_exit"), width=10, command=self.exit_panel).pack(side=tk.RIGHT)
        tk.Button(top_actions, text=tr("btn_autostart"), width=10, command=self.toggle_autostart).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        tk.Button(top_actions, text=tr("btn_open_logs"), width=10, command=self.open_logs_dir).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        tk.Button(top_actions, text=tr("btn_refresh"), width=10, command=self.refresh_status).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self.btn_stop = tk.Button(top_actions, text=tr("btn_stop"), width=10, command=self.stop_daemon)
        self.btn_stop.pack(side=tk.RIGHT, padx=(0, 6))
        self.btn_start = tk.Button(top_actions, text=tr("btn_start"), width=10, command=self.start_daemon)
        self.btn_start.pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(
            frame,
            text="데몬이 시작되어야 봇 사용이 가능합니다. (새 메시지 감시 및 Codex 자동 응답 실행)",
            anchor="w",
            justify="left",
            fg="#374151",
        ).pack(fill=tk.X, pady=(0, 8))

        runtime_wrap = tk.Frame(frame, bg="#f3f5f8", bd=1, relief=tk.SOLID, padx=8, pady=6)
        runtime_wrap.pack(fill=tk.X, pady=(2, 10))
        self.runtime_daemon_chip = tk.Label(
            runtime_wrap,
            textvariable=self.runtime_daemon_var,
            font=("Consolas", 10, "bold"),
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=4,
            bg="#dbeafe",
            fg="#1f2937",
        )
        self.runtime_daemon_chip.pack(side=tk.LEFT, padx=(0, 6))
        self.runtime_codex_chip = tk.Label(
            runtime_wrap,
            textvariable=self.runtime_codex_var,
            font=("Consolas", 10, "bold"),
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=4,
            bg="#e5e7eb",
            fg="#1f2937",
        )
        self.runtime_codex_chip.pack(side=tk.LEFT, padx=(0, 6))
        self.runtime_gui_chip = tk.Label(
            runtime_wrap,
            textvariable=self.runtime_gui_var,
            font=("Consolas", 10, "bold"),
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=4,
            bg="#e5e7eb",
            fg="#1f2937",
        )
        self.runtime_gui_chip.pack(side=tk.LEFT, padx=(0, 6))
        self.runtime_autostart_chip = tk.Label(
            runtime_wrap,
            textvariable=self.runtime_autostart_var,
            font=("Consolas", 10, "bold"),
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=4,
            bg="#e5e7eb",
            fg="#1f2937",
        )
        self.runtime_autostart_chip.pack(side=tk.LEFT)

        notebook = ttk.Notebook(frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        telegram_tab = tk.Frame(notebook, padx=4, pady=4)
        ops_tab = tk.Frame(notebook, padx=4, pady=4)
        notebook.add(telegram_tab, text="텔레그램 봇 관리")
        notebook.add(ops_tab, text="운영/로그")
        notebook.select(telegram_tab)

        codex_cfg = tk.LabelFrame(ops_tab, text=tr("label_codex_prefs"), padx=10, pady=8)
        codex_cfg.pack(fill=tk.X, pady=(0, 10))

        tk.Label(codex_cfg, text=tr("label_codex_model"), width=14, anchor="w").grid(row=0, column=0, sticky="w")
        tk.OptionMenu(codex_cfg, self.codex_model_var, *CODEX_MODEL_CHOICES).grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )

        tk.Label(codex_cfg, text=tr("label_codex_reasoning"), width=14, anchor="w").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        tk.OptionMenu(codex_cfg, self.codex_reasoning_var, *CODEX_REASONING_CHOICES).grid(
            row=1, column=1, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        tk.Label(codex_cfg, text=tr("label_rewriter_model"), width=14, anchor="w").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        tk.OptionMenu(codex_cfg, self.rewriter_model_var, *CODEX_MODEL_CHOICES).grid(
            row=2, column=1, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        tk.Label(codex_cfg, text=tr("label_rewriter_reasoning"), width=14, anchor="w").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )
        tk.OptionMenu(codex_cfg, self.rewriter_reasoning_var, *CODEX_REASONING_CHOICES).grid(
            row=3, column=1, sticky="w", padx=(0, 8), pady=(6, 0)
        )

        tk.Button(
            codex_cfg,
            text=tr("btn_apply_codex_prefs"),
            width=10,
            command=self.apply_codex_preferences,
        ).grid(row=0, column=2, rowspan=4, sticky="ns")

        tk.Label(codex_cfg, text=tr("label_codex_config_path"), width=14, anchor="w").grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        tk.Label(codex_cfg, textvariable=self.codex_config_path_var, anchor="w", justify="left").grid(
            row=4, column=1, columnspan=2, sticky="w", pady=(8, 0)
        )

        rewriter_prompt_cfg = tk.LabelFrame(ops_tab, text=tr("label_rewriter_prompt"), padx=10, pady=8)
        rewriter_prompt_cfg.pack(fill=tk.X, pady=(0, 10))
        tk.Label(rewriter_prompt_cfg, text=tr("label_rewriter_prompt_path"), width=14, anchor="w").grid(
            row=0, column=0, sticky="nw"
        )
        tk.Label(
            rewriter_prompt_cfg,
            textvariable=self.rewriter_prompt_path_var,
            anchor="w",
            justify="left",
            wraplength=820,
        ).grid(row=0, column=1, sticky="w")

        prompt_wrap = tk.Frame(rewriter_prompt_cfg, bd=1, relief=tk.SOLID)
        prompt_wrap.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        prompt_scroll = tk.Scrollbar(prompt_wrap, orient=tk.VERTICAL)
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.rewriter_prompt_text = tk.Text(
            prompt_wrap,
            height=7,
            wrap=tk.WORD,
            yscrollcommand=prompt_scroll.set,
            font=("Consolas", 10),
        )
        self.rewriter_prompt_text.pack(fill=tk.X, expand=True)
        prompt_scroll.config(command=self.rewriter_prompt_text.yview)
        tk.Button(
            rewriter_prompt_cfg,
            text=tr("btn_save_rewriter_prompt"),
            width=12,
            command=self.save_rewriter_prompt,
        ).grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        rewriter_prompt_cfg.grid_columnconfigure(1, weight=1)

        tg_cfg = tk.LabelFrame(telegram_tab, text="Telegram 다중 봇 설정", padx=10, pady=8)
        tg_cfg.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        tk.Label(tg_cfg, text="설정 파일", width=12, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(tg_cfg, textvariable=self.bots_config_path_var, anchor="w").grid(
            row=0, column=1, columnspan=4, sticky="w"
        )

        toolbar = tk.Frame(tg_cfg)
        toolbar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 4))
        tk.Button(toolbar, text="봇 추가", width=10, command=self.open_add_bot_dialog).pack(side=tk.LEFT)
        tk.Button(toolbar, text="선택 삭제", width=10, command=self.remove_selected_bot).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        tk.Button(toolbar, text="운영여부 전환", width=10, command=self.toggle_selected_bot_active).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.bot_log_view_btn = tk.Button(
            toolbar,
            text=tr("btn_bot_log_view"),
            width=10,
            command=self._open_selected_bot_codex_log,
        )
        self.bot_log_view_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.bot_detail_view_btn = tk.Button(
            toolbar,
            text=tr("btn_bot_detail_view"),
            width=10,
            command=self._open_selected_bot_detail,
        )
        self.bot_detail_view_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.bot_page_prev_btn = tk.Button(toolbar, text="이전", width=8, command=self._go_bot_prev_page)
        self.bot_page_prev_btn.pack(side=tk.RIGHT)
        tk.Label(toolbar, textvariable=self.bot_page_var, width=10, anchor="e").pack(side=tk.RIGHT, padx=(0, 8))
        self.bot_page_next_btn = tk.Button(toolbar, text="다음", width=8, command=self._go_bot_next_page)
        self.bot_page_next_btn.pack(side=tk.RIGHT, padx=(0, 6))

        tree_wrap = tk.Frame(tg_cfg, bd=1, relief=tk.SOLID)
        tree_wrap.grid(row=2, column=0, columnspan=5, sticky="nsew", pady=(0, 4))
        self.bot_tree = ttk.Treeview(
            tree_wrap,
            columns=("work", "monitor", "display", "username", "bot_id", "updated_at"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.bot_tree.heading("work", text="작업상태")
        self.bot_tree.heading("monitor", text="운영여부")
        self.bot_tree.heading("display", text="표시 이름")
        self.bot_tree.heading("username", text="username")
        self.bot_tree.heading("bot_id", text="bot_id")
        self.bot_tree.heading("updated_at", text="수정일")
        self.bot_tree.column("work", width=96, anchor="center")
        self.bot_tree.column("monitor", width=96, anchor="center")
        self.bot_tree.column("display", width=230, anchor="w")
        self.bot_tree.column("username", width=150, anchor="w")
        self.bot_tree.column("bot_id", width=160, anchor="w")
        self.bot_tree.column("updated_at", width=180, anchor="w")
        y_scroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.bot_tree.yview)
        x_scroll = ttk.Scrollbar(tree_wrap, orient=tk.HORIZONTAL, command=self.bot_tree.xview)
        self.bot_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.bot_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.bot_tree.bind("<<TreeviewSelect>>", self._on_bot_list_select)
        self.bot_tree.tag_configure("monitor_off", foreground="#9ca3af")

        tk.Label(
            tg_cfg,
            textvariable=self.bot_empty_hint_var,
            anchor="w",
            justify="left",
            fg="#3a3a3a",
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(2, 0))

        bot_detail = tk.LabelFrame(tg_cfg, text="선택 봇 정보 (토큰 수정 불가)", padx=8, pady=6)
        bot_detail.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        tk.Label(bot_detail, text="bot_id", width=9, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(bot_detail, textvariable=self.bot_detail_bot_id_var, width=20, anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        tk.Label(bot_detail, text="username", width=9, anchor="w").grid(row=0, column=2, sticky="w")
        tk.Label(bot_detail, textvariable=self.bot_detail_username_var, width=22, anchor="w").grid(
            row=0, column=3, sticky="w"
        )

        tk.Label(bot_detail, text="bot 이름", width=9, anchor="w").grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Label(bot_detail, textvariable=self.bot_detail_name_var, width=20, anchor="w").grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )
        tk.Label(bot_detail, text="표시 이름", width=9, anchor="w").grid(row=1, column=2, sticky="w", pady=(6, 0))
        tk.Entry(bot_detail, textvariable=self.bot_detail_alias_var, width=24).grid(
            row=1, column=3, sticky="w", pady=(6, 0)
        )

        tk.Label(bot_detail, text="메모", width=9, anchor="w").grid(row=2, column=0, sticky="w", pady=(6, 0))
        tk.Entry(bot_detail, textvariable=self.bot_detail_memo_var, width=56).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(6, 0)
        )
        tk.Checkbutton(bot_detail, text="운영 사용가능", variable=self.bot_detail_active_var).grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )
        tk.Label(
            bot_detail,
            text="변경 시 자동 저장됩니다.",
            anchor="e",
            justify="right",
            fg="#1e3a8a",
        ).grid(row=3, column=3, sticky="e", pady=(6, 0))

        global_users = tk.LabelFrame(tg_cfg, text="전역 허용 사용자 ID", padx=8, pady=6)
        global_users.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.allowed_users_listbox = tk.Listbox(global_users, height=5, width=26)
        self.allowed_users_listbox.grid(row=0, column=0, rowspan=3, sticky="w", pady=(0, 0))
        self.allowed_user_entry = tk.Entry(global_users, width=24)
        self.allowed_user_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))
        tk.Button(global_users, text="ID 검증+추가", width=12, command=self.add_allowed_user).grid(
            row=0, column=2, sticky="w", padx=(6, 0)
        )
        tk.Button(global_users, text="ID 삭제", width=8, command=self.remove_selected_allowed_user).grid(
            row=0, column=3, sticky="w", padx=(6, 0)
        )
        tk.Label(
            global_users,
            text="이 사용자만 허용됩니다.",
            anchor="w",
            justify="left",
            fg="#6d2a2a",
        ).grid(row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(6, 0))

        tg_cfg.grid_rowconfigure(2, weight=1)
        tg_cfg.grid_columnconfigure(0, weight=1)
        tg_cfg.grid_columnconfigure(1, weight=1)
        tg_cfg.grid_columnconfigure(2, weight=1)

        log_head = tk.Frame(ops_tab)
        log_head.pack(fill=tk.X, anchor="w")
        tk.Label(log_head, text=tr("label_latest_log")).pack(side=tk.LEFT, anchor="w")
        tk.Button(log_head, text=tr("btn_log_detail"), width=10, command=self.open_log_detail).pack(
            side=tk.RIGHT, anchor="e"
        )
        tk.Label(ops_tab, textvariable=self.log_var, justify="left", anchor="w", width=100).pack(
            anchor="w", pady=(0, 12)
        )

        self._load_codex_preferences_ui()
        self._load_telegram_config_ui(force=True)
        self.refresh_status()
        self.after(500, self.prompt_autostart_once)
        self.after(2000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        self.refresh_status()
        self.after(2000, self._auto_refresh)

    @staticmethod
    def _safe_bot_key(bot_id: str) -> str:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(bot_id or "").strip())
        return key or "unknown"

    @staticmethod
    def _latest_file_by_patterns(base_dir: Path, patterns: tuple[str, ...]) -> Path | None:
        if not base_dir.exists():
            return None
        candidates: list[Path] = []
        for pattern in patterns:
            for path in base_dir.glob(pattern):
                if path.is_file():
                    candidates.append(path)
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        except OSError:
            return sorted(candidates)[-1]

    @staticmethod
    def _read_json_dict(path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if isinstance(loaded, dict):
            return loaded
        return {}

    def _bot_workspace_path(self, bot_id: str) -> Path:
        return BOT_WORKSPACES_DIR / self._safe_bot_key(bot_id)

    @staticmethod
    def _is_any_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if IS_WINDOWS_NATIVE:
            return _is_wsl_pid_alive(pid)
        return _is_pid_alive(pid)

    def _bot_runtime_snapshot(self, row: dict[str, object]) -> tuple[str, str]:
        monitor_enabled = bool(row.get("active", False))
        monitor_state = "사용가능" if monitor_enabled else "사용중지"
        if not monitor_enabled:
            return "비활성", monitor_state

        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            return "중지", monitor_state

        workspace = BOT_WORKSPACES_DIR / self._safe_bot_key(bot_id)
        state_dir = workspace / "state"
        logs_dir = workspace / "logs"
        worker_pid = _read_pid(state_dir / "daemon-worker.pid")
        worker_alive = _is_service_pid_alive(worker_pid, "daemon_service.py")
        codex_pid = _read_pid(state_dir / "codex-app-server.pid")
        if codex_pid <= 0:
            # Transition compatibility: worker not restarted yet may still write old pid filename.
            codex_pid = _read_pid(state_dir / "codex-runner.pid")
        codex_alive = self._is_any_pid_alive(codex_pid)

        session_meta = self._read_json_dict(logs_dir / "codex-session-current.json")
        has_active_turn = False
        sessions = session_meta.get("sessions")
        if isinstance(sessions, dict):
            for payload in sessions.values():
                if not isinstance(payload, dict):
                    continue
                active_turn = str(payload.get("active_turn_id") or "").strip()
                queued_count = int(payload.get("queued_count") or 0)
                if active_turn or queued_count > 0:
                    has_active_turn = True
                    break

        activity_recent = False
        activity_file = self._latest_file_by_patterns(logs_dir, ("codex-app-server*.log",))
        try:
            if activity_file is None:
                raise OSError("activity log missing")
            age = time.time() - activity_file.stat().st_mtime
            activity_recent = age <= 45.0
        except OSError:
            activity_recent = False

        if not worker_alive:
            work_state = "중지"
        elif has_active_turn:
            work_state = "작업중"
        elif codex_alive and activity_recent:
            work_state = "작업중"
        else:
            work_state = "유휴"

        return work_state, monitor_state

    @staticmethod
    def _task_dirs_sorted(tasks_dir: Path) -> list[Path]:
        out: list[Path] = []
        if not tasks_dir.exists():
            return out
        for path in tasks_dir.glob("msg_*"):
            if path.is_dir():
                out.append(path)
        for chat_dir in tasks_dir.glob("chat_*"):
            if not chat_dir.is_dir():
                continue
            for path in chat_dir.glob("msg_*"):
                if path.is_dir():
                    out.append(path)
        try:
            out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            out.sort(key=lambda p: str(p), reverse=True)
        return out

    @staticmethod
    def _display_value(value: object) -> str:
        rendered = str(value or "").strip()
        return rendered if rendered else "-"

    def _open_bot_codex_log_by_row(self, row: dict[str, object]) -> None:
        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            messagebox.showinfo(tr("msg_info"), tr("msg_select_bot_first"))
            return
        logs_dir = self._bot_workspace_path(bot_id) / "logs"
        main_log_path = self._latest_file_by_patterns(
            logs_dir,
            ("codex-app-server*.log",),
        )
        rewriter_log_path = self._latest_file_by_patterns(
            logs_dir,
            ("codex-agent-rewriter*.log",),
        )
        if main_log_path is None and rewriter_log_path is None:
            messagebox.showinfo(tr("msg_info"), tr("msg_bot_codex_log_not_found", bot_id=bot_id))
            return

        main_content = _read_log_file(main_log_path) if main_log_path is not None else ""
        if not main_content:
            main_content = tr("msg_bot_main_log_empty", bot_id=bot_id)

        rewriter_content = _read_log_file(rewriter_log_path) if rewriter_log_path is not None else ""
        if not rewriter_content:
            rewriter_content = tr("msg_bot_rewriter_log_empty", bot_id=bot_id)

        title_parts = [f"{tr('btn_bot_log_view')} - bot_{bot_id}"]
        if main_log_path is not None:
            title_parts.append(main_log_path.name)
        if rewriter_log_path is not None:
            title_parts.append(rewriter_log_path.name)
        self._show_log_tabs_window(
            " | ".join(title_parts),
            [
                (tr("tab_bot_log_main"), main_content),
                (tr("tab_bot_log_rewriter"), rewriter_content),
            ],
        )

    def _open_selected_bot_codex_log(self) -> None:
        row = self._selected_bot_row()
        if row is None:
            messagebox.showinfo(tr("msg_info"), tr("msg_select_bot_first"))
            return
        self._open_bot_codex_log_by_row(row)

    def _open_bot_detail_by_row(self, row: dict[str, object]) -> None:
        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            messagebox.showinfo(tr("msg_info"), tr("msg_select_bot_first"))
            return

        workspace = self._bot_workspace_path(bot_id)
        logs_dir = workspace / "logs"
        tasks_dir = workspace / "tasks"
        state_dir = workspace / "state"
        messages_store = workspace / "messages" / "telegram_messages.json"
        results_dir = workspace / "results"

        worker_pid = _read_pid(state_dir / "daemon-worker.pid")
        worker_alive = _is_service_pid_alive(worker_pid, "daemon_service.py")
        codex_pid = _read_pid(state_dir / "codex-app-server.pid")
        if codex_pid <= 0:
            codex_pid = _read_pid(state_dir / "codex-runner.pid")
        codex_alive = self._is_any_pid_alive(codex_pid)

        session_meta_path = logs_dir / "codex-session-current.json"
        app_state_path = state_dir / "codex-app-session-state.json"
        session_meta = self._read_json_dict(session_meta_path)
        app_state = self._read_json_dict(app_state_path)

        model = str(session_meta.get("model") or self.codex_model_var.get() or "").strip()
        reasoning = str(session_meta.get("reasoning_effort") or self.codex_reasoning_var.get() or "").strip()
        codex_version = str(session_meta.get("codex_cli_version") or "").strip()
        transport = str(session_meta.get("transport") or "").strip()
        listen = str(session_meta.get("listen") or "").strip()
        run_id = str(session_meta.get("run_id") or "").strip()
        thread_id = str(session_meta.get("thread_id") or "").strip()
        updated_at = str(session_meta.get("updated_at") or "").strip()

        thread_ids_by_chat = session_meta.get("thread_ids_by_chat")
        if isinstance(thread_ids_by_chat, dict):
            thread_chat_count = len(thread_ids_by_chat)
        else:
            thread_chat_count = 0

        app_chats = app_state.get("chats")
        if isinstance(app_chats, dict):
            app_chat_count = len(app_chats)
        else:
            app_chat_count = 0

        task_dirs = self._task_dirs_sorted(tasks_dir)
        recent_task_dirs = []
        for task_path in task_dirs[:8]:
            try:
                rel = task_path.relative_to(tasks_dir)
                recent_task_dirs.append(str(rel))
            except Exception:
                recent_task_dirs.append(str(task_path.name))

        latest_codex_log = self._latest_file_by_patterns(
            logs_dir,
            ("codex-app-server*.log",),
        )

        lines: list[str] = [
            f"bot_id: {self._display_value(bot_id)}",
            f"username: {self._display_value('@' + str(row.get('bot_username') or '').strip() if str(row.get('bot_username') or '').strip() else '')}",
            f"표시 이름(alias): {self._display_value(row.get('alias'))}",
            f"운영여부(active): {'사용가능' if bool(row.get('active', False)) else '사용중지'}",
            "",
            "[프로세스]",
            f"worker_pid: {worker_pid if worker_pid > 0 else '-'} ({'alive' if worker_alive else 'dead'})",
            f"codex_pid: {codex_pid if codex_pid > 0 else '-'} ({'alive' if codex_alive else 'dead'})",
            "",
            "[경로]",
            f"workspace: {workspace}",
            f"tasks_dir: {tasks_dir}",
            f"logs_dir: {logs_dir}",
            f"state_dir: {state_dir}",
            f"messages_store: {messages_store}",
            f"results_dir: {results_dir}",
            "",
            "[Task 폴더 요약]",
            f"task_folder_count: {len(task_dirs)}",
        ]
        if recent_task_dirs:
            lines.append("recent_task_folders:")
            for item in recent_task_dirs:
                lines.append(f"  - {item}")
        else:
            lines.append("recent_task_folders: -")

        lines.extend(
            [
                "",
                "[Codex 메타]",
                f"model: {self._display_value(model)}",
                f"reasoning: {self._display_value(reasoning)}",
                f"version: {self._display_value(codex_version)}",
                f"transport: {self._display_value(transport)}",
                f"listen: {self._display_value(listen)}",
                f"run_id: {self._display_value(run_id)}",
                f"thread_id: {self._display_value(thread_id)}",
                f"threads_by_chat_count: {thread_chat_count}",
                f"app_state_chat_count: {app_chat_count}",
                f"updated_at: {self._display_value(updated_at)}",
                f"session_meta_file: {session_meta_path if session_meta_path.exists() else '-'}",
                f"app_state_file: {app_state_path if app_state_path.exists() else '-'}",
                f"latest_codex_log: {latest_codex_log if latest_codex_log is not None else '-'}",
            ]
        )

        self._show_log_content_window(
            f"{tr('msg_bot_detail_title')} - bot_{bot_id}",
            "\n".join(lines),
        )

    def _open_selected_bot_detail(self) -> None:
        row = self._selected_bot_row()
        if row is None:
            messagebox.showinfo(tr("msg_info"), tr("msg_select_bot_first"))
            return
        self._open_bot_detail_by_row(row)

    def _count_bot_worker_processes(self) -> int:
        cfg = load_bots_config(BOTS_CONFIG_FILE)
        bots = cfg.get("bots") if isinstance(cfg.get("bots"), list) else []
        count = 0
        for row in bots:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("active", False)):
                continue
            bot_id = str(row.get("bot_id") or "").strip()
            if not bot_id:
                continue
            key = self._safe_bot_key(bot_id)
            pid_path = BOT_WORKSPACES_DIR / key / "state" / "daemon-worker.pid"
            pid = _read_pid(pid_path)
            if _is_service_pid_alive(pid, "daemon_service.py"):
                count += 1
        return count

    def refresh_status(self) -> None:
        daemon_pid = _read_pid(PID_FILE)

        daemon_alive = _is_service_pid_alive(daemon_pid, "daemon_service.py")
        worker_count = self._count_bot_worker_processes()
        state_text = "START" if daemon_alive else "STOP"
        self.title(f"{tr('window_title')} [{state_text}]")

        autostart_enabled = _is_autostart_enabled()
        has_gui = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt" or sys.platform == "darwin")

        self.status_var.set(tr("status_running") if daemon_alive else tr("status_stopped"))
        self.daemon_state_var.set(f"데몬 상태: {self.status_var.get()}")
        self.pid_var.set(str(daemon_pid) if daemon_alive else "-")
        if daemon_alive:
            manager_label = str(daemon_pid) if daemon_pid > 0 else "-"
            self.codex_var.set(f"manager={manager_label}, workers={worker_count}")
        else:
            manager_label = "-"
            self.codex_var.set("-")
        self.gui_var.set(tr("status_gui_available") if has_gui else tr("status_gui_not_detected"))
        self.autostart_var.set(
            tr("status_autostart_enabled") if autostart_enabled else tr("status_autostart_disabled")
        )

        # One-line runtime status chips (daemon/codex/gui/autostart)
        self.runtime_daemon_var.set(f"DAEMON PID: {self.pid_var.get()}")
        if daemon_alive:
            self.runtime_codex_var.set(f"CODEX PID: mgr={manager_label}, workers={worker_count}")
        else:
            self.runtime_codex_var.set("CODEX PID: -")
        self.runtime_gui_var.set(f"GUI SESSION: {self.gui_var.get()}")
        if sys.platform == "darwin":
            prefix = "LOGIN AUTOSTART"
        elif IS_WINDOWS_NATIVE:
            prefix = "WINDOWS AUTOSTART"
        else:
            prefix = "AUTOSTART"
        self.runtime_autostart_var.set(f"{prefix}: {self.autostart_var.get()}")
        self._set_runtime_chip_style(
            self.daemon_state_chip,
            bg=("#dcfce7" if daemon_alive else "#fee2e2"),
            fg=("#14532d" if daemon_alive else "#7f1d1d"),
        )
        self._set_runtime_chip_style(
            self.runtime_daemon_chip,
            bg=("#dcfce7" if daemon_alive else "#fee2e2"),
            fg=("#14532d" if daemon_alive else "#7f1d1d"),
        )
        self._set_runtime_chip_style(
            self.runtime_codex_chip,
            bg=("#dbeafe" if daemon_alive else "#e5e7eb"),
            fg=("#1e3a8a" if daemon_alive else "#374151"),
        )
        self._set_runtime_chip_style(
            self.runtime_gui_chip,
            bg=("#dcfce7" if has_gui else "#fef3c7"),
            fg=("#14532d" if has_gui else "#92400e"),
        )
        self._set_runtime_chip_style(
            self.runtime_autostart_chip,
            bg=("#dbeafe" if autostart_enabled else "#e5e7eb"),
            fg=("#1e3a8a" if autostart_enabled else "#374151"),
        )

        self.log_var.set(_compact_log_line(_latest_daemon_log_line()))
        self._refresh_telegram_ui_if_needed()
        self._sync_state_buttons(daemon_alive)

    def _set_runtime_chip_style(self, chip: tk.Label | None, *, bg: str, fg: str) -> None:
        if chip is None:
            return
        chip.configure(bg=bg, fg=fg)

    def _load_codex_preferences_ui(self) -> None:
        model, reasoning, rewriter_model, rewriter_reasoning, path = _load_codex_preferences()
        if model not in CODEX_MODEL_CHOICES:
            model = DEFAULT_CODEX_MODEL
        if reasoning not in CODEX_REASONING_CHOICES:
            reasoning = DEFAULT_CODEX_REASONING_EFFORT
        if rewriter_model not in CODEX_MODEL_CHOICES:
            rewriter_model = DEFAULT_REWRITER_CODEX_MODEL
        if rewriter_reasoning not in CODEX_REASONING_CHOICES:
            rewriter_reasoning = DEFAULT_REWRITER_CODEX_REASONING_EFFORT
        self.codex_model_var.set(model)
        self.codex_reasoning_var.set(reasoning)
        self.rewriter_model_var.set(rewriter_model)
        self.rewriter_reasoning_var.set(rewriter_reasoning)
        self.codex_config_path_var.set(path or "-")
        prompt_text, prompt_path = _load_rewriter_prompt()
        self.rewriter_prompt_path_var.set(prompt_path or "-")
        self._set_rewriter_prompt_ui(prompt_text)

    def _set_rewriter_prompt_ui(self, text: str) -> None:
        if self.rewriter_prompt_text is None:
            return
        self.rewriter_prompt_text.delete("1.0", tk.END)
        self.rewriter_prompt_text.insert("1.0", str(text or "").strip())

    def _get_rewriter_prompt_ui(self) -> str:
        if self.rewriter_prompt_text is None:
            return DEFAULT_REWRITER_PROMPT_TEXT
        text = str(self.rewriter_prompt_text.get("1.0", tk.END) or "").strip()
        return text or DEFAULT_REWRITER_PROMPT_TEXT

    def _bots_config_last_mtime(self) -> float:
        try:
            return BOTS_CONFIG_FILE.stat().st_mtime
        except OSError:
            return -1.0

    def _refresh_telegram_ui_if_needed(self) -> None:
        mtime = self._bots_config_last_mtime()
        if self._bots_config_mtime is not None and mtime == self._bots_config_mtime:
            return
        self._load_telegram_config_ui(force=True)

    @staticmethod
    def _sorted_bot_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            rows,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def _load_telegram_config_ui(self, force: bool = False) -> None:
        if not force:
            mtime = self._bots_config_last_mtime()
            if self._bots_config_mtime is not None and mtime == self._bots_config_mtime:
                return
        data = load_bots_config(BOTS_CONFIG_FILE)
        bots = data.get("bots") if isinstance(data.get("bots"), list) else []
        allowed = data.get("allowed_users_global") if isinstance(data.get("allowed_users_global"), list) else []

        selected_bot_id = ""
        selected_row = self._selected_bot_row()
        if isinstance(selected_row, dict):
            selected_bot_id = str(selected_row.get("bot_id") or "")

        selected_user = ""
        if self.allowed_users_listbox is not None:
            sel_user = self.allowed_users_listbox.curselection()
            if sel_user:
                selected_user = str(self.allowed_users_listbox.get(int(sel_user[0]))).strip()

        self._bot_rows_cache = [row for row in bots if isinstance(row, dict)]
        if selected_bot_id:
            ordered = self._sorted_bot_rows(self._bot_rows_cache)
            for idx, row in enumerate(ordered):
                if str(row.get("bot_id") or "").strip() == selected_bot_id:
                    self.bot_page_index = idx // self.bot_page_size
                    break
        self._render_bot_page(selected_bot_id=selected_bot_id)

        if self.allowed_users_listbox is not None:
            self.allowed_users_listbox.delete(0, tk.END)
            normalized: list[int] = []
            for item in allowed:
                try:
                    uid = int(item)
                except Exception:
                    continue
                if uid > 0:
                    normalized.append(uid)
            selected_user_idx: int | None = None
            for uid in sorted(set(normalized)):
                self.allowed_users_listbox.insert(tk.END, str(uid))
                if selected_user and str(uid) == selected_user:
                    selected_user_idx = self.allowed_users_listbox.size() - 1
            if selected_user_idx is not None:
                self.allowed_users_listbox.selection_set(selected_user_idx)

        self._bots_config_mtime = self._bots_config_last_mtime()
        self._on_bot_list_select()

    def _render_bot_page(self, selected_bot_id: str = "") -> None:
        if self.bot_tree is None:
            return
        self.bot_tree.delete(*self.bot_tree.get_children())
        self._bot_rows_page_cache = []

        ordered = self._sorted_bot_rows(self._bot_rows_cache)
        total = len(ordered)
        if total <= 0:
            self.bot_empty_hint_var.set("등록된 봇이 없습니다. '봇 추가'로 새 봇을 등록하세요.")
            self.bot_page_var.set("0 / 0")
            if self.bot_page_prev_btn is not None:
                self.bot_page_prev_btn.config(state=tk.DISABLED)
            if self.bot_page_next_btn is not None:
                self.bot_page_next_btn.config(state=tk.DISABLED)
            if self.bot_log_view_btn is not None:
                self.bot_log_view_btn.config(state=tk.DISABLED)
            if self.bot_detail_view_btn is not None:
                self.bot_detail_view_btn.config(state=tk.DISABLED)
            self._clear_selected_bot_details()
            return

        total_pages = max(1, (total + self.bot_page_size - 1) // self.bot_page_size)
        self.bot_page_index = max(0, min(self.bot_page_index, total_pages - 1))
        start = self.bot_page_index * self.bot_page_size
        end = min(start + self.bot_page_size, total)
        page_rows = ordered[start:end]
        self._bot_rows_page_cache = page_rows
        self.bot_empty_hint_var.set(
            f"총 {total}개 봇 | 페이지 {self.bot_page_index + 1}/{total_pages} | 정렬: 최근 수정순"
        )
        self.bot_page_var.set(f"{self.bot_page_index + 1} / {total_pages}")
        if self.bot_page_prev_btn is not None:
            self.bot_page_prev_btn.config(state=(tk.NORMAL if self.bot_page_index > 0 else tk.DISABLED))
        if self.bot_page_next_btn is not None:
            self.bot_page_next_btn.config(
                state=(tk.NORMAL if self.bot_page_index < total_pages - 1 else tk.DISABLED)
            )

        selected_iid = ""
        for idx, row in enumerate(page_rows):
            work_state, monitor_state = self._bot_runtime_snapshot(row)
            bot_id = str(row.get("bot_id") or "-")
            uname = str(row.get("bot_username") or "").strip()
            name = str(row.get("bot_name") or "").strip()
            alias = str(row.get("alias") or "").strip()
            display = alias or name or (f"@{uname}" if uname else "-")
            updated = str(row.get("updated_at") or "-")
            iid = str(idx)
            tags = ("monitor_off",) if monitor_state == "사용중지" else ()
            self.bot_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(work_state, monitor_state, display, f"@{uname}" if uname else "-", bot_id, updated),
                tags=tags,
            )
            if selected_bot_id and selected_bot_id == bot_id:
                selected_iid = iid

        if not selected_iid and page_rows:
            selected_iid = "0"
        if selected_iid:
            self.bot_tree.selection_set(selected_iid)
            self.bot_tree.focus(selected_iid)
            self.bot_tree.see(selected_iid)

    def _go_bot_prev_page(self) -> None:
        if self.bot_page_index <= 0:
            return
        self.bot_page_index -= 1
        self._render_bot_page()
        self._on_bot_list_select()

    def _go_bot_next_page(self) -> None:
        ordered = self._sorted_bot_rows(self._bot_rows_cache)
        total = len(ordered)
        if total <= 0:
            return
        total_pages = max(1, (total + self.bot_page_size - 1) // self.bot_page_size)
        if self.bot_page_index >= total_pages - 1:
            return
        self.bot_page_index += 1
        self._render_bot_page()
        self._on_bot_list_select()

    def _clear_selected_bot_details(self) -> None:
        self._suspend_bot_detail_autosave = True
        try:
            self.bot_detail_bot_id_var.set("-")
            self.bot_detail_username_var.set("-")
            self.bot_detail_name_var.set("-")
            self.bot_detail_alias_var.set("")
            self.bot_detail_memo_var.set("")
            self.bot_detail_active_var.set(False)
        finally:
            self._suspend_bot_detail_autosave = False

    def _set_selected_bot_details(self, row: dict[str, object]) -> None:
        self._suspend_bot_detail_autosave = True
        try:
            self.bot_detail_bot_id_var.set(str(row.get("bot_id") or "-"))
            uname = str(row.get("bot_username") or "").strip()
            self.bot_detail_username_var.set(f"@{uname}" if uname else "-")
            self.bot_detail_name_var.set(str(row.get("bot_name") or "").strip() or "-")
            self.bot_detail_alias_var.set(str(row.get("alias") or "").strip())
            self.bot_detail_memo_var.set(str(row.get("memo") or "").strip())
            self.bot_detail_active_var.set(bool(row.get("active", False)))
        finally:
            self._suspend_bot_detail_autosave = False

    def _on_bot_detail_var_changed(self, *_args: object) -> None:
        if self._suspend_bot_detail_autosave:
            return
        if self._selected_bot_row() is None:
            return
        if self._bot_detail_autosave_job:
            try:
                self.after_cancel(self._bot_detail_autosave_job)
            except Exception:
                pass
        self._bot_detail_autosave_job = self.after(250, self._auto_save_selected_bot_details)

    def _auto_save_selected_bot_details(self) -> None:
        self._bot_detail_autosave_job = None
        self.save_selected_bot_details(notify=False)

    def _on_bot_list_select(self, _event: object | None = None) -> None:
        row = self._selected_bot_row()
        if row is None:
            if self.bot_log_view_btn is not None:
                self.bot_log_view_btn.config(state=tk.DISABLED)
            if self.bot_detail_view_btn is not None:
                self.bot_detail_view_btn.config(state=tk.DISABLED)
            self._clear_selected_bot_details()
            return
        if self.bot_log_view_btn is not None:
            self.bot_log_view_btn.config(state=tk.NORMAL)
        if self.bot_detail_view_btn is not None:
            self.bot_detail_view_btn.config(state=tk.NORMAL)
        self._set_selected_bot_details(row)

    def _select_bot_by_id(self, bot_id: str) -> None:
        if self.bot_tree is None:
            return
        target = str(bot_id or "").strip()
        if not target:
            return
        ordered = self._sorted_bot_rows(self._bot_rows_cache)
        for idx, row in enumerate(ordered):
            if str(row.get("bot_id") or "").strip() == target:
                self.bot_page_index = idx // self.bot_page_size
                self._render_bot_page(selected_bot_id=target)
                self._on_bot_list_select()
                return

    def _collect_allowed_users_from_ui(self) -> list[int]:
        if self.allowed_users_listbox is None:
            return []
        out: list[int] = []
        for idx in range(self.allowed_users_listbox.size()):
            raw = self.allowed_users_listbox.get(idx).strip()
            try:
                uid = int(raw)
            except Exception:
                continue
            if uid > 0:
                out.append(uid)
        return sorted(set(out))

    def _persist_telegram_config(self, notify: bool) -> bool:
        data = load_bots_config(BOTS_CONFIG_FILE)
        data["allowed_users_global"] = self._collect_allowed_users_from_ui()
        data["bots"] = self._bot_rows_cache
        try:
            save_bots_config(BOTS_CONFIG_FILE, data)
        except Exception as exc:
            messagebox.showerror(tr("msg_error"), f"텔레그램 설정 저장 실패: {exc}")
            return False
        self._load_telegram_config_ui(force=True)
        if notify:
            messagebox.showinfo(tr("msg_info"), f"텔레그램 설정 저장 완료\n{BOTS_CONFIG_FILE}")
        return True

    def save_telegram_config(self) -> None:
        ok, detail = self._validate_allowed_users_before_save()
        if not ok:
            messagebox.showerror(tr("msg_error"), detail)
            return
        self._persist_telegram_config(notify=True)

    def _all_configured_tokens(self) -> list[str]:
        out: list[str] = []
        for row in self._bot_rows_cache:
            if not isinstance(row, dict):
                continue
            token = str(row.get("token") or "").strip()
            if token:
                out.append(token)
        return out

    def _validate_allowed_users_before_save(self) -> tuple[bool, str]:
        allowed_ids = self._collect_allowed_users_from_ui()
        if not allowed_ids:
            return False, "허용 사용자 ID가 없습니다. 최소 1개 이상 등록하세요."

        for uid in allowed_ids:
            ok_fmt, parsed_uid, err = validate_user_id_format(str(uid))
            if not ok_fmt or parsed_uid is None:
                return False, f"허용 사용자 ID 형식 오류: {uid}\n{err}"

        tokens = self._all_configured_tokens()
        if not tokens:
            # Bot token is not configured yet: allow format-only save.
            return True, ""

        token = tokens[0]
        failed: list[str] = []
        for uid in allowed_ids:
            live_ok, live_msg = validate_user_id_live(token, uid, timeout_sec=8.0)
            if not live_ok:
                failed.append(f"- {uid}: {live_msg}")
        if failed:
            preview = "\n".join(failed[:5])
            if len(failed) > 5:
                preview += f"\n...외 {len(failed) - 5}건"
            return False, (
                "허용 사용자 라이브 검증에 실패했습니다.\n"
                "봇과 사용자의 대화 시작 여부/차단 상태를 확인하세요.\n\n"
                f"{preview}"
            )
        return True, ""

    def _validate_multibot_start_config(self) -> tuple[bool, str]:
        data = load_bots_config(BOTS_CONFIG_FILE)
        allowed = data.get("allowed_users_global") if isinstance(data.get("allowed_users_global"), list) else []
        allowed_ids: list[int] = []
        for item in allowed:
            try:
                uid = int(item)
            except Exception:
                continue
            if uid > 0:
                allowed_ids.append(uid)
        if not allowed_ids:
            return False, "허용 사용자 ID가 없습니다. Telegram 설정에서 최소 1개를 등록하세요."

        bots = data.get("bots") if isinstance(data.get("bots"), list) else []
        registered_count = 0
        for row in bots:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("active", False)):
                token = str(row.get("token") or "").strip()
                bot_id = str(row.get("bot_id") or "").strip()
                if token and bot_id:
                    registered_count += 1
                continue
            token = str(row.get("token") or "").strip()
            bot_id = str(row.get("bot_id") or "").strip()
            if token and bot_id:
                registered_count += 1
        if registered_count <= 0:
            return False, "등록된 봇 토큰이 없습니다. Telegram 설정에서 토큰을 검증 후 추가하세요."
        return True, ""

    def _selected_bot_row(self) -> dict[str, object] | None:
        if self.bot_tree is None:
            return None
        selected = self.bot_tree.selection()
        if not selected:
            return None
        try:
            idx = int(str(selected[0]))
        except Exception:
            return None
        if idx < 0 or idx >= len(self._bot_rows_page_cache):
            return None
        row = self._bot_rows_page_cache[idx]
        if not isinstance(row, dict):
            return None
        return row

    def open_add_bot_dialog(self) -> None:
        existing_tokens_by_bot_id: dict[str, str] = {}
        for row in self._bot_rows_cache:
            bot_id = str(row.get("bot_id") or "").strip()
            token = str(row.get("token") or "").strip()
            if bot_id and token:
                existing_tokens_by_bot_id[bot_id] = token

        try:
            dialog = AddBotDialog(self, existing_tokens_by_bot_id)
        except Exception as exc:
            messagebox.showerror(tr("msg_error"), f"봇 추가 창을 열 수 없습니다.\n{exc}")
            return
        self.wait_window(dialog)
        payload = dialog.result
        if not isinstance(payload, dict):
            return

        token = str(payload.get("token") or "").strip()
        bot_id = str(payload.get("bot_id") or "").strip()
        bot_username = str(payload.get("bot_username") or "").strip()
        bot_name = str(payload.get("bot_name") or "").strip()
        if not token or not bot_id:
            return

        data = load_bots_config(BOTS_CONFIG_FILE)
        existing_alias = ""
        existing_memo = ""
        existing_active: bool | None = None
        bots = data.get("bots") if isinstance(data.get("bots"), list) else []
        for item in bots:
            if not isinstance(item, dict):
                continue
            if str(item.get("bot_id") or "").strip() != bot_id:
                continue
            existing_alias = str(item.get("alias") or "").strip()
            existing_memo = str(item.get("memo") or "").strip()
            existing_active = bool(item.get("active", False))
            break
        # New bot defaults to active so manager can spawn worker immediately.
        target_active = existing_active if isinstance(existing_active, bool) else True
        try:
            data = upsert_bot_config(
                data,
                token=token,
                bot_id=bot_id,
                bot_username=bot_username,
                bot_name=bot_name,
                alias=existing_alias,
                memo=existing_memo,
                active=target_active,
            )
        except ValueError as exc:
            messagebox.showerror(tr("msg_error"), str(exc))
            return
        data = set_allowed_users_config(data, self._collect_allowed_users_from_ui())
        try:
            save_bots_config(BOTS_CONFIG_FILE, data)
        except Exception as exc:
            _diag_log(
                f"ERROR add_bot save_failed bot_id={bot_id} config={BOTS_CONFIG_FILE} detail={exc!r}"
            )
            messagebox.showerror(tr("msg_error"), tr("msg_bot_add_save_failed", detail=str(exc)))
            return

        verify_data = load_bots_config(BOTS_CONFIG_FILE)
        verify_bots = verify_data.get("bots") if isinstance(verify_data.get("bots"), list) else []
        verify_row: dict[str, object] | None = None
        for item in verify_bots:
            if not isinstance(item, dict):
                continue
            if str(item.get("bot_id") or "").strip() == bot_id:
                verify_row = item
                break
        if verify_row is None:
            _diag_log(
                f"ERROR add_bot verify_missing bot_id={bot_id} config={BOTS_CONFIG_FILE}"
            )
            messagebox.showerror(
                tr("msg_error"),
                tr("msg_bot_add_verify_failed", config=BOTS_CONFIG_FILE, bot_id=bot_id),
            )
            return
        verify_active = bool(verify_row.get("active", False))
        if verify_active != bool(target_active):
            _diag_log(
                f"WARN add_bot verify_active_mismatch bot_id={bot_id} expected={int(bool(target_active))} actual={int(verify_active)}"
            )
        _diag_log(
            f"INFO add_bot saved bot_id={bot_id} active={int(verify_active)} config={BOTS_CONFIG_FILE}"
        )

        self._load_telegram_config_ui(force=True)
        self._select_bot_by_id(bot_id)
        messagebox.showinfo(
            tr("msg_info"),
            tr("msg_bot_add_done", bot_id=bot_id, bot_username=bot_username or "-"),
        )
        allowed_after = verify_data.get("allowed_users_global")
        if not isinstance(allowed_after, list) or len(allowed_after) <= 0:
            messagebox.showwarning(tr("msg_info"), tr("msg_bot_add_warn_no_allowed_users"))

    def add_bot_token(self) -> None:
        # backward-compatible alias for old button wiring
        self.open_add_bot_dialog()

    def save_selected_bot_details(self, notify: bool = True) -> None:
        row = self._selected_bot_row()
        if row is None:
            if notify:
                messagebox.showinfo(tr("msg_info"), "저장할 봇을 선택하세요.")
            return
        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            if notify:
                messagebox.showerror(tr("msg_error"), "선택한 봇의 bot_id가 비어 있습니다.")
            return
        alias = (self.bot_detail_alias_var.get() or "").strip()
        memo = (self.bot_detail_memo_var.get() or "").strip()
        active = bool(self.bot_detail_active_var.get())

        current_alias = str(row.get("alias") or "").strip()
        current_memo = str(row.get("memo") or "").strip()
        current_active = bool(row.get("active", False))
        if alias == current_alias and memo == current_memo and active == current_active:
            return

        updated = False
        for item in self._bot_rows_cache:
            if str(item.get("bot_id") or "").strip() != bot_id:
                continue
            item["alias"] = alias
            item["memo"] = memo
            item["active"] = active
            updated = True
            break
        if not updated:
            if notify:
                messagebox.showerror(tr("msg_error"), "선택한 봇을 캐시에서 찾지 못했습니다.")
            return
        if not self._persist_telegram_config(notify=False):
            return
        self._select_bot_by_id(bot_id)
        if notify:
            messagebox.showinfo(tr("msg_info"), "선택 봇 정보 저장 완료")

    def remove_selected_bot(self) -> None:
        row = self._selected_bot_row()
        if row is None:
            messagebox.showinfo(tr("msg_info"), "삭제할 봇을 선택하세요.")
            return
        bot_id = str(row.get("bot_id") or "").strip()
        data = load_bots_config(BOTS_CONFIG_FILE)
        data = remove_bot_config(data, bot_id)
        data = set_allowed_users_config(data, self._collect_allowed_users_from_ui())
        save_bots_config(BOTS_CONFIG_FILE, data)
        self._load_telegram_config_ui(force=True)

    def toggle_selected_bot_active(self) -> None:
        row = self._selected_bot_row()
        if row is None:
            messagebox.showinfo(tr("msg_info"), "운영여부를 변경할 봇을 선택하세요.")
            return
        bot_id = str(row.get("bot_id") or "").strip()
        active = not bool(row.get("active", False))
        updated = False
        for item in self._bot_rows_cache:
            if str(item.get("bot_id") or "").strip() != bot_id:
                continue
            item["active"] = active
            updated = True
            break
        if not updated:
            messagebox.showerror(tr("msg_error"), "선택한 봇을 캐시에서 찾지 못했습니다.")
            return
        self._suspend_bot_detail_autosave = True
        try:
            self.bot_detail_active_var.set(active)
        finally:
            self._suspend_bot_detail_autosave = False
        self._persist_telegram_config(notify=False)
        self._select_bot_by_id(bot_id)

    def _choose_validation_token(self) -> str:
        tokens = self._all_configured_tokens()
        if tokens:
            return tokens[0]
        return ""

    def add_allowed_user(self) -> None:
        if self.allowed_user_entry is None or self.allowed_users_listbox is None:
            return
        raw = self.allowed_user_entry.get().strip()
        ok, user_id, err = validate_user_id_format(raw)
        if not ok or user_id is None:
            messagebox.showerror(tr("msg_error"), err)
            return

        token = self._choose_validation_token()
        live_ok = False
        live_msg = ""
        if token:
            live_ok, live_msg = validate_user_id_live(token, user_id, timeout_sec=8.0)
        if token and not live_ok:
            proceed = messagebox.askyesno(
                tr("msg_info"),
                f"{live_msg}\n\n형식은 유효합니다. 그래도 추가할까요?",
            )
            if not proceed:
                return

        current = set(self._collect_allowed_users_from_ui())
        current.add(user_id)
        self.allowed_users_listbox.delete(0, tk.END)
        for uid in sorted(current):
            self.allowed_users_listbox.insert(tk.END, str(uid))
        self.allowed_user_entry.delete(0, tk.END)
        self._auto_save_allowed_users()

    def remove_selected_allowed_user(self) -> None:
        if self.allowed_users_listbox is None:
            return
        selected = self.allowed_users_listbox.curselection()
        if not selected:
            messagebox.showinfo(tr("msg_info"), "삭제할 사용자 ID를 선택하세요.")
            return
        if self.allowed_users_listbox.size() <= 1:
            messagebox.showerror(tr("msg_error"), "최소 1개의 허용 사용자 ID가 필요합니다.")
            return
        self.allowed_users_listbox.delete(int(selected[0]))
        self._auto_save_allowed_users()

    def _auto_save_allowed_users(self) -> None:
        ok, detail = self._validate_allowed_users_before_save()
        if not ok:
            messagebox.showerror(tr("msg_error"), detail)
            return
        self._persist_telegram_config(notify=False)

    def apply_codex_preferences(self) -> None:
        model = (self.codex_model_var.get() or "").strip() or DEFAULT_CODEX_MODEL
        reasoning = (self.codex_reasoning_var.get() or "").strip() or DEFAULT_CODEX_REASONING_EFFORT
        rewriter_model = (self.rewriter_model_var.get() or "").strip() or DEFAULT_REWRITER_CODEX_MODEL
        rewriter_reasoning = (
            (self.rewriter_reasoning_var.get() or "").strip() or DEFAULT_REWRITER_CODEX_REASONING_EFFORT
        )
        if model not in CODEX_MODEL_CHOICES:
            model = DEFAULT_CODEX_MODEL
        if reasoning not in CODEX_REASONING_CHOICES:
            reasoning = DEFAULT_CODEX_REASONING_EFFORT
        if rewriter_model not in CODEX_MODEL_CHOICES:
            rewriter_model = DEFAULT_REWRITER_CODEX_MODEL
        if rewriter_reasoning not in CODEX_REASONING_CHOICES:
            rewriter_reasoning = DEFAULT_REWRITER_CODEX_REASONING_EFFORT
        self.codex_model_var.set(model)
        self.codex_reasoning_var.set(reasoning)
        self.rewriter_model_var.set(rewriter_model)
        self.rewriter_reasoning_var.set(rewriter_reasoning)

        ok, detail = _save_codex_preferences(model, reasoning, rewriter_model, rewriter_reasoning)
        if not ok:
            _diag_log(f"WARN codex_prefs_save_failed detail={detail!r}")
            messagebox.showerror(tr("msg_error"), tr("msg_codex_prefs_save_failed", detail=detail))
            return
        prompt_text = self._get_rewriter_prompt_ui()
        ok_prompt, prompt_path = _save_rewriter_prompt(prompt_text)
        if not ok_prompt:
            _diag_log(f"WARN rewriter_prompt_save_failed detail={prompt_path!r}")
            messagebox.showerror(tr("msg_error"), tr("msg_codex_prefs_save_failed", detail=prompt_path))
            return

        self.codex_config_path_var.set(detail)
        self.rewriter_prompt_path_var.set(prompt_path)
        _diag_log(
            "INFO codex_prefs_saved "
            f"model={model} reasoning={reasoning} "
            f"rewriter_model={rewriter_model} rewriter_reasoning={rewriter_reasoning} "
            f"path={detail} prompt_path={prompt_path}"
        )
        messagebox.showinfo(
            tr("msg_info"),
            tr(
                "msg_codex_prefs_saved",
                model=model,
                reasoning=reasoning,
                rewriter_model=rewriter_model,
                rewriter_reasoning=rewriter_reasoning,
                path=detail,
                prompt_path=prompt_path,
            ),
        )

    def save_rewriter_prompt(self) -> None:
        prompt_text = self._get_rewriter_prompt_ui()
        ok_prompt, prompt_path = _save_rewriter_prompt(prompt_text)
        if not ok_prompt:
            _diag_log(f"WARN rewriter_prompt_save_failed detail={prompt_path!r}")
            messagebox.showerror(
                tr("msg_error"),
                tr("msg_rewriter_prompt_save_failed", detail=prompt_path),
            )
            return

        self.rewriter_prompt_path_var.set(prompt_path)
        _diag_log(f"INFO rewriter_prompt_saved prompt_path={prompt_path}")
        messagebox.showinfo(
            tr("msg_info"),
            tr("msg_rewriter_prompt_saved", prompt_path=prompt_path),
        )

    def _sync_state_buttons(self, daemon_alive: bool) -> None:
        if self.btn_start is None or self.btn_stop is None:
            return
        if daemon_alive:
            self.btn_start.config(relief=tk.SUNKEN, bd=3)
            self.btn_stop.config(relief=tk.RAISED, bd=2)
            return
        self.btn_start.config(relief=tk.RAISED, bd=2)
        self.btn_stop.config(relief=tk.SUNKEN, bd=3)

    def _show_log_content_window(self, title: str, content: str) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("920x600")

        outer = tk.Frame(win, padx=8, pady=8)
        outer.pack(fill=tk.BOTH, expand=True)

        y_scroll = tk.Scrollbar(outer, orient=tk.VERTICAL)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll = tk.Scrollbar(outer, orient=tk.HORIZONTAL)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        text = tk.Text(
            outer,
            wrap=tk.NONE,
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
            font=("Consolas", 10),
        )
        text.pack(fill=tk.BOTH, expand=True)
        y_scroll.config(command=text.yview)
        x_scroll.config(command=text.xview)

        text.insert("1.0", content)
        text.config(state=tk.DISABLED)

    def _show_log_tabs_window(self, title: str, tabs: list[tuple[str, str]]) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("980x660")

        outer = tk.Frame(win, padx=8, pady=8)
        outer.pack(fill=tk.BOTH, expand=True)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)

        for tab_title, tab_content in tabs:
            tab_frame = tk.Frame(notebook)
            notebook.add(tab_frame, text=tab_title)

            y_scroll = tk.Scrollbar(tab_frame, orient=tk.VERTICAL)
            y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            x_scroll = tk.Scrollbar(tab_frame, orient=tk.HORIZONTAL)
            x_scroll.pack(side=tk.BOTTOM, fill=tk.X)

            text = tk.Text(
                tab_frame,
                wrap=tk.NONE,
                yscrollcommand=y_scroll.set,
                xscrollcommand=x_scroll.set,
                font=("Consolas", 10),
            )
            text.pack(fill=tk.BOTH, expand=True)
            y_scroll.config(command=text.yview)
            x_scroll.config(command=text.xview)

            text.insert("1.0", tab_content)
            text.config(state=tk.DISABLED)

    def _open_log_detail_by_key(self, key: str) -> None:
        selected: tuple[str, str, str] | None = None
        for spec in LOG_TYPE_SPECS:
            if spec[0] == key:
                selected = spec
                break
        if selected is None:
            return

        _, pattern, label_key = selected
        label = tr(label_key)
        log_path = _latest_log_file_by_pattern(pattern)
        if log_path is None:
            messagebox.showinfo(tr("msg_info"), tr("msg_log_detail_not_found", label=label))
            return

        content = _read_log_file(log_path)
        if not content:
            messagebox.showinfo(tr("msg_info"), tr("msg_log_detail_not_found", label=label))
            return

        window_title = f"{tr('msg_log_detail_title')} - {label} - {log_path.name}"
        self._show_log_content_window(window_title, content)

    def open_log_detail(self) -> None:
        picker = tk.Toplevel(self)
        picker.title(tr("msg_log_picker_title"))
        picker.geometry("460x270")
        picker.resizable(False, False)
        picker.transient(self)

        root_frame = tk.Frame(picker, padx=12, pady=12)
        root_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(root_frame, text=tr("msg_log_picker_desc")).pack(anchor="w", pady=(0, 8))

        selected_key = tk.StringVar(value=LOG_TYPE_SPECS[0][0])
        for key, _pattern, label_key in LOG_TYPE_SPECS:
            tk.Radiobutton(
                root_frame,
                text=tr(label_key),
                variable=selected_key,
                value=key,
                anchor="w",
                justify="left",
                wraplength=420,
            ).pack(fill=tk.X, anchor="w")

        btns = tk.Frame(root_frame)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))

        def _open_and_close() -> None:
            chosen = selected_key.get().strip()
            picker.destroy()
            self._open_log_detail_by_key(chosen)

        tk.Button(btns, text=tr("btn_open"), width=10, command=_open_and_close).pack(side=tk.RIGHT)
        tk.Button(btns, text=tr("btn_cancel"), width=10, command=picker.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def on_window_close(self) -> None:
        # Keep the app accessible from taskbar: close button behaves as minimize.
        try:
            self.iconify()
            _diag_log("INFO panel_window_close iconify")
        except Exception as exc:
            _diag_log(f"WARN panel_window_close iconify_failed exc={exc!r}")
            self.destroy()

    def exit_panel(self) -> None:
        _diag_log("INFO panel_exit requested stop_daemon_on_exit=1")
        self._stop_daemon_internal(notify_when_not_running=False)
        self.destroy()

    def start_daemon(self) -> None:
        _diag_log("INFO daemon_start requested")

        ok_users, users_detail = self._validate_allowed_users_before_save()
        if not ok_users:
            messagebox.showerror(tr("msg_error"), users_detail)
            self.refresh_status()
            return

        if not self._persist_telegram_config(notify=False):
            self.refresh_status()
            return
        ok_cfg, cfg_detail = self._validate_multibot_start_config()
        if not ok_cfg:
            messagebox.showerror(tr("msg_error"), cfg_detail)
            self.refresh_status()
            return

        pid = _read_pid(PID_FILE)
        if _is_service_pid_alive(pid, "daemon_service.py"):
            messagebox.showinfo(tr("msg_info"), tr("msg_daemon_running", pid=pid))
            self.refresh_status()
            return

        if not SERVICE_SCRIPT.exists():
            messagebox.showerror(tr("msg_error"), tr("msg_missing_file", path=SERVICE_SCRIPT))
            return

        model = (self.codex_model_var.get() or "").strip() or DEFAULT_CODEX_MODEL
        reasoning = (self.codex_reasoning_var.get() or "").strip() or DEFAULT_CODEX_REASONING_EFFORT
        rewriter_model = (self.rewriter_model_var.get() or "").strip() or DEFAULT_REWRITER_CODEX_MODEL
        rewriter_reasoning = (
            (self.rewriter_reasoning_var.get() or "").strip() or DEFAULT_REWRITER_CODEX_REASONING_EFFORT
        )
        if model not in CODEX_MODEL_CHOICES:
            model = DEFAULT_CODEX_MODEL
        if reasoning not in CODEX_REASONING_CHOICES:
            reasoning = DEFAULT_CODEX_REASONING_EFFORT
        if rewriter_model not in CODEX_MODEL_CHOICES:
            rewriter_model = DEFAULT_REWRITER_CODEX_MODEL
        if rewriter_reasoning not in CODEX_REASONING_CHOICES:
            rewriter_reasoning = DEFAULT_REWRITER_CODEX_REASONING_EFFORT
        self.codex_model_var.set(model)
        self.codex_reasoning_var.set(reasoning)
        self.rewriter_model_var.set(rewriter_model)
        self.rewriter_reasoning_var.set(rewriter_reasoning)
        ok_pref, detail_pref = _save_codex_preferences(model, reasoning, rewriter_model, rewriter_reasoning)
        if ok_pref:
            self.codex_config_path_var.set(detail_pref)
        else:
            _diag_log(f"WARN codex_prefs_autosave_failed detail={detail_pref!r}")
        prompt_text = self._get_rewriter_prompt_ui()
        ok_prompt, prompt_path = _save_rewriter_prompt(prompt_text)
        if ok_prompt:
            self.rewriter_prompt_path_var.set(prompt_path)
        else:
            _diag_log(f"WARN rewriter_prompt_autosave_failed detail={prompt_path!r}")
            messagebox.showerror(tr("msg_error"), tr("msg_codex_prefs_save_failed", detail=prompt_path))
            self.refresh_status()
            return

        if IS_WINDOWS_NATIVE:
            wsl_root, wsl_service_script = _wsl_path_bundle()
            if not wsl_root or not wsl_service_script:
                _diag_log("ERROR daemon_start aborted wsl_path_unresolved")
                messagebox.showerror(tr("msg_error"), tr("msg_wsl_path_not_resolved"))
                return
            wsl_bots_config = _to_wsl_path(BOTS_CONFIG_FILE)
            wsl_bot_workspaces = _to_wsl_path(BOT_WORKSPACES_DIR)
            wsl_rewriter_prompt_path = _to_wsl_path(Path(prompt_path))
            script = (
                f"cd {shlex.quote(wsl_root)} || exit 1; "
                "PY='.venv/bin/python'; "
                "if [ ! -x \"$PY\" ]; then PY='python3'; fi; "
                "if [ \"$PY\" = 'python3' ] && ! command -v python3 >/dev/null 2>&1; then "
                "  echo 'python3 not found in WSL PATH'; exit 127; "
                "fi; "
                "if ! command -v codex >/dev/null 2>&1; then "
                "  echo 'codex CLI not found in WSL PATH'; exit 126; "
                "fi; "
                "mkdir -p logs; "
                "touch logs/.daemon_write_test.$$ >/dev/null 2>&1 || "
                "{ echo 'logs directory is not writable'; exit 13; }; "
                "rm -f logs/.daemon_write_test.$$ >/dev/null 2>&1 || true; "
                "if [ -f .daemon_service.pid ]; then "
                "  old_pid=$(cat .daemon_service.pid 2>/dev/null || true); "
                "  if [ -n \"$old_pid\" ] && kill -0 \"$old_pid\" 2>/dev/null; then "
                "    cmdline=$(tr '\\0' ' ' </proc/$old_pid/cmdline 2>/dev/null || true); "
                "    printf '%s' \"$cmdline\" | grep -F -- 'daemon_service.py' >/dev/null && "
                "      { echo \"daemon already running pid=$old_pid\"; exit 0; }; "
                "  fi; "
                "  rm -f .daemon_service.pid >/dev/null 2>&1 || true; "
                "fi; "
                f"SONOLBOT_UI_LANG={shlex.quote(UI_LANG)} "
                "SONOLBOT_MULTI_BOT_MANAGER=1 "
                f"SONOLBOT_BOTS_CONFIG={shlex.quote(wsl_bots_config)} "
                f"SONOLBOT_BOT_WORKSPACES_DIR={shlex.quote(wsl_bot_workspaces)} "
                f"SONOLBOT_CODEX_MODEL={shlex.quote(model)} "
                f"SONOLBOT_CODEX_REASONING_EFFORT={shlex.quote(reasoning)} "
                f"DAEMON_AGENT_REWRITER_MODEL={shlex.quote(rewriter_model)} "
                f"DAEMON_AGENT_REWRITER_REASONING_EFFORT={shlex.quote(rewriter_reasoning)} "
                f"DAEMON_AGENT_REWRITER_PROMPT_FILE={shlex.quote(wsl_rewriter_prompt_path)} "
                "LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUTF8=1 PYTHONIOENCODING=UTF-8 "
                f"nohup \"$PY\" {shlex.quote(wsl_service_script)} "
                ">> /tmp/sonolbot-daemon-launch.log 2>&1 & "
                "new_pid=''; "
                "for _i in 1 2 3 4 5 6 7 8 9 10 "
                "11 12 13 14 15 16 17 18 19 20 "
                "21 22 23 24 25 26 27 28 29 30; do "
                "  new_pid=$(cat .daemon_service.pid 2>/dev/null || true); "
                "  [ -n \"$new_pid\" ] && break; "
                "  sleep 0.2; "
                "done; "
                "if [ -z \"$new_pid\" ]; then "
                "  echo 'daemon pid file not created'; "
                "  tail -n 6 /tmp/sonolbot-daemon-launch.log 2>/dev/null || true; "
                "  exit 11; "
                "fi; "
                "for _i in 1 2 3 4 5 6 7 8 9 10; do "
                "  kill -0 \"$new_pid\" 2>/dev/null && break; "
                "  sleep 0.2; "
                "done; "
                "if ! kill -0 \"$new_pid\" 2>/dev/null; then "
                "  echo \"daemon pid not alive: $new_pid\"; "
                "  tail -n 6 /tmp/sonolbot-daemon-launch.log 2>/dev/null || true; "
                "  exit 12; "
                "fi; "
                "cmdline_new=$(tr '\\0' ' ' </proc/$new_pid/cmdline 2>/dev/null || true); "
                "printf '%s' \"$cmdline_new\" | grep -F -- 'daemon_service.py' >/dev/null || { "
                "  echo \"daemon cmdline mismatch pid=$new_pid cmd=$cmdline_new\"; "
                "  tail -n 6 /tmp/sonolbot-daemon-launch.log 2>/dev/null || true; "
                "  exit 14; "
                "}; "
                "echo \"daemon started pid=$new_pid\""
            )
            proc = _run_wsl_bash(script, timeout=30)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip() or f"return_code={proc.returncode}"
                _diag_log(
                    f"ERROR daemon_start failed rc={proc.returncode} detail={detail[:240]!r}",
                    throttle_key="daemon_start_failed",
                    min_interval_sec=10,
                )
                messagebox.showerror(tr("msg_error"), tr("msg_daemon_start_failed", detail=detail))
                self.refresh_status()
                return

            # Verify daemon really started; avoid silent "stopped" state after successful shell return.
            started_ok = False
            for _ in range(12):
                time.sleep(0.25)
                daemon_pid = _read_pid(PID_FILE)
                if _is_service_pid_alive(daemon_pid, "daemon_service.py"):
                    started_ok = True
                    break
            if not started_ok:
                daemon_line = _latest_daemon_log_line()
                launch_tail_proc = _run_wsl_bash(
                    "tail -n 6 /tmp/sonolbot-daemon-launch.log 2>/dev/null || true",
                    timeout=6,
                )
                launch_tail = (launch_tail_proc.stdout or "").strip()
                detail_parts = ["daemon process was not detected after launch request."]
                if daemon_line and daemon_line != "-":
                    detail_parts.append(f"daemon log: {daemon_line}")
                if launch_tail:
                    detail_parts.append("daemon launch log (tail):")
                    detail_parts.append(launch_tail)
                detail = "\n".join(detail_parts)
                _diag_log(
                    f"ERROR daemon_start verify_failed detail={detail[:400]!r}",
                    throttle_key="daemon_start_verify_failed",
                    min_interval_sec=5,
                )
                messagebox.showerror(tr("msg_error"), tr("msg_daemon_start_failed", detail=detail))
                self.refresh_status()
                return
        else:
            env = os.environ.copy()
            env.setdefault("LANG", "C.UTF-8")
            env.setdefault("LC_ALL", "C.UTF-8")
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "UTF-8")
            _augment_path_for_macos(env)
            env["SONOLBOT_MULTI_BOT_MANAGER"] = "1"
            env["SONOLBOT_BOTS_CONFIG"] = str(BOTS_CONFIG_FILE)
            env["SONOLBOT_BOT_WORKSPACES_DIR"] = str(BOT_WORKSPACES_DIR)
            env["SONOLBOT_CODEX_MODEL"] = model
            env["SONOLBOT_CODEX_REASONING_EFFORT"] = reasoning
            env["DAEMON_AGENT_REWRITER_MODEL"] = rewriter_model
            env["DAEMON_AGENT_REWRITER_REASONING_EFFORT"] = rewriter_reasoning
            env["DAEMON_AGENT_REWRITER_PROMPT_FILE"] = prompt_path

            kwargs: dict[str, object] = {
                "cwd": str(ROOT),
                "env": env,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([sys.executable, str(SERVICE_SCRIPT)], **kwargs)  # type: ignore[arg-type]

        time.sleep(0.8)
        self.refresh_status()

    def stop_daemon(self) -> None:
        self._stop_daemon_internal(notify_when_not_running=True)

    def _stop_daemon_internal(self, notify_when_not_running: bool) -> None:
        summary = _stop_all_runtime_processes()
        daemon_targets = summary.get("daemon_targets", [])
        app_targets = summary.get("app_server_targets", [])
        failed_daemons = summary.get("failed_daemons", [])
        failed_apps = summary.get("failed_app_servers", [])

        has_targets = bool(daemon_targets) or bool(app_targets)
        if not has_targets and notify_when_not_running:
            messagebox.showinfo(tr("msg_info"), tr("msg_daemon_not_running"))
            self.refresh_status()
            return

        if failed_daemons or failed_apps:
            _diag_log(
                "WARN daemon_stop_incomplete "
                f"failed_daemons={failed_daemons} failed_app_servers={failed_apps}"
            )
        time.sleep(0.5)
        self.refresh_status()

    def open_logs_dir(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(LOGS_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(LOGS_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(LOGS_DIR)])
        except Exception as exc:
            messagebox.showerror(tr("msg_error"), tr("msg_open_logs_failed", exc=exc))

    def prompt_autostart_once(self) -> None:
        if AUTOSTART_PROMPT_FLAG.exists():
            return
        try:
            AUTOSTART_PROMPT_FLAG.write_text(
                time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
            )
        except Exception:
            pass

        if _is_autostart_enabled():
            return

        answer = messagebox.askyesno(
            tr("msg_autostart_title"),
            tr("msg_autostart_prompt_macos") if sys.platform == "darwin" else tr("msg_autostart_prompt"),
        )
        if not answer:
            return

        if _set_autostart(True):
            messagebox.showinfo(tr("msg_autostart_title"), tr("msg_autostart_registered"))
        else:
            messagebox.showerror(tr("msg_autostart_title"), tr("msg_autostart_register_failed"))
        self.refresh_status()

    def toggle_autostart(self) -> None:
        current = _is_autostart_enabled()
        target = not current
        ok = _set_autostart(target)
        if ok:
            messagebox.showinfo(
                tr("msg_autostart_title"),
                tr("msg_autostart_enabled") if target else tr("msg_autostart_disabled"),
            )
        else:
            messagebox.showerror(tr("msg_autostart_title"), tr("msg_autostart_toggle_failed"))
        self.refresh_status()


def main() -> int:
    cli_args = _parse_cli_args(sys.argv[1:])
    existing_pid = _acquire_panel_lock()
    if existing_pid > 0:
        _diag_log(f"INFO panel_launch_blocked already_running pid={existing_pid}")
        try:
            popup = tk.Tk()
            popup.withdraw()
            messagebox.showinfo(tr("msg_panel_running_title"), tr("msg_panel_already_running"))
            popup.destroy()
        except Exception:
            pass
        return 0

    try:
        app = ControlPanel()
        if bool(cli_args.autostart_start_daemon):
            _diag_log("INFO autostart_boot daemon_auto_start_requested")
            app.after(1200, app.start_daemon)
        app.mainloop()
    finally:
        _release_panel_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
