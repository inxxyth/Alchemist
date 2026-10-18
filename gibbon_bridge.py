"""Gibbon SIS → MSOE bridge.

Maps Gibbon (MySQL) tables into the MSOE relational store and knowledge
graph via :mod:`sis_adapter`:

  gibbonPerson              → students / staff
  gibbonAttendanceLogPerson → attendance_logs
  gibbonFinanceInvoice      → fee_ledgers  (best-effort)
  gibbonStaff               → staff

Usage:
  python gibbon_bridge.py --full
  python gibbon_bridge.py --students-only
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import pymysql

import sis_adapter

GIBBON_MYSQL = {
    "host": os.environ.get("GIBBON_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("GIBBON_MYSQL_PORT", "3306")),
    "user": os.environ.get("GIBBON_MYSQL_USER", "gibbon"),
    "password": os.environ.get("GIBBON_MYSQL_PASSWORD", "msoe_gibbon_2026"),
    "database": os.environ.get("GIBBON_MYSQL_DB", "gibbon"),
    "connect_timeout": 8,
    "charset": "utf8mb4",
}

# Client-configurable identity literals (F.8 — env with demo defaults).
_ORG_NAME = os.environ.get("MSOE_SCHOOL_NAME", "MSOE Academy Dubai")
_ORG_EMIRATE = os.environ.get("MSOE_EMIRATE", "Dubai")
_ORG_CURRICULUM = os.environ.get("MSOE_CURRICULUM", "British")
_ORG_PARENT_EMAIL = os.environ.get("MSOE_PARENT_EMAIL", "parent@msoe.ae")
_ORG_PARENT_PHONE = os.environ.get("MSOE_PARENT_PHONE", "+971500000000")


def gibbon_conn():
    return pymysql.connect(**GIBBON_MYSQL)


def fetch_students(limit: int = 5000) -> list[dict[str, Any]]:
    # Gibbon v31: role is gibbonRoleIDPrimary (Student=003); grade via
    # gibbonStudentEnrolment → gibbonYearGroup.sequenceNumber.
    # Emits source_student_id (gibbonPersonID) — the surrogate PK is assigned
    # by the MSOE students table, not by the source system.
    sql = f"""
        SELECT * FROM (
            SELECT
                p.gibbonPersonID AS source_student_id,
                COALESCE(NULLIF(p.studentID, ''), '') AS emirates_id,
                p.firstName AS first_name,
                p.surname AS last_name,
                CASE p.gender WHEN 'M' THEN 'M' WHEN 'F' THEN 'F' ELSE 'U' END AS gender,
                COALESCE(p.dob, '2015-01-01') AS date_of_birth,
                COALESCE(MAX(yg.sequenceNumber), 7) AS grade_level_raw,
                '{_ORG_CURRICULUM}' AS curriculum_track,
                '{_ORG_NAME}' AS school_name,
                '{_ORG_EMIRATE}' AS emirate,
                COALESCE(MAX(NULLIF(p.address1Country, '')), 'UAE') AS nationality,
                COALESCE(MAX(NULLIF(p.emailAlternate, '')), 'Parent/Guardian') AS parent_full_name,
                COALESCE(MAX(NULLIF(p.email, '')), '{_ORG_PARENT_EMAIL}') AS parent_email,
                COALESCE(MAX(NULLIF(p.phone1, '')), '{_ORG_PARENT_PHONE}') AS parent_phone,
                'up_to_date' AS vaccination_status,
                MAX(DATE(p.lastTimestamp)) AS enrollment_date
            FROM gibbonPerson p
            LEFT JOIN gibbonStudentEnrolment se
                   ON se.gibbonPersonID = p.gibbonPersonID
            LEFT JOIN gibbonYearGroup yg
                   ON yg.gibbonYearGroupID = se.gibbonYearGroupID
            WHERE p.gibbonRoleIDPrimary = 003
              AND p.status = 'Full'
            GROUP BY p.gibbonPersonID, p.firstName, p.surname, p.gender, p.dob, p.studentID
        ) t
        LIMIT %s
    """
    rows: list[dict[str, Any]] = []
    with gibbon_conn() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (limit,))
            for r in cur.fetchall():
                raw = r.pop("grade_level_raw", 7)
                try:
                    r["grade_level"] = min(12, max(1, int(str(raw).strip() or "7")))
                except Exception:
                    r["grade_level"] = 7
                rows.append(r)
    return rows


def fetch_staff(limit: int = 500) -> list[dict[str, Any]]:
    sql = """
        SELECT
            p.gibbonPersonID AS source_staff_id,
            CONCAT(p.firstName, ' ', p.surname) AS full_name,
            COALESCE(NULLIF(p.email, ''),
                     CONCAT('staff', p.gibbonPersonID, '@msoe.ae')) AS email,
            'teacher' AS role,
            'Mathematics' AS subjects,
            1 AS is_available,
            6 AS max_periods,
            0 AS assigned
        FROM gibbonPerson p
        WHERE p.gibbonRoleIDPrimary = 002
          AND p.status = 'Full'
        LIMIT %s
    """
    with gibbon_conn() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (limit,))
            return list(cur.fetchall())


def fetch_attendance(limit: int = 5000) -> list[dict[str, Any]]:
    """Attendance pull from gibbonAttendanceLogPerson + gibbonAttendanceCode.

    Gibbon's ``type`` column on the log is Core/Additional — the status lives on
    ``gibbonAttendanceCode.name`` ("Present", "Present - Late", "Absent", ...).
    Emits gibbon-shaped keys so :func:`sis_adapter.map_attendance` can dialect-map
    them (``gibbonPersonID`` / ``date`` / ``name``).
    """
    sql = """
        SELECT
            l.gibbonPersonID AS gibbonPersonID,
            l.date AS date,
            COALESCE(c.name, 'Absent') AS name
        FROM gibbonAttendanceLogPerson l
        LEFT JOIN gibbonAttendanceCode c
               ON c.gibbonAttendanceCodeID = l.gibbonAttendanceCodeID
        WHERE l.date IS NOT NULL
        ORDER BY l.date DESC
        LIMIT %s
    """
    try:
        with gibbon_conn() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, (limit,))
                return list(cur.fetchall())
    except Exception as exc:
        print(f"[bridge] attendance pull skipped: {exc}")
        return []


def fetch_fees(limit: int = 5000) -> list[dict[str, Any]]:
    """Best-effort finance pull: gibbonFinanceInvoice → student fee ledger rows.

    Gibbon invoices are addressed to an invoicee (parent/guardian). Each invoice
    fans out to that family's children as one fee row per student. Emits
    gibbon-shaped keys for :func:`sis_adapter.map_fee`.
    """
    sql = """
        SELECT
            fc.gibbonPersonID AS gibbonPersonID,
            CONCAT('INV-', inv.gibbonFinanceInvoiceID) AS invoice_number,
            COALESCE(bs.name, 'Term 1') AS term_label,
            COALESCE(SUM(invf.fee), 0) AS amount,
            COALESCE(inv.paidAmount, 0) AS amount_paid,
            COALESCE(inv.invoiceDueDate, CURDATE()) AS dueDate,
            inv.status AS status,
            GREATEST(DATEDIFF(CURDATE(), inv.invoiceDueDate), 0) AS days_overdue
        FROM gibbonFinanceInvoice inv
        JOIN gibbonFinanceInvoicee ie
               ON ie.gibbonFinanceInvoiceeID = inv.gibbonFinanceInvoiceeID
        JOIN gibbonFamilyAdult fa
               ON fa.gibbonPersonID = ie.gibbonPersonID
        JOIN gibbonFamilyChild fc
               ON fc.gibbonFamilyID = fa.gibbonFamilyID
        LEFT JOIN gibbonFinanceInvoiceFee invf
               ON invf.gibbonFinanceInvoiceID = inv.gibbonFinanceInvoiceID
        LEFT JOIN gibbonFinanceBillingSchedule bs
               ON bs.gibbonFinanceBillingScheduleID = inv.gibbonFinanceBillingScheduleID
        GROUP BY inv.gibbonFinanceInvoiceID, fc.gibbonPersonID,
                 inv.paidAmount, inv.invoiceDueDate, inv.status, bs.name
        ORDER BY inv.invoiceDueDate DESC
        LIMIT %s
    """
    try:
        with gibbon_conn() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, (limit,))
                return list(cur.fetchall())
    except Exception as exc:
        print(f"[bridge] fees pull skipped: {exc}")
        return []


def map_students_to_canonical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append({
            "source_student_id": r.get("source_student_id"),
            "emirates_id": r.get("emirates_id"),
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "gender": r.get("gender"),
            "date_of_birth": r.get("date_of_birth"),
            "grade_level": r.get("grade_level"),
            "curriculum_track": r.get("curriculum_track"),
            "school_name": r.get("school_name"),
            "emirate": r.get("emirate"),
            "nationality": r.get("nationality"),
            "parent_full_name": r.get("parent_full_name"),
            "parent_email": r.get("parent_email"),
            "parent_phone": r.get("parent_phone"),
            "vaccination_status": r.get("vaccination_status"),
            "enrollment_date": r.get("enrollment_date"),
        })
    return out


def sync_all(students_only: bool = False,
             school_id: str = sis_adapter.DEFAULT_SCHOOL_ID) -> dict[str, Any]:
    result: dict[str, Any] = {"source": "gibbon", "school_id": school_id, "syncs": {}}
    students = map_students_to_canonical(fetch_students())
    result["syncs"]["students"] = sis_adapter.sync_students(
        students, dialect="gibbon", school_id=school_id)
    if not students_only:
        staff = fetch_staff()
        result["syncs"]["staff"] = sis_adapter.sync_staff(
            staff, dialect="gibbon", school_id=school_id)
        att = fetch_attendance()
        if att:
            result["syncs"]["attendance"] = sis_adapter.sync_attendance(
                att, dialect="gibbon", school_id=school_id)
        fees = fetch_fees()
        if fees:
            result["syncs"]["fees"] = sis_adapter.sync_fees(
                fees, dialect="gibbon", school_id=school_id)
    # Mirror into Neo4j with derived risk flags from MSOE metrics
    risk_map: dict[str, list[str]] = {}
    mapped = [sis_adapter.map_student(s, "gibbon", school_id) for s in students]
    for m in mapped:
        key = m.get("source_student_id")
        if key:
            risk_map[key] = sis_adapter.compute_risk_flags(m)
    result["syncs"]["graph"] = sis_adapter.sync_students_to_graph(
        mapped, risk_map, school_id=school_id)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Gibbon → MSOE bridge")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--students-only", action="store_true")
    args = parser.parse_args()
    try:
        result = sync_all(students_only=args.students_only)
    except Exception as exc:
        print(f"[bridge] FAILED: {exc}", file=sys.stderr)
        return 1
    import json
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
