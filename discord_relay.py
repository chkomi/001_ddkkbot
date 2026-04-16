#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discord relay: Discord WebSocket Gateway → discord_messages.json

daemon_service.py가 관리하는 별도 프로세스로 실행됩니다.
Discord 메시지를 받아 telegram_messages.json과 동일한 포맷으로 저장합니다.

환경변수:
  DISCORD_BOT_TOKEN          - Discord 봇 토큰 (필수)
  DISCORD_ALLOWED_USERS      - 허용 사용자 ID 목록 (쉼표 구분, 비워두면 모두 허용)
  DISCORD_ALLOWED_CHANNELS   - 허용 채널 ID 목록 (쉼표 구분, 비워두면 모두 허용)
  DISCORD_MESSAGE_STORE      - 메시지 저장 파일 경로 (기본: discord_messages.json)
  DISCORD_MESSAGE_RETENTION_DAYS - 메시지 보관 기간 (기본: 7일)
  DISCORD_RESPOND_WITHOUT_MENTION - 0=멘션만 처리, 1=모든 메시지 처리 (기본: 0)
  LOGS_DIR                   - 로그 파일 디렉토리
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(BASE_DIR / "logs"))).resolve()
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── 로깅 설정 ──────────────────────────────────────────────────────────────────
log_file = LOGS_DIR / f"discord-relay-{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("discord_relay")

try:
    import discord
except ImportError:
    logger.error("discord.py가 설치되지 않았습니다. pip install discord.py 를 실행하세요.")
    sys.exit(1)

# ── 설정 ──────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
STORE_PATH = Path(
    os.getenv("DISCORD_MESSAGE_STORE", str(BASE_DIR / "discord_messages.json"))
).resolve()
RETENTION_DAYS = int(os.getenv("DISCORD_MESSAGE_RETENTION_DAYS", "7"))
RESPOND_WITHOUT_MENTION = os.getenv("DISCORD_RESPOND_WITHOUT_MENTION", "0").strip() == "1"

_ALLOWED_USERS: set[int] = set()
_allowed_users_raw = os.getenv("DISCORD_ALLOWED_USERS", "").strip()
if _allowed_users_raw:
    for _v in re.split(r"[,\s]+", _allowed_users_raw):
        if _v.strip().isdigit():
            _ALLOWED_USERS.add(int(_v.strip()))

_ALLOWED_CHANNELS: set[int] = set()
_allowed_channels_raw = os.getenv("DISCORD_ALLOWED_CHANNELS", "").strip()
if _allowed_channels_raw:
    for _v in re.split(r"[,\s]+", _allowed_channels_raw):
        if _v.strip().isdigit():
            _ALLOWED_CHANNELS.add(int(_v.strip()))

_store_lock = threading.Lock()


# ── 메시지 저장소 ──────────────────────────────────────────────────────────────

def _load_store() -> dict:
    if not STORE_PATH.exists():
        return {"last_update_id": 0, "messages": []}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_update_id": 0, "messages": []}


def _save_store(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _prune_old_messages(messages: list[dict]) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    kept = []
    for m in messages:
        ts_str = m.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts >= cutoff:
                kept.append(m)
        except Exception:
            kept.append(m)
    return kept


def append_discord_message(
    message_id: int,
    channel_id: int,
    user_id: int,
    username: str,
    text: str,
    attachments: list[dict] | None = None,
) -> None:
    with _store_lock:
        store = _load_store()
        last_id = int(store.get("last_update_id", 0))
        new_id = max(last_id + 1, message_id)
        store["last_update_id"] = new_id

        entry: dict = {
            "message_id": message_id,
            "chat_id": channel_id,      # Discord channel_id → chat_id 필드 재사용
            "user_id": user_id,
            "username": username,
            "type": "user",
            "text": text,
            "timestamp": datetime.now().isoformat(),
            "processed": False,
            "source": "discord",
        }
        if attachments:
            entry["files"] = attachments

        messages = store.get("messages", [])
        messages.append(entry)
        store["messages"] = _prune_old_messages(messages)
        _save_store(store)

    logger.info(f"메시지 저장: channel={channel_id} user={user_id} ({username}) text={text[:50]!r}")


# ── Discord 클라이언트 ──────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    logger.info(f"Discord 봇 로그인: {client.user} (id={client.user.id})")  # type: ignore[union-attr]


@client.event
async def on_message(message: discord.Message) -> None:
    # 봇 자신의 메시지는 무시
    if message.author.bot:
        return

    user_id = message.author.id
    channel_id = message.channel.id

    # 허용 채널 필터
    if _ALLOWED_CHANNELS and channel_id not in _ALLOWED_CHANNELS:
        return

    # 허용 사용자 필터
    if _ALLOWED_USERS and user_id not in _ALLOWED_USERS:
        logger.info(f"허용되지 않은 사용자 무시: user_id={user_id}")
        return

    # 멘션 필터
    bot_user = client.user
    is_mentioned = bot_user is not None and bot_user.mentioned_in(message)
    if not RESPOND_WITHOUT_MENTION and not is_mentioned:
        return

    # 텍스트 정제 (멘션 제거)
    text = message.content
    if bot_user is not None:
        text = text.replace(f"<@{bot_user.id}>", "").replace(f"<@!{bot_user.id}>", "").strip()

    # 첨부파일 처리
    attachments: list[dict] = []
    for att in message.attachments:
        attachments.append({
            "file_id": str(att.id),
            "file_name": att.filename,
            "file_url": att.url,
            "file_size": att.size,
            "content_type": att.content_type or "application/octet-stream",
        })

    # 메시지 저장
    append_discord_message(
        message_id=message.id,
        channel_id=channel_id,
        user_id=user_id,
        username=str(message.author),
        text=text,
        attachments=attachments if attachments else None,
    )


def main() -> None:
    if not BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
        sys.exit(1)

    logger.info(f"Discord relay 시작 (store={STORE_PATH})")
    client.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
