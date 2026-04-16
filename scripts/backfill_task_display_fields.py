#!/usr/bin/env python3
"""Backfill display title/summary/status fields in task index files."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from task_commands import _build_task_item


INDEX_FILENAME = "index.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _iter_index_paths(tasks_dir: Path, recursive: bool) -> list[Path]:
    if recursive:
        paths = sorted(tasks_dir.glob(f"**/{INDEX_FILENAME}"))
        return [p for p in paths if p.is_file()]
    single = tasks_dir / INDEX_FILENAME
    return [single] if single.exists() else []


def _backfill_one(index_path: Path, force: bool) -> tuple[int, int]:
    payload = _read_json(index_path, {"tasks": []})
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return (0, 0)

    tasks_dir = index_path.parent
    changed = 0
    total = 0

    for idx, entry in enumerate(tasks):
        if not isinstance(entry, dict):
            continue
        total += 1
        row = _build_task_item(entry, tasks_dir=tasks_dir, include_instrunction=False)

        existing_title = str(entry.get("display_title") or "").strip()
        existing_state = str(entry.get("title_state") or "").strip().lower()

        updates = {
            "display_subtitle": row.get("display_subtitle") or "",
            "work_status": row.get("work_status") or row.get("status") or "unknown",
            "ops_status": row.get("ops_status") or "unknown",
            "title_updated_at": row.get("title_updated_at") or str(entry.get("timestamp") or ""),
        }

        if force or not existing_title or existing_state != "final":
            updates["display_title"] = row.get("display_title") or existing_title or "새 작업"
            updates["title_state"] = row.get("title_state") or (existing_state if existing_state in ("provisional", "final") else "provisional")

        before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry.update(updates)
        tasks[idx] = entry
        after = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        if before != after:
            changed += 1

    if changed > 0:
        payload["tasks"] = tasks
        payload["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return total, changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill display fields in Sonolbot task indexes")
    parser.add_argument("--tasks-dir", default="tasks", help="Root task directory (contains index.json)")
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories for index.json")
    parser.add_argument("--force", action="store_true", help="Overwrite even finalized titles")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    tasks_dir = Path(args.tasks_dir).resolve()
    index_paths = _iter_index_paths(tasks_dir, recursive=bool(args.recursive))

    scanned = 0
    updated = 0
    files_updated = 0
    details: list[dict[str, Any]] = []

    for index_path in index_paths:
        total, changed = _backfill_one(index_path=index_path, force=bool(args.force))
        scanned += total
        updated += changed
        if changed > 0:
            files_updated += 1
        details.append(
            {
                "index_path": str(index_path),
                "tasks_total": total,
                "tasks_updated": changed,
            }
        )

    summary = {
        "ok": True,
        "tasks_dir": str(tasks_dir),
        "recursive": bool(args.recursive),
        "force": bool(args.force),
        "index_files": len(index_paths),
        "index_files_updated": files_updated,
        "tasks_scanned": scanned,
        "tasks_updated": updated,
        "details": details,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"index_files={summary['index_files']} updated={summary['index_files_updated']}")
        print(f"tasks_scanned={summary['tasks_scanned']} tasks_updated={summary['tasks_updated']}")
        for item in details:
            print(f"- {item['index_path']} | total={item['tasks_total']} | updated={item['tasks_updated']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
