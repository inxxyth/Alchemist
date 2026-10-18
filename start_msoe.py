"""MSOE master startup — boot Docker stack + FastAPI agentic backend.

Usage (PowerShell):
    python start_msoe.py
    python start_msoe.py --skip-docker     # API only
    python start_msoe.py --smoke           # boot, wait for healthy, keep running

Environment contract (names only — never hardcode secrets):
    OPENROUTER_API_KEY     enables Nvidia Nemotron via OpenRouter
    OPENROUTER_MODEL       default: nvidia/nemotron-3.5-lightning
    OPENROUTER_FALLBACK_MODEL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_HOST = "127.0.0.1"
API_PORT = 8000
BASE = f"http://{API_HOST}:{API_PORT}"
LOG_OUT = ROOT / "uvicorn.log"
LOG_ERR = ROOT / "uvicorn.err"


def log(msg: str) -> None:
    print(f"[start_msoe] {msg}", flush=True)


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        timeout=timeout,
    )


def docker_available() -> bool:
    try:
        proc = run(["docker", "info"], timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def ensure_docker_stack() -> bool:
    if not docker_available():
        log("Docker Desktop is not responding. Start Docker Desktop, then re-run.")
        return False
    proc = run(["docker", "compose", "up", "-d"], timeout=180)
    if proc.returncode != 0:
        log("docker compose up failed:")
        print(proc.stdout)
        print(proc.stderr)
        return False
    for _ in range(30):
        ps = run(["docker", "compose", "ps"])
        if "msoe_postgres" in ps.stdout and "msoe_neo4j" in ps.stdout:
            time.sleep(2)
            break
        time.sleep(2)
    log("Docker stack is up (msoe_postgres, msoe_neo4j).")
    return True


def llm_status_summary() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3.5-lightning")
    if key:
        log(f"LLM: OpenRouter configured (model={model}, key env=OPENROUTER_API_KEY)")
    else:
        log("LLM: OPENROUTER_API_KEY not set — deterministic fallback engine in use.")


def start_api() -> subprocess.Popen:
    if LOG_OUT.exists():
        LOG_OUT.unlink()
    if LOG_ERR.exists():
        LOG_ERR.unlink()
    out = open(LOG_OUT, "w", encoding="utf-8")
    err = open(LOG_ERR, "w", encoding="utf-8")
    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", API_HOST, "--port", str(API_PORT),
    ]
    log("$ " + " ".join(cmd))
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=err, text=True)


def wait_for_health(timeout_s: int = 40) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
                last = json.loads(resp.read().decode())
                if last.get("postgres") == "connected" and last.get("neo4j") == "connected":
                    return last
        except Exception as exc:
            last = {"error": str(exc)}
        time.sleep(1.5)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="MSOE master startup")
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--smoke", action="store_true",
                        help="Start, wait for healthy, print status, keep running")
    args = parser.parse_args()

    log(f"Project root: {ROOT}")
    llm_status_summary()

    if not args.skip_docker:
        if not ensure_docker_stack():
            return 1

    proc = start_api()
    log(f"uvicorn pid={proc.pid}  logs: {LOG_OUT.name} / {LOG_ERR.name}")
    health = wait_for_health()
    log("health: " + json.dumps(health, indent=2))

    if health.get("status") == "healthy":
        log("MSOE is LIVE.")
        log(f"  Landing : {BASE}/")
        log(f"  Swagger : {BASE}/docs")
        log(f"  Health  : {BASE}/health")
        log(f"  SIS     : {BASE}/api/v1/sis/status")
        log("  Services:")
        log("    POST /api/v1/audit/compliance")
        log("    POST /api/v1/curriculum/harmonize")
        log("    POST /api/v1/operations/substitute-dispatch")
        log("    POST /api/v1/executive/query")
        if args.smoke:
            log("Smoke mode — leaving uvicorn running (Ctrl+C to stop).")
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
        return 0

    log("MSOE is DEGRADED — see health payload and uvicorn.err")
    if LOG_ERR.exists():
        print(LOG_ERR.read_text(encoding="utf-8")[-2000:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
