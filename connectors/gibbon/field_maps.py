"""Gibbon (v31) source field maps — moved from sis_adapter per C.1.

Source key → canonical key.  Pure data; no imports from sis_adapter so the
registry can load these without an import cycle.
"""

from __future__ import annotations

STUDENT_FIELD_MAP = {
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
}

ATTENDANCE_FIELD_MAP = {
    "gibbonPersonID": "source_student_id",
    "date": "attendance_date",
    # gibbonAttendanceCode.name ("Present", "Present - Late", "Absent", ...).
    # The raw gibbonAttendanceLogPerson.type column is Core/Additional, not a status.
    "name": "status",
}

FEE_FIELD_MAP = {
    "gibbonPersonID": "source_student_id",
    "amount": "amount_due",
    "amount_paid": "amount_paid",
    "dueDate": "due_date",
    "status": "status",
    "days_overdue": "days_overdue",
}

STAFF_FIELD_MAP = {
    "gibbonStaffID": "source_staff_id",
    "fullName": "full_name",
    "email": "email",
    "subjects": "subjects",
    "is_available": "is_available",
    "max_periods": "max_periods",
    "assigned": "assigned",
}

# Distinctive keys used for payload auto-detection.
DETECT_KEYS = frozenset(STUDENT_FIELD_MAP) | {"gibbonStaffID", "gibbonAttendanceCodeID"}
