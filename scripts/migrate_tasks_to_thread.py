#!/usr/bin/env python3
"""Migrate legacy msg_* task entries to thread_* task entries.

This script supports dry-run and apply modes.
- dry-run (default): prints what would change
- apply: updates index.json and legacy_task_thread_map.json, and tries to move folders

Safety-first migration:
- Only converts msg_* when a deterministic thread id exists (entry.thread_id or map file).
- Does not infer thread ids from codex_session.session_id (can collapse multiple tasks).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

INDEX_FILENAME = "index.json"
MAP_FILENAME = "legacy_task_thread_map.json"
TASK_ID_MSG_PREFIX = "msg_"
TASK_ID_THREAD_PREFIX = "thread_"
REDIRECT_FILENAME = "MIGRATED_TO_THREAD.txt"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _compact(value: Any, max_len: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _normalize_task_id(task_id: Any = None, thread_id: Any = None, message_id: Any = None) -> str:
    raw = str(task_id or "").strip()
    if raw:
        if raw.startswith(TASK_ID_THREAD_PREFIX):
            tid = raw[len(TASK_ID_THREAD_PREFIX) :].strip()
            return f"{TASK_ID_THREAD_PREFIX}{tid}" if tid else ""
        if raw.startswith(TASK_ID_MSG_PREFIX):
            msg_id = _safe_int(raw[len(TASK_ID_MSG_PREFIX) :], 0)
            return f"{TASK_ID_MSG_PREFIX}{msg_id}" if msg_id > 0 else ""
        if raw.isdigit():
            return f"{TASK_ID_MSG_PREFIX}{int(raw)}"
        return f"{TASK_ID_THREAD_PREFIX}{raw}"

    tid = str(thread_id or "").strip()
    if tid:
        if tid.startswith(TASK_ID_THREAD_PREFIX):
            tid = tid[len(TASK_ID_THREAD_PREFIX) :]
        return f"{TASK_ID_THREAD_PREFIX}{tid}" if tid else ""

    msg_id = _safe_int(message_id, 0)
    if msg_id > 0:
        return f"{TASK_ID_MSG_PREFIX}{msg_id}"
    return ""


def _task_id_from_entry(entry: dict[str, Any]) -> str:
    explicit = _normalize_task_id(task_id=entry.get("task_id"), thread_id=entry.get("thread_id"))
    if explicit:
        return explicit

    task_dir = str(entry.get("task_dir") or "").strip()
    if task_dir:
        name = Path(task_dir).name
        by_dir = _normalize_task_id(task_id=name)
        if by_dir:
            return by_dir

    return _normalize_task_id(message_id=entry.get("latest_message_id") or entry.get("message_id"))


def _parse_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _sort_key(entry: dict[str, Any]) -> tuple[float, int, str]:
    return (
        _parse_epoch(entry.get("timestamp")),
        _safe_int(entry.get("latest_message_id"), _safe_int(entry.get("message_id"), 0)),
        str(entry.get("task_id") or ""),
    )


def _merge_entries(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    preferred = new if _sort_key(new) >= _sort_key(old) else old
    secondary = old if preferred is new else new
    merged = dict(secondary)
    merged.update(preferred)

    source_ids = sorted(
        {
            _safe_int(v, 0)
            for v in (old.get("source_message_ids") or []) + (new.get("source_message_ids") or [])
            if _safe_int(v, 0) > 0
        }
    )
    if source_ids:
        merged["source_message_ids"] = source_ids
        merged["latest_message_id"] = max(source_ids)

    related = []
    seen: set[str] = set()
    for raw in (old.get("related_task_ids") or []) + (new.get("related_task_ids") or []):
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        related.append(value)
    if related:
        merged["related_task_ids"] = related

    return merged


def _resolve_thread_id(entry: dict[str, Any], mapping: dict[str, str], task_id: str) -> str:
    raw = str(entry.get("thread_id") or "").strip()
    if raw:
        return raw[len(TASK_ID_THREAD_PREFIX) :] if raw.startswith(TASK_ID_THREAD_PREFIX) else raw

    mapped = str(mapping.get(task_id) or "").strip()
    if mapped:
        return mapped

    return ""


def _iter_task_roots(tasks_dir: Path, chat_id: int | None) -> list[Path]:
    if chat_id is not None:
        target = tasks_dir / f"chat_{chat_id}"
        return [target] if target.exists() else []

    roots: list[Path] = []
    chat_roots = sorted(p for p in tasks_dir.glob("chat_*") if p.is_dir())
    roots.extend(chat_roots)
    if (tasks_dir / INDEX_FILENAME).exists():
        roots.append(tasks_dir)
    return roots


def _move_legacy_dir(old_dir: Path, new_dir: Path) -> tuple[bool, str]:
    if not old_dir.exists() or not old_dir.is_dir():
        return False, "old_missing"
    if new_dir.exists():
        redirect = old_dir / REDIRECT_FILENAME
        redirect.write_text(f"migrated_to={new_dir}\n", encoding="utf-8")
        return False, "target_exists_redirect_only"
    try:
        old_dir.rename(new_dir)
        return True, "renamed"
    except OSError as exc:
        return False, f"rename_failed:{exc}"


def migrate_root(root: Path, apply: bool) -> dict[str, Any]:
    index_path = root / INDEX_FILENAME
    payload = _read_json(index_path, {"tasks": []})
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return {
            "task_root": str(root),
            "index_path": str(index_path),
            "total": 0,
            "converted": 0,
            "unresolved": [],
            "dedup_merged": 0,
            "dir_moves": [],
            "changed": False,
        }

    mapping_path = root / MAP_FILENAME
    mapping = _read_json(mapping_path, {})
    if not isinstance(mapping, dict):
        mapping = {}

    merged: dict[str, dict[str, Any]] = {}
    converted = 0
    dedup_merged = 0
    unresolved: list[dict[str, Any]] = []
    dir_moves: list[dict[str, Any]] = []
    changed = False

    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        original_task_id = _task_id_from_entry(entry)
        if not original_task_id:
            unresolved.append({"reason": "missing_task_id", "entry": _compact(entry)})
            continue

        normalized_task_id = original_task_id
        if original_task_id.startswith(TASK_ID_MSG_PREFIX):
            resolved_thread_id = _resolve_thread_id(entry, mapping, original_task_id)
            if not resolved_thread_id:
                unresolved.append(
                    {
                        "task_id": original_task_id,
                        "reason": "thread_id_not_found",
                        "hint": "index.thread_id / legacy_task_thread_map.json 확인",
                    }
                )
            else:
                normalized_task_id = f"{TASK_ID_THREAD_PREFIX}{resolved_thread_id}"
                mapping[original_task_id] = resolved_thread_id
                converted += 1
                changed = True

                old_dir = Path(str(entry.get("task_dir") or (root / original_task_id))).resolve()
                new_dir = (root / normalized_task_id).resolve()
                if apply and old_dir != new_dir:
                    moved, note = _move_legacy_dir(old_dir=old_dir, new_dir=new_dir)
                    dir_moves.append(
                        {
                            "from": str(old_dir),
                            "to": str(new_dir),
                            "moved": bool(moved),
                            "note": note,
                        }
                    )
                entry["thread_id"] = resolved_thread_id
                entry["task_dir"] = str(new_dir)

        if normalized_task_id.startswith(TASK_ID_THREAD_PREFIX):
            entry["thread_id"] = str(entry.get("thread_id") or normalized_task_id[len(TASK_ID_THREAD_PREFIX) :]).strip()

        msg_id = _safe_int(entry.get("latest_message_id"), _safe_int(entry.get("message_id"), 0))
        source_ids = entry.get("source_message_ids") if isinstance(entry.get("source_message_ids"), list) else []
        normalized_source = sorted({_safe_int(v, 0) for v in source_ids if _safe_int(v, 0) > 0})
        if msg_id > 0 and msg_id not in normalized_source:
            normalized_source.append(msg_id)
            normalized_source = sorted(set(normalized_source))
        if normalized_source:
            entry["source_message_ids"] = normalized_source
            entry["latest_message_id"] = max(normalized_source)
        if msg_id > 0:
            entry["message_id"] = msg_id

        entry["task_id"] = normalized_task_id

        existing = merged.get(normalized_task_id)
        if existing is None:
            merged[normalized_task_id] = entry
        else:
            merged[normalized_task_id] = _merge_entries(existing, entry)
            dedup_merged += 1
            changed = True

    migrated_tasks = sorted(merged.values(), key=_sort_key, reverse=True)

    if apply and changed:
        payload["tasks"] = migrated_tasks
        payload["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_json_atomic(index_path, payload)
        _write_json_atomic(mapping_path, mapping)

    return {
        "task_root": str(root),
        "index_path": str(index_path),
        "total": len(tasks),
        "converted": converted,
        "unresolved": unresolved,
        "dedup_merged": dedup_merged,
        "dir_moves": dir_moves,
        "changed": changed,
        "would_write": bool(changed and not apply),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate Sonolbot tasks from msg_* to thread_* model")
    parser.add_argument("--tasks-dir", default="tasks", help="Task root directory")
    parser.add_argument("--chat-id", type=int, default=None, help="Specific chat_id to migrate")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    tasks_dir = Path(args.tasks_dir).resolve()
    roots = _iter_task_roots(tasks_dir=tasks_dir, chat_id=args.chat_id)

    reports: list[dict[str, Any]] = []
    for root in roots:
        if not (root / INDEX_FILENAME).exists():
            continue
        reports.append(migrate_root(root=root, apply=bool(args.apply)))

    total_entries = sum(int(r.get("total", 0)) for r in reports)
    total_converted = sum(int(r.get("converted", 0)) for r in reports)
    total_unresolved = sum(len(r.get("unresolved", [])) for r in reports)
    total_changed_roots = sum(1 for r in reports if bool(r.get("changed")))

    summary = {
        "ok": True,
        "apply": bool(args.apply),
        "tasks_dir": str(tasks_dir),
        "roots": len(reports),
        "roots_changed": total_changed_roots,
        "entries_total": total_entries,
        "entries_converted": total_converted,
        "entries_unresolved": total_unresolved,
        "reports": reports,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"mode={mode} roots={summary['roots']} changed={summary['roots_changed']}")
        print(
            f"entries_total={summary['entries_total']} converted={summary['entries_converted']} unresolved={summary['entries_unresolved']}"
        )
        for report in reports:
            unresolved = len(report.get("unresolved", []))
            print(
                f"- {report['task_root']} | total={report['total']} converted={report['converted']} "
                f"unresolved={unresolved} changed={report['changed']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
