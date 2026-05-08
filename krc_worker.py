#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KRC 발주공고 작업 큐 워커.

daemon_service.py 에 의해 subprocess 로 관리되는 독립 프로세스.
KRC_API_BASE 서버의 /api/notices/tasks 를 주기적으로 폴링해서
translate / infographic / slides 작업을 처리한다.

환경변수:
  KRC_API_BASE             - KRC 서버 베이스 URL (예: https://krcglobal.vercel.app/api)
  WORKER_SECRET            - 워커 인증 토큰
  KRC_WORKER_INTERVAL_SEC  - 폴링 주기 초 (기본: 1800 = 30분)
  SONOLBOT_CLAUDE_CLI      - claude CLI 경로 (없으면 자동 탐색)
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# ── R2 업로드 ─────────────────────────────────────────────────────────────────
def _upload_r2(file_path: str, object_key: str) -> str:
    """Cloudflare R2 에 파일 업로드 후 공개 URL 반환.

    Vercel 은 read-only 파일시스템이라 서버에서 직접 저장 불가.
    krc_worker 가 생성한 이미지를 R2 에 직접 올리고 URL 만 /complete 에 전달.
    """
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise RuntimeError("boto3 미설치: pip install boto3")

    account_id = os.environ.get("R2_ACCOUNT_ID", "")
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    bucket     = os.environ.get("R2_BUCKET_NAME", "krcglobal")
    if not account_id or not access_key or not secret_key:
        raise RuntimeError("R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY 환경변수가 필요합니다.")
    endpoint   = f"https://{account_id}.r2.cloudflarestorage.com"

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )
    s3.upload_file(
        file_path, bucket, object_key,
        ExtraArgs={"ContentType": "image/png"},
    )
    return object_key  # R2 object key 반환 — krcglobal API가 stream_from_r2로 서빙


# ── 설정 ──────────────────────────────────────────────────────────────────────
KRC_BASE       = os.environ.get("KRC_API_BASE", "").rstrip("/")
WORKER_SECRET  = os.environ.get("WORKER_SECRET", "")
IDLE_SLEEP_SEC = int(os.environ.get("KRC_WORKER_IDLE_SEC", "60"))  # pending 없을 때 대기 초
WORKER_ID      = "ddkkbot-krc"

_CLAUDE_CANDIDATES = [
    os.environ.get("SONOLBOT_CLAUDE_CLI", ""),
    shutil.which("claude") or "",
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/usr/local/bin/claude",
]
CLAUDE_CLI = next((p for p in _CLAUDE_CANDIDATES if p and Path(p).exists()), "")

_CODEX_CANDIDATES = [
    shutil.which("codex") or "",
    os.path.expanduser("~/.npm-global/bin/codex"),
]
CODEX_CLI = next((p for p in _CODEX_CANDIDATES if p and Path(p).exists()), "")

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = Path(os.environ.get("LOGS_DIR", str(BASE_DIR / "logs"))).resolve()
LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOGS_DIR / f"krc-worker-{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KRC] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("krc_worker")


# ── HTTP 헬퍼 ─────────────────────────────────────────────────────────────────
try:
    import requests as _req
except ImportError:
    logger.error("requests 미설치. pip install requests")
    sys.exit(1)

_HEADERS = lambda: {"Authorization": f"Bearer {WORKER_SECRET}"}


def _get(path: str, **params) -> dict:
    return _req.get(f"{KRC_BASE}{path}", headers=_HEADERS(),
                    params=params, timeout=15).json()


def _post(path: str, **kwargs) -> dict:
    return _req.post(f"{KRC_BASE}{path}", headers=_HEADERS(),
                     timeout=60, **kwargs).json()


# ── Claude CLI 실행 ───────────────────────────────────────────────────────────
def run_claude(prompt: str, timeout: int = 180) -> str:
    if not CLAUDE_CLI:
        raise RuntimeError("claude CLI 를 찾을 수 없습니다.")
    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude 실행 실패 (rc={result.returncode}): {result.stderr[:300]}")
    return result.stdout.strip()


