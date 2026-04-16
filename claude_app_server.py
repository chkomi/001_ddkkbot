#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code CLI adapter — 구독 기반 (Anthropic API 키 불필요).

`claude` CLI를 subprocess로 실행하여 codex app-server와 동일한
JSON Lines stdio 프로토콜을 에뮬레이션합니다.

사전 요건:
  - Claude Code CLI 설치: npm install -g @anthropic-ai/claude-code
  - claude.ai Pro/Max 구독 후 로그인 완료

환경변수:
  SONOLBOT_CLAUDE_MODEL     - 모델 지정 (기본: 자동)
  SONOLBOT_CLAUDE_CLI       - claude 바이너리 경로 (기본: PATH 검색 → ~/.npm-global/bin/claude)
  SONOLBOT_CLAUDE_ALLOW_BASH - bash 도구 허용 여부 (기본: 1)

Protocol (codex app-server와 동일):
  stdin  ← {"id": N, "method": "turn/start",  "params": {...}}
  stdin  ← {"id": N, "method": "turn/steer",  "params": {...}}
  stdout → {"id": N, "result": {...}}
  stdout → {"method": "turn/completed",           "params": {...}}
  stdout → {"method": "codex/event/agent_message", "params": {...}}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

BASE_DIR = Path(__file__).resolve().parent
IS_REWRITER = os.getenv("SONOLBOT_AGENT_REWRITER", "0").strip() == "1"

# ── Claude CLI 경로 탐색 ───────────────────────────────────────────────────────

