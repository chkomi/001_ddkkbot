#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Slack relay: Slack Socket Mode → slack_messages.json

daemon_service.py가 관리하는 별도 프로세스로 실행됩니다.
Discord relay와 동일한 메시지 포맷으로 저장합니다.

환경변수:
  SLACK_BOT_TOKEN              - Slack 봇 토큰 (xoxb-..., 필수)
  SLACK_APP_TOKEN              - Slack 앱 토큰 (xapp-..., Socket Mode 필수)
  SLACK_ALLOWED_USERS          - 허용 사용자 ID 목록 (쉼표 구분, 비워두면 모두 허용)
  SLACK_ALLOWED_CHANNELS       - 허용 채널 ID 목록 (쉼표 구분, 비워두면 모두 허용)
  SLACK_MESSAGE_STORE          - 메시지 저장 파일 경로 (기본: slack_messages.json)
  SLACK_MESSAGE_RETENTION_DAYS - 메시지 보관 기간 (기본: 7일)
  LOGS_DIR                     - 로그 파일 디렉토리
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
log_file = LOGS_DIR / f"slack-relay-{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("slack_relay")

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:
    logger.error("slack-bolt가 설치되지 않았습니다. pip install slack-bolt 를 실행하세요.")
    sys.exit(1)

# 슬랙 스킬에서 ID 인코더 재사용 (.codex/skills/sonolbot-slack/scripts/slack_io.py)
_SKILL_PATH = BASE_DIR / ".codex" / "skills" / "sonolbot-slack" / "scripts"
if str(_SKILL_PATH) not in sys.path:
    sys.path.insert(0, str(_SKILL_PATH))
try:
    from slack_io import encode_slack_id, encode_slack_ts  # type: ignore
except ImportError:
    # 폴백: 인라인 정의
    _BASE36_DIGITS_LOCAL = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    def encode_slack_id(value: str) -> int:  # type: ignore
        s = (value or "").strip().upper()
        try:
            return int(s, 36) if s else 0
        except ValueError:
            return abs(hash(s)) & ((1 << 60) - 1)
    def encode_slack_ts(ts: str) -> int:  # type: ignore
        s = (ts or "").strip()
        if not s:
            return 0
        if "." in s:
            secs, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]
            try:
                return int(secs) * 1_000_000 + int(frac)
            except ValueError:
                return 0
        try:
            return int(s) * 1_000_000
        except ValueError:
            return 0

# ── 설정 ──────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()
STORE_PATH = Path(
    os.getenv("SLACK_MESSAGE_STORE", str(BASE_DIR / "slack_messages.json"))
).resolve()
RETENTION_DAYS = int(os.getenv("SLACK_MESSAGE_RETENTION_DAYS", "7"))

_ALLOWED_USERS: set[str] = set()
_allowed_users_raw = os.getenv("SLACK_ALLOWED_USERS", "").strip()
if _allowed_users_raw:
    for _v in re.split(r"[,\s]+", _allowed_users_raw):
        if _v.strip():
            _ALLOWED_USERS.add(_v.strip())

_ALLOWED_CHANNELS: set[str] = set()
_allowed_channels_raw = os.getenv("SLACK_ALLOWED_CHANNELS", "").strip()
if _allowed_channels_raw:
    for _v in re.split(r"[,\s]+", _allowed_channels_raw):
        if _v.strip():
            _ALLOWED_CHANNELS.add(_v.strip())

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


def append_slack_message(
    message_id: str,
    channel_id: str,
    user_id: str,
    username: str,
    text: str,
    files: list[dict] | None = None,
) -> None:
    """슬랙 메시지를 store 에 저장.

    데몬 본체가 chat_id/user_id/message_id 를 int 로 다루기 때문에 base36
    인코딩한 int 형태로 저장하고, 원본 문자열도 디버그용으로 함께 보존한다.
    """
    chat_id_int = encode_slack_id(channel_id)
    user_id_int = encode_slack_id(user_id)
    msg_id_int = encode_slack_ts(message_id)

    with _store_lock:
        store = _load_store()
        last_id = int(store.get("last_update_id", 0))
        store["last_update_id"] = last_id + 1

        entry: dict = {
            "message_id": msg_id_int,
            "chat_id": chat_id_int,
            "user_id": user_id_int,
            "username": username,
            "type": "user",
            "text": text,
            "timestamp": datetime.now().isoformat(),
            "processed": False,
            "source": "slack",
            # 디버그/추적용 원본 (데몬은 사용하지 않음)
            "_slack_message_ts": message_id,
            "_slack_channel_id": channel_id,
            "_slack_user_id": user_id,
        }
        if files:
            entry["files"] = files

        messages = store.get("messages", [])
        messages.append(entry)
        store["messages"] = _prune_old_messages(messages)
        _save_store(store)

    logger.info(
        f"메시지 저장: channel={channel_id} ({chat_id_int}) "
        f"user={user_id} ({user_id_int}) text={text[:50]!r}"
    )


