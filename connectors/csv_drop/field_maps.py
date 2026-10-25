"""CSV drop column contract (Phase 4, N.4).

The Tier 3 fallback for "our IT won't do an API" (INTEGRATION-PLAN.md §3):
a directory the school's IT drops CSV files into.  This module is the
**documented column contract** the files are validated against — hand it to
the school's IT lead as-is.

File names (one entity per file; anything else in the directory is ignored
and logged):

    students.csv        required: source_student_id, first_name, last_name
    attendance.csv      required: source_student_id, attendance_date, status
    fees.csv            required: source_student_id, invoice_number, amount_due
    staff.csv           required: source_staff_id, full_name, email
    leave_requests.csv  required: source_staff_id, leave_date, periods

Optional columns are accepted on each file (listed below); unknown columns
are ignored and logged, never guessed at.  A file missing a required column
is **rejected whole** (logged with file + column); a row missing a required
value is **rejected alone** (logged with file + line + column).  Nothing
malformed is ever partially ingested.

``school_id`` is stamped by the connector from its own config — never read
from the file.  A ``school_id`` column in a drop file is ignored and logged,
so a drop cannot address another school's rows.

Columns are already canonical names: the contract's point is that IT fills a
template, not that we translate another SIS's headers.
"""

from __future__ import annotations

# entity key → (required columns, optional columns)
COLUMN_CONTRACT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "students": (
        ("source_student_id", "first_name", "last_name"),
        ("emirates_id", "gender", "date_of_birth", "grade_level",
         "curriculum_track", "school_name", "emirate", "nationality",
         "parent_full_name", "parent_email", "parent_phone",
         "vaccination_status", "vaccination_expiry", "enrollment_date",
         "is_active"),
    ),
    "attendance": (
        ("source_student_id", "attendance_date", "status"),
        (),
    ),
    "fees": (
        ("source_student_id", "invoice_number", "amount_due"),
        ("amount_paid", "term_label", "due_date", "status", "days_overdue"),
    ),
    "staff": (
        ("source_staff_id", "full_name", "email"),
        ("role", "subjects", "is_available", "max_periods", "assigned"),
    ),
    "leave_requests": (
        ("source_staff_id", "leave_date", "periods"),
        ("reason", "subject_needed"),
    ),
}

# File name (lowercase) → entity key.  Anything else in the drop directory is
# ignored and logged — a dropped "final_roster_v2.csv" must not be guessed at.
FILE_TO_ENTITY: dict[str, str] = {
    "students.csv": "students",
    "attendance.csv": "attendance",
    "fees.csv": "fees",
    "staff.csv": "staff",
    "leave_requests.csv": "leave_requests",
}

# Columns the connector always stamps itself; a drop file cannot supply them.
STAMPED_COLUMNS = ("school_id",)

# Distinctive keys used for payload auto-detection (webhook shape).
DETECT_KEYS = frozenset(
    {col for req, opt in COLUMN_CONTRACT.values() for col in req + opt}
) | set(FILE_TO_ENTITY) | {"rows", "file"}
