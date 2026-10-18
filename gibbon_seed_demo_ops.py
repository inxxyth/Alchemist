"""Seed minimal Gibbon attendance + finance demo rows so gibbon_bridge has data.

Both target tables (gibbonAttendanceLogPerson, gibbonFinanceInvoice*) are empty
in this stack, so every INSERT is additive — nothing existing is modified or
deleted. Re-runnable: skips seeding when rows already exist.

Usage:
  python gibbon_seed_demo_ops.py
  python gibbon_seed_demo_ops.py --force   # re-seed even if rows exist
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys

import pymysql

GIBBON_MYSQL = {
    "host": os.environ.get("GIBBON_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("GIBBON_MYSQL_PORT", "3306")),
    "user": os.environ.get("GIBBON_MYSQL_USER", "gibbon"),
    "password": os.environ.get("GIBBON_MYSQL_PASSWORD", "msoe_gibbon_2026"),
    "database": os.environ.get("GIBBON_MYSQL_DB", "gibbon"),
    "connect_timeout": 8,
    "charset": "utf8mb4",
}


def conn():
    return pymysql.connect(**GIBBON_MYSQL)


def pick(cur, sql: str, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def seed_attendance(cur, student_ids: list[int], taker_id: int, days: int, rng: random.Random) -> int:
    # Gibbon core attendance codes (gibbon.sql seed): 001 Present, 002 Present - Late,
    # 004 Absent, 005 Left. Weights favour present.
    codes = [(1, 0.80), (2, 0.10), (4, 0.08), (5, 0.02)]
    code_ids = [c for c, _ in codes]
    weights = [w for _, w in codes]

    today = dt.date.today()
    rows = []
    for sid in student_ids:
        for offset in range(days):
            day = today - dt.timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            code = rng.choices(code_ids, weights=weights, k=1)[0]
            direction = "In" if code in (1, 2) else "Out"
            rows.append((code, sid, direction, day, taker_id))

    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO gibbonAttendanceLogPerson ("
        "gibbonAttendanceCodeID, gibbonPersonID, direction, type, reason, context, "
        "comment, date, gibbonPersonIDTaker, timestampTaken"
        ") VALUES (%s, %s, %s, 'Core', '', 'Form Group', '', %s, %s, NOW())",
        rows,
    )
    return len(rows)


def seed_finance(cur, creator_id: int, rng: random.Random) -> dict[str, int]:
    counts = {"categories": 0, "fees": 0, "invoicees": 0, "invoices": 0, "invoice_fees": 0}

    cur.execute(
        "INSERT INTO gibbonFinanceFeeCategory (name, nameShort, description, active, "
        "gibbonPersonIDCreator, timestampCreator) "
        "SELECT * FROM (SELECT 'Tuition' AS a, 'TUI' AS b, 'Term tuition (MSOE demo)' AS c, "
        "'Y' AS d, %s AS e, NOW() AS f) AS chk "
        "WHERE NOT EXISTS (SELECT 1 FROM gibbonFinanceFeeCategory WHERE nameShort='TUI')",
        (creator_id,),
    )
    counts["categories"] += cur.rowcount
    cur.execute("SELECT gibbonFinanceFeeCategoryID FROM gibbonFinanceFeeCategory WHERE nameShort='TUI' LIMIT 1")
    cat_id = cur.fetchone()[0]

    # One fee per school year present in gibbonSchoolYear.
    years = pick(cur, "SELECT gibbonSchoolYearID FROM gibbonSchoolYear ORDER BY gibbonSchoolYearID")
    for (year_id,) in years:
        cur.execute(
            "INSERT INTO gibbonFinanceFee (gibbonSchoolYearID, name, nameShort, description, "
            "active, gibbonFinanceFeeCategoryID, fee, gibbonPersonIDCreator, timestampCreator) "
            "SELECT * FROM (SELECT %s AS y, 'Tuition Fee' AS n, 'TUIT' AS s, "
            "'Annual tuition (MSOE demo)' AS d, 'Y' AS a, %s AS c, 18000.00 AS f, %s AS p, NOW() AS t) AS chk "
            "WHERE NOT EXISTS (SELECT 1 FROM gibbonFinanceFee WHERE gibbonSchoolYearID=%s AND nameShort='TUIT')",
            (year_id, cat_id, creator_id, year_id),
        )
        counts["fees"] += cur.rowcount

    # Families that have both an adult (invoicee) and at least one child (student).
    families = pick(
        cur,
        """
        SELECT DISTINCT fa.gibbonFamilyID, fa.gibbonPersonID AS adult_id
        FROM gibbonFamilyAdult fa
        JOIN gibbonFamilyChild fc ON fc.gibbonFamilyID = fa.gibbonFamilyID
        LIMIT 25
        """,
    )
    today = dt.date.today()
    for family_id, adult_id in families:
        cur.execute(
            "INSERT INTO gibbonFinanceInvoicee (gibbonPersonID, invoiceTo) VALUES (%s, 'Family')",
            (adult_id,),
        )
        invoicee_id = cur.lastrowid
        counts["invoicees"] += 1

        due = today + dt.timedelta(days=rng.choice([-30, -10, 14, 30]))
        days_overdue = max(0, (today - due).days)
        if days_overdue > 60:
            status, paid_amount = "Paid - Partial", 0.0
        elif days_overdue > 0:
            status, paid_amount = "Issued", 0.0
        elif rng.random() < 0.4:
            status, paid_amount = "Paid", 18000.0
        else:
            status, paid_amount = "Issued", 0.0

        cur.execute(
            "INSERT INTO gibbonFinanceInvoice (gibbonSchoolYearID, gibbonFinanceInvoiceeID, "
            "invoiceTo, billingScheduleType, status, invoiceIssueDate, invoiceDueDate, "
            "paidDate, paidAmount, notes, `key`, gibbonPersonIDCreator, timestampCreator) "
            "VALUES ((SELECT MAX(gibbonSchoolYearID) FROM gibbonSchoolYear), %s, 'Family', "
            "'Ad Hoc', %s, %s, %s, %s, %s, 'MSOE demo invoice', MD5(RAND()), %s, NOW())",
            (invoicee_id, status, today - dt.timedelta(days=45), due,
             (today if status == "Paid" else None), paid_amount, creator_id),
        )
        invoice_id = cur.lastrowid
        counts["invoices"] += 1

        cur.execute(
            "INSERT INTO gibbonFinanceInvoiceFee (gibbonFinanceInvoiceID, feeType, "
            "gibbonFinanceFeeID, name, description, gibbonFinanceFeeCategoryID, fee, sequenceNumber) "
            "SELECT %s, 'Standard', gibbonFinanceFeeID, name, description, "
            "gibbonFinanceFeeCategoryID, fee, 1 FROM gibbonFinanceFee WHERE nameShort='TUIT' LIMIT 1",
            (invoice_id,),
        )
        counts["invoice_fees"] += cur.rowcount

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Gibbon demo attendance + finance rows")
    parser.add_argument("--force", action="store_true", help="seed even if rows already exist")
    parser.add_argument("--students", type=int, default=60)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM gibbonAttendanceLogPerson")
                att_existing = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM gibbonFinanceInvoice")
                inv_existing = cur.fetchone()[0]
                if (att_existing or inv_existing) and not args.force:
                    print(f"[seed] skipped: attendance={att_existing} invoices={inv_existing} (use --force)")
                    return 0

                students = [r[0] for r in pick(
                    cur,
                    "SELECT gibbonPersonID FROM gibbonPerson WHERE gibbonRoleIDPrimary=003 "
                    "AND status='Full' ORDER BY gibbonPersonID LIMIT %s",
                    (args.students,),
                )]
                takers = [r[0] for r in pick(
                    cur,
                    "SELECT gibbonPersonID FROM gibbonPerson WHERE gibbonRoleIDPrimary=002 "
                    "AND status='Full' ORDER BY gibbonPersonID LIMIT 1",
                )]
                if not students or not takers:
                    print("[seed] FAILED: no students/teachers found in gibbonPerson", file=sys.stderr)
                    return 1
                taker_id = takers[0]

                att = seed_attendance(cur, students, taker_id, args.days, rng)
                fin = seed_finance(cur, creator_id=taker_id, rng=rng)
            c.commit()
    except Exception as exc:
        print(f"[seed] FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"[seed] attendance rows inserted: {att}")
    print(f"[seed] finance: {fin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
