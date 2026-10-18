"""MSOE SIS seeder.

Connects to the local PostgreSQL container, rebuilds the three core SIS tables,
and injects 1,500 UAE-centric student profiles with deliberate edge-case flags.

Usage:
    python seed_sis.py
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg2
from faker import Faker
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "msoe_db",
    "user": "msoe_admin",
    "password": "msoe_secure_2026",
}

SEED = 20260215
NUM_STUDENTS = 1_500
ATTENDANCE_DAYS = 60  # school days of attendance history per student

random.seed(SEED)
fake = Faker("en_US")
Faker.seed(SEED)

# UAE-centric reference data -------------------------------------------------

FIRST_NAMES_MALE = [
    "Ahmed", "Mohammed", "Ali", "Omar", "Khalid", "Hassan", "Youssef", "Ibrahim",
    "Saeed", "Rashid", "Hamad", "Sultan", "Abdullah", "Faisal", "Mansour", "Zayed",
    "Tariq", "Bilal", "Karim", "Nasser", "Salem", "Jaber", "Fahad", "Majid",
    "Arjun", "Rohan", "Aditya", "Vivaan", "Kabir", "Rahul", "Sanjay", "Vikram",
    "Daniel", "James", "Oliver", "Ethan", "Lucas", "Adam", "Ryan", "Noah",
]

FIRST_NAMES_FEMALE = [
    "Fatima", "Aisha", "Maryam", "Noor", "Layla", "Hessa", "Maha", "Latifa",
    "Shamma", "Alia", "Sara", "Reem", "Dana", "Amna", "Hind", "Mouza",
    "Ananya", "Ishita", "Priya", "Neha", "Kavya", "Diya", "Meera", "Riya",
    "Emma", "Sophia", "Olivia", "Mia", "Zara", "Hannah", "Lily", "Grace",
]

FAMILY_NAMES = [
    "Al Maktoum", "Al Nahyan", "Al Qasimi", "Al Sharqi", "Al Nuaimi",
    "Al Suwaidi", "Al Falasi", "Al Marri", "Al Zaabi", "Al Dhaheri",
    "Al Mansoori", "Al Hameli", "Al Ketbi", "Al Rumaithi", "Al Blooshi",
    "Al Hosani", "Al Mazrouei", "Al Ameri", "Al Shamsi", "Al Tunaiji",
    "Khan", "Ahmed", "Ali", "Hussain", "Rahman", "Sheikh", "Malik", "Patel",
    "Sharma", "Gupta", "Reddy", "Iyer", "Nair", "Joseph", "Thomas", "Mathew",
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson",
]

SCHOOLS = [
    ("GEMS Wellington International School", "British", "Dubai"),
    ("GEMS Dubai American Academy", "IB", "Dubai"),
    ("GEMS Metropole School", "British", "Dubai"),
    ("GEMS Millennium School", "CBSE", "Dubai"),
    ("Taaleem Dubai British School", "British", "Dubai"),
    ("Taaleem Greenfield International School", "IB", "Dubai"),
    ("Taaleem Uptown International School", "IB", "Dubai"),
    ("Aldar Education Cranleigh Abu Dhabi", "British", "Abu Dhabi"),
    ("Aldar Education Yasmina British Academy", "British", "Abu Dhabi"),
    ("Aldar Education Raha International School", "IB", "Abu Dhabi"),
    ("ADEK Emirates National Schools", "MoE", "Abu Dhabi"),
    ("KHDA Dubai International Academy", "IB", "Dubai"),
    ("Sharjah American International School", "British", "Sharjah"),
    ("Ajman Academy", "MoE", "Ajman"),
    ("Ras Al Khaimah Academy", "British", "Ras Al Khaimah"),
]

CURRICULA = ["British", "IB", "CBSE", "MoE"]

VACCINATION_STATUSES = [
    "up_to_date",
    "up_to_date",
    "up_to_date",
    "up_to_date",
    "up_to_date",
    "partial",
    "expired",
    "missing",
]

FEE_STATUSES = ["current", "current", "current", "current", "current", "current", "overdue"]

CHRONIC_ATTENDANCE_RATE = Decimal("0.78")  # well under 85%
NORMAL_ATTENDANCE_LOW = Decimal("0.90")
NORMAL_ATTENDANCE_HIGH = Decimal("0.99")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
DROP TABLE IF EXISTS attendance_logs CASCADE;
DROP TABLE IF EXISTS fee_ledgers CASCADE;
DROP TABLE IF EXISTS students CASCADE;

CREATE TABLE students (
    student_id          BIGSERIAL PRIMARY KEY,
    emirates_id         VARCHAR(15) NOT NULL UNIQUE,
    first_name          VARCHAR(64) NOT NULL,
    last_name           VARCHAR(64) NOT NULL,
    gender              VARCHAR(10) NOT NULL,
    date_of_birth       DATE NOT NULL,
    grade_level         SMALLINT NOT NULL CHECK (grade_level BETWEEN 1 AND 12),
    curriculum_track    VARCHAR(16) NOT NULL
                        CHECK (curriculum_track IN ('British','IB','CBSE','MoE')),
    school_name         VARCHAR(128) NOT NULL,
    emirate             VARCHAR(32) NOT NULL,
    nationality         VARCHAR(64) NOT NULL,
    parent_full_name    VARCHAR(128) NOT NULL,
    parent_email        VARCHAR(128) NOT NULL,
    parent_phone        VARCHAR(20) NOT NULL,
    vaccination_status  VARCHAR(16) NOT NULL
                        CHECK (vaccination_status IN ('up_to_date','partial','expired','missing')),
    vaccination_expiry  DATE,
    enrollment_date     DATE NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE attendance_logs (
    attendance_id       BIGSERIAL PRIMARY KEY,
    student_id          BIGINT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    attendance_date     DATE NOT NULL,
    status              VARCHAR(16) NOT NULL
                        CHECK (status IN ('present','absent','late','excused')),
    minutes_present     SMALLINT NOT NULL DEFAULT 0,
    notes               VARCHAR(255),
    UNIQUE (student_id, attendance_date)
);

CREATE TABLE fee_ledgers (
    fee_id              BIGSERIAL PRIMARY KEY,
    student_id          BIGINT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    invoice_number      VARCHAR(32) NOT NULL UNIQUE,
    term_label          VARCHAR(32) NOT NULL,
    amount_due          NUMERIC(12,2) NOT NULL CHECK (amount_due >= 0),
    amount_paid         NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
    currency            VARCHAR(3) NOT NULL DEFAULT 'AED',
    due_date            DATE NOT NULL,
    paid_date           DATE,
    status              VARCHAR(16) NOT NULL
                        CHECK (status IN ('current','partial','overdue','paid','waived')),
    days_overdue        INTEGER NOT NULL DEFAULT 0,
    notes               VARCHAR(255)
);

CREATE INDEX idx_attendance_student_date ON attendance_logs (student_id, attendance_date);
CREATE INDEX idx_attendance_status ON attendance_logs (status);
CREATE INDEX idx_fees_student ON fee_ledgers (student_id);
CREATE INDEX idx_fees_status ON fee_ledgers (status);
CREATE INDEX idx_students_grade ON students (grade_level);
CREATE INDEX idx_students_curriculum ON students (curriculum_track);
"""


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def make_emirates_id() -> str:
    """UAE Emirates ID: 15 digits — 784 (country) + YYYY (birth year) + NNNNNNN (serial) + C (checksum)."""
    year = random.randint(1970, 2018)
    serial = random.randint(1000000, 9999999)
    checksum = random.randint(0, 9)
    return f"784{year:04d}{serial:07d}{checksum}"


