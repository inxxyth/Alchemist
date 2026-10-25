"""OpenEduCat-like source field maps — moved from sis_adapter per C.1."""

from __future__ import annotations

STUDENT_FIELD_MAP = {
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
}

ATTENDANCE_FIELD_MAP = {
    "student_id": "source_student_id",
    "attendance_date": "attendance_date",
    "status": "status",
}

FEE_FIELD_MAP = {
    "student_id": "source_student_id",
    "amount_due": "amount_due",
    "amount_paid": "amount_paid",
    "due_date": "due_date",
    "status": "status",
    "days_overdue": "days_overdue",
}

STAFF_FIELD_MAP = {
    "id": "source_staff_id",
    "full_name": "full_name",
    "email": "email",
    "subjects": "subjects",
    "is_available": "is_available",
    "max_periods": "max_periods",
    "assigned": "assigned",
}

# 'guardian_name' is distinctive; 'id' is too generic to detect on alone.
DETECT_KEYS = frozenset({"guardian_name", "guardian_email", "emirates_id"})
