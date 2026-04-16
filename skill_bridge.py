#!/usr/bin/env python3
"""Bridge helpers to load skill scripts from .codex/skills and build runtime configs."""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv()

_BASE_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _BASE_DIR / ".codex" / "skills"

_TELEGRAM_SKILL_CACHE = None
_DISCORD_SKILL_CACHE = None
_SLACK_SKILL_CACHE = None
_TASK_SKILL_CACHE = None
_DEFAULT_MAX_TELEGRAM_FILE_BYTES = 50 * 1024 * 1024
_DEFAULT_ALLOWED_SKILLS = ("sonolbot-telegram", "sonolbot-tasks")


def _load_module_from_path(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module spec: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _normalize_skill_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _allowed_skills() -> set[str]:
    raw = os.getenv("SONOLBOT_ALLOWED_SKILLS", "").strip()
    if not raw:
        return {_normalize_skill_name(v) for v in _DEFAULT_ALLOWED_SKILLS}

    items = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    parsed = {_normalize_skill_name(v) for v in items}
    if not parsed:
        return {_normalize_skill_name(v) for v in _DEFAULT_ALLOWED_SKILLS}
    return parsed


def _ensure_skill_allowed(skill_name: str) -> None:
    normalized = _normalize_skill_name(skill_name)
    allowed = _allowed_skills()
    if normalized in allowed:
        return
    rendered_allowed = ", ".join(sorted(allowed)) if allowed else "(none)"
    raise PermissionError(
        f"Skill '{skill_name}' is blocked by SONOLBOT_ALLOWED_SKILLS. "
        f"Allowed: {rendered_allowed}"
    )


def get_telegram_skill():
    global _TELEGRAM_SKILL_CACHE
    if _TELEGRAM_SKILL_CACHE is not None:
        return _TELEGRAM_SKILL_CACHE
    _ensure_skill_allowed("sonolbot-telegram")
    p = _SKILLS_DIR / "sonolbot-telegram" / "scripts" / "telegram_io.py"
    if not p.exists():
        # backward compatibility
        p = _SKILLS_DIR / "sonolbot_telegram" / "scripts" / "telegram_io.py"
    if not p.exists():
        raise FileNotFoundError(f"Telegram skill script not found: {p}")
    _TELEGRAM_SKILL_CACHE = _load_module_from_path("sonolbot_telegram_io", p)
    return _TELEGRAM_SKILL_CACHE


def get_discord_skill():
    global _DISCORD_SKILL_CACHE
    if _DISCORD_SKILL_CACHE is not None:
        return _DISCORD_SKILL_CACHE
    _ensure_skill_allowed("sonolbot-discord")
    p = _SKILLS_DIR / "sonolbot-discord" / "scripts" / "discord_io.py"
    if not p.exists():
        raise FileNotFoundError(f"Discord skill script not found: {p}")
    _DISCORD_SKILL_CACHE = _load_module_from_path("sonolbot_discord_io", p)
    return _DISCORD_SKILL_CACHE


def get_slack_skill():
    global _SLACK_SKILL_CACHE
    if _SLACK_SKILL_CACHE is not None:
        return _SLACK_SKILL_CACHE
    _ensure_skill_allowed("sonolbot-slack")
    p = _SKILLS_DIR / "sonolbot-slack" / "scripts" / "slack_io.py"
    if not p.exists():
        raise FileNotFoundError(f"Slack skill script not found: {p}")
    _SLACK_SKILL_CACHE = _load_module_from_path("sonolbot_slack_io", p)
    return _SLACK_SKILL_CACHE


def get_task_skill():
    global _TASK_SKILL_CACHE
    if _TASK_SKILL_CACHE is not None:
        return _TASK_SKILL_CACHE
    _ensure_skill_allowed("sonolbot-tasks")
    p = _SKILLS_DIR / "sonolbot-tasks" / "scripts" / "task_memory.py"
    if not p.exists():
        # backward compatibility
        p = _SKILLS_DIR / "sonolnot-tasks" / "scripts" / "task_memory.py"
    if not p.exists():
        raise FileNotFoundError(f"Task skill script not found: {p}")
    _TASK_SKILL_CACHE = _load_module_from_path("sonolnot_tasks_memory", p)
    return _TASK_SKILL_CACHE


def get_tasks_dir() -> Path:
    val = os.getenv("TASKS_DIR") or str(_BASE_DIR / "tasks")
    return Path(val).resolve()


def get_logs_dir() -> Path:
    val = os.getenv("LOGS_DIR")
    if not val:
        val = os.getenv("TELEGRAM_LOGS_DIR") or os.getenv("TASKS_LOGS_DIR") or str(_BASE_DIR / "logs")
    return Path(val).resolve()


def get_messenger_skill():
    """현재 SONOLBOT_PLATFORM에 따라 메신저 스킬 반환."""
    platform = os.getenv("SONOLBOT_PLATFORM", "telegram").strip().lower()
    if platform == "discord":
        return get_discord_skill()
    if platform == "slack":
        return get_slack_skill()
    return get_telegram_skill()


def build_discord_runtime() -> dict[str, Any]:
    """Discord 런타임 변수 빌드."""
    mod = get_discord_skill()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    allowed_users = os.getenv("DISCORD_ALLOWED_USERS", "")
    allowed_channels = os.getenv("DISCORD_ALLOWED_CHANNELS", "")
    api_timeout = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "20"))  # 공통 변수 재사용
    retention_days = int(os.getenv("DISCORD_MESSAGE_RETENTION_DAYS", "7"))
    store_path = os.getenv("DISCORD_MESSAGE_STORE", str(_BASE_DIR / "discord_messages.json"))
    work_dir = os.getenv("WORK_DIR") or str(_BASE_DIR)
    tasks_dir = os.getenv("TELEGRAM_TASKS_DIR") or str(get_tasks_dir())
    logs_dir = os.getenv("TELEGRAM_LOGS_DIR") or str(get_logs_dir())

    return mod.build_runtime_vars(
        {
            "discord_bot_token": token,
            "discord_allowed_users": allowed_users,
            "discord_allowed_channels": allowed_channels,
            "api_timeout_sec": api_timeout,
            "message_retention_days": retention_days,
            "store_path": store_path,
            "work_dir": work_dir,
            "tasks_dir": tasks_dir,
            "logs_dir": logs_dir,
        }
    )


