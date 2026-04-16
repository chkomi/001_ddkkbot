#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows launcher entry for building control_panel.exe with PyInstaller.

This script starts control_panel.bat without showing an additional console
window (when packaged with --noconsole) and exits immediately.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOG_RETENTION_DAYS = 7
LAUNCHER_LOG_PREFIX = "control-panel-launcher"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _detect_root_dir() -> Path:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path(__file__).resolve().parent)
    candidates.append(Path.cwd())

    for candidate in candidates:
        if (candidate / "control_panel.bat").exists():
            return candidate
    return candidates[0]


def _logs_dir(root: Path) -> Path:
    configured = os.getenv("LOGS_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return (root / "logs").resolve()


def _daily_log_path(logs_dir: Path) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    return logs_dir / f"{LAUNCHER_LOG_PREFIX}-{day}.log"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def _cleanup_old_logs(logs_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now().date() - timedelta(days=max(1, retention_days) - 1)
    pattern = f"{LAUNCHER_LOG_PREFIX}-*.log"
    for path in logs_dir.glob(pattern):
        match = DATE_RE.search(path.stem)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _write_log(log_path: Path, message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _safe_write_log(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    try:
        _write_log(log_path, message)
    except OSError:
        return


def main() -> int:
    root = _detect_root_dir()
    log_path: Path | None = None
    try:
        logs_dir = _logs_dir(root)
        logs_dir.mkdir(parents=True, exist_ok=True)
        retention_days = _env_int("LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS, minimum=1)
        _cleanup_old_logs(logs_dir, retention_days)
        log_path = _daily_log_path(logs_dir)
    except OSError:
        log_path = None

    _safe_write_log(
        log_path,
        "launcher_start "
        f"frozen={getattr(sys, 'frozen', False)} "
        f"root={root} cwd={Path.cwd()} exe={sys.executable}",
    )

    bat = root / "control_panel.bat"
    if not bat.exists():
        _safe_write_log(log_path, f"ERROR missing_bat path={bat}")
        return 1

    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    forwarded_args = [str(v) for v in sys.argv[1:] if str(v).strip()]
    cmd = ["cmd.exe", "/c", str(bat), "__silent__", *forwarded_args]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _safe_write_log(log_path, f"ERROR launch_failed cmd={cmd!r} exc={exc!r}")
        return 1

    _safe_write_log(log_path, f"launch_ok pid={proc.pid} cmd={cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
