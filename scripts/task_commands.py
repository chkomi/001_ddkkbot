#!/usr/bin/env python3
"""Utility commands for Sonolbot task browsing/activation."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TASKS_DIR = ROOT_DIR / "tasks"
INDEX_FILENAME = "index.json"
RELATED_FILENAME = "related_tasks.json"
TASK_INFO_FILENAME = "task_info.txt"
TASK_META_FILENAME = "task_meta.json"
INSTRUNCTION_CANDIDATES = ("INSTRUNCTION.md", "INSTRUCTIONS.md")
TASK_ID_THREAD_PREFIX = "thread_"
TASK_ID_MSG_PREFIX = "msg_"
OPS_NOISE_PATTERNS = (
    r"\back\b",
    r"dns",
    r"network",
    r"네트워크",
    r"송신\s*실패",
    r"재전송",
    r"재시도",
    r"전송\s*대기",
    r"telegram",
    r"텔레그램",
)
WORK_SIGNAL_PATTERNS = (r"완료", r"적용", r"반영", r"수정", r"추가", r"구현", r"정리", r"분석", r"작성", r"생성", r"업데이트")
WAITING_PATTERNS = (r"대기", r"보류", r"진행 중")
BLOCKED_PATTERNS = (r"차단", r"실패", r"오류", r"불가")
SMALLTALK_PREFIX = ("안녕", "반가워", "고마워", "감사", "ㅎ", "ㅋㅋ", "하이")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _short(text: str, limit: int = 120) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _compact_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _parse_datetime_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _sanitize_thread_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(TASK_ID_THREAD_PREFIX):
        text = text[len(TASK_ID_THREAD_PREFIX) :]
    return text.strip()


def _normalize_task_id(task_id: Any = None, thread_id: Any = None, message_id: Any = None) -> str:
    raw = str(task_id or "").strip()
    if raw:
        if raw.startswith(TASK_ID_THREAD_PREFIX):
            tid = _sanitize_thread_id(raw)
            return f"{TASK_ID_THREAD_PREFIX}{tid}" if tid else ""
        if raw.startswith(TASK_ID_MSG_PREFIX):
            msg_id = _safe_int(raw[len(TASK_ID_MSG_PREFIX) :], 0)
            return f"{TASK_ID_MSG_PREFIX}{msg_id}" if msg_id > 0 else ""
        if raw.isdigit():
            return f"{TASK_ID_MSG_PREFIX}{int(raw)}"
        tid = _sanitize_thread_id(raw)
        return f"{TASK_ID_THREAD_PREFIX}{tid}" if tid else ""

    tid = _sanitize_thread_id(thread_id)
    if tid:
        return f"{TASK_ID_THREAD_PREFIX}{tid}"

    msg_id = _safe_int(message_id, 0)
    if msg_id > 0:
        return f"{TASK_ID_MSG_PREFIX}{msg_id}"
    return ""


def _thread_id_from_task_id(task_id: str) -> str:
    if str(task_id).startswith(TASK_ID_THREAD_PREFIX):
        return str(task_id)[len(TASK_ID_THREAD_PREFIX) :]
    return ""


def _task_id_from_entry(entry: dict[str, Any]) -> str:
    task_id = _normalize_task_id(
        task_id=entry.get("task_id"),
        thread_id=entry.get("thread_id"),
    )
    if task_id:
        return task_id

    task_dir = str(entry.get("task_dir") or "").strip()
    if task_dir:
        task_name = Path(task_dir).name
        task_id = _normalize_task_id(task_id=task_name)
        if task_id:
            return task_id
    message_id = _safe_int(entry.get("latest_message_id"), _safe_int(entry.get("message_id"), 0))
    if message_id > 0:
        return _normalize_task_id(message_id=message_id)
    return ""


def _entry_sort_key(item: dict[str, Any]) -> tuple[float, int, str]:
    ts = _parse_datetime_epoch(item.get("timestamp"))
    latest_message_id = _safe_int(item.get("latest_message_id"), _safe_int(item.get("message_id"), 0))
    task_id = str(item.get("task_id") or "")
    return (ts, latest_message_id, task_id)


def _load_tasks(tasks_dir: Path) -> list[dict[str, Any]]:
    index_path = tasks_dir / INDEX_FILENAME
    payload = _read_json(index_path, {"tasks": []})
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return []

    merged: dict[str, dict[str, Any]] = {}
    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        task_id = _task_id_from_entry(item)
        if not task_id:
            continue
        item["task_id"] = task_id
        if not str(item.get("thread_id") or "").strip():
            item["thread_id"] = _thread_id_from_task_id(task_id)

        source_message_ids = item.get("source_message_ids")
        if not isinstance(source_message_ids, list):
            source_message_ids = []
        source_message_ids = sorted({_safe_int(v, 0) for v in source_message_ids if _safe_int(v, 0) > 0})
        latest_message_id = _safe_int(item.get("latest_message_id"), 0)
        message_id = _safe_int(item.get("message_id"), 0)
        if source_message_ids and latest_message_id <= 0:
            latest_message_id = max(source_message_ids)
        if latest_message_id <= 0:
            latest_message_id = message_id
        item["latest_message_id"] = latest_message_id
        item["message_id"] = message_id
        item["source_message_ids"] = source_message_ids

        if not str(item.get("task_dir") or "").strip():
            item["task_dir"] = str(tasks_dir / task_id)

        prev = merged.get(task_id)
        if prev is None or _entry_sort_key(item) >= _entry_sort_key(prev):
            merged[task_id] = item

    rows = list(merged.values())
    rows.sort(key=_entry_sort_key, reverse=True)
    return rows


def _detect_instruction_file(task_dir: Path) -> Path | None:
    for name in INSTRUNCTION_CANDIDATES:
        path = task_dir / name
        if path.exists():
            return path
    return None


def _extract_result_line(task_dir: Path) -> str:
    path = task_dir / TASK_INFO_FILENAME
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("[결과] "):
                return line[len("[결과] ") :].strip()
    except Exception:
        return ""
    return ""


def _extract_latest_change(task_dir: Path) -> str:
    payload = _read_json(task_dir / TASK_META_FILENAME, {})
    notes = payload.get("change_notes", [])
    if not isinstance(notes, list) or not notes:
        return ""
    last = notes[-1]
    if not isinstance(last, dict):
        return ""
    ts = str(last.get("timestamp") or "").strip()
    note = str(last.get("note") or "").strip()
    if ts and note:
        return f"{ts} | {note}"
    return note or ts


def _extract_related_ids(task_dir: Path) -> list[str]:
    payload = _read_json(task_dir / RELATED_FILENAME, {})
    items = payload.get("related_tasks", [])
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = _normalize_task_id(
            task_id=item.get("task_id"),
            thread_id=item.get("thread_id"),
            message_id=item.get("message_id"),
        )
        if not task_id:
            continue
        out.append(task_id)
    return out


def _contains_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    for pattern in patterns:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return True
    return False


def _extract_latest_change_note(latest_change: str) -> str:
    value = _compact_space(latest_change)
    if not value:
        return ""
    if "|" in value:
        return _compact_space(value.split("|", 1)[1])
    return value


def _is_ops_noise_text(text: str) -> bool:
    return _contains_pattern(text, OPS_NOISE_PATTERNS)


def _has_work_signal(text: str) -> bool:
    return _contains_pattern(text, WORK_SIGNAL_PATTERNS) and not _is_ops_noise_text(text)


def _derive_work_status(result_summary: str, result_line: str, latest_change: str) -> str:
    text = _compact_space(f"{result_summary} {result_line} {_extract_latest_change_note(latest_change)}")
    if not text:
        return "in_progress"
    if _contains_pattern(text, WAITING_PATTERNS):
        return "waiting"
    if _contains_pattern(text, BLOCKED_PATTERNS) and not _is_ops_noise_text(text):
        return "blocked"
    if _has_work_signal(text):
        return "updated"
    if "작업 진행 중" in text:
        return "in_progress"
    return "updated"


def _derive_ops_status(result_summary: str, result_line: str, latest_change: str) -> str:
    text = _compact_space(f"{result_summary} {result_line} {_extract_latest_change_note(latest_change)}")
    if not text:
        return "unknown"
    if _contains_pattern(text, (r"dns", r"network", r"네트워크")):
        return "network_error"
    if _is_ops_noise_text(text):
        return "send_retry"
    return "ok"


def _derive_display_title(entry: dict[str, Any], instruction: str, result_summary: str, latest_change: str) -> str:
    value = _compact_space(str(entry.get("display_title") or ""))
    if value:
        return _short(value, 30)

    normalized_instruction = _compact_space(re.sub(r"^/\w+\s*", "", instruction)).strip()
    normalized_instruction = re.sub(r"^(안녕(?:하세요)?|반가워요?|고마워요?|감사(?:합니다)?)[\\s,!.]*", "", normalized_instruction).strip()
    if normalized_instruction:
        if len(normalized_instruction) <= 18 and normalized_instruction.startswith(SMALLTALK_PREFIX):
            return "일반 문의"
        return _short(normalized_instruction, 30)

    candidate = _extract_latest_change_note(latest_change)
    if candidate and not _is_ops_noise_text(candidate):
        return _short(candidate, 30)
    if result_summary and not _is_ops_noise_text(result_summary):
        return _short(result_summary, 30)
    return "새 작업"


def _derive_display_subtitle(entry: dict[str, Any], instruction: str, result_summary: str, result_line: str, latest_change: str) -> str:
    value = _compact_space(str(entry.get("display_subtitle") or ""))
    if value:
        return _short(value, 60)

    candidates = [
        _compact_space(result_summary or result_line),
        _extract_latest_change_note(latest_change),
        _compact_space(instruction),
    ]
    for idx, item in enumerate(candidates):
        if not item:
            continue
        if idx < 2 and _is_ops_noise_text(item):
            continue
        return _short(item, 60)
    return "요약 정보 없음"


def _build_task_item(
    entry: dict[str, Any],
    tasks_dir: Path,
    include_instrunction: bool,
) -> dict[str, Any]:
    task_id = _task_id_from_entry(entry)
    thread_id = _sanitize_thread_id(entry.get("thread_id")) or _thread_id_from_task_id(task_id)
    message_id = _safe_int(entry.get("message_id"), 0)
    latest_message_id = _safe_int(entry.get("latest_message_id"), message_id)

    fallback_dir = tasks_dir / task_id if task_id else tasks_dir / f"msg_{message_id}"
    task_dir = Path(entry.get("task_dir") or fallback_dir).resolve()
    instruction_file = _detect_instruction_file(task_dir)
    instruction_text = ""
    if include_instrunction and instruction_file is not None:
        try:
            instruction_text = instruction_file.read_text(encoding="utf-8")
        except Exception:
            instruction_text = ""

    instruction = str(entry.get("instruction") or "").strip()
    result_summary = str(entry.get("result_summary") or "").strip()
    result_line = _extract_result_line(task_dir)
    related_ids = _extract_related_ids(task_dir)
    latest_change = _extract_latest_change(task_dir)
    codex_session = entry.get("codex_session") if isinstance(entry.get("codex_session"), dict) else {}
    work_status = _derive_work_status(result_summary, result_line, latest_change)
    ops_status = _derive_ops_status(result_summary, result_line, latest_change)
    display_title = _derive_display_title(entry, instruction, result_summary, latest_change)
    display_subtitle = _derive_display_subtitle(entry, instruction, result_summary, result_line, latest_change)
    title_state = str(entry.get("title_state") or "provisional").strip().lower()
    if title_state not in ("provisional", "final"):
        title_state = "provisional"
    title_updated_at = str(entry.get("title_updated_at") or "").strip() or str(entry.get("timestamp") or "").strip()

    source_message_ids = entry.get("source_message_ids")
    if not isinstance(source_message_ids, list):
        source_message_ids = []
    source_message_ids = sorted({_safe_int(v, 0) for v in source_message_ids if _safe_int(v, 0) > 0})

    return {
        "task_id": task_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "latest_message_id": latest_message_id,
        "source_message_ids": source_message_ids,
        "task_dir": str(task_dir),
        "task_dir_name": task_dir.name,
        "timestamp": str(entry.get("timestamp") or "").strip(),
        "status": work_status,
        "work_status": work_status,
        "ops_status": ops_status,
        "instruction": instruction,
        "instruction_short": _short(instruction, 100),
        "result_summary": result_summary,
        "result_summary_short": _short(result_summary or result_line, 100),
        "result_line": result_line,
        "instruction_file": str(instruction_file) if instruction_file else "",
        "instruction_text": instruction_text,
        "latest_change": latest_change,
        "related_task_ids": related_ids,
        "codex_session": codex_session,
        "display_title": display_title,
        "display_subtitle": display_subtitle,
        "title_state": title_state,
        "title_updated_at": title_updated_at,
    }


def _resolve_target(target: str, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    raw = (target or "").strip()
    if not raw:
        return None

    normalized_task_target = _normalize_task_id(task_id=raw)
    if normalized_task_target:
        for item in tasks:
            if _task_id_from_entry(item) == normalized_task_target:
                return item

    m = re.fullmatch(r"msg_(\d+)", raw, flags=re.IGNORECASE)
    if m:
        raw = m.group(1)

    if raw.isdigit():
        msg_id = int(raw)
        for item in tasks:
            latest_message_id = _safe_int(item.get("latest_message_id"), _safe_int(item.get("message_id"), 0))
            if latest_message_id == msg_id or _safe_int(item.get("message_id"), 0) == msg_id:
                return item
        return None

    q = raw.lower()
    best: tuple[int, float, dict[str, Any]] | None = None
    for item in tasks:
        instruction = str(item.get("instruction") or "").lower()
        result_summary = str(item.get("result_summary") or "").lower()
        display_title = str(item.get("display_title") or "").lower()
        display_subtitle = str(item.get("display_subtitle") or "").lower()
        task_dir = str(item.get("task_dir") or "").lower()
        task_id = str(item.get("task_id") or "").lower()

        score = 0
        if q in instruction:
            score += 3
        if q in result_summary:
            score += 2
        if q in display_title:
            score += 3
        if q in display_subtitle:
            score += 2
        if q in task_dir:
            score += 1
        if q in task_id:
            score += 2
        if score <= 0:
            continue
        candidate = (score, _entry_sort_key(item)[0], item)
        if best is None or candidate > best:
            best = candidate
    return best[2] if best else None


def _print_list(items: list[dict[str, Any]], total: int) -> None:
    print(f"tasks_total={total} shown={len(items)}")
    for item in items:
        session_id = ""
        codex_session = item.get("codex_session")
        if isinstance(codex_session, dict):
            session_id = str(codex_session.get("session_id") or "").strip()
        line = (
            f"- {item['task_id']} | status={item['work_status']} | ops={item['ops_status']} | ts={item['timestamp']} "
            f"| dir={item['task_dir_name']}"
        )
        print(line)
        print(f"  title={item['display_title']}")
        print(f"  summary={item['display_subtitle']}")
        if session_id:
            print(f"  session_id={session_id}")


def cmd_list(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks_dir).resolve()
    tasks = _load_tasks(tasks_dir)
    keyword = (args.keyword or "").strip().lower()
    if keyword:
        filtered = []
        for item in tasks:
            doc = " ".join(
                [
                    str(item.get("instruction") or ""),
                    str(item.get("result_summary") or ""),
                    str(item.get("display_title") or ""),
                    str(item.get("display_subtitle") or ""),
                    str(item.get("task_dir") or ""),
                    str(item.get("task_id") or ""),
                ]
            ).lower()
            if keyword in doc:
                filtered.append(item)
        tasks = filtered

    limit = max(1, int(args.limit))
    selected = tasks[:limit]
    rows = [_build_task_item(item, tasks_dir=tasks_dir, include_instrunction=False) for item in selected]

    if args.json:
        print(
            json.dumps(
                {
                    "tasks_total": len(tasks),
                    "shown": len(rows),
                    "tasks": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    _print_list(rows, total=len(tasks))
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks_dir).resolve()
    tasks = _load_tasks(tasks_dir)
    target = str(args.target or "").strip()
    found = _resolve_target(target, tasks)
    if found is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"task not found for target={target}",
                    "hint": "use list command first",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    row = _build_task_item(
        found,
        tasks_dir=tasks_dir,
        include_instrunction=bool(args.include_instrunction),
    )

    if args.json:
        print(json.dumps({"ok": True, "task": row}, ensure_ascii=False, indent=2))
        return 0

    print(f"ok=true target={target}")
    print(f"task_id={row['task_id']}")
    if row["thread_id"]:
        print(f"thread_id={row['thread_id']}")
    if _safe_int(row.get("message_id"), 0) > 0:
        print(f"message_id={row['message_id']}")
    print(f"task_dir={row['task_dir']}")
    print(f"status={row['work_status']}")
    print(f"ops_status={row['ops_status']}")
    print(f"timestamp={row['timestamp']}")
    print(f"instruction_file={row['instruction_file']}")
    print(f"title={row['display_title']}")
    print(f"summary={row['display_subtitle']}")
    if row["latest_change"]:
        print(f"latest_change={row['latest_change']}")
    if row["related_task_ids"]:
        rel = ",".join(str(v) for v in row["related_task_ids"])
        print(f"related_task_ids={rel}")
    codex_session = row.get("codex_session")
    if isinstance(codex_session, dict):
        session_id = str(codex_session.get("session_id") or "").strip()
        if session_id:
            print(f"session_id={session_id}")
    if bool(args.include_instrunction):
        print("---- INSTRUNCTION BEGIN ----")
        print(row.get("instruction_text") or "")
        print("---- INSTRUNCTION END ----")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sonolbot task helper commands")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List task entries")
    p_list.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--keyword", default="")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_activate = sub.add_parser("activate", help="Resolve one task and print context")
    p_activate.add_argument("target", help="task_id | thread_<id> | msg_<id> | keyword")
    p_activate.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    p_activate.add_argument("--include-instrunction", action="store_true")
    p_activate.add_argument("--json", action="store_true")
    p_activate.set_defaults(func=cmd_activate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
