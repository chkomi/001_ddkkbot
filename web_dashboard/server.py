#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ddkkbot 웹 대시보드 FastAPI 서버.

실행:
  python3 web_dashboard/server.py
  또는 uvicorn web_dashboard.server:app --port 8765

환경변수:
  WEB_DASHBOARD_PORT     포트 (기본: 8765)
  WEB_DASHBOARD_EXTERNAL 외부 접근 허용 (0=localhost만, 1=외부 허용)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("fastapi가 설치되지 않았습니다. pip install fastapi uvicorn 을 실행하세요.")
    sys.exit(1)

app = FastAPI(title="ddkkbot Dashboard", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ── 상태 헬퍼 ──────────────────────────────────────────────────────────────────

def _read_pid_file(path: Path) -> int | None:
    try:
        val = path.read_text().strip()
        pid = int(val)
        os.kill(pid, 0)  # 프로세스 존재 확인
        return pid
    except Exception:
        return None


def _get_daemon_status() -> dict[str, Any]:
    pid_file = BASE_DIR / ".daemon_service.pid"
    codex_pid_file = BASE_DIR / ".codex_app_server.pid"

    daemon_pid = _read_pid_file(pid_file)
    codex_pid = _read_pid_file(codex_pid_file)

    ai_provider = _read_env_value("SONOLBOT_AI_PROVIDER", "codex")
    platform_list = []
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        platform_list.append("telegram")
    if os.getenv("DISCORD_BOT_TOKEN"):
        platform_list.append("discord")
    if os.getenv("SLACK_BOT_TOKEN"):
        platform_list.append("slack")

    return {
        "running": daemon_pid is not None,
        "daemon_pid": daemon_pid,
        "ai_pid": codex_pid,
        "ai_provider": ai_provider,
        "platforms": platform_list,
        "timestamp": datetime.now().isoformat(),
    }


def _get_tasks(limit: int = 20) -> list[dict[str, Any]]:
    index_path = BASE_DIR / "tasks" / "index.json"
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        tasks = sorted(tasks, key=lambda t: t.get("timestamp", ""), reverse=True)
        return tasks[:limit]
    except Exception:
        return []


def _read_env() -> dict[str, str]:
    """현재 .env 내용을 읽어 비밀값은 마스킹하여 반환."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    sensitive_re = re.compile(r"(?i)(token|key|secret|password|passwd)")
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if sensitive_re.search(k) and v and not v.startswith("YOUR_"):
                v = v[:4] + "***" + v[-2:] if len(v) > 8 else "***"
            result[k] = v
    except Exception:
        pass
    return result


def _read_env_value(key: str, default: str = "") -> str:
    """프로세스 환경변수 대신 .env 파일에서 직접 값을 읽어 반환."""
    env_path = BASE_DIR / ".env"
    key_pattern = re.compile(f"^{re.escape(key)}\\s*=\\s*(.*)$")
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = key_pattern.match(line.strip())
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return os.getenv(key, default)


def _update_env(key: str, value: str) -> bool:
    """안전하게 .env 파일의 특정 키 값을 업데이트."""
    if re.search(r"[\n\r]", value):
        return False
    env_path = BASE_DIR / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    key_pattern = re.compile(f"^{re.escape(key)}\\s*=")
    updated = False
    new_lines = []
    for line in lines:
        if key_pattern.match(line):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")

    try:
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        tmp.replace(env_path)
        return True
    except Exception:
        return False


# ── SSE 로그 스트리밍 ──────────────────────────────────────────────────────────

async def _log_stream_generator(logs_dir: Path) -> AsyncGenerator[str, None]:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = logs_dir / f"daemon-{today}.log"
    last_size = 0

    # 기존 마지막 50줄 전송
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-50:]:
                yield f"data: {json.dumps({'line': line, 'ts': today})}\n\n"
            last_size = log_file.stat().st_size
        except Exception:
            pass

    # 이후 실시간 스트리밍
    while True:
        await asyncio.sleep(1)
        # 날짜 전환 처리
        today_now = datetime.now().strftime("%Y-%m-%d")
        if today_now != today:
            today = today_now
            log_file = logs_dir / f"daemon-{today}.log"
            last_size = 0

        if not log_file.exists():
            continue
        try:
            current_size = log_file.stat().st_size
            if current_size <= last_size:
                continue
            with log_file.open(encoding="utf-8", errors="replace") as f:
                f.seek(last_size)
                new_content = f.read()
            last_size = current_size
            for line in new_content.splitlines():
                if line.strip():
                    yield f"data: {json.dumps({'line': line, 'ts': today_now})}\n\n"
        except Exception:
            pass


# ── API 엔드포인트 ──────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_get_daemon_status())


@app.get("/api/tasks")
async def api_tasks(limit: int = 20) -> JSONResponse:
    return JSONResponse({"tasks": _get_tasks(limit)})


@app.get("/api/settings")
async def api_settings() -> JSONResponse:
    return JSONResponse({"settings": _read_env()})


@app.post("/api/settings")
async def api_settings_update(request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    value = str(body.get("value", "")).strip()
    if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise HTTPException(status_code=400, detail="올바르지 않은 키 이름입니다.")
    if _update_env(key, value):
        return JSONResponse({"ok": True})
    raise HTTPException(status_code=500, detail="설정 저장에 실패했습니다.")


@app.post("/api/daemon/start")
async def api_daemon_start() -> JSONResponse:
    status = _get_daemon_status()
    if status["running"]:
        return JSONResponse({"ok": True, "message": "이미 실행 중입니다."})
    exe = BASE_DIR / "mybot_autoexecutor.sh"
    if not exe.exists():
        raise HTTPException(status_code=404, detail="mybot_autoexecutor.sh 파일을 찾을 수 없습니다.")
    subprocess.Popen(
        ["bash", str(exe)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return JSONResponse({"ok": True, "message": "봇을 시작했습니다."})


@app.post("/api/daemon/stop")
async def api_daemon_stop() -> JSONResponse:
    status = _get_daemon_status()
    if not status["running"] or not status["daemon_pid"]:
        return JSONResponse({"ok": True, "message": "이미 중지 상태입니다."})
    try:
        os.kill(int(status["daemon_pid"]), signal.SIGTERM)
        return JSONResponse({"ok": True, "message": "봇을 중지했습니다."})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/stream")
async def api_logs_stream() -> StreamingResponse:
    logs_dir = Path(os.getenv("LOGS_DIR", str(BASE_DIR / "logs"))).resolve()
    return StreamingResponse(
        _log_stream_generator(logs_dir),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 정적 파일 + SPA ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── 진입점 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import uvicorn

    port = int(os.getenv("WEB_DASHBOARD_PORT", "8765"))
    external = os.getenv("WEB_DASHBOARD_EXTERNAL", "0").strip() == "1"
    host = "0.0.0.0" if external else "127.0.0.1"
    print(f"ddkkbot 웹 대시보드 시작: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
