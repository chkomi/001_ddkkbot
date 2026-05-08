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
import base64
import json
import os
import re
import shutil
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

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("fastapi가 설치되지 않았습니다. pip install fastapi uvicorn 을 실행하세요.")
    sys.exit(1)

from scripts.bot_config_store import (
    default_config_path as _bots_default_config_path,
    derive_bot_id as _derive_bot_id,
    load_config as _load_bots_config,
    normalize_platform as _normalize_platform,
    remove_bot as _remove_bot_in_cfg,
    save_config as _save_bots_config,
    set_allowed_users_global as _set_allowed_users_global,
    set_bot_active as _set_bot_active_in_cfg,
    update_bot_meta as _update_bot_meta_in_cfg,
    upsert_bot as _upsert_bot_in_cfg,
    validate_token_for_platform as _validate_token_for_platform,
)
from scripts.telegram_validation import fetch_bot_profile as _telegram_fetch_profile

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


def _read_active_bot_platforms() -> list[dict[str, Any]]:
    """`.control_panel_telegram_bots.json` 에서 active=true 봇 목록을 단순 형태로 반환."""
    cfg_name = _read_env_value("SONOLBOT_BOTS_CONFIG", ".control_panel_telegram_bots.json")
    cfg_path = (BASE_DIR / cfg_name).resolve() if not Path(cfg_name).is_absolute() else Path(cfg_name)
    if not cfg_path.exists():
        return []
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    bots = data.get("bots") if isinstance(data, dict) else None
    if not isinstance(bots, list):
        return []
    out: list[dict[str, Any]] = []
    for row in bots:
        if not isinstance(row, dict):
            continue
        if not bool(row.get("active")):
            continue
        out.append(
            {
                "platform": str(row.get("platform") or "telegram").strip().lower(),
                "bot_id": str(row.get("bot_id") or "").strip(),
                "alias": str(row.get("alias") or "").strip(),
            }
        )
    return out


