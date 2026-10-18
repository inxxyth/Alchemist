"""SIS Modular Integration Layer — bridges open-source SIS schemas to MSOE.

Translates standard open-source school-management table shapes (Gibbon /
OpenEduCat / Canvas-style) into the MSOE PostgreSQL relational store
(``msoe_db``) and the Neo4j knowledge graph (``msoe_neo4j``).

Canonical entities
------------------
``Student`` · ``Attendance`` · ``Fee`` · ``Staff`` · ``LeaveRequest``

Source dialects (normalised to canonical form)
----------------------------------------------
* **Gibbon-like**  — ``gibbonPerson``, ``gibbonAttendanceLogPerson``,
  ``gibbonFinanceInvoice``, ``gibbonStaff``
* **OpenEduCat-like** — ``op.student``, ``op.attendance``, ``op.fees``,
  ``op.faculty``
* **Canvas-like webhooks** — ``sis.student``, ``sis.student.score``,
  ``enrollment``, ``user``

Sync modes
----------
* **Batched**  — ``sync_*`` upserts from a list of canonical / dialect dicts.
* **Real-time** — ``handle_webhook`` routes a webhook event payload into the
  same upserts (mirrors LMS webhook contracts).

Safety
------
Writes go to MSOE tables only. All mapping is pure; no secret handling.
Neo4j sync creates/updates the same Student/RiskFlag shape the knowledge
graph builder produces, so executive RAG paths keep working.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Iterable, Optional

from contextlib import contextmanager

import psycopg2
from neo4j import GraphDatabase

# Connection settings mirror main.py / docker-compose (no import cycle).
PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "msoe_graph_2026")
PG_CONNECT_TIMEOUT = 5

# Client-configurable defaults (F.8 — env with fallback to current demo values).
DEFAULT_SCHOOL_ID = "default"
DEFAULT_SCHOOL_NAME = os.environ.get("MSOE_SCHOOL_NAME", "MSOE Academy")
DEFAULT_EMIRATE = os.environ.get("MSOE_EMIRATE", "Dubai")
DEFAULT_PARENT_EMAIL = os.environ.get("MSOE_PARENT_EMAIL", "parent@msoe.ae")
DEFAULT_STAFF_EMAIL = os.environ.get("MSOE_STAFF_EMAIL", "staff@msoe.ae")
DEFAULT_PARENT_PHONE = os.environ.get("MSOE_PARENT_PHONE", "+971500000000")
DEFAULT_CURRICULUM = os.environ.get("MSOE_CURRICULUM", "British")


@contextmanager
def get_pg_cursor():
    """Per-call PG cursor (same non-pooled pattern as main.py)."""
    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    try:
        cur = conn.cursor()
        try:
            yield cur, conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Canonical row shapes (docs / validation only — dicts are accepted as-is)
# ---------------------------------------------------------------------------
# Student:  student_id, emirates_id, first_name, last_name, grade_level,
#           curriculum_track, school_name, emirate, parent_full_name,
#           parent_email, vaccination_status, vaccination_expiry
# Attendance: student_id, attendance_date, status (present|absent|late|excused)
# Fee:      student_id, amount_due, amount_paid, due_date, status, days_overdue
# Staff:    staff_id, full_name, email, subjects[], is_available,
#           max_periods, assigned


RISK_TYPES = ("VACCINATION_EXPIRED", "CHRONIC_ATTENDANCE", "FEE_OVERDUE")

# ---------------------------------------------------------------------------
# Dialect field maps — source key → canonical key
# ---------------------------------------------------------------------------

STUDENT_FIELD_MAPS: dict[str, dict[str, str]] = {
    "gibbon": {
        "gibbonPersonID": "source_student_id",
        "nationalID": "emirates_id",
        "firstName": "first_name",
        "surname": "last_name",
        "grade": "grade_level",
        "curriculum": "curriculum_track",
        "school": "school_name",
        "emirate": "emirate",
        "parent_name": "parent_full_name",
        "parent_email": "parent_email",
        "vaccination_status": "vaccination_status",
        "vaccination_expiry": "vaccination_expiry",
    },
    "openeducat": {
        "id": "source_student_id",
        "emirates_id": "emirates_id",
        "first_name": "first_name",
        "last_name": "last_name",
        "grade_level": "grade_level",
        "course": "curriculum_track",
        "school_name": "school_name",
        "emirate": "emirate",
        "guardian_name": "parent_full_name",
        "guardian_email": "parent_email",
        "vaccination_status": "vaccination_status",
        "vaccination_expiry": "vaccination_expiry",
    },
    "canvas": {
        "sis_user_id": "source_student_id",
        "national_id": "emirates_id",
        "first_name": "first_name",
        "last_name": "last_name",
        "grade_level": "grade_level",
        "curriculum_track": "curriculum_track",
        "school_name": "school_name",
        "emirate": "emirate",
        "parent_full_name": "parent_full_name",
        "parent_email": "parent_email",
        "vaccination_status": "vaccination_status",
        "vaccination_expiry": "vaccination_expiry",
    },
}

ATTENDANCE_FIELD_MAPS: dict[str, dict[str, str]] = {
    "gibbon": {
        "gibbonPersonID": "source_student_id",
        "date": "attendance_date",
        # gibbonAttendanceCode.name ("Present", "Present - Late", "Absent", ...).
        # The raw gibbonAttendanceLogPerson.type column is Core/Additional, not a status.
        "name": "status",
    },
    "openeducat": {
        "student_id": "source_student_id",
        "attendance_date": "attendance_date",
        "status": "status",
    },
    "canvas": {
        "sis_user_id": "source_student_id",
        "attendance_date": "attendance_date",
        "status": "status",
    },
}

FEE_FIELD_MAPS: dict[str, dict[str, str]] = {
    "gibbon": {
        "gibbonPersonID": "source_student_id",
        "amount": "amount_due",
        "amount_paid": "amount_paid",
        "dueDate": "due_date",
        "status": "status",
        "days_overdue": "days_overdue",
    },
    "openeducat": {
        "student_id": "source_student_id",
        "amount_due": "amount_due",
        "amount_paid": "amount_paid",
        "due_date": "due_date",
        "status": "status",
        "days_overdue": "days_overdue",
    },
    "canvas": {
        "sis_user_id": "source_student_id",
        "amount_due": "amount_due",
        "amount_paid": "amount_paid",
        "due_date": "due_date",
        "status": "status",
        "days_overdue": "days_overdue",
    },
}

STAFF_FIELD_MAPS: dict[str, dict[str, str]] = {
    "gibbon": {
        "gibbonStaffID": "source_staff_id",
        "fullName": "full_name",
        "email": "email",
        "subjects": "subjects",
        "is_available": "is_available",
        "max_periods": "max_periods",
        "assigned": "assigned",
    },
    "openeducat": {
        "id": "source_staff_id",
        "full_name": "full_name",
        "email": "email",
        "subjects": "subjects",
        "is_available": "is_available",
        "max_periods": "max_periods",
        "assigned": "assigned",
    },
    "canvas": {
        "sis_user_id": "source_staff_id",
        "name": "full_name",
        "email": "email",
        "subjects": "subjects",
        "is_available": "is_available",
        "max_periods": "max_periods",
        "assigned": "assigned",
    },
}


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------

def _detect_dialect(row: dict[str, Any]) -> Optional[str]:
    keys = set(row.keys())
    for dialect, fmap in (
        ("gibbon", STUDENT_FIELD_MAPS["gibbon"]),
        ("openeducat", STUDENT_FIELD_MAPS["openeducat"]),
        ("canvas", STUDENT_FIELD_MAPS["canvas"]),
    ):
        if keys & set(fmap.keys()):
            return dialect
    return None


def _apply_map(row: dict[str, Any], field_map: dict[str, str],
               canonical_defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out = dict(canonical_defaults or {})
    for src, dest in field_map.items():
        if src in row and row[src] is not None:
            out[dest] = row[src]
    # Pass through already-canonical keys
    for key in field_map.values():
        if key in row and key not in out:
            out[key] = row[key]
    return out


def _as_grade(value: Any, default: int = 1) -> int:
    try:
        g = int(value)
        return min(12, max(1, g))
    except Exception:
        return default


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except Exception:
        return None


def _normalize_status(raw: Any) -> str:
    """Map LMS attendance verbs onto MSOE: present|absent|late|excused."""
    s = str(raw or "").strip().lower()
    if s in {"present", "p", "here", "checked_in", "checked-in", "active"}:
        return "present"
    if s in {"late", "tardy", "partial", "arrived_late"}:
        return "late"
    if s in {"excused", "excuse", "approved", "medical", "leave"}:
        return "excused"
    # Gibbon attendance-code names: "Present", "Present - Late", "Absent", "Left", ...
    if "late" in s or s in {"tardy", "partial"}:
        return "late"
    if "present" in s:
        return "present"
    if "excused" in s or "excuse" in s:
        return "excused"
    return "absent"


def _normalize_vaccination(raw: Any) -> str:
    s = str(raw or "up_to_date").strip().lower().replace(" ", "_")
    if s in {"valid", "current", "up-to-date", "up_to_date", "complete", "ok"}:
        return "up_to_date"
    if s in {"partial", "incomplete"}:
        return "partial"
    if s in {"expired", "lapsed"}:
        return "expired"
    if s in {"missing", "none", "absent", "unknown"}:
        return "missing"
    return "up_to_date"


def _normalize_curriculum(raw: Any) -> str:
    s = str(raw or "British").strip()
    for opt in ("British", "IB", "CBSE", "MoE"):
        if s.lower() == opt.lower():
            return opt
    return "British"


def map_student(row: dict[str, Any], dialect: Optional[str] = None,
                school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    """Normalise a SIS student row to the MSOE ``students`` shape."""
    dialect = dialect or _detect_dialect(row) or "canvas"
    fmap = STUDENT_FIELD_MAPS.get(dialect, STUDENT_FIELD_MAPS["canvas"])
    canon = _apply_map(row, fmap)
    first = str(canon.get("first_name") or row.get("firstName") or row.get("first_name") or "").strip()
    last = str(canon.get("last_name") or row.get("surname") or row.get("last_name") or "").strip()
    raw_emirates = canon.get("emirates_id") or row.get("emirates_id") or ""
    emirates_id = str(raw_emirates).strip()[:15] or None
    # F.4: a missing national ID is NULL — never a fabricated one occupying
    # a unique key.  The old hash-synthesised mock is deliberately removed.
    source_student_id = canon.get("source_student_id") or row.get("source_student_id")
    if source_student_id is not None:
        source_student_id = str(source_student_id)[:64]
    # Legacy: callers that still send student_id as the source-native id.
    legacy_sid = canon.get("student_id") or row.get("student_id")
    if source_student_id is None and legacy_sid is not None:
        source_student_id = str(legacy_sid)[:64]
    dob = _as_date(canon.get("date_of_birth") or row.get("date_of_birth") or row.get("dob"))
    if dob is None:
        dob = date(2015, 1, 1)
    enrollment = _as_date(canon.get("enrollment_date") or row.get("enrollment_date")) or date.today()
    return {
        "school_id": str(canon.get("school_id") or row.get("school_id") or school_id)[:64],
        "source_student_id": source_student_id,
        "emirates_id": emirates_id,
        "first_name": first or "Unknown",
        "last_name": last or "Student",
        "gender": str(canon.get("gender") or row.get("gender") or "U")[:10],
        "date_of_birth": dob,
        "grade_level": _as_grade(canon.get("grade_level")),
        "curriculum_track": _normalize_curriculum(canon.get("curriculum_track")),
        "school_name": str(canon.get("school_name") or DEFAULT_SCHOOL_NAME)[:128],
        "emirate": str(canon.get("emirate") or DEFAULT_EMIRATE)[:32],
        "nationality": str(canon.get("nationality") or row.get("nationality") or "UAE")[:64],
        "parent_full_name": str(canon.get("parent_full_name") or "Parent/Guardian")[:128],
        "parent_email": str(canon.get("parent_email") or DEFAULT_PARENT_EMAIL)[:128],
        "parent_phone": str(canon.get("parent_phone") or row.get("parent_phone") or DEFAULT_PARENT_PHONE)[:20],
        "vaccination_status": _normalize_vaccination(canon.get("vaccination_status")),
        "vaccination_expiry": _as_date(canon.get("vaccination_expiry")),
        "enrollment_date": enrollment,
        "is_active": bool(canon.get("is_active", True)),
    }


def map_attendance(row: dict[str, Any], dialect: Optional[str] = None,
                   school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    dialect = dialect or _detect_dialect(row) or "canvas"
    fmap = ATTENDANCE_FIELD_MAPS.get(dialect, ATTENDANCE_FIELD_MAPS["canvas"])
    canon = _apply_map(row, fmap)
    source_sid = canon.get("source_student_id") or row.get("source_student_id")
    if source_sid is not None:
        source_sid = str(source_sid)[:64]
    legacy_sid = canon.get("student_id") or row.get("student_id")
    return {
        "school_id": str(canon.get("school_id") or row.get("school_id") or school_id)[:64],
        "source_student_id": source_sid,
        "student_id": legacy_sid,
        "attendance_date": _as_date(canon.get("attendance_date")) or date.today(),
        "status": _normalize_status(canon.get("status")),
    }


def map_fee(row: dict[str, Any], dialect: Optional[str] = None,
            school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    dialect = dialect or _detect_dialect(row) or "canvas"
    fmap = FEE_FIELD_MAPS.get(dialect, FEE_FIELD_MAPS["canvas"])
    canon = _apply_map(row, fmap)
    try:
        amount_due = float(canon.get("amount_due") or 0)
    except Exception:
        amount_due = 0.0
    try:
        amount_paid = float(canon.get("amount_paid") or 0)
    except Exception:
        amount_paid = 0.0
    status = str(canon.get("status") or "").strip().lower()
    # Live CHECK: current | partial | overdue | paid | waived
    if status in {"open", "pending", "unpaid"}:
        status = "current"
    if status not in {"current", "partial", "overdue", "paid", "waived"}:
        if amount_paid <= 0 and amount_due > 0:
            status = "overdue" if (canon.get("days_overdue") or 0) else "current"
        elif amount_paid < amount_due:
            status = "partial"
        else:
            status = "paid"
    days_overdue = canon.get("days_overdue")
    try:
        days_overdue = int(days_overdue)
    except Exception:
        days_overdue = 0
    due_date = _as_date(canon.get("due_date")) or date.today()
    invoice = canon.get("invoice_number") or row.get("invoice_number")
    if not invoice:
        sid = canon.get("source_student_id") or row.get("source_student_id") \
            or canon.get("student_id") or row.get("student_id") or "X"
        invoice = f"INV-{sid}-{due_date.isoformat()}"[:32]
    source_sid = canon.get("source_student_id") or row.get("source_student_id")
    if source_sid is not None:
        source_sid = str(source_sid)[:64]
    legacy_sid = canon.get("student_id") or row.get("student_id")
    return {
        "school_id": str(canon.get("school_id") or row.get("school_id") or school_id)[:64],
        "source_student_id": source_sid,
        "student_id": legacy_sid,
        "invoice_number": str(invoice)[:32],
        "term_label": str(canon.get("term_label") or row.get("term_label") or "Term 1")[:32],
        "amount_due": amount_due,
        "amount_paid": amount_paid,
        "due_date": due_date,
        "status": status,
        "days_overdue": max(0, days_overdue),
    }


def map_staff(row: dict[str, Any], dialect: Optional[str] = None,
              school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    dialect = dialect or _detect_dialect(row) or "canvas"
    fmap = STAFF_FIELD_MAPS.get(dialect, STAFF_FIELD_MAPS["canvas"])
    canon = _apply_map(row, fmap)
    subjects = canon.get("subjects") or row.get("subjects") or []
    if isinstance(subjects, str):
        subjects = [s.strip() for s in subjects.split(",") if s.strip()]
    if not isinstance(subjects, (list, tuple)):
        subjects = []
    source_sid = canon.get("source_staff_id") or row.get("source_staff_id")
    if source_sid is not None:
        source_sid = str(source_sid)[:64]
    legacy_sid = canon.get("staff_id") or row.get("staff_id")
    if source_sid is None and legacy_sid is not None:
        source_sid = str(legacy_sid)[:64]
    return {
        "school_id": str(canon.get("school_id") or row.get("school_id") or school_id)[:64],
        "source_staff_id": source_sid,
        "full_name": str(canon.get("full_name") or "Teacher")[:128],
        "email": str(canon.get("email") or DEFAULT_STAFF_EMAIL)[:128],
        "role": str(canon.get("role") or row.get("role") or "teacher")[:32],
        "subjects": list(subjects),
        "is_available": bool(canon.get("is_available", True)),
        "max_periods": int(canon.get("max_periods") or 6),
        "assigned": int(canon.get("assigned") or 0),
    }


# ---------------------------------------------------------------------------
# PostgreSQL upserts (MSOE schema)
# ---------------------------------------------------------------------------

def _resolve_student_pk(cur, school_id: str,
                        source_student_id: Optional[str],
                        legacy_student_id: Any) -> Optional[int]:
    """Resolve a source-native student id to the surrogate ``student_id`` PK."""
    if source_student_id:
        cur.execute(
            "SELECT student_id FROM students WHERE school_id = %s AND source_student_id = %s LIMIT 1",
            (school_id, str(source_student_id)[:64]),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    if legacy_student_id is not None:
        # Direct surrogate id supplied by the caller (API path).
        try:
            return int(legacy_student_id)
        except (TypeError, ValueError):
            pass
    return None


def sync_students(rows: Iterable[dict[str, Any]],
                  dialect: Optional[str] = None,
                  school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    """Upsert students on ``(school_id, source_student_id)``.

    Keeps the surrogate ``student_id`` BIGSERIAL key but never treats the
    SIS-native id as the PK.  Returns upsert count plus a source→surrogate
    id map so downstream attendance/fee rows can resolve their FK.
    """
    mapped = [map_student(r, dialect, school_id) for r in rows]
    mapped = [m for m in mapped if m.get("source_student_id") or m.get("emirates_id")]
    if not mapped:
        return {"upserted": 0, "skipped": 0, "table": "students", "id_map": {}}
    id_map: dict[str, int] = {}
    with get_pg_cursor() as (cur, _conn):
        for m in mapped:
            cur.execute(
                """
                INSERT INTO students (
                    school_id, source_student_id, emirates_id, first_name, last_name,
                    gender, date_of_birth, grade_level, curriculum_track, school_name,
                    emirate, nationality, parent_full_name, parent_email,
                    parent_phone, vaccination_status, vaccination_expiry,
                    enrollment_date, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (school_id, source_student_id) DO UPDATE SET
                    emirates_id = EXCLUDED.emirates_id,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    gender = EXCLUDED.gender,
                    date_of_birth = EXCLUDED.date_of_birth,
                    grade_level = EXCLUDED.grade_level,
                    curriculum_track = EXCLUDED.curriculum_track,
                    school_name = EXCLUDED.school_name,
                    emirate = EXCLUDED.emirate,
                    nationality = EXCLUDED.nationality,
                    parent_full_name = EXCLUDED.parent_full_name,
                    parent_email = EXCLUDED.parent_email,
                    parent_phone = EXCLUDED.parent_phone,
                    vaccination_status = EXCLUDED.vaccination_status,
                    vaccination_expiry = EXCLUDED.vaccination_expiry,
                    enrollment_date = EXCLUDED.enrollment_date,
                    is_active = EXCLUDED.is_active
                RETURNING student_id
                """,
                (
                    m["school_id"], m["source_student_id"], m["emirates_id"],
                    m["first_name"], m["last_name"], m["gender"], m["date_of_birth"],
                    m["grade_level"], m["curriculum_track"], m["school_name"],
                    m["emirate"], m["nationality"], m["parent_full_name"],
                    m["parent_email"], m["parent_phone"], m["vaccination_status"],
                    m["vaccination_expiry"], m["enrollment_date"], m["is_active"],
                ),
            )
            row = cur.fetchone()
            if row and m.get("source_student_id"):
                id_map[str(m["source_student_id"])] = row[0]
    return {"upserted": len(mapped), "skipped": 0, "table": "students", "id_map": id_map}


def sync_attendance(rows: Iterable[dict[str, Any]],
                    dialect: Optional[str] = None,
                    school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    mapped = [map_attendance(r, dialect, school_id) for r in rows]
    mapped = [m for m in mapped if m.get("source_student_id") or m.get("student_id")]
    if not mapped:
        return {"upserted": 0, "table": "attendance_logs"}
    inserted = 0
    with get_pg_cursor() as (cur, _conn):
        for m in mapped:
            pk = _resolve_student_pk(cur, m["school_id"],
                                     m.get("source_student_id"), m.get("student_id"))
            if pk is None:
                continue
            cur.execute(
                """
                INSERT INTO attendance_logs (school_id, student_id, attendance_date, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (student_id, attendance_date) DO UPDATE
                SET status = EXCLUDED.status
                """,
                (m["school_id"], pk, m["attendance_date"], m["status"]),
            )
            inserted += 1
    return {"upserted": inserted, "table": "attendance_logs"}


def sync_fees(rows: Iterable[dict[str, Any]],
              dialect: Optional[str] = None,
              school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    mapped = [map_fee(r, dialect, school_id) for r in rows]
    mapped = [m for m in mapped if m.get("source_student_id") or m.get("student_id")]
    if not mapped:
        return {"upserted": 0, "table": "fee_ledgers"}
    inserted = 0
    with get_pg_cursor() as (cur, _conn):
        for m in mapped:
            pk = _resolve_student_pk(cur, m["school_id"],
                                     m.get("source_student_id"), m.get("student_id"))
            if pk is None:
                continue
            cur.execute(
                """
                INSERT INTO fee_ledgers (
                    school_id, student_id, invoice_number, term_label,
                    amount_due, amount_paid, due_date, status, days_overdue
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, invoice_number) DO UPDATE SET
                    student_id = EXCLUDED.student_id,
                    amount_due = EXCLUDED.amount_due,
                    amount_paid = EXCLUDED.amount_paid,
                    due_date = EXCLUDED.due_date,
                    status = EXCLUDED.status,
                    days_overdue = EXCLUDED.days_overdue
                """,
                (
                    m["school_id"], pk, m["invoice_number"], m["term_label"],
                    m["amount_due"], m["amount_paid"], m["due_date"],
                    m["status"], m["days_overdue"],
                ),
            )
            inserted += 1
    return {"upserted": inserted, "table": "fee_ledgers"}


def sync_staff(rows: Iterable[dict[str, Any]],
               dialect: Optional[str] = None,
               school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    mapped = [map_staff(r, dialect, school_id) for r in rows]
    mapped = [m for m in mapped if m.get("email")]
    if not mapped:
        return {"upserted": 0, "table": "staff"}
    inserted = 0
    with get_pg_cursor() as (cur, _conn):
        for m in mapped:
            if m.get("source_staff_id"):
                cur.execute(
                    """
                    INSERT INTO staff (
                        school_id, source_staff_id, full_name, email, role, subjects,
                        is_available, max_periods, assigned
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (school_id, source_staff_id) DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        email = EXCLUDED.email,
                        role = EXCLUDED.role,
                        subjects = EXCLUDED.subjects,
                        is_available = EXCLUDED.is_available,
                        max_periods = EXCLUDED.max_periods,
                        assigned = EXCLUDED.assigned
                    """,
                    (
                        m["school_id"], m["source_staff_id"], m["full_name"], m["email"],
                        m["role"], m["subjects"], m["is_available"],
                        m["max_periods"], m["assigned"],
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO staff (
                        school_id, full_name, email, role, subjects,
                        is_available, max_periods, assigned
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (school_id, email) DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        role = EXCLUDED.role,
                        subjects = EXCLUDED.subjects,
                        is_available = EXCLUDED.is_available,
                        max_periods = EXCLUDED.max_periods,
                        assigned = EXCLUDED.assigned
                    """,
                    (
                        m["school_id"], m["full_name"], m["email"], m["role"],
                        m["subjects"], m["is_available"], m["max_periods"], m["assigned"],
                    ),
                )
            inserted += 1
    return {"upserted": inserted, "table": "staff"}


# ---------------------------------------------------------------------------
# Neo4j bridge (Student / RiskFlag consistent with build_knowledge_graph.py)
# ---------------------------------------------------------------------------

def _neo4j_driver():
    return GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        max_connection_pool_size=8,
        connection_acquisition_timeout=15,
    )


def compute_risk_flags(student: dict[str, Any],
                       attendance_rate: Optional[float] = None,
                       max_days_overdue: int = 0) -> list[str]:
    """Derive MSOE risk types from canonical student + derived metrics."""
    flags: list[str] = []
    vax = (student.get("vaccination_status") or "").lower()
    if vax in {"expired", "missing"}:
        flags.append("VACCINATION_EXPIRED")
    if attendance_rate is not None and float(attendance_rate) < 85:
        flags.append("CHRONIC_ATTENDANCE")
    if int(max_days_overdue or 0) > 60:
        flags.append("FEE_OVERDUE")
    return flags


def sync_students_to_graph(students: list[dict[str, Any]],
                           risk_by_student: Optional[dict[str, list[str]]] = None,
                           school_id: str = DEFAULT_SCHOOL_ID
                           ) -> dict[str, Any]:
    """Create/update Student nodes and HAS_RISK → RiskFlag edges in Neo4j."""
    if not students:
        return {"students_upserted": 0, "risk_edges": 0}
    risk_by_student = risk_by_student or {}
    rows = []
    for s in students:
        sid = s.get("source_student_id") or s.get("student_id")
        if not sid:
            continue
        sid = str(sid)
        full_name = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
        rows.append({
            "student_id": sid,
            "school_id": s.get("school_id") or school_id,
            "full_name": full_name,
            "grade_level": int(s.get("grade_level") or 1),
            "school_name": s.get("school_name") or DEFAULT_SCHOOL_NAME,
            "emirate": s.get("emirate") or DEFAULT_EMIRATE,
            "risks": risk_by_student.get(sid, []),
        })

    driver = _neo4j_driver()
    risk_edges = 0
    try:
        with driver.session() as session:
            session.run(
                """
                UNWIND $rows AS row
                MERGE (s:Student {student_id: row.student_id, school_id: row.school_id})
                SET s.full_name = row.full_name,
                    s.grade_level = row.grade_level,
                    s.school_name = row.school_name,
                    s.emirate = row.emirate
                """,
                rows=rows,
            )
            for row in rows:
                for risk in row["risks"]:
                    session.run(
                        """
                        MATCH (s:Student {student_id: $sid, school_id: $school_id})
                        MERGE (rf:RiskFlag {type: $risk, school_id: $school_id})
                        MERGE (s)-[:HAS_RISK]->(rf)
                        WITH rf, $risk AS risk
                        MATCH (std:Standard)-[:PART_OF]->(p:Policy)
                        WHERE std.heading CONTAINS CASE risk
                            WHEN 'VACCINATION_EXPIRED' THEN 'Campus Health'
                            WHEN 'CHRONIC_ATTENDANCE' THEN 'Attendance'
                            WHEN 'FEE_OVERDUE' THEN 'Finance'
                            ELSE 'Compliance' END
                        WITH rf, std LIMIT 1
                        MERGE (rf)-[:ADDRESSED_BY]->(std)
                        """,
                        sid=row["student_id"], risk=risk, school_id=row["school_id"],
                    )
                    risk_edges += 1
    finally:
        driver.close()
    return {"students_upserted": len(rows), "risk_edges": risk_edges}


# ---------------------------------------------------------------------------
# Webhook router (real-time sync hooks)
# ---------------------------------------------------------------------------
# Mirrors common LMS webhook envelopes:
#   {"event": "sis.student.updated", "data": {...}}
#   {"event": "enrollment.created", "data": {...}}
#   {"event": "sis.user.updated", "data": {...}}

_EVENT_ROUTES = {
    "sis.student.updated": "student",
    "sis.student.created": "student",
    "student.updated": "student",
    "student.created": "student",
    "sis.user.updated": "student",
    "user.updated": "staff_or_student",
    "sis.attendance.updated": "attendance",
    "attendance.updated": "attendance",
    "sis.fees.updated": "fee",
    "fee.updated": "fee",
    "sis.staff.updated": "staff",
    "staff.updated": "staff",
    "enrollment.updated": "student",
}


def handle_webhook(payload: dict[str, Any],
                   dialect: Optional[str] = None,
                   school_id: str = DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    """Route a webhook event into the appropriate SIS sync."""
    event = str(payload.get("event") or payload.get("type") or "").strip().lower()
    data = payload.get("data") or payload.get("object") or payload
    if not isinstance(data, dict):
        return {"status": "ERROR", "detail": "webhook data must be an object"}
    sid = str(payload.get("school_id") or data.get("school_id") or school_id)[:64]

    kind = _EVENT_ROUTES.get(event)
    if kind is None:
        # Infer from payload keys
        if any(k in data for k in ("attendance_date", "status", "type")) and (
                "student_id" in data or "source_student_id" in data):
            kind = "attendance"
        elif any(k in data for k in ("amount_due", "amount", "due_date", "dueDate")):
            kind = "fee"
        elif any(k in data for k in ("subjects", "max_periods", "gibbonStaffID")):
            kind = "staff"
        else:
            kind = "student"

    if kind == "student":
        result = sync_students([data], dialect=dialect, school_id=sid)
        # keep graph consistent for real-time student updates
        mapped = map_student(data, dialect, sid)
        graph_key = mapped.get("source_student_id")
        if graph_key:
            risks = compute_risk_flags(mapped)
            graph = sync_students_to_graph(
                [mapped], {graph_key: risks}, school_id=sid,
            )
            result["graph"] = graph
    elif kind == "staff_or_student":
        if "subjects" in data or "max_periods" in data:
            result = sync_staff([data], dialect=dialect, school_id=sid)
        else:
            result = sync_students([data], dialect=dialect, school_id=sid)
    elif kind == "attendance":
        result = sync_attendance([data], dialect=dialect, school_id=sid)
    elif kind == "fee":
        result = sync_fees([data], dialect=dialect, school_id=sid)
    elif kind == "staff":
        result = sync_staff([data], dialect=dialect, school_id=sid)
    else:
        result = {"status": "ERROR", "detail": f"unhandled event '{event}'"}

    return {
        "status": "OK",
        "event": event or "inferred",
        "routed_to": kind,
        "school_id": sid,
        "result": result,
    }


def batch_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Batched sync from a SIS export / nightly job.

    Accepts::
        {
          "dialect": "gibbon"|"openeducat"|"canvas"|null,
          "school_id": "default",
          "students": [...], "attendance": [...],
          "fees": [...], "staff": [...],
          "sync_graph": true
        }
    """
    dialect = payload.get("dialect")
    sid = str(payload.get("school_id") or DEFAULT_SCHOOL_ID)[:64]
    summary: dict[str, Any] = {
        "status": "OK",
        "dialect": dialect or "auto",
        "school_id": sid,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "syncs": {},
    }
    students = payload.get("students") or []
    if students:
        summary["syncs"]["students"] = sync_students(students, dialect=dialect, school_id=sid)
    attendance = payload.get("attendance") or []
    if attendance:
        summary["syncs"]["attendance"] = sync_attendance(attendance, dialect=dialect, school_id=sid)
    fees = payload.get("fees") or []
    if fees:
        summary["syncs"]["fees"] = sync_fees(fees, dialect=dialect, school_id=sid)
    staff = payload.get("staff") or []
    if staff:
        summary["syncs"]["staff"] = sync_staff(staff, dialect=dialect, school_id=sid)

    if payload.get("sync_graph", True) and students:
        mapped = [map_student(s, dialect, sid) for s in students]
        risk_map = {}
        for m in mapped:
            graph_key = m.get("source_student_id")
            if not graph_key:
                continue
            risk_map[graph_key] = compute_risk_flags(m)
        summary["syncs"]["graph"] = sync_students_to_graph(mapped, risk_map, school_id=sid)

    summary["finished_at"] = datetime.utcnow().isoformat() + "Z"
    return summary


# ---------------------------------------------------------------------------
# Schema health probe
# ---------------------------------------------------------------------------

def schema_status() -> dict[str, Any]:
    """Describe what the adapter can currently write to."""
    tables = {"students": False, "attendance_logs": False,
              "fee_ledgers": False, "staff": False}
    try:
        with get_pg_cursor() as (cur, _conn):
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN %s
                """,
                (tuple(tables.keys()),),
            )
            found = {r[0] for r in cur.fetchall()}
            for t in tables:
                tables[t] = t in found
    except Exception as exc:
        return {"status": "ERROR", "detail": str(exc)[:200], "tables": tables}
    return {
        "status": "OK" if all(tables.values()) else "DEGRADED",
        "tables": tables,
        "dialects_supported": list(STUDENT_FIELD_MAPS.keys()),
        "webhook_events": list(_EVENT_ROUTES.keys()),
    }
