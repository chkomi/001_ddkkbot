#!/usr/bin/env python3
"""Telegram token/user-id validation helpers for control panel and migration."""

from __future__ import annotations

import re
from typing import Any

import requests


BOT_TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{20,}$")
USER_ID_RE = re.compile(r"^[0-9]{4,20}$")


def mask_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + ("*" * (len(token) - 8)) + token[-4:]


def validate_bot_token_format(token: str) -> tuple[bool, str]:
    token = (token or "").strip()
    if not token:
        return False, "토큰이 비어 있습니다."
    if not BOT_TOKEN_RE.match(token):
        return False, "토큰 형식이 올바르지 않습니다. (예: 123456789:AA...)"
    return True, ""


def validate_user_id_format(user_id_raw: str) -> tuple[bool, int | None, str]:
    value = (user_id_raw or "").strip()
    if not value:
        return False, None, "사용자 ID가 비어 있습니다."
    if not USER_ID_RE.match(value):
        return False, None, "사용자 ID는 숫자만 입력해야 합니다."
    try:
        user_id = int(value)
    except ValueError:
        return False, None, "사용자 ID 숫자 변환에 실패했습니다."
    return True, user_id, ""


def fetch_bot_profile(token: str, timeout_sec: float = 8.0) -> tuple[bool, dict[str, Any], str]:
    ok, err = validate_bot_token_format(token)
    if not ok:
        return False, {}, err
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=max(2.0, float(timeout_sec)),
        )
    except Exception as exc:
        return False, {}, f"네트워크 오류: {exc}"

    if resp.status_code != 200:
        return False, {}, f"Telegram API 응답 코드 오류: {resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return False, {}, "Telegram API JSON 파싱 실패"
    if not data.get("ok"):
        desc = str(data.get("description") or "unknown")
        return False, {}, f"토큰 검증 실패: {desc}"
    result = data.get("result")
    if not isinstance(result, dict):
        return False, {}, "bot profile 형식 오류"
    return True, result, ""


def validate_user_id_live(token: str, user_id: int, timeout_sec: float = 8.0) -> tuple[bool, str]:
    """Best-effort live validation using getChat.

    Telegram limitations:
    - 봇과 상호작용을 시작하지 않은 사용자는 getChat 실패가 날 수 있다.
    - 따라서 실패 시 '형식은 유효하나 라이브 검증 실패'로 취급 가능한 메시지를 반환한다.
    """
    ok, profile, err = fetch_bot_profile(token, timeout_sec=timeout_sec)
    if not ok:
        return False, f"토큰 검증 실패로 사용자 라이브 검증 불가: {err}"
    _ = profile

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": int(user_id)},
            timeout=max(2.0, float(timeout_sec)),
        )
    except Exception as exc:
        return False, f"사용자 라이브 검증 네트워크 오류: {exc}"

    if resp.status_code != 200:
        return False, f"사용자 라이브 검증 응답 코드 오류: {resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return False, "사용자 라이브 검증 JSON 파싱 실패"
    if data.get("ok"):
        return True, "사용자 라이브 검증 성공"
    desc = str(data.get("description") or "unknown")
    return False, f"사용자 라이브 검증 실패: {desc}"