def _get_daemon_status() -> dict[str, Any]:
    pid_file = BASE_DIR / ".daemon_service.pid"
    codex_pid_file = BASE_DIR / ".codex_app_server.pid"

    daemon_pid = _read_pid_file(pid_file)
    codex_pid = _read_pid_file(codex_pid_file)

    ai_provider = _read_env_value("SONOLBOT_AI_PROVIDER", "codex")
    multi_bot_raw = _read_env_value("SONOLBOT_MULTI_BOT_MANAGER", "1").strip().lower()
    multi_bot = multi_bot_raw in {"1", "true", "yes", "on", ""}

    active_bots = _read_active_bot_platforms() if multi_bot else []
    platform_set = {b["platform"] for b in active_bots if b.get("platform")}

    if not multi_bot:
        if os.getenv("TELEGRAM_BOT_TOKEN"):
            platform_set.add("telegram")
        if os.getenv("DISCORD_BOT_TOKEN"):
            platform_set.add("discord")
        if os.getenv("SLACK_BOT_TOKEN"):
            platform_set.add("slack")

    return {
        "running": daemon_pid is not None,
        "daemon_pid": daemon_pid,
        "ai_pid": codex_pid,
        "ai_provider": ai_provider,
        "platforms": sorted(platform_set),
        "multi_bot": multi_bot,
        "active_bots": active_bots,
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


# ── Provider 인증 상태 ─────────────────────────────────────────────────────────

def _resolve_cli(name: str) -> str | None:
    """PATH 와 일반적인 npm-global 경로에서 CLI 실행 파일을 찾는다."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".npm-global" / "bin" / name,
        home / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _run_cli(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def _codex_auth_status() -> dict[str, Any]:
    bin_path = _resolve_cli("codex")
    if not bin_path:
        return {"installed": False, "logged_in": False, "message": "codex CLI 미설치"}
    rc, out, err = _run_cli([bin_path, "login", "status"])
    text = (out + err).strip()
    if rc != 0:
        return {
            "installed": True,
            "logged_in": False,
            "message": text or "codex login status 실패",
        }
    lower = text.lower()
    logged_in = "logged in" in lower and "not" not in lower.split("logged in")[0]
    method = ""
    m = re.search(r"using\s+(\S+)", text, re.IGNORECASE)
    if m:
        method = m.group(1).strip().rstrip(".")
    result: dict[str, Any] = {
        "installed": True,
        "logged_in": logged_in,
        "method": method,
        "message": text,
    }
    if not logged_in:
        return result
    # JWT 디코딩으로 상세 정보 추출
    auth_file = Path.home() / ".codex" / "auth.json"
    if not auth_file.exists():
        return result
    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
        id_token = (auth_data.get("tokens") or {}).get("id_token", "")
        if not id_token:
            return result
        parts = id_token.split(".")
        if len(parts) < 2:
            return result
        padding = 4 - len(parts[1]) % 4
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * padding))
        openai_auth = payload.get("https://api.openai.com/auth") or {}
        result["email"] = payload.get("email", "")
        result["name"] = payload.get("name", "")
        result["auth_provider"] = payload.get("auth_provider", "")
        result["subscription_type"] = openai_auth.get("chatgpt_plan_type", "")
        until = openai_auth.get("chatgpt_subscription_active_until", "")
        result["subscription_until"] = until[:10] if until else ""  # YYYY-MM-DD
        orgs = openai_auth.get("organizations") or []
        default_org = next((o for o in orgs if o.get("is_default")), None)
        result["org_name"] = (default_org or {}).get("title", "")
    except Exception:
        pass
    return result


def _claude_auth_status() -> dict[str, Any]:
    bin_path = _resolve_cli("claude")
    if not bin_path:
        return {"installed": False, "logged_in": False, "message": "claude CLI 미설치"}
    rc, out, err = _run_cli([bin_path, "auth", "status", "--json"])
    text_err = err.strip()
    payload = (out or "").strip()
    if not payload:
        return {
            "installed": True,
            "logged_in": False,
            "message": text_err or "claude auth status 실패",
        }
    try:
        data = json.loads(payload)
    except Exception:
        return {
            "installed": True,
            "logged_in": False,
            "message": payload[:200],
        }
    return {
        "installed": True,
        "logged_in": bool(data.get("loggedIn")),
        "method": data.get("authMethod") or "",
        "email": data.get("email") or "",
        "subscription_type": data.get("subscriptionType") or "",
        "org_name": data.get("orgName") or "",
        "api_provider": data.get("apiProvider") or "",
    }


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


_AUTH_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_AUTH_TTL_SEC = 15.0


@app.get("/api/auth-status")
async def api_auth_status() -> JSONResponse:
    now = time.time()
    if _AUTH_CACHE["data"] and (now - _AUTH_CACHE["ts"]) < _AUTH_TTL_SEC:
        return JSONResponse(_AUTH_CACHE["data"])
    data = await asyncio.to_thread(
        lambda: {
            "codex": _codex_auth_status(),
            "claude": _claude_auth_status(),
        }
    )
    _AUTH_CACHE["data"] = data
    _AUTH_CACHE["ts"] = now
    return JSONResponse(data)


@app.get("/api/tasks")
async def api_tasks(limit: int = 20) -> JSONResponse:
    return JSONResponse({"tasks": _get_tasks(limit)})


# ── 봇 관리 ────────────────────────────────────────────────────────────────────

def _bots_config_path() -> Path:
    return _bots_default_config_path(BASE_DIR)


def _public_bot(row: dict[str, Any]) -> dict[str, Any]:
    """API 응답용 — 토큰 원본은 제거하고 마스킹된 값만 노출."""
    out = {
        "platform": row.get("platform") or "telegram",
        "bot_id": row.get("bot_id") or "",
        "token_masked": row.get("token_masked") or "",
        "bot_username": row.get("bot_username") or "",
        "bot_name": row.get("bot_name") or "",
        "alias": row.get("alias") or "",
        "memo": row.get("memo") or "",
        "active": bool(row.get("active")),
        "discord_allowed_users": row.get("discord_allowed_users") or "",
        "slack_allowed_users": row.get("slack_allowed_users") or "",
        "slack_allowed_channels": row.get("slack_allowed_channels") or "",
        "slack_app_token_set": bool((row.get("slack_app_token") or "").strip()),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }
    return out


@app.get("/api/bots")
async def api_bots_list() -> JSONResponse:
    cfg = _load_bots_config(_bots_config_path())
    return JSONResponse(
        {
            "allowed_users_global": cfg.get("allowed_users_global") or [],
            "bots": [_public_bot(b) for b in (cfg.get("bots") or [])],
        }
    )


@app.post("/api/bots")
async def api_bots_create(request: Request) -> JSONResponse:
    body = await request.json()
    token = str(body.get("token") or "").strip()
    platform = _normalize_platform(body.get("platform"))
    alias = str(body.get("alias") or "").strip()
    memo = str(body.get("memo") or "").strip()
    active = bool(body.get("active", True))
    discord_allowed_users = str(body.get("discord_allowed_users") or "").strip()
    slack_app_token = str(body.get("slack_app_token") or "").strip()
    slack_allowed_users = str(body.get("slack_allowed_users") or "").strip()
    slack_allowed_channels = str(body.get("slack_allowed_channels") or "").strip()

    if not token:
        raise HTTPException(status_code=400, detail="토큰이 비어 있습니다.")
    ok, err = _validate_token_for_platform(token, platform)
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    if platform == "slack":
        if not slack_app_token:
            raise HTTPException(status_code=400, detail="Slack App-Level Token(xapp-...)도 필수입니다.")
        from scripts.bot_config_store import validate_slack_app_token as _val_app
        ok2, err2 = _val_app(slack_app_token)
        if not ok2:
            raise HTTPException(status_code=400, detail=err2)

    bot_id = _derive_bot_id(token, platform)
    if not bot_id:
        raise HTTPException(status_code=400, detail="bot_id 도출에 실패했습니다.")

    bot_username = ""
    bot_name = ""
    if platform == "telegram":
        try:
            ok_live, profile, _ = await asyncio.to_thread(_telegram_fetch_profile, token, 6.0)
            if ok_live and isinstance(profile, dict):
                bot_username = str(profile.get("username") or "")
                bot_name = str(profile.get("first_name") or "")
        except Exception:
            pass

    cfg_path = _bots_config_path()
    cfg = _load_bots_config(cfg_path)
    try:
        cfg = _upsert_bot_in_cfg(
            cfg,
            token=token,
            bot_id=bot_id,
            platform=platform,
            bot_username=bot_username,
            bot_name=bot_name,
            alias=alias,
            memo=memo,
            active=active,
            discord_allowed_users=discord_allowed_users,
            slack_app_token=slack_app_token,
            slack_allowed_users=slack_allowed_users,
            slack_allowed_channels=slack_allowed_channels,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _save_bots_config(cfg_path, cfg)
    row = next((b for b in cfg["bots"] if b.get("bot_id") == bot_id), None)
    return JSONResponse({"ok": True, "bot": _public_bot(row) if row else None})


@app.patch("/api/bots/{bot_id}")
async def api_bots_update(bot_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    cfg_path = _bots_config_path()
    cfg = _load_bots_config(cfg_path)
    if not any(str(b.get("bot_id")) == bot_id for b in cfg.get("bots") or []):
        raise HTTPException(status_code=404, detail="bot_id 없음")
    cfg = _update_bot_meta_in_cfg(
        cfg,
        bot_id,
        alias=body.get("alias") if "alias" in body else None,
        memo=body.get("memo") if "memo" in body else None,
        active=bool(body["active"]) if "active" in body else None,
        discord_allowed_users=str(body["discord_allowed_users"]).strip()
        if "discord_allowed_users" in body
        else None,
    )
    _save_bots_config(cfg_path, cfg)
    row = next((b for b in cfg["bots"] if b.get("bot_id") == bot_id), None)
    return JSONResponse({"ok": True, "bot": _public_bot(row) if row else None})


@app.delete("/api/bots/{bot_id}")
async def api_bots_delete(bot_id: str) -> JSONResponse:
    cfg_path = _bots_config_path()
    cfg = _load_bots_config(cfg_path)
    if not any(str(b.get("bot_id")) == bot_id for b in cfg.get("bots") or []):
        raise HTTPException(status_code=404, detail="bot_id 없음")
    cfg = _remove_bot_in_cfg(cfg, bot_id)
    _save_bots_config(cfg_path, cfg)
    return JSONResponse({"ok": True})


@app.put("/api/bots/allowed-users")
async def api_bots_set_allowed_users(request: Request) -> JSONResponse:
    body = await request.json()
    raw = body.get("user_ids")
    user_ids: list[int] = []
    if isinstance(raw, list):
        for v in raw:
            try:
                n = int(v)
                if n > 0:
                    user_ids.append(n)
            except Exception:
                continue
    elif isinstance(raw, str):
        for chunk in re.split(r"[,\s]+", raw.strip()):
            if not chunk:
                continue
            try:
                n = int(chunk)
                if n > 0:
                    user_ids.append(n)
            except Exception:
                continue
    cfg_path = _bots_config_path()
    cfg = _load_bots_config(cfg_path)
    cfg = _set_allowed_users_global(cfg, user_ids)
    _save_bots_config(cfg_path, cfg)
    return JSONResponse({"ok": True, "allowed_users_global": cfg["allowed_users_global"]})


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
    raw = html_path.read_text(encoding="utf-8")
    # 정적 파일에 mtime 기반 버전 쿼리 자동 부착 → 브라우저 캐시 무효화
    def _bust(name: str) -> str:
        try:
            mt = int((STATIC_DIR / name).stat().st_mtime)
        except OSError:
            mt = int(time.time())
        return f"/static/{name}?v={mt}"
    raw = raw.replace("/static/app.js", _bust("app.js"))
    raw = raw.replace("/static/style.css", _bust("style.css"))
    return HTMLResponse(raw, headers={"Cache-Control": "no-cache"})


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
