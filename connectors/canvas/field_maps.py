"""Canvas-like webhook source field maps — moved from sis_adapter per C.1."""

from __future__ import annotations

STUDENT_FIELD_MAP = {
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
}

ATTENDANCE_FIELD_MAP = {
    "sis_user_id": "source_student_id",
    "attendance_date": "attendance_date",
    "status": "status",
}

FEE_FIELD_MAP = {
    "sis_user_id": "source_student_id",
    "amount_due": "amount_due",
    "amount_paid": "amount_paid",
    "due_date": "due_date",
    "status": "status",
    "days_overdue": "days_overdue",
}

STAFF_FIELD_MAP = {
    "sis_user_id": "source_staff_id",
    "name": "full_name",
    "email": "email",
    "subjects": "subjects",
    "is_available": "is_available",
    "max_periods": "max_periods",
    "assigned": "assigned",
}

DETECT_KEYS = frozenset({"sis_user_id"})
