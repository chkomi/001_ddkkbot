#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drain pending Telegram work by reusing daemon runtime logic.

This script intentionally delegates to `DaemonService` to avoid logic drift.
It runs bounded cycles and exits when the system becomes idle.
"""

from __future__ import annotations

import argparse
import sys

from daemon_service import DaemonService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drain pending Sonolbot tasks once and exit.")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=120,
        help="Maximum daemon cycles to run before giving up (default: 120).",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=1.0,
        help="Sleep seconds between cycles (default: 1.0).",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Do not acquire daemon lock (not recommended).",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    service = DaemonService()
    rc = service.drain_pending_once(
        max_cycles=max(1, int(args.max_cycles)),
        sleep_sec=max(0.2, float(args.sleep_sec)),
        use_lock=not bool(args.no_lock),
    )
    if rc == 0:
        print("Drain completed: no pending/stateful work.")
    else:
        print(f"Drain finished with code={rc}. Check logs/daemon-YYYY-MM-DD.log.")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