def make_phone() -> str:
    prefix = random.choice(["+97150", "+97152", "+97154", "+97155", "+97156", "+97158"])
    return f"{prefix}{random.randint(1000000, 9999999)}"


def make_parent_email(first: str, last: str, idx: int) -> str:
    slug_last = last.lower().replace(" ", ".")
    slug_first = first.lower().replace(" ", ".")
    domain = random.choice(
        ["gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "eim.ae"]
    )
    pattern = random.choice(
        [
            f"{slug_first}.{slug_last}{idx % 97}@{domain}",
            f"{slug_first}{idx % 1000}@{domain}",
            f"{slug_last}.family{idx % 50}@{domain}",
        ]
    )
    return pattern.replace("..", ".")


def make_date_of_birth(grade: int) -> date:
    # Typical age for grade N in UAE schools: ~5 + grade, with some variance
    age = 5 + grade + random.choice([-1, 0, 0, 0, 1])
    birth_year = date.today().year - age
    return date(birth_year, random.randint(1, 12), random.randint(1, 28))


def make_enrollment_date(dob: date) -> date:
    earliest = dob + timedelta(days=5 * 365)
    latest = date.today() - timedelta(days=30)
    if earliest >= latest:
        earliest = dob + timedelta(days=4 * 365)
    span = (latest - earliest).days
    return earliest + timedelta(days=random.randint(0, max(span, 1)))


def school_days_back(count: int) -> list[date]:
    """Return `count` weekday dates ending today (exclusive of weekends)."""
    days: list[date] = []
    cursor = date.today() - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:  # Mon-Fri
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def build_students() -> list[dict]:
    # Pre-assign edge-case cohorts so the ratios are exact
    n = NUM_STUDENTS
    n_vax_issue = round(n * 0.05)       # ~5%
    n_fee_overdue = round(n * 0.08)     # ~8%
    n_chronic_att = round(n * 0.10)     # ~10%

    vax_flag_ids = set(random.sample(range(n), n_vax_issue))
    fee_flag_ids = set(random.sample(range(n), n_fee_overdue))
    att_flag_ids = set(random.sample(range(n), n_chronic_att))

    students: list[dict] = []
    for i in range(n):
        gender = random.choice(["male", "female"])
        if gender == "male":
            first = random.choice(FIRST_NAMES_MALE)
        else:
            first = random.choice(FIRST_NAMES_FEMALE)
        last = random.choice(FAMILY_NAMES)

        grade = random.randint(1, 12)
        school_name, curriculum, emirate = random.choice(SCHOOLS)
        # Occasionally override curriculum to broaden distribution
        if random.random() < 0.35:
            curriculum = random.choice(CURRICULA)

        dob = make_date_of_birth(grade)
        enrollment = make_enrollment_date(dob)

        nationality = random.choices(
            population=[
                "Emirati", "Indian", "Pakistani", "British", "Egyptian",
                "Filipino", "Jordanian", "Lebanese", "American", "Canadian",
                "Syrian", "Palestinian", "Sudanese", "Omani", "Saudi",
            ],
            weights=[22, 20, 12, 8, 7, 6, 5, 4, 4, 3, 3, 3, 2, 1, 1],
        )[0]

        # Vaccination edge case (~5%)
        if i in vax_flag_ids:
            vax_status = random.choice(["expired", "missing", "partial"])
            if vax_status == "missing":
                vax_expiry = None
            else:
                vax_expiry = date.today() - timedelta(days=random.randint(1, 400))
        else:
            vax_status = "up_to_date"
            vax_expiry = date.today() + timedelta(days=random.randint(30, 900))

        parent_first = random.choice(FIRST_NAMES_MALE + FIRST_NAMES_FEMALE)
        parent_last = last  # same family
        parent_email = make_parent_email(parent_first, parent_last, i)

        students.append(
            {
                "idx": i,
                "emirates_id": make_emirates_id(),
                "first_name": first,
                "last_name": last,
                "gender": gender,
                "date_of_birth": dob,
                "grade_level": grade,
                "curriculum_track": curriculum,
                "school_name": school_name,
                "emirate": emirate,
                "nationality": nationality,
                "parent_full_name": f"{parent_first} {parent_last}",
                "parent_email": parent_email,
                "parent_phone": make_phone(),
                "vaccination_status": vax_status,
                "vaccination_expiry": vax_expiry,
                "enrollment_date": enrollment,
                "is_active": True,
                "fee_overdue": i in fee_flag_ids,
                "chronic_attendance": i in att_flag_ids,
            }
        )

    return students


def build_attendance(students: list[dict]) -> list[tuple]:
    rows: list[tuple] = []
    calendar = school_days_back(ATTENDANCE_DAYS)

    for s in students:
        if s["chronic_attendance"]:
            # Target <85% attendance rate. Late still counts as attended,
            # so only use absent/excused for non-attended days.
            target_rate = float(CHRONIC_ATTENDANCE_RATE) + random.uniform(-0.05, 0.03)
        else:
            target_rate = random.uniform(float(NORMAL_ATTENDANCE_LOW), float(NORMAL_ATTENDANCE_HIGH))

        target_present = max(1, round(ATTENDANCE_DAYS * target_rate))
        absent_slots = ATTENDANCE_DAYS - target_present

        # Choose which days are non-present
        non_present_days = set(random.sample(range(ATTENDANCE_DAYS), min(absent_slots, ATTENDANCE_DAYS)))

        for day_idx, att_date in enumerate(calendar):
            if day_idx in non_present_days:
                if s["chronic_attendance"]:
                    # Late counts as attended — avoid it so rate stays <85%
                    status = random.choices(["absent", "excused"], weights=[0.70, 0.30])[0]
                else:
                    status = random.choices(
                        ["absent", "late", "excused"], weights=[0.55, 0.25, 0.20]
                    )[0]
            else:
                status = "present"

            if status == "present":
                minutes = random.randint(340, 400)
                notes = None
            elif status == "late":
                minutes = random.randint(120, 330)
                notes = random.choice(
                    ["Arrived after morning registration", "Traffic delay", "Medical appointment"]
                )
            elif status == "excused":
                minutes = 0
                notes = random.choice(
                    ["Approved family leave", "School trip", "Medical certificate provided"]
                )
            else:  # absent
                minutes = 0
                notes = random.choice(
                    ["Unexcused absence", "Parent reported illness", "No contact from guardian"]
                )

            rows.append((s["student_id"], att_date, status, minutes, notes))

    return rows


def build_fees(students: list[dict]) -> list[tuple]:
    rows: list[tuple] = []
    today = date.today()

    terms = [
        ("Term 1 2025-2026", date(2025, 9, 1), date(2025, 12, 15)),
        ("Term 2 2025-2026", date(2026, 1, 5), date(2026, 3, 20)),
        ("Term 3 2025-2026", date(2026, 4, 7), date(2026, 6, 30)),
    ]

    for s in students:
        # Tuition band by grade (AED)
        base = 18000 + s["grade_level"] * 1500 + random.randint(-1500, 4000)
        base = max(base, 12000)

        for term_idx, (term_label, term_start, due_default) in enumerate(terms, start=1):
            amount_due = Decimal(base / 3).quantize(Decimal("0.01"))
            invoice = f"INV-{s['student_id']:05d}-{term_idx}"

            # Fee edge case (~8%) → term 3 is overdue >60 days
            if s["fee_overdue"] and term_idx == 3:
                due_date = today - timedelta(days=random.randint(61, 140))
                days_overdue = (today - due_date).days
                amount_paid = Decimal("0.00")
                paid_date = None
                status = "overdue"
                notes = "Payment delay >60 days — escalated to finance"
            elif not s["fee_overdue"] and random.random() < 0.12:
                # Partial payment — keep due date current so it never trips >60 overdue
                due_date = today + timedelta(days=random.randint(5, 45))
                amount_paid = (amount_due * Decimal(str(random.uniform(0.2, 0.7)))).quantize(Decimal("0.01"))
                days_overdue = 0
                paid_date = None
                status = "partial"
                notes = "Instalment plan active"
            elif s["fee_overdue"] and term_idx == 2 and random.random() < 0.30:
                # Flagged cohort may also show a mild partial on term 2 (under 60 days)
                due_date = today - timedelta(days=random.randint(10, 55))
                days_overdue = (today - due_date).days
                amount_paid = (amount_due * Decimal("0.35")).quantize(Decimal("0.01"))
                paid_date = None
                status = "partial" if days_overdue <= 60 else "overdue"
                notes = "Instalment plan active"
            elif random.random() < 0.70:
                due_date = due_default
                amount_paid = amount_due
                paid_date = min(due_default, today) - timedelta(days=random.randint(0, 20))
                days_overdue = 0
                status = "paid"
                notes = None
            else:
                # Unpaid but not late — future-dated invoice
                due_date = today + timedelta(days=random.randint(5, 45))
                amount_paid = Decimal("0.00")
                paid_date = None
                days_overdue = 0
                status = "current"
                notes = None

            rows.append(
                (
                    s["student_id"],
                    invoice,
                    term_label,
                    amount_due,
                    amount_paid,
                    "AED",
                    due_date,
                    paid_date,
                    status,
                    days_overdue,
                    notes,
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 72)
    print("MSOE SIS SEEDER")
    print("=" * 72)
    print(f"Target: {DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    print(f"Students to generate: {NUM_STUDENTS}")
    print()

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False
    except psycopg2.OperationalError as exc:
        print(f"[ERROR] Could not connect to PostgreSQL: {exc}")
        return 1

    cur = conn.cursor()

    try:
        print("[1/4] Dropping and recreating schema ...")
        cur.execute(DDL)
        conn.commit()
        print("      OK — tables: students, attendance_logs, fee_ledgers")

        print("[2/4] Generating student profiles ...")
        students = build_students()
        student_rows = [
            (
                s["emirates_id"],
                s["first_name"],
                s["last_name"],
                s["gender"],
                s["date_of_birth"],
                s["grade_level"],
                s["curriculum_track"],
                s["school_name"],
                s["emirate"],
                s["nationality"],
                s["parent_full_name"],
                s["parent_email"],
                s["parent_phone"],
                s["vaccination_status"],
                s["vaccination_expiry"],
                s["enrollment_date"],
                s["is_active"],
            )
            for s in students
        ]
        execute_values(
            cur,
            """
            INSERT INTO students (
                emirates_id, first_name, last_name, gender, date_of_birth,
                grade_level, curriculum_track, school_name, emirate, nationality,
                parent_full_name, parent_email, parent_phone,
                vaccination_status, vaccination_expiry, enrollment_date, is_active
            ) VALUES %s
            RETURNING student_id
            """,
            student_rows,
            page_size=len(student_rows),
        )
        ids = [r[0] for r in cur.fetchall()]
        if len(ids) != len(students):
            raise RuntimeError(f"ID fetch mismatch: got {len(ids)} ids for {len(students)} students")
        for s, sid in zip(students, ids):
            s["student_id"] = sid
        conn.commit()
        print(f"      OK — inserted {len(ids)} students")

        print("[3/4] Generating attendance logs ...")
        att_rows = build_attendance(students)
        execute_values(
            cur,
            """
            INSERT INTO attendance_logs (
                student_id, attendance_date, status, minutes_present, notes
            ) VALUES %s
            """,
            att_rows,
            page_size=5_000,
        )
        conn.commit()
        print(f"      OK — inserted {len(att_rows)} attendance logs")

        print("[4/4] Generating fee ledger entries ...")
        fee_rows = build_fees(students)
        execute_values(
            cur,
            """
            INSERT INTO fee_ledgers (
                student_id, invoice_number, term_label, amount_due, amount_paid,
                currency, due_date, paid_date, status, days_overdue, notes
            ) VALUES %s
            """,
            fee_rows,
            page_size=5_000,
        )
        conn.commit()
        print(f"      OK — inserted {len(fee_rows)} fee ledger entries")

    except Exception as exc:
        conn.rollback()
        print(f"[ERROR] Seeding failed: {exc}")
        raise
    finally:
        cur.close()
        conn.close()

    # ---- Verification report ------------------------------------------------
    print()
    print("=" * 72)
    print("VERIFICATION REPORT")
    print("=" * 72)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    def report(title: str, sql: str) -> None:
        cur.execute(sql)
        rows = cur.fetchall()
        print(f"\n{title}")
        for row in rows:
            print(f"  {row}")

    report("Table counts:", """
        SELECT 'students' AS tbl, COUNT(*) FROM students
        UNION ALL
        SELECT 'attendance_logs', COUNT(*) FROM attendance_logs
        UNION ALL
        SELECT 'fee_ledgers', COUNT(*) FROM fee_ledgers
    """)

    report("Edge-case: vaccination issues (~5% expected):", """
        SELECT vaccination_status, COUNT(*),
               ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
        FROM students
        GROUP BY vaccination_status
        ORDER BY vaccination_status
    """)

    report("Edge-case: fee overdue >60 days (~8% expected):", """
        SELECT COUNT(DISTINCT s.student_id) AS students_flagged,
               ROUND(100.0 * COUNT(DISTINCT s.student_id) / (SELECT COUNT(*) FROM students), 2) AS pct
        FROM students s
        JOIN fee_ledgers f ON f.student_id = s.student_id
        WHERE f.status = 'overdue' AND f.days_overdue > 60
    """)

    report("Edge-case: chronic attendance <85% (~10% expected):", """
        SELECT COUNT(*) AS students_below_85,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM students), 2) AS pct
        FROM (
            SELECT student_id,
                   ROUND(100.0 * SUM(CASE WHEN status IN ('present','late') THEN 1 ELSE 0 END)
                         / COUNT(*), 2) AS attendance_rate
            FROM attendance_logs
            GROUP BY student_id
            HAVING SUM(CASE WHEN status IN ('present','late') THEN 1 ELSE 0 END)::decimal
                   / COUNT(*) < 0.85
        ) t
    """)

    report("Attendance status distribution:", """
        SELECT status, COUNT(*) FROM attendance_logs GROUP BY status ORDER BY status
    """)

    report("Fee status distribution:", """
        SELECT status, COUNT(*) FROM fee_ledgers GROUP BY status ORDER BY status
    """)

    report("Grade level distribution:", """
        SELECT grade_level, COUNT(*) FROM students GROUP BY grade_level ORDER BY grade_level
    """)

    report("Curriculum track distribution:", """
        SELECT curriculum_track, COUNT(*) FROM students
        GROUP BY curriculum_track ORDER BY curriculum_track
    """)

    report("Sample students (5):", """
        SELECT student_id, emirates_id, first_name, last_name, grade_level,
               curriculum_track, school_name, parent_email, vaccination_status
        FROM students ORDER BY student_id LIMIT 5
    """)

    cur.close()
    conn.close()

    print()
    print("=" * 72)
    print("SEED COMPLETE — no errors.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
