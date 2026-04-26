"""Pure helper utilities for task identifiers, inline keyboards, and guide-edit detection.

These were previously private staticmethods on `DaemonService`. They are extracted
here so they can be unit-tested in isolation and reused without pulling in the
full daemon class. All functions are pure (no `self`, no I/O).
"""

from __future__ import annotations

import re
from typing import Any


# Constants required by the helpers below. Kept in sync with daemon_service.py.
INLINE_TASK_SELECT_CALLBACK_PREFIX = "task_select:"
CALLBACK_TASK_SELECT_PREFIX = "__cb__:task_select:"
TASK_GUIDE_TRIGGER_TEXT = "task 지침"
TASK_GUIDE_EDIT_KEYWORDS = (
    "추가",
    "변경",
    "수정",
    "편집",
    "갱신",
    "업데이트",
    "교체",
    "덮어",
    "고쳐",
    "바꿔",
    "추가해",
    "변경해",
    "수정해",
)


def normalize_ui_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_task_id_token(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"msg_\d+", text, flags=re.IGNORECASE):
        return text.lower()
    if re.fullmatch(r"thread_[A-Za-z0-9._:-]+", text, flags=re.IGNORECASE):
        return text
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text):
        return f"thread_{text}"
    if text.isdigit():
        return f"msg_{text}"
    return ""


def build_single_task_inline_select_keyboard(task_id: str) -> list[list[dict[str, str]]]:
    normalized = normalize_task_id_token(task_id)
    if not normalized:
        return []
    return [[{"text": "선택", "callback_data": f"{INLINE_TASK_SELECT_CALLBACK_PREFIX}{normalized}"}]]


def extract_callback_task_select_id(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized.lower().startswith(CALLBACK_TASK_SELECT_PREFIX):
        return ""
    suffix = normalized[len(CALLBACK_TASK_SELECT_PREFIX):].strip()
    return normalize_task_id_token(suffix)


def clear_temp_task_seed(state: dict[str, Any]) -> None:
    state["temp_task_first_text"] = ""
    state["temp_task_first_message_id"] = 0
    state["temp_task_first_timestamp"] = ""


def is_task_guide_edit_request_text(text: str) -> bool:
    normalized = normalize_ui_text(text).lower()
    if not normalized:
        return False
    if TASK_GUIDE_TRIGGER_TEXT not in normalized:
        return False
    if "보기" in normalized:
        return False
    return any(keyword in normalized for keyword in TASK_GUIDE_EDIT_KEYWORDS)