# ── Slack 앱 이벤트 핸들러 ──────────────────────────────────────────────────────

app = App(token=BOT_TOKEN)


@app.event("message")
def handle_message(event: dict, client, logger: logging.Logger) -> None:
    # 봇 메시지, 서브타입(편집/삭제 등) 무시
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event.get("user", "")
    channel_id = event.get("channel", "")
    ts = event.get("ts", "")

    if not user_id or not ts:
        return

    # 허용 채널 필터
    if _ALLOWED_CHANNELS and channel_id not in _ALLOWED_CHANNELS:
        return

    # 허용 사용자 필터
    if _ALLOWED_USERS and user_id not in _ALLOWED_USERS:
        logger.info(f"허용되지 않은 사용자 무시: user_id={user_id}")
        return

    # 사용자 표시명 조회
    username = user_id
    try:
        info = client.users_info(user=user_id)
        profile = info["user"].get("profile", {})
        username = profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        pass

    # 텍스트 정제 (봇 멘션 제거)
    text = event.get("text", "") or ""
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

    # 첨부파일 처리
    files: list[dict] = []
    for f in event.get("files", []):
        files.append({
            "file_id": f.get("id", ""),
            "file_name": f.get("name", ""),
            "file_url": f.get("url_private", ""),
            "file_size": f.get("size", 0),
            "content_type": f.get("mimetype", "application/octet-stream"),
        })

    append_slack_message(
        message_id=ts,
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        text=text,
        files=files if files else None,
    )


@app.event("app_mention")
def handle_mention(event: dict, client, logger: logging.Logger) -> None:
    """멘션도 동일하게 처리 (message 이벤트와 중복 방지)."""
    # message 이벤트로 이미 처리되므로 별도 로직 없음
    pass


@app.action(re.compile(r"^btn_.*"))
def handle_button_action(ack, body, client, logger: logging.Logger) -> None:
    """Block Kit 버튼 클릭 처리.

    텔레그램의 reply keyboard 와 동일하게, 클릭한 라벨을 사용자 메시지로
    store 에 추가해 daemon 이 동일한 입력 흐름을 타도록 한다.
    """
    ack()  # 3초 내 ACK 필수 — 안 하면 슬랙이 "responded too late" 경고
    try:
        actions = body.get("actions") or []
        if not actions:
            return
        action = actions[0]
        label = str(action.get("value") or action.get("text", {}).get("text") or "").strip()
        if not label:
            return

        user = body.get("user") or {}
        user_id = str(user.get("id") or "")
        username = str(user.get("name") or user_id or "user")

        channel = body.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        if not channel_id or not user_id:
            return

        if _ALLOWED_USERS and user_id not in _ALLOWED_USERS:
            return
        if _ALLOWED_CHANNELS and channel_id not in _ALLOWED_CHANNELS:
            return

        # 합성 ts (실제 클릭 시각 기반)
        synthetic_ts = body.get("trigger_id") or body.get("action_ts") or ""
        if not synthetic_ts or "." not in str(synthetic_ts):
            from time import time as _now
            synthetic_ts = f"{_now():.6f}"

        append_slack_message(
            message_id=str(synthetic_ts),
            channel_id=channel_id,
            user_id=user_id,
            username=username,
            text=label,
            files=None,
        )
        logger.info(
            f"버튼 클릭 → 메시지 저장: channel={channel_id} user={user_id} label={label!r}"
        )
    except Exception as exc:
        logger.error(f"버튼 클릭 처리 실패: {exc}")


def main() -> None:
    if not BOT_TOKEN:
        logger.error("SLACK_BOT_TOKEN이 설정되지 않았습니다.")
        sys.exit(1)
    if not APP_TOKEN:
        logger.error("SLACK_APP_TOKEN이 설정되지 않았습니다. (Socket Mode용 xapp-... 토큰)")
        sys.exit(1)

    logger.info(f"Slack relay 시작 (store={STORE_PATH})")
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
