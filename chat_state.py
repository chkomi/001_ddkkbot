"""Chat state schema for app-server-driven Telegram/Discord chats.

The runtime representation is still a plain `dict[str, Any]` so existing
`state["xxx"] = ...` access patterns keep working. This module just gives the
schema a name and a single source of truth for the default values.
"""

from __future__ import annotations

from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# UI mode tokens. Kept here so the state schema is self-contained, but
# daemon_service.py still defines its own copies for backward compatibility.
# ---------------------------------------------------------------------------
UI_MODE_IDLE = "idle"


class ChatState(TypedDict, total=False):
    # --- Codex thread / turn ---
    thread_id: str
    active_turn_id: str
    active_message_ids: set[int]
    active_task_ids: set[str]
    queued_messages: list[dict[str, Any]]
    app_generation: int

    # --- Streaming response buffers ---
    delta_text: str
    final_text: str
    last_agent_message_sent: str
    last_agent_message_raw: str
    last_progress_sent_at: float
    last_progress_len: int
    last_turn_started_at: float
    last_lease_heartbeat_at: float

    # --- Failed-reply retry book-keeping ---
    failed_reply_text: str
    failed_reply_ids: set[int]

    # --- UI / menu state ---
    ui_mode: str
    ui_mode_expires_at: float

    # --- Resume-existing-task picker ---
    resume_choice_inline_only: bool
    resume_candidates: list[str]
    resume_candidate_buttons: list[str]
    resume_candidate_map: dict[str, str]
    selected_task_id: str
    selected_task_packet: str
    resume_target_thread_id: str
    resume_thread_switch_pending: bool
    resume_recent_chat_summary_once: str
    resume_context_inject_once: bool

    # --- New-task carryover state ---
    pending_new_task_summary: str
    force_new_thread_once: bool

    # --- Temp-task seed (first message before the user picks new vs resume) ---
    temp_task_first_text: str
    temp_task_first_message_id: int
    temp_task_first_timestamp: str

    # --- Bot rename helper ---
    bot_rename_base_name: str


def create_chat_state(*, ui_mode_idle: str = UI_MODE_IDLE) -> ChatState:
    """Return a fresh chat state populated with the default empty values."""
    return {
        # Codex thread / turn
        "thread_id": "",
        "active_turn_id": "",
        "active_message_ids": set(),
        "active_task_ids": set(),
        "queued_messages": [],
        "app_generation": 0,
        # Streaming response buffers
        "delta_text": "",
        "final_text": "",
        "last_agent_message_sent": "",
        "last_agent_message_raw": "",
        "last_progress_sent_at": 0.0,
        "last_progress_len": 0,
        "last_turn_started_at": 0.0,
        "last_lease_heartbeat_at": 0.0,
        # Failed-reply retry book-keeping
        "failed_reply_text": "",
        "failed_reply_ids": set(),
        # UI / menu state
        "ui_mode": ui_mode_idle,
        "ui_mode_expires_at": 0.0,
        # Resume-existing-task picker
        "resume_choice_inline_only": False,
        "resume_candidates": [],
        "resume_candidate_buttons": [],
        "resume_candidate_map": {},
        "selected_task_id": "",
        "selected_task_packet": "",
        "resume_target_thread_id": "",
        "resume_thread_switch_pending": False,
        "resume_recent_chat_summary_once": "",
        "resume_context_inject_once": False,
        # New-task carryover state
        "pending_new_task_summary": "",
        "force_new_thread_once": False,
        # Temp-task seed
        "temp_task_first_text": "",
        "temp_task_first_message_id": 0,
        "temp_task_first_timestamp": "",
        # Bot rename helper
        "bot_rename_base_name": "",
    }
