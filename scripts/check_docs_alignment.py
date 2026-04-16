#!/usr/bin/env python3
"""Lightweight docs/runtime drift checker for Sonolbot."""

from __future__ import annotations

from pathlib import Path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    agents = root / "AGENTS.md"
    agents_coder = root / "AGENTS__FOR_CODER.md"
    daemon = root / "daemon_service.py"

    agents_text = _read(agents)
    coder_text = _read(agents_coder)
    daemon_text = _read(daemon)

    issues: list[str] = []

    if not agents_text:
        issues.append(f"missing or unreadable: {agents}")
    if not coder_text:
        issues.append(f"missing or unreadable: {agents_coder}")
    if not daemon_text:
        issues.append(f"missing or unreadable: {daemon}")

    if agents_text and "AGENTS__FOR_CODER.md" not in agents_text:
        issues.append("AGENTS.md must reference AGENTS__FOR_CODER.md")
    if agents_text and "tasks/chat_{chat_id}/thread_{thread_id}" not in agents_text:
        issues.append("AGENTS.md should document thread-based task path")

    if coder_text and "codex app-server" not in coder_text:
        issues.append("AGENTS__FOR_CODER.md should mention codex app-server transport")

    if coder_text and "tasks/chat_{chat_id}/thread_{thread_id}" not in coder_text:
        issues.append("AGENTS__FOR_CODER.md should document chat-partitioned thread task path")

    if coder_text and "thread_id" not in coder_text:
        issues.append("AGENTS__FOR_CODER.md should explain app-server thread_id terminology")

    if coder_text and "task_id" not in coder_text:
        issues.append("AGENTS__FOR_CODER.md should explain task_id terminology")

    if daemon_text and 'DEFAULT_CODEX_TRANSPORT_MODE = "app_server"' not in daemon_text:
        issues.append("daemon_service.py default transport unexpectedly changed from app_server")

    if daemon_text and "SONOLBOT_TASKS_PARTITION_BY_CHAT" in daemon_text:
        if coder_text and "SONOLBOT_TASKS_PARTITION_BY_CHAT" not in coder_text:
            issues.append("AGENTS__FOR_CODER.md should mention SONOLBOT_TASKS_PARTITION_BY_CHAT")

    if issues:
        for item in issues:
            print(f"[DOC_CHECK][WARN] {item}")
        return 1

    print("[DOC_CHECK] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
