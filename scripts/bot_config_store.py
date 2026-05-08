#!/usr/bin/env python3
"""Config store for multi-bot telegram settings."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.telegram_validation import mask_token, validate_bot_token_format


SECURE_FILE_MODE = 0o600
SECURE_DIR_MODE = 0o700
DEFAULT_BOTS_CONFIG = ".control_panel_telegram_bots.json"

SUPPORTED_PLATFORMS = ("telegram", "discord", "slack")
DISCORD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")
SLACK_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]{20,}$")
SLACK_APP_TOKEN_RE = re.compile(r"^xapp-\d+-[A-Z0-9]+-\d+-[A-Za-z0-9]+$")


def normalize_platform(value: object) -> str:
    raw = str(value or "telegram").strip().lower()
    return raw if raw in SUPPORTED_PLATFORMS else "telegram"


def derive_bot_id(token: str, platform: str) -> str:
    """토큰과 플랫폼으로부터 안정적인 bot_id를 도출한다."""
    token = (token or "").strip()
    plat = normalize_platform(platform)
    if not token:
        return ""
    if plat == "discord":
        prefix = token.split(".", 1)[0]
        try:
            import base64
            padded = prefix + "=" * (-len(prefix) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            user_id = decoded.decode("ascii", errors="ignore").strip()
            if user_id.isdigit():
                return f"discord_{user_id}"
        except Exception:
            pass
        return f"discord_{prefix[:16]}"
    if plat == "slack":
        # xoxb-<TEAM>-<APP>-<SECRET> — 가운데 두 숫자 세그먼트가 안정적인 식별자
        parts = token.split("-")
        if len(parts) >= 3 and parts[1].isdigit():
            app_seg = parts[2] if len(parts) >= 3 else ""
            if app_seg.isdigit():
                return f"slack_{parts[1]}_{app_seg}"
            return f"slack_{parts[1]}"
        return f"slack_{(parts[1] if len(parts) > 1 else token)[:16]}"
    # telegram
    prefix = token.split(":", 1)[0].strip()
    return f"tg_{prefix}" if prefix else ""


def validate_token_for_platform(token: str, platform: str) -> tuple[bool, str]:
    token = (token or "").strip()
    if not token:
        return False, "토큰이 비어 있습니다."
    plat = normalize_platform(platform)
    if plat == "discord":
        if not DISCORD_TOKEN_RE.match(token):
            return False, "디스코드 토큰 형식이 올바르지 않습니다."
        return True, ""
    if plat == "slack":
        if not SLACK_BOT_TOKEN_RE.match(token):
            return False, "슬랙 봇 토큰 형식이 올바르지 않습니다 (xoxb-...)."
        return True, ""
    return validate_bot_token_format(token)


def validate_slack_app_token(token: str) -> tuple[bool, str]:
    token = (token or "").strip()
    if not token:
        return False, "App-Level Token이 비어 있습니다."
    if not SLACK_APP_TOKEN_RE.match(token):
        return False, "슬랙 App-Level Token 형식이 올바르지 않습니다 (xapp-...)."
    return True, ""


def default_config_path(root_dir: Path) -> Path:
    raw = (os.getenv("SONOLBOT_BOTS_CONFIG", "") or "").strip()
    if raw:
        raw_path = Path(raw).expanduser()
        if not raw_path.is_absolute():
            return (root_dir / raw_path).resolve()
        return raw_path.resolve()
    return (root_dir / DEFAULT_BOTS_CONFIG).resolve()


def _secure_path(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(SECURE_DIR_MODE)
        else:
            path.chmod(SECURE_FILE_MODE)
    except OSError:
        return


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty_config() -> dict[str, Any]:
    return {
        "version": 1,
        "allowed_users_global": [],
        "bots": [],
        "updated_at": _now_str(),
    }


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_config()
    out = _empty_config()
    out["version"] = int(raw.get("version") or 1)

    allowed = raw.get("allowed_users_global")
    parsed_allowed: list[int] = []
    if isinstance(allowed, list):
        for item in allowed:
            try:
                parsed_allowed.append(int(item))
            except Exception:
                continue
    out["allowed_users_global"] = sorted(set(v for v in parsed_allowed if v > 0))

    bots = raw.get("bots")
    parsed_bots: list[dict[str, Any]] = []
    if isinstance(bots, list):
        for item in bots:
            if not isinstance(item, dict):
                continue
            token = str(item.get("token") or "").strip()
            bot_id = str(item.get("bot_id") or "").strip()
            if not token or not bot_id:
                continue
            platform = normalize_platform(item.get("platform"))
            ok, _ = validate_token_for_platform(token, platform)
            if not ok:
                continue
            row = {
                "platform": platform,
                "token": token,
                "token_masked": str(item.get("token_masked") or mask_token(token)),
                "bot_id": bot_id,
                "bot_username": str(item.get("bot_username") or "").strip(),
                "bot_name": str(item.get("bot_name") or "").strip(),
                "alias": str(item.get("alias") or "").strip(),
                "memo": str(item.get("memo") or "").strip(),
                "active": bool(item.get("active", False)),
                "created_at": str(item.get("created_at") or _now_str()),
                "updated_at": str(item.get("updated_at") or _now_str()),
            }
            if platform == "discord":
                row["discord_allowed_users"] = str(item.get("discord_allowed_users") or "").strip()
            if platform == "slack":
                row["slack_app_token"] = str(item.get("slack_app_token") or "").strip()
                row["slack_allowed_users"] = str(item.get("slack_allowed_users") or "").strip()
                row["slack_allowed_channels"] = str(item.get("slack_allowed_channels") or "").strip()
            parsed_bots.append(row)
    # dedupe by bot_id (last wins)
    dedup: dict[str, dict[str, Any]] = {}
    for row in parsed_bots:
        dedup[str(row["bot_id"])] = row
    out["bots"] = list(dedup.values())
    out["updated_at"] = str(raw.get("updated_at") or _now_str())
    return out


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_config()
    return normalize_config(data)


def save_config(path: Path, data: dict[str, Any]) -> None:
    normalized = normalize_config(data)
    normalized["updated_at"] = _now_str()
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_path(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _secure_path(path)


def upsert_bot(
    data: dict[str, Any],
    *,
    token: str,
    bot_id: str,
    platform: str = "telegram",
    bot_username: str = "",
    bot_name: str = "",
    alias: str = "",
    memo: str = "",
    active: bool = False,
    discord_allowed_users: str = "",
    slack_app_token: str = "",
    slack_allowed_users: str = "",
    slack_allowed_channels: str = "",
    allow_token_update: bool = False,
) -> dict[str, Any]:
    out = normalize_config(data)
    token = token.strip()
    bot_id = bot_id.strip()
    platform = normalize_platform(platform)
    now = _now_str()
    updated = False
    for row in out["bots"]:
        if str(row.get("bot_id")) == bot_id:
            old_token = str(row.get("token") or "").strip()
            if old_token and old_token != token and not allow_token_update:
                raise ValueError(
                    f"bot_id={bot_id} token cannot be changed; remove and re-register this bot."
                )
            row["platform"] = platform
            row["token"] = token
            row["token_masked"] = mask_token(token)
            row["bot_username"] = bot_username.strip()
            row["bot_name"] = bot_name.strip()
            row["alias"] = alias.strip()
            row["memo"] = memo.strip()
            row["active"] = bool(active)
            row["updated_at"] = now
            if platform == "discord":
                row["discord_allowed_users"] = discord_allowed_users.strip()
            else:
                row.pop("discord_allowed_users", None)
            if platform == "slack":
                row["slack_app_token"] = slack_app_token.strip()
                row["slack_allowed_users"] = slack_allowed_users.strip()
                row["slack_allowed_channels"] = slack_allowed_channels.strip()
            else:
                row.pop("slack_app_token", None)
                row.pop("slack_allowed_users", None)
                row.pop("slack_allowed_channels", None)
            updated = True
            break
    if not updated:
        new_row: dict[str, Any] = {
            "platform": platform,
            "token": token,
            "token_masked": mask_token(token),
            "bot_id": bot_id,
            "bot_username": bot_username.strip(),
            "bot_name": bot_name.strip(),
            "alias": alias.strip(),
            "memo": memo.strip(),
            "active": bool(active),
            "created_at": now,
            "updated_at": now,
        }
        if platform == "discord":
            new_row["discord_allowed_users"] = discord_allowed_users.strip()
        if platform == "slack":
            new_row["slack_app_token"] = slack_app_token.strip()
            new_row["slack_allowed_users"] = slack_allowed_users.strip()
            new_row["slack_allowed_channels"] = slack_allowed_channels.strip()
        out["bots"].append(new_row)
    out["updated_at"] = now
    return out


def remove_bot(data: dict[str, Any], bot_id: str) -> dict[str, Any]:
    out = normalize_config(data)
    key = str(bot_id).strip()
    out["bots"] = [row for row in out["bots"] if str(row.get("bot_id")) != key]
    out["updated_at"] = _now_str()
    return out


def set_bot_active(data: dict[str, Any], bot_id: str, active: bool) -> dict[str, Any]:
    out = normalize_config(data)
    key = str(bot_id).strip()
    for row in out["bots"]:
        if str(row.get("bot_id")) == key:
            row["active"] = bool(active)
            row["updated_at"] = _now_str()
            break
    out["updated_at"] = _now_str()
    return out


def update_bot_meta(
    data: dict[str, Any],
    bot_id: str,
    *,
    alias: str | None = None,
    memo: str | None = None,
    active: bool | None = None,
    discord_allowed_users: str | None = None,
    slack_app_token: str | None = None,
    slack_allowed_users: str | None = None,
    slack_allowed_channels: str | None = None,
) -> dict[str, Any]:
    """토큰을 건드리지 않고 메타 필드만 갱신."""
    out = normalize_config(data)
    key = str(bot_id).strip()
    for row in out["bots"]:
        if str(row.get("bot_id")) != key:
            continue
        if alias is not None:
            row["alias"] = alias.strip()
        if memo is not None:
            row["memo"] = memo.strip()
        if active is not None:
            row["active"] = bool(active)
        if discord_allowed_users is not None and row.get("platform") == "discord":
            row["discord_allowed_users"] = discord_allowed_users.strip()
        if row.get("platform") == "slack":
            if slack_app_token is not None:
                row["slack_app_token"] = slack_app_token.strip()
            if slack_allowed_users is not None:
                row["slack_allowed_users"] = slack_allowed_users.strip()
            if slack_allowed_channels is not None:
                row["slack_allowed_channels"] = slack_allowed_channels.strip()
        row["updated_at"] = _now_str()
        break
    out["updated_at"] = _now_str()
    return out


def set_allowed_users_global(data: dict[str, Any], user_ids: list[int]) -> dict[str, Any]:
    out = normalize_config(data)
    out["allowed_users_global"] = sorted(set(int(v) for v in user_ids if int(v) > 0))
    out["updated_at"] = _now_str()
    return out


def _parse_env_pairs(env_text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in env_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        pairs.append((k.strip(), v.strip()))
    return pairs


def _render_env_pairs(pairs: list[tuple[str, str]]) -> str:
    return "\n".join([f"{k}={v}" for k, v in pairs]).rstrip() + "\n"


def migrate_legacy_env_if_needed(root_dir: Path, config_path: Path) -> tuple[bool, str]:
    """Migrate TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS into bots config once."""
    if config_path.exists():
        return False, "config already exists"
    env_path = (root_dir / ".env").resolve()
    if not env_path.exists():
        return False, ".env not found"

    try:
        env_text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f".env read failed: {exc}"
    pairs = _parse_env_pairs(env_text)
    kv = {k: v for k, v in pairs}
    token = str(kv.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allowed_raw = str(kv.get("TELEGRAM_ALLOWED_USERS") or "").strip()
    ok, err = validate_bot_token_format(token)
    if not ok:
        return False, f"legacy token missing/invalid: {err}"

    user_ids: list[int] = []
    for chunk in re.split(r"[,\s]+", allowed_raw):
        if not chunk.strip():
            continue
        try:
            val = int(chunk.strip())
        except ValueError:
            continue
        if val > 0:
            user_ids.append(val)
    prefix = token.split(":", 1)[0].strip()
    bot_id = prefix or "unknown"
    data = _empty_config()
    data["allowed_users_global"] = sorted(set(user_ids))
    data = upsert_bot(data, token=token, bot_id=bot_id, active=False)
    save_config(config_path, data)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = env_path.with_name(f".env.bak.{ts}")
    try:
        backup.write_text(env_text, encoding="utf-8")
        _secure_path(backup)
    except OSError:
        pass

    filtered = [(k, v) for (k, v) in pairs if k not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"}]
    if not any(k == "SONOLBOT_BOTS_CONFIG" for k, _ in filtered):
        filtered.append(("SONOLBOT_BOTS_CONFIG", str(config_path)))
    try:
        env_path.write_text(_render_env_pairs(filtered), encoding="utf-8")
        _secure_path(env_path)
    except OSError:
        return True, f"migrated config, but failed to rewrite .env (backup: {backup})"

    return True, f"migrated from .env to {config_path} (backup: {backup})"