def build_slack_runtime() -> dict[str, Any]:
    """Slack 런타임 변수 빌드."""
    mod = get_slack_skill()
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    allowed_users = os.getenv("SLACK_ALLOWED_USERS", "")
    allowed_channels = os.getenv("SLACK_ALLOWED_CHANNELS", "")
    api_timeout = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "20"))
    retention_days = int(os.getenv("SLACK_MESSAGE_RETENTION_DAYS", "7"))
    store_path = os.getenv("SLACK_MESSAGE_STORE", str(_BASE_DIR / "slack_messages.json"))
    work_dir = os.getenv("WORK_DIR") or str(_BASE_DIR)
    tasks_dir = os.getenv("TELEGRAM_TASKS_DIR") or str(get_tasks_dir())
    logs_dir = os.getenv("TELEGRAM_LOGS_DIR") or str(get_logs_dir())

    return mod.build_runtime_vars(
        {
            "slack_bot_token": bot_token,
            "slack_app_token": app_token,
            "slack_allowed_users": allowed_users,
            "slack_allowed_channels": allowed_channels,
            "api_timeout_sec": api_timeout,
            "message_retention_days": retention_days,
            "store_path": store_path,
            "work_dir": work_dir,
            "tasks_dir": tasks_dir,
            "logs_dir": logs_dir,
        }
    )


def build_messenger_runtime() -> dict[str, Any]:
    """현재 SONOLBOT_PLATFORM에 따라 메신저 런타임 반환."""
    platform = os.getenv("SONOLBOT_PLATFORM", "telegram").strip().lower()
    if platform == "discord":
        return build_discord_runtime()
    if platform == "slack":
        return build_slack_runtime()
    return build_telegram_runtime()


def build_telegram_runtime() -> dict[str, Any]:
    mod = get_telegram_skill()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    user_id_raw = os.getenv("TELEGRAM_USER_ID", "").strip()
    user_id = int(user_id_raw) if user_id_raw.isdigit() else None

    include_24h = os.getenv("TELEGRAM_INCLUDE_24H_CONTEXT", "1") == "1"
    api_timeout = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "20"))
    polling_timeout = int(os.getenv("TELEGRAM_POLLING_INTERVAL", "1"))
    message_retention_days = int(os.getenv("TELEGRAM_MESSAGE_RETENTION_DAYS", "7"))
    max_file_bytes_raw = os.getenv("TELEGRAM_MAX_FILE_BYTES", "").strip()
    try:
        max_file_bytes = int(max_file_bytes_raw) if max_file_bytes_raw else _DEFAULT_MAX_TELEGRAM_FILE_BYTES
    except ValueError:
        max_file_bytes = _DEFAULT_MAX_TELEGRAM_FILE_BYTES
    max_file_bytes = max(1, max_file_bytes)

    work_dir = os.getenv("WORK_DIR") or str(_BASE_DIR)
    tasks_dir = os.getenv("TELEGRAM_TASKS_DIR") or str(get_tasks_dir())
    logs_dir = os.getenv("TELEGRAM_LOGS_DIR") or str(get_logs_dir())

    return mod.build_runtime_vars(
        {
            "telegram_bot_token": token,
            "telegram_allowed_users": allowed,
            "telegram_user_id": user_id,
            "telegram_include_24h_context": include_24h,
            "work_dir": work_dir,
            "tasks_dir": tasks_dir,
            "logs_dir": logs_dir,
            "api_timeout_sec": api_timeout,
            "polling_timeout_sec": polling_timeout,
            "message_retention_days": message_retention_days,
            "max_telegram_file_bytes": max_file_bytes,
        }
    )