def _find_claude_cli() -> str:
    """claude 바이너리 경로를 반환. 없으면 예외 발생."""
    # 1) 환경변수 직접 지정
    explicit = os.getenv("SONOLBOT_CLAUDE_CLI", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    # 2) PATH 검색
    found = shutil.which("claude")
    if found:
        return found

    # 3) npm global bin 기본 경로
    candidates = [
        Path.home() / ".npm-global" / "bin" / "claude",
        Path.home() / ".npm" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    raise FileNotFoundError(
        "claude CLI를 찾을 수 없습니다.\n"
        "설치: npm install -g @anthropic-ai/claude-code\n"
        "설치 후 claude login 을 실행해 구독 계정으로 로그인하세요."
    )


CLAUDE_CLI = None  # 첫 사용 시 초기화


def get_claude_cli() -> str:
    global CLAUDE_CLI
    if CLAUDE_CLI is None:
        CLAUDE_CLI = _find_claude_cli()
    return CLAUDE_CLI


# ── 서버 클래스 ───────────────────────────────────────────────────────────────

class ClaudeAppServer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # thread_id → claude session_id (대화 컨텍스트 유지)
        self._sessions: dict[str, str] = {}

    # ── 출력 헬퍼 ──────────────────────────────────────────────────────────────

    def _emit(self, obj: dict[str, Any]) -> None:
        with self._lock:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _emit_notification(self, method: str, params: dict[str, Any]) -> None:
        self._emit({"method": method, "params": params})

    def _emit_response(self, req_id: int, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        obj: dict[str, Any] = {"id": req_id}
        if error:
            obj["error"] = error
        else:
            obj["result"] = result or {}
        self._emit(obj)

    # ── 명령어 빌드 ────────────────────────────────────────────────────────────

    def _build_cmd(self, user_text: str, session_id: str | None = None) -> list[str]:
        cli = get_claude_cli()
        cmd = [cli]

        # 모델 지정 (선택)
        model = os.getenv("SONOLBOT_CLAUDE_MODEL", "").strip()
        if model:
            cmd.extend(["--model", model])

        # 구조화된 스트리밍 출력 (--print와 함께 쓸 때 --verbose 필요)
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # 권한 프롬프트 자동 승인 (봇 환경)
        cmd.append("--dangerously-skip-permissions")

        # 이전 세션 재개 (대화 컨텍스트 유지)
        if session_id:
            cmd.extend(["--resume", session_id])

        # 비대화형 실행
        cmd.extend(["--print", user_text])

        return cmd

    def _build_rewriter_cmd(self, user_text: str) -> list[str]:
        """Agent Rewriter용 단순 완성 명령어."""
        cli = get_claude_cli()

        rewriter_prompt = (
            "당신은 텔레그램/디스코드 사용자에게 보여줄 중간 진행 안내문 재작성 전용 어시스턴트다.\n"
            "목표: 원문의 의미를 유지하면서 사용자 친화적인 한국어 안내문으로 바꿔라.\n"
            "규칙:\n"
            "1) 1~3문장으로 작성, 사용자가 무엇을 진행 중인지 구체적으로.\n"
            "2) 내부 기술/구조/운영 용어 절대 노출 금지 (thread, msg_번호, INSTRUNCTION.md 등).\n"
            "3) 결과는 설명문만, 머리말/꼬리말/코드블록 없음.\n"
            "4) 강조 필요 시 <b>와 <code>만 최소 사용.\n\n"
            f"재작성할 원문:\n{user_text}"
        )
        model = os.getenv("DAEMON_AGENT_REWRITER_MODEL", "").strip()
        cmd = [cli]
        if model and model not in ("gpt-5.3-codex", "gpt-4o"):  # codex 모델명은 무시
            cmd.extend(["--model", model])
        cmd.extend([
            "--output-format", "text",
            "--dangerously-skip-permissions",
            "--print", rewriter_prompt,
        ])
        return cmd

    # ── Turn 처리 ──────────────────────────────────────────────────────────────

    def _handle_turn_start(self, req_id: int, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", uuid.uuid4().hex))
        turn_id = uuid.uuid4().hex

        # 입력 텍스트 추출
        user_text_parts: list[str] = []
        for item in params.get("input", []):
            if isinstance(item, dict) and item.get("type") == "text":
                t = str(item.get("text", "")).strip()
                if t:
                    user_text_parts.append(t)
        user_text = "\n".join(user_text_parts).strip() or "(내용 없음)"

        # 즉시 응답
        self._emit_response(req_id, {"turn": {"id": turn_id, "threadId": thread_id, "status": "started"}})

        # 백그라운드에서 처리
        threading.Thread(
            target=self._process_turn,
            args=(thread_id, turn_id, user_text),
            daemon=True,
        ).start()

    def _handle_turn_steer(self, req_id: int, params: dict[str, Any]) -> None:
        # steer는 현재 실행 중인 turn에 추가 지시를 보내는 기능.
        # claude --print는 단발 실행이므로 다음 turn에 반영됨.
        self._emit_response(req_id, {"turnId": params.get("turnId", "")})

    def _process_turn(self, thread_id: str, turn_id: str, user_text: str) -> None:
        """claude CLI를 실행하고 결과를 스트리밍합니다."""
        session_id = self._sessions.get(thread_id)

        if IS_REWRITER:
            cmd = self._build_rewriter_cmd(user_text)
        else:
            cmd = self._build_cmd(user_text, session_id)

        final_text = ""
        new_session_id: str | None = None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
            )

            assert proc.stdout is not None

            if IS_REWRITER:
                # text 포맷: stdout 전체가 최종 결과
                final_text = proc.stdout.read().strip()
            else:
                # stream-json 포맷 파싱
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # JSON이 아닌 출력은 그대로 전달
                        if line.strip():
                            self._emit_notification("codex/event/agent_message", {
                                "threadId": thread_id,
                                "message": line.strip(),
                            })
                        continue

                    msg_type = obj.get("type", "")

                    if msg_type == "assistant":
                        # 어시스턴트 응답 블록
                        content = (obj.get("message") or {}).get("content") or []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    self._emit_notification("codex/event/agent_message", {
                                        "threadId": thread_id,
                                        "message": text,
                                    })

                    elif msg_type == "result":
                        # 최종 결과
                        final_text = str(obj.get("result", "")).strip()
                        if not final_text:
                            final_text = str(obj.get("text", "")).strip()
                        sid = obj.get("session_id")
                        if sid:
                            new_session_id = sid

                    elif msg_type == "system" and obj.get("session_id"):
                        new_session_id = obj["session_id"]

            proc.wait()

            if proc.returncode != 0 and not final_text:
                stderr_out = (proc.stderr.read() if proc.stderr else "").strip()
                if "not logged in" in stderr_out.lower() or "login" in stderr_out.lower():
                    final_text = (
                        "[오류] Claude Code에 로그인되어 있지 않습니다.\n"
                        "터미널에서 `claude login` 을 실행해 구독 계정으로 로그인하세요."
                    )
                else:
                    final_text = f"[오류] claude 실행 실패 (종료코드: {proc.returncode})"
                    if stderr_out:
                        final_text += f"\n{stderr_out[:300]}"

            # 세션 ID 저장 → 다음 turn에서 대화 컨텍스트 유지
            if new_session_id:
                self._sessions[thread_id] = new_session_id

        except FileNotFoundError:
            final_text = (
                "[오류] claude 명령어를 찾을 수 없습니다.\n"
                "설치: npm install -g @anthropic-ai/claude-code\n"
                "설치 후: claude login"
            )
        except Exception as e:
            final_text = f"[오류] 처리 중 문제 발생: {type(e).__name__}: {e}"

        # codex 프로토콜 호환: final_text를 task_complete 이벤트로 먼저 전달
        if final_text:
            self._emit_notification("codex/event/task_complete", {
                "conversationId": thread_id,
                "msg": {
                    "last_agent_message": final_text,
                },
            })

        # turn 완료 알림
        self._emit_notification("turn/completed", {
            "threadId": thread_id,
            "turn": {
                "id": turn_id,
                "status": "completed",
                "finalMessage": final_text,
            },
        })

    # ── 메인 루프 ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            req_id = obj.get("id")
            method = obj.get("method", "")
            params = obj.get("params") or {}

            if method in ("initialize", "initialized"):
                if req_id is not None:
                    self._emit_response(int(req_id), {"capabilities": {}})
            elif method in ("thread/start", "thread/resume"):
                # thread_id 발급 또는 유지
                thread_id = str(params.get("threadId") or "").strip()
                if not thread_id:
                    thread_id = uuid.uuid4().hex
                    with self._lock:
                        # threadId → sessions 매핑 보존
                        pass
                if req_id is not None:
                    self._emit_response(int(req_id), {"thread": {"id": thread_id}})
            elif method == "turn/start":
                if req_id is None:
                    continue
                self._handle_turn_start(int(req_id), params)
            elif method == "turn/steer":
                if req_id is not None:
                    self._handle_turn_steer(int(req_id), params)
            elif method == "ping":
                self._emit_response(int(req_id) if req_id is not None else 0, {"pong": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code CLI app-server adapter")
    parser.add_argument("--listen", default="stdio://", help="listen mode (stdio:// only)")
    parser.parse_args()

    # 시작 전 claude CLI 존재 확인
    try:
        cli = get_claude_cli()
        # stderr에만 로그 출력 (stdout은 프로토콜 전용)
        print(f"[claude_app_server] using claude at: {cli}", file=sys.stderr, flush=True)
    except FileNotFoundError as e:
        print(f"[claude_app_server] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    ClaudeAppServer().run()


if __name__ == "__main__":
    main()
