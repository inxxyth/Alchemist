"""FX.1–FX.6 functional-fix tests (Improvement.md Part 2) — SYNTHETIC data.

Fixtures use TEST-/SYNTHETIC- names and ``@synthetic.example`` addresses in a
dedicated ``school_id = 'fx_fixes_test'`` so the seeded default cohort is never
touched.  The suite cleans up ONLY the rows it created (same idiom as
``test_policy_pack._clear_school``) so it is re-runnable and the session
student count returns to baseline.

Covers
------
* FX.1 exact teacher match wins; ambiguous short name -> 409 listing
  candidates (never LIMIT 1); a name containing LIKE wildcards matches
  nothing
* FX.2 subject vocabulary: Maths<->Mathematics both directions, match_rule
  reported per candidate ("exact"|"alias"|"group"); Art vs Mathematics is an
  honest empty
* FX.3 ranking ladder: internal before agency even with less capacity;
  deterministic across runs; cover_class + day_rate + recommendation_reason
  exposed; no money language in the response
* FX.4 zero-attendance student surfaces as DATA_GAP with NO parent
  notification staged; a student with real attendance rows behaves exactly as
  before; DATA_GAP cases ordered after real risk cases; the seeded cohort's
  findings match the pre-fix snapshot
* FX.5 generated_at is aware UTC (+00:00)
* FX.6 retention cutoffs derive from ONE clock; dry-run deletes nothing;
  --apply --days 1 refused (exit 2)

Run (from the container network)::

    python -u test_fixes.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
TIMEOUT = 60
SCHOOL = "fx_fixes_test"
SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "fixes", "pre_fix_compliance_default.json")

API_KEYS_RAW = os.environ.get("MSOE_API_KEYS", "")
if not API_KEYS_RAW:
    print("FATAL: MSOE_API_KEYS environment variable is not set.")
    sys.exit(1)
API_KEY = API_KEYS_RAW.split(",")[0].strip().split(":", 1)[1].strip()

PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}

RESULTS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name:<62} {detail}")
    return bool(condition)


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Connection": "close", "X-API-Key": API_KEY}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:800]}
    except Exception as exc:
        return 0, {"error": repr(exc)}


def pg():
    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=5)
    return conn


# ---------------------------------------------------------------------------
# Fixtures — SYNTHETIC rows in SCHOOL, removed at teardown
# ---------------------------------------------------------------------------

def seed_fixtures() -> dict:
    """Create the synthetic roll. Returns the created ids for teardown."""
    ids: dict = {"staff": [], "students": []}
    conn = pg()
    try:
        with conn.cursor() as cur:
            staff_rows = [
                # (full_name, email, subjects, is_available, max, assigned, is_agency, day_rate)
                ("John Smith", "john.smith@synthetic.example", ["Mathematics"],
                 True, 6, 0, False, None),
                ("Johnny Cash", "johnny.cash@synthetic.example", ["English"],
                 True, 6, 0, False, None),
                ("Mo Maths", "mo.maths@synthetic.example", ["Maths"],
                 True, 6, 0, False, None),
                ("Test Artsy", "test.artsy@synthetic.example", ["Art"],
                 True, 6, 0, False, None),
                # FX.3 ladder: agency with huge capacity must lose to internal
                ("Agency Amy", "agency.amy@synthetic.example", ["Physics"],
                 True, 20, 0, True, 9999.00),
                ("Internal Ian", "internal.ian@synthetic.example", ["Physics"],
                 True, 4, 1, False, None),
            ]
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='staff' AND column_name='is_agency'")
            has_agency_cols = cur.fetchone() is not None
            for name, email, subjects, avail, mx, assigned, agency, rate in staff_rows:
                if has_agency_cols:
                    cur.execute(
                        "INSERT INTO staff (school_id, full_name, email, role, subjects, "
                        "is_available, max_periods, assigned, is_agency, day_rate) "
                        "VALUES (%s,%s,%s,'teacher',%s,%s,%s,%s,%s,%s) RETURNING staff_id",
                        (SCHOOL, name, email, subjects, avail, mx, assigned, agency, rate),
                    )
                else:
                    # Pre-fix schema has no is_agency/day_rate (FX.3 adds them).
                    cur.execute(
                        "INSERT INTO staff (school_id, full_name, email, role, subjects, "
                        "is_available, max_periods, assigned) "
                        "VALUES (%s,%s,%s,'teacher',%s,%s,%s,%s) RETURNING staff_id",
                        (SCHOOL, name, email, subjects, avail, mx, assigned),
                    )
                ids["staff"].append(cur.fetchone()[0])

            student_rows = [
                # FX.4 zero-attendance: NEVER appears in attendance_logs
                ("SYNTHETIC Zero", "AttendanceGap", "up_to_date"),
                # FX.4 real-attendance control: 10 rows, 2 present -> 20%
                ("SYNTHETIC Real", "AttendanceRows", "up_to_date"),
                # FX.4 snapshot-stability control: real rows, all present -> 100%
                ("SYNTHETIC Fine", "AttendanceFine", "up_to_date"),
            ]
            for first, last, vax in student_rows:
                cur.execute(
                    "INSERT INTO students (school_id, source_student_id, first_name, "
                    "last_name, gender, date_of_birth, grade_level, curriculum_track, "
                    "school_name, emirate, nationality, parent_full_name, parent_email, "
                    "parent_phone, vaccination_status, enrollment_date) "
                    "VALUES (%s,%s,%s,%s,'female','2015-01-01',5,'British',%s,"
                    "'Dubai','SYNTHETIC','SYNTHETIC Parent','parent@synthetic.example',"
                    "'0000000000',%s,'2025-09-01') RETURNING student_id",
                    (SCHOOL, f"fx-{first.lower().replace(' ', '-')}", first, last,
                     "SYNTHETIC School", vax),
                )
                ids["students"].append(cur.fetchone()[0])

            real_id = ids["students"][1]
            fine_id = ids["students"][2]
            day0 = datetime(2026, 10, 1).date()
            for i in range(10):
                status = "present" if i < 2 else "absent"
                cur.execute(
                    "INSERT INTO attendance_logs (school_id, student_id, attendance_date, "
                    "status) VALUES (%s,%s,%s,%s)",
                    (SCHOOL, real_id, day0 + timedelta(days=i), status),
                )
                cur.execute(
                    "INSERT INTO attendance_logs (school_id, student_id, attendance_date, "
                    "status) VALUES (%s,%s,%s,'present')",
                    (SCHOOL, fine_id, day0 + timedelta(days=i)),
                )
        conn.commit()
    finally:
        conn.close()
    return ids


def teardown(ids: dict) -> None:
    """Remove ONLY the rows this suite created (by exact id)."""
    conn = pg()
    try:
        with conn.cursor() as cur:
            if ids["students"]:
                cur.execute("DELETE FROM attendance_logs WHERE student_id = ANY(%s)",
                            (ids["students"],))
                cur.execute("DELETE FROM fee_ledgers WHERE student_id = ANY(%s)",
                            (ids["students"],))
                cur.execute("DELETE FROM students WHERE student_id = ANY(%s)",
                            (ids["students"],))
            if ids["staff"]:
                cur.execute("DELETE FROM staff WHERE staff_id = ANY(%s)", (ids["staff"],))
        conn.commit()
    finally:
        conn.close()


def count_students() -> int:
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM students")
            return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

def part_fx1(_ids: dict) -> None:
    print("\n--- FX.1 teacher match (exact | 409 ambiguous | wildcard-safe) ---")
    s, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "  John   Smith  ", "leave_date": "2026-11-03",
        "subject_needed": "Mathematics", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    staged_name = ((b.get("leave_request") or {}).get("payload") or {}).get("teacher_name")
    ok("FX.1 exact match on normalised name ('  John   Smith  ' -> John Smith)",
       s == 200 and staged_name == "John Smith",
       f"HTTP {s} staged_teacher={staged_name!r}")

    s, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "Johnny", "leave_date": "2026-11-03",
        "subject_needed": "Mathematics", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    detail = json.dumps(b, default=str)
    ok("FX.1 'Johnny' with John Smith + Johnny Cash on the roll -> 409",
       s == 409 and "John Smith" in detail and "Johnny Cash" in detail,
       f"HTTP {s} candidates_in_body="
       f"{'John Smith' in detail and 'Johnny Cash' in detail}")

    s, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "J%", "leave_date": "2026-11-03",
        "subject_needed": "Mathematics", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    ok("FX.1 name with literal % is not a wildcard (404, not the whole roll)",
       s == 404 and "J%" in json.dumps(b, default=str),
       f"HTTP {s}")


def part_fx2(_ids: dict) -> None:
    print("\n--- FX.2 subject vocabulary (alias + group, honest empty) ---")
    s, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "Johnny Cash", "leave_date": "2026-11-04",
        "subject_needed": "Maths", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    cands = b.get("candidates") or []
    names = {c.get("full_name"): c.get("subject_match_rule") for c in cands}
    ok("FX.2 'Maths' finds a teacher holding ['Mathematics']",
       s == 200 and "John Smith" in names,
       f"HTTP {s} candidates={sorted(names)}")
    ok("FX.2 match_rule reported per candidate ('alias' for Maths->Mathematics)",
       names.get("John Smith") == "alias",
       f"rule={names.get('John Smith')!r}")

    s, b = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "Johnny Cash", "leave_date": "2026-11-04",
        "subject_needed": "Mathematics", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    names = {c.get("full_name"): c.get("subject_match_rule")
             for c in (b.get("candidates") or [])}
    ok("FX.2 vice versa: 'Mathematics' finds a teacher holding ['Maths']",
       s == 200 and "Mo Maths" in names,
       f"HTTP {s} candidates={sorted(names)}")

    # Honest empty: the only Art holder is the requester herself -> no cover
    # exists and the API must say so, not borrow a Mathematics teacher.
    s2, b2 = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "Test Artsy", "leave_date": "2026-11-04",
        "subject_needed": "Art", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    ok("FX.2 'Art' vs ['Mathematics'] roll -> honest empty, no over-merge",
       s2 == 200 and (b2.get("candidates") or []) == []
       and b2.get("recommended_substitute") is None,
       f"HTTP {s2} candidates={[c.get('full_name') for c in (b2.get('candidates') or [])]}")

    # An Art need must never surface Mathematics holders either.
    s3, b3 = call("POST", "/api/v1/operations/substitute-dispatch", {
        "teacher_name": "John Smith", "leave_date": "2026-11-04",
        "subject_needed": "Art", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    })
    art_cands = [c.get("full_name") for c in (b3.get("candidates") or [])]
    ok("FX.2 'Art' need surfaces only Art holders (never Mathematics teachers)",
       s3 == 200 and art_cands == ["Test Artsy"],
       f"art_need_candidates={art_cands}")


def part_fx3(ids: dict) -> None:
    print("\n--- FX.3 cost-aware ranking ladder (ordering only, no money talk) ---")
    payload = {
        "teacher_name": "John Smith", "leave_date": "2026-11-05",
        "subject_needed": "Physics", "periods": 1,
        "reason": "SYNTHETIC fixture", "school_id": SCHOOL,
    }
    s, b = call("POST", "/api/v1/operations/substitute-dispatch", payload)
    cands = b.get("candidates") or []
    order1 = [c.get("staff_id") for c in cands]
    rec = b.get("recommended_substitute") or {}
    agency_cap = next((c.get("capacity_remaining") for c in cands
                       if c.get("full_name") == "Agency Amy"), None)
    internal_cap = next((c.get("capacity_remaining") for c in cands
                         if c.get("full_name") == "Internal Ian"), None)
    ok("FX.3 agency with huge capacity LOSES to internal with less capacity",
       s == 200 and rec.get("full_name") == "Internal Ian"
       and (agency_cap or 0) > (internal_cap or 0),
       f"HTTP {s} recommended={rec.get('full_name')!r} "
       f"agency_cap={agency_cap} internal_cap={internal_cap}")

    s2, b2 = call("POST", "/api/v1/operations/substitute-dispatch", payload)
    order2 = [c.get("staff_id") for c in (b2.get("candidates") or [])]
    ok("FX.3 ordering deterministic across runs",
       s2 == 200 and order1 == order2 and len(order1) >= 2,
       f"order1={order1} order2={order2}")

    blob = json.dumps(b, default=str)
    per_cand_fields = all(
        "cover_class" in c and "day_rate" in c for c in cands
    ) if cands else False
    ok("FX.3 per-candidate cover_class + day_rate exposed",
       per_cand_fields and rec.get("cover_class") in ("internal", "agency"),
       f"rec_cover_class={rec.get('cover_class')!r} rec_day_rate={rec.get('day_rate')!r}")
    reason = rec.get("recommendation_reason") or ""
    ok("FX.3 recommendation_reason present (cover/capacity/rate phrasing)",
       "internal" in reason and "capacity" in reason,
       f"reason={reason!r}")

    # Money-language scan covers the NEW dispatch fields only — the roi block
    # is pre-existing (roi_model) and carries its own honesty wording.
    dispatch_blob = json.dumps(
        {"candidates": cands, "recommended_substitute": rec}, default=str)
    banned = [w for w in ("saved", "cheaper", "avoided", "AED")
              if w.lower() in dispatch_blob.lower()]
    ok("FX.3 no money language in the ranking fields (ordering only)",
       not banned, f"banned_found={banned}")

    ok("FX.3 students table untouched by dispatch",
       ids.get("students_before") == count_students(),
       f"before={ids.get('students_before')} after={count_students()}")


def part_fx4(_ids: dict) -> None:
    print("\n--- FX.4 zero-attendance blind spot (DATA_GAP, internal only) ---")
    s, b = call("POST", "/api/v1/audit/compliance",
                {"school_id": SCHOOL, "max_students": 50})
    viols = b.get("violations") or []
    notes = b.get("parent_notifications") or []
    gap = [v for v in viols if v.get("risk_type") == "DATA_GAP"]
    gap_names = [v.get("student_name") for v in gap]
    ok("FX.4 zero-attendance student surfaces as DATA_GAP (not invisible)",
       s == 200 and any("SYNTHETIC Zero" in (n or "") for n in gap_names),
       f"HTTP {s} gap_names={gap_names}")
    gap_detail = gap[0].get("risk_detail", {}) if gap else {}
    ok("FX.4 attendance_rate is None (no data), not 100.0, and record count returned",
       gap_detail.get("attendance_rate") is None
       and gap_detail.get("attendance_records") == 0,
       f"detail={gap_detail}")

    gap_ids = {v.get("student_id") for v in gap}
    leaked = [n for n in notes
              if (n.get("student_id") in gap_ids
                  and n.get("risk_type") == "DATA_GAP")]
    ok("FX.4 NO SEND_PARENT_NOTIFICATION staged for a data-gap student, ever",
       not leaked and all(n.get("risk_type") != "DATA_GAP" for n in notes),
       f"gap_notifications={len(leaked)} total_notes={len(notes)}")

    real = [v for v in viols if v.get("student_name") == "SYNTHETIC Real AttendanceRows"]
    real_detail = (real[0].get("risk_detail") if real else {}) or {}
    ok("FX.4 student with real attendance rows behaves exactly as before "
       "(CHRONIC_ATTENDANCE at 20%, same detail shape)",
       len(real) == 1 and real[0].get("risk_type") == "CHRONIC_ATTENDANCE"
       and isinstance(real_detail.get("attendance_rate"), float)
       and abs(float(real_detail.get("attendance_rate", -1)) - 20.0) < 0.01
       and set(real_detail) == {"vaccination_status", "attendance_rate",
                                "max_days_overdue"},
       f"real={[(v.get('risk_type'), v.get('risk_detail')) for v in real]}")
    real_notes = [n for n in notes if n.get("student_id") == (real[0].get("student_id")
                  if real else None)]
    ok("FX.4 real-risk student still gets a parent notification staged",
       len(real_notes) == 1, f"real_notes={len(real_notes)}")

    types = [v.get("risk_type") for v in viols]
    last_real = max((i for i, t in enumerate(types) if t != "DATA_GAP"), default=-1)
    first_gap = min((i for i, t in enumerate(types) if t == "DATA_GAP"), default=len(types))
    ok("FX.4 DATA_GAP cases ordered AFTER real risk cases",
       bool(gap) and first_gap > last_real,
       f"last_real_idx={last_real} first_gap_idx={first_gap}")

    # Seeded-cohort snapshot: findings byte-stable modulo the FX.4 additions.
    s2, b2 = call("POST", "/api/v1/audit/compliance",
                  {"max_students": 50, "school_id": "default"})
    with open(SNAPSHOT, encoding="utf-8") as fh:
        pre = json.load(fh)
    pre_v = pre.get("violations") or []
    post_v = [v for v in (b2.get("violations") or []) if v.get("risk_type") != "DATA_GAP"]
    pre_keys = [(v.get("student_id"), v.get("risk_type")) for v in pre_v]
    post_keys = [(v.get("student_id"), v.get("risk_type")) for v in post_v]
    ok("FX.4 seeded cohort: same findings in same order as pre-fix snapshot",
       s2 == 200 and pre_keys == post_keys,
       f"pre_n={len(pre_keys)} post_n={len(post_keys)} equal={pre_keys == post_keys}")
    fields_stable = True
    drift = ""
    for old, new in zip(pre_v, post_v):
        for k, v in (old.get("risk_detail") or {}).items():
            if (new.get("risk_detail") or {}).get(k) != v:
                fields_stable = False
                drift = f"{old.get('student_id')}:{k} {v!r} -> {(new.get('risk_detail') or {}).get(k)!r}"
                break
        if not fields_stable:
            break
    ok("FX.4 seeded cohort: pre-fix risk_detail values unchanged",
       fields_stable, drift or "all pre-existing fields identical")
    pre_note_keys = sorted((n.get("student_id"), n.get("risk_type"))
                           for n in (pre.get("parent_notifications") or []))
    post_note_keys = sorted((n.get("student_id"), n.get("risk_type"))
                            for n in (b2.get("parent_notifications") or []))
    ok("FX.4 seeded cohort: parent notification set unchanged",
       pre_note_keys == post_note_keys,
       f"pre_n={len(pre_note_keys)} post_n={len(post_note_keys)}")


def part_fx5(_ids: dict) -> None:
    print("\n--- FX.5 aware UTC timestamps (+00:00) ---")
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
    s, b = call("POST", "/api/v1/audit/compliance",
                {"school_id": SCHOOL, "max_students": 5})
    ok("FX.5 compliance generated_at is aware UTC",
       s == 200 and bool(pat.match(str(b.get("generated_at", "")))),
       f"generated_at={b.get('generated_at')!r}")
    s2, b2 = call("GET", "/api/v1/roi/report?school_id=" + SCHOOL)
    roi_gen = (b2.get("generated_at") or (b.get("roi") or {}).get("generated_at") or "")
    ok("FX.5 roi report generated_at is aware UTC",
       s2 == 200 and bool(pat.match(str(roi_gen))),
       f"roi.generated_at={roi_gen!r}")


def part_fx6(_ids: dict) -> None:
    print("\n--- FX.6 one clock for both retention cutoffs ---")
    import retention_sweep
    fixed = datetime(2026, 10, 24, 13, 45, 12, tzinfo=timezone.utc)
    try:
        d_cut = retention_sweep.cutoff_for("date", 30, now=fixed)
        t_cut = retention_sweep.cutoff_for("timestamptz", 30, now=fixed)
        ok("FX.6 both cutoffs derive from ONE injected now (date + timestamptz)",
           d_cut == fixed.date() - timedelta(days=30)
           and t_cut == fixed - timedelta(days=30),
           f"date_cutoff={d_cut} ts_cutoff={t_cut}")
        ok("FX.6 date cutoff is the UTC date of the timestamp cutoff",
           d_cut == t_cut.date(), f"{d_cut} vs {t_cut.date()}")
    except TypeError as exc:
        ok("FX.6 both cutoffs derive from ONE injected now (date + timestamptz)",
           False, f"cutoff_for() rejects now= kwarg: {exc!r}")
        ok("FX.6 date cutoff is the UTC date of the timestamp cutoff",
           False, "not reached")

    here = os.path.dirname(os.path.abspath(__file__))
    before_att = _count("attendance_logs")
    before_aud = _count("audit_log")
    p = subprocess.run([sys.executable, os.path.join(here, "retention_sweep.py")],
                       capture_output=True, text=True, timeout=120)
    after_att = _count("attendance_logs")
    after_aud = _count("audit_log")
    ok("FX.6 dry-run exits 0 and deletes nothing",
       p.returncode == 0 and "no rows deleted" in p.stdout.lower()
       and before_att == after_att and before_aud == after_aud,
       f"rc={p.returncode} att {before_att}->{after_att} audit {before_aud}->{after_aud}")

    p2 = subprocess.run([sys.executable, os.path.join(here, "retention_sweep.py"),
                         "--apply", "--days", "1"],
                        capture_output=True, text=True, timeout=120)
    ok("FX.6 --apply --days 1 refused (exit 2, min 30d guard)",
       p2.returncode == 2 and "30" in (p2.stderr + p2.stdout),
       f"rc={p2.returncode}")


def _count(table: str) -> int:
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    print("=" * 72)
    print("MSOE FX.1-FX.6 FUNCTIONAL-FIX SUITE (SYNTHETIC fixtures)")
    print("=" * 72)
    session_start = count_students()
    ids = seed_fixtures()
    ids["students_before"] = count_students()  # after seeding: dispatch must not move it
    try:
        part_fx1(ids)
        part_fx2(ids)
        part_fx3(ids)
        part_fx4(ids)
        part_fx5(ids)
        part_fx6(ids)
    finally:
        teardown(ids)
    ok("suite left the student count unchanged (cleanup complete)",
       count_students() == session_start,
       f"session_start={session_start} after={count_students()}")
    passed = sum(1 for _n, c, _d in RESULTS if c)
    total = len(RESULTS)
    print("=" * 72)
    print(f"SUMMARY: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
