"""
텔레그램 사용자 ID 확인 스크립트 (requests 기반)

사용 방법:
1. .env 파일에 TELEGRAM_BOT_TOKEN 설정 (봇 토큰만 있으면 됨)
2. python get_my_id.py 실행
3. 텔레그램 앱에서 본인의 봇에게 아무 메시지나 보내기
4. 이 스크립트가 당신의 ID를 출력합니다!
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_TIMEOUT = float(os.getenv("TELEGRAM_API_TIMEOUT_SEC", "20"))


def _api_get(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    res = requests.get(url, params=params or {}, timeout=API_TIMEOUT)
    res.raise_for_status()
    payload = res.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def get_user_id() -> None:
    """봇에게 메시지를 보낸 사용자의 ID 확인"""
    print("=" * 60)
    print("텔레그램 사용자 ID 확인")
    print("=" * 60)

    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        print("\n❌ 오류: .env 파일에 TELEGRAM_BOT_TOKEN을 먼저 설정하세요!")
        print("\n순서:")
        print("1. 텔레그램 앱에서 @BotFather 찾기")
        print("2. /newbot 명령으로 봇 생성")
        print("3. 받은 토큰을 .env 파일에 입력")
        print("   TELEGRAM_BOT_TOKEN=1234567890:ABCdef...")
        return

    try:
        me = _api_get("getMe").get("result", {})
        username = me.get("username", "(unknown)")

        print(f"\n✅ 봇: @{username}")
        print(f"\n📱 지금 텔레그램 앱에서 @{username} 에게")
        print("   아무 메시지나 보내주세요! (예: 안녕)")
        print("\n대기 중", end="", flush=True)

        existing_updates = _api_get("getUpdates", params={"timeout": 1}).get("result", [])
        last_update_id = existing_updates[-1]["update_id"] if existing_updates else 0

        for _ in range(60):
            time.sleep(1)
            print(".", end="", flush=True)

            updates = _api_get(
                "getUpdates",
                params={"offset": last_update_id + 1, "timeout": 1},
            ).get("result", [])

            if not updates:
                continue

            print("\n\n✅ 메시지 감지!\n")

            for update in updates:
                update_id = int(update.get("update_id", 0))
                if update_id > last_update_id:
                    last_update_id = update_id

                msg = update.get("message") or {}
                user = msg.get("from") or {}
                if not user:
                    continue

                first_name = user.get("first_name", "")
                last_name = user.get("last_name", "")
                full_name = f"{first_name} {last_name}".strip()
                username = user.get("username")
                user_id = user.get("id")
                text = msg.get("text", "(텍스트 없음)")

                print("=" * 60)
                print("발견한 사용자 정보:")
                print("=" * 60)
                print(f"이름: {full_name or '(이름 없음)'}")
                print(f"유저네임: @{username}" if username else "유저네임: 없음")
                print(f"사용자 ID: {user_id}")
                print(f"메시지: {text}")
                print("=" * 60)
                print(f"\n✨ 당신의 텔레그램 ID는: {user_id}")
                print("\n이 ID를 .env 파일에 추가하세요:")
                print(f"TELEGRAM_ALLOWED_USERS={user_id}")
                print("=" * 60)
                return

        print("\n\n⏱️  시간 초과!")
        print("60초 동안 메시지가 없었습니다.")
        print("다시 실행하고 봇에게 메시지를 보내주세요.")

    except Exception as exc:
        print(f"\n\n❌ 오류: {exc}")
        print("\n봇 토큰이 올바른지 확인하세요.")


if __name__ == "__main__":
    get_user_id()
