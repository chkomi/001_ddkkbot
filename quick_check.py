#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skill-only quick checker for pending messages (Telegram or Discord).

Exit code:
- 0: no pending user messages
- 1: pending user messages exist
- 2: error
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from skill_bridge import (
    build_discord_runtime,
    build_slack_runtime,
    build_telegram_runtime,
    get_discord_skill,
    get_slack_skill,
    get_telegram_skill,
)


def main() -> int:
    root = Path(__file__).resolve().parent
    platform = os.getenv("SONOLBOT_PLATFORM", "telegram").strip().lower()

    if platform == "discord":
        store_path = Path(os.getenv("DISCORD_MESSAGE_STORE", str(root / "discord_messages.json"))).resolve()
        runtime = build_discord_runtime()
        discord = get_discord_skill()
        pending = discord.get_pending_messages(str(store_path), include_bot=False)
        print(f"[QUICK_CHECK] platform=discord pending={len(pending)} store={store_path}")
        return 1 if pending else 0

    if platform == "slack":
        store_path = Path(os.getenv("SLACK_MESSAGE_STORE", str(root / "slack_messages.json"))).resolve()
        runtime = build_slack_runtime()
        slack = get_slack_skill()
        pending = slack.get_pending_messages(str(store_path), include_bot=False)
        print(f"[QUICK_CHECK] platform=slack pending={len(pending)} store={store_path}")
        return 1 if pending else 0

    # 기본: Telegram
    store_path = Path(os.getenv("TELEGRAM_MESSAGE_STORE", str(root / "telegram_messages.json"))).resolve()
    runtime = build_telegram_runtime()
    telegram = get_telegram_skill()
    _, pending, _ = telegram.poll_store_and_get_pending(
        runtime=runtime,
        store_path=str(store_path),
        include_bot=False,
    )
    print(f"[QUICK_CHECK] platform=telegram pending={len(pending)} store={store_path}")
    return 1 if pending else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[QUICK_CHECK][ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