def run_codex(prompt: str, timeout: int = 600) -> str:
    """Codex CLI 비대화식 실행 — 인포그래픽 생성용.

    --full-auto: 사용자 확인 프롬프트 없이 자동 실행.
    capture_output=True 환경에서 확인 프롬프트가 뜨면 응답 불가 → 타임아웃.
    """
    if not CODEX_CLI:
        raise RuntimeError("codex CLI 를 찾을 수 없습니다.")
    result = subprocess.run(
        [CODEX_CLI, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", prompt],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        raise RuntimeError(f"codex 실행 실패 (rc={result.returncode}): {stderr or '(stderr 없음)'}")
    return result.stdout.strip()


def extract_json(text: str) -> dict:
    """응답에서 첫 JSON 객체 추출. 코드블록 안도 처리."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"JSON 없음: {text[:300]}")


# ── 작업 처리기 ───────────────────────────────────────────────────────────────
def handle_translate(tid: int, notice: dict) -> None:
    """title_ko / text_excerpt_ko / summary_ko 번역 후 /complete JSON."""
    details = notice.get("details") or {}
    excerpt = (details.get("text_excerpt") or "")[:2000]

    prompt = (
        "아래 발주공고를 한국어로 번역하고 분석해줘.\n"
        "반드시 JSON 형식만 출력. 다른 설명 없이.\n\n"
        f"영문 제목: {notice.get('title', '')}\n"
        f"발주처: {notice.get('client', '')}\n"
        f"국가: {notice.get('country', '')}\n"
        f"마감일: {notice.get('deadline', '')}\n"
        f"계약 규모: {notice.get('contractValue', '')}\n"
        f"영문 본문 발췌:\n{excerpt or '(없음)'}\n\n"
        "출력 형식:\n"
        "{\n"
        '  "title_ko": "정확한 한국어 번역 제목",\n'
        '  "text_excerpt_ko": "본문 발췌 한국어 번역 (원문 없으면 빈 문자열)",\n'
        '  "summary_ko": "KRC 해외사업 담당자 관점 5줄 요약. 마감일·금액·KRC 적합성 포함."\n'
        "}"
    )

    output = run_claude(prompt)
    data = extract_json(output)

    resp = _post(f"/notices/tasks/{tid}/complete",
                 json={"fields_to_update": {
                     "title_ko":        data.get("title_ko", ""),
                     "text_excerpt_ko": data.get("text_excerpt_ko", ""),
                     "summary_ko":      data.get("summary_ko", ""),
                 }})
    if not resp.get("success"):
        raise RuntimeError(f"complete 실패: {resp}")
    logger.info(f"translate #{tid} 완료 — notice #{notice['id']}")


def handle_infographic(tid: int, notice: dict) -> None:
    """Codex --full-auto 로 $Imagegen 인포그래픽 생성 후 /complete multipart."""
    nid        = notice["id"]
    title_ko   = notice.get("titleKo") or notice.get("title", "")
    output_path = f"/tmp/krc_infographic_{nid}_{tid}.png"

    prompt = (
        f"$Imagegen 아래 발주공고 정보를 담은 인포그래픽 이미지를 만들어줘.\n"
        f"생성된 이미지를 {output_path} 경로에 저장해.\n\n"
        f"공고 제목: {title_ko}\n"
        f"발주처: {notice.get('client', '')}\n"
        f"국가: {notice.get('country', '')}\n"
        f"마감일: {notice.get('deadline', '')}\n"
        f"계약 규모: {notice.get('contractValue', '')}\n"
        f"핵심 요약: {notice.get('summaryKo', '')}\n\n"
        "스타일: KRC(한국농어촌공사) 해외사업 보고 자료 스타일. "
        "깔끔한 비즈니스 인포그래픽. 파란색 계열. 한국어 텍스트. "
        "이미지 크기: 가로 1280px × 세로 720px (16:9 가로 방향 landscape). "
        "세로(portrait) 방향은 절대 사용하지 않을 것."
    )

    run_codex(prompt, timeout=600)

    if not Path(output_path).exists():
        raise RuntimeError(f"이미지 파일 미생성: {output_path}")

    # R2에 업로드 → krcglobal API 경로(/api/notices/<nid>/infographic)를 URL로 저장
    # Vercel read-only 파일시스템 우회 — r2_key는 서버가 stream_from_r2로 서빙
    object_key = f"infographics/notice_{nid}_{tid}.png"
    r2_key = _upload_r2(output_path, object_key)
    api_url = f"/api/notices/{nid}/infographic"

    resp = _post(f"/notices/tasks/{tid}/complete",
                 json={"result": {
                     "infographic_url": api_url,
                     "r2_key": r2_key,
                     "mode": "r2", "source": "codex-imagegen",
                 }})
    if not resp.get("success"):
        raise RuntimeError(f"complete 실패: {resp}")
    logger.info(f"infographic #{tid} 완료 — notice #{nid} → {api_url} (r2:{r2_key})")


HANDLERS: dict = {
    "translate":   handle_translate,
    "infographic": handle_infographic,
    # slides: 제외 (NotebookLM 파일 export 불가)
}

# task_type 별 동시 처리 수
TASK_CONCURRENCY: dict = {
    "translate":   3,
    "infographic": 1,
    "summary":     2,
    "review":      2,
}

# task_type 실행 순서
TASK_ORDER = ["translate", "infographic", "summary", "review"]


# ── 단일 task 처리 (스레드에서 호출) ─────────────────────────────────────────
def _run_one(task: dict) -> bool:
    tid       = task["id"]
    nid       = task["noticeId"]
    task_type = task["taskType"]
    handler   = HANDLERS.get(task_type)
    if not handler:
        return False

    # claim
    try:
        c = _post(f"/notices/tasks/{tid}/claim", json={"worker_id": WORKER_ID})
        if not c.get("success"):
            logger.info(f"claim 실패 #{tid}: {c}")
            return False
    except Exception as e:
        logger.warning(f"claim 예외 #{tid}: {e}")
        return False

    # 공고 조회
    try:
        notice = _get(f"/notices/{nid}").get("data", {})
        if not notice or not notice.get("id"):
            raise ValueError(f"공고 데이터 없음 (nid={nid})")
    except Exception as e:
        _post(f"/notices/tasks/{tid}/fail", json={"error": f"공고 조회 실패: {e}"})
        return False

    # 처리
    logger.info(f"처리 시작: {task_type} #{tid} notice #{nid}")
    try:
        handler(tid, notice)
        return True
    except Exception as e:
        logger.error(f"{task_type} #{tid} 실패: {e}")
        try:
            _post(f"/notices/tasks/{tid}/fail", json={"error": str(e)[:500]})
        except Exception:
            pass
        return False


# ── 메인 폴링 ────────────────────────────────────────────────────────────────
def process_pending() -> int:
    if not KRC_BASE or not WORKER_SECRET:
        logger.warning("KRC_API_BASE 또는 WORKER_SECRET 미설정. 폴링 건너뜀.")
        return 0

    try:
        data = _get("/notices/tasks", status="pending", limit=30)
    except Exception as e:
        logger.warning(f"task 목록 조회 실패: {e}")
        return 0

    tasks = data.get("data", [])
    if not tasks:
        return 0

    # task_type 별로 분류 후 TASK_ORDER 순서로 병렬 처리
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for t in tasks:
        groups[t["taskType"]].append(t)

    processed = 0
    for task_type in TASK_ORDER:
        group = groups.get(task_type, [])
        if not group:
            continue
        max_w = TASK_CONCURRENCY.get(task_type, 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
            futures = [pool.submit(_run_one, t) for t in group]
            for f in concurrent.futures.as_completed(futures):
                if f.result():
                    processed += 1

    return processed


def main() -> None:
    # 단일 인스턴스 보장 — MultiBotManager 가 여러 봇 워커를 띄울 때
    # 각각이 krc_worker 를 시작하므로 lock 파일로 첫 번째만 실행.
    lock_path = Path(__file__).resolve().parent / ".krc_worker.lock"
    try:
        lock_fh = open(lock_path, "w")
        fcntl.lockf(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("다른 krc_worker 인스턴스가 실행 중. 종료합니다.")
        sys.exit(0)

    logger.info(
        f"KRC 워커 시작 — base={KRC_BASE} "
        f"idle={IDLE_SLEEP_SEC}s "
        f"claude={CLAUDE_CLI or '(없음)'} "
        f"codex={CODEX_CLI or '(없음)'}"
    )
    while True:
        try:
            count = process_pending()
            if count:
                logger.info(f"사이클 완료: {count}건 처리")
                continue  # 처리 건수 있으면 즉시 다음 사이클
        except Exception as e:
            logger.error(f"폴링 루프 예외: {e}")
        time.sleep(IDLE_SLEEP_SEC)  # pending 없으면 60초 대기


if __name__ == "__main__":
    main()
