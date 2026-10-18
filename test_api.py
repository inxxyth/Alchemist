"""MSOE API smoke-test suite (sequential, must finish < 10 minutes).

Hits:
  GET  /health
  POST /api/v1/audit/compliance
  POST /api/v1/curriculum/harmonize
  POST /api/v1/operations/substitute-dispatch
  POST /api/v1/executive/query   (grounded + refusal)
  GET  /api/v1/approvals/pending
  POST /api/v1/approvals/{id}/execute  (approve + reject)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
TIMEOUT = 60  # per-request cap — keeps whole suite well under 10 minutes

# Per-client API key (F.6). Format: "school_id:secret,school_id2:secret2".
# Fail loudly if unset — the auth middleware will reject every request without it.
API_KEYS_RAW = os.environ.get("MSOE_API_KEYS", "")
if not API_KEYS_RAW:
    print("FATAL: MSOE_API_KEYS environment variable is not set.")
    print("  Expected format: school_id:secret,school_id2:secret2")
    print("  Example: MSOE_API_KEYS=default:test-secret-abc123")
    sys.exit(1)

# Use the first key's secret for all smoke-test calls.
_first_entry = API_KEYS_RAW.split(",")[0].strip()
if ":" not in _first_entry:
    print(f"FATAL: MSOE_API_KEYS entry '{_first_entry}' missing ':' separator.")
    print("  Expected format: school_id:secret")
    sys.exit(1)
API_KEY = _first_entry.split(":", 1)[1].strip()
if not API_KEY:
    print("FATAL: MSOE_API_KEYS secret is empty after parsing.")
    sys.exit(1)

RESULTS: list[tuple[str, bool, float, str]] = []


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    # Connection: close avoids keep-alive sockets lingering after timeouts
    # (prevents WinError 10048 ephemeral-port exhaustion on Windows).
    headers = {"Connection": "close", "X-API-Key": API_KEY}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, time.time() - t0, body
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:500]}
        return e.code, time.time() - t0, body
    except Exception as e:
        return 0, time.time() - t0, {"error": str(e)}


def check(name: str, status: int, dt: float, body: dict,
          expect_status: int = 200, must_contain: list[str] | None = None) -> bool:
    ok = status == expect_status
    detail = f"HTTP {status}"
    if must_contain:
        blob = json.dumps(body, default=str)
        missing = [k for k in must_contain if k not in blob]
        if missing:
            ok = False
            detail += f" missing={missing}"
    RESULTS.append((name, ok, dt, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name:<55} {detail}  ({dt:.2f}s)")
    if not ok:
        print("       body:", json.dumps(body, default=str)[:600])
    return ok


def main() -> int:
    t_suite = time.time()
    print("=" * 72)
    print("MSOE API SMOKE TEST SUITE")
    print("=" * 72)

    # 1 — health
    s, dt, b = call("GET", "/health")
    check("GET /health", s, dt, b, must_contain=["postgres", "neo4j", "healthy"])

    # 2 — compliance audit (broad cohort so risk flags are included)
    s, dt, b = call("POST", "/api/v1/audit/compliance", {
        "max_students": 50,
    })
    ok = check("POST /api/v1/audit/compliance", s, dt, b,
               must_contain=["PENDING_HUMAN_APPROVAL", "policy_citations", "parent_notifications"])
    first_note_id = None
    second_note_id = None
    if ok and b.get("parent_notifications"):
        ids = [n.get("action_id") for n in b["parent_notifications"] if n.get("action_id")]
        first_note_id = ids[0] if ids else None
        second_note_id = ids[1] if len(ids) > 1 else None

    # 3 — curriculum harmonize
    s, dt, b = call("POST", "/api/v1/curriculum/harmonize", {
        "lesson_plan": (
            "Students explore projectile motion in Physics. They calculate "
            "range and height using kinematic equations and complete a "
            "worksheet on free body diagrams."
        ),
        "subject": "Physics",
        "grade_level": 8,
        "teacher_name": "Aisha Al Mansoori",
    })
    check("POST /api/v1/curriculum/harmonize", s, dt, b,
          must_contain=["alignment_score", "gaps", "citations", "node_ids", "enhanced_lesson_plan"])

    # 4 — substitute dispatch
    s, dt, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "James Wilson",
        "leave_date": "2026-10-22",
        "subject_needed": "Mathematics",
        "periods": 2,
        "reason": "Medical appointment",
    })
    ok = check("POST /api/v1/operations/substitute-dispatch", s, dt, b,
               must_contain=["PENDING_HUMAN_APPROVAL", "candidates", "notification_draft",
                             "leave_request"])
    leave_action_id = None
    if ok:
        leave_action_id = (b.get("leave_request") or {}).get("action_id")

    # 5 — executive query (grounded)
    s, dt, b = call("POST", "/api/v1/executive/query", {
        "query": "Show me all Grade 8 classrooms with chronic attendance and correlate with fee delays",
        "role": "principal",
    })
    check("POST /api/v1/executive/query (grounded)", s, dt, b,
          must_contain=["GROUNDED", "graph_evidence", "citations", "source_file"])

    # 5b — executive query (refusal)
    s, dt, b = call("POST", "/api/v1/executive/query", {
        "query": "Tell me about alien spacecraft crash sites near Dubai schools",
        "role": "coo",
    })
    check("POST /api/v1/executive/query (refusal)", s, dt, b,
          must_contain=["REFUSED_INSUFFICIENT_DATA", "refusal_reason"])

    # 6 — approvals pending
    s, dt, b = call("GET", "/api/v1/approvals/pending")
    ok = check("GET /api/v1/approvals/pending", s, dt, b,
               must_contain=["pending_actions", "PENDING_HUMAN_APPROVAL"])
    pending_ids: list[str] = []
    if ok:
        pending_ids = [i["action_id"] for i in b.get("pending_actions", [])]

    # 7 — execute an approval (approve)
    exec_id = first_note_id or (pending_ids[0] if pending_ids else None)
    if exec_id:
        s, dt, b = call("POST", f"/api/v1/approvals/{exec_id}/execute", {
            "decision": "approve", "reviewer": "principal",
            "note": "Approved during smoke test",
        })
        check("POST /api/v1/approvals/{id}/execute (approve)", s, dt, b,
              must_contain=["EXECUTED", "APPROVED", "executed_result"])
    else:
        print("[FAIL] no pending action_id available for execute test")
        RESULTS.append(("POST /api/v1/approvals/{id}/execute (approve)", False, 0, "no action_id"))

    # 8 — execute an approval (reject) — use a second, still-pending action
    remaining = [i for i in pending_ids if i != exec_id]
    reject_id = second_note_id if (second_note_id and second_note_id != exec_id) else (
        remaining[0] if remaining else (
            leave_action_id if leave_action_id != exec_id else None
        )
    )
    if reject_id:
        s, dt, b = call("POST", f"/api/v1/approvals/{reject_id}/execute", {
            "decision": "reject", "reviewer": "coo", "note": "Rejected during smoke test",
        })
        check("POST /api/v1/approvals/{id}/execute (reject)", s, dt, b,
              must_contain=["REJECTED"])
    else:
        print("[WARN] no second pending action for reject test — creating via dispatch")
        s, dt, b = call("POST", "/api/v1/operations/substitute-dispatch", {
            "teacher_name": "David Brown",
            "leave_date": "2026-10-23",
            "subject_needed": "English",
            "periods": 1,
            "reason": "Family matter",
        })
        rid = (b.get("leave_request") or {}).get("action_id")
        if rid:
            s2, dt2, b2 = call("POST", f"/api/v1/approvals/{rid}/execute", {
                "decision": "reject", "reviewer": "coo",
            })
            check("POST /api/v1/approvals/{id}/execute (reject)", s2, dt2, b2,
                  must_contain=["REJECTED"])
        else:
            RESULTS.append(("POST /api/v1/approvals/{id}/execute (reject)", False, 0, "no action_id"))
            print("[FAIL] reject test — could not obtain action_id")

    # ---- summary
    elapsed = time.time() - t_suite
    passed = sum(1 for _, ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    print()
    print("=" * 72)
    print(f"SUMMARY: {passed}/{total} passed   suite_elapsed={elapsed:.1f}s")
    print("=" * 72)
    for name, ok, dt, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<55} {detail}")
    print()
    if elapsed > 600:
        print("WARNING: suite exceeded 10 minutes")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
