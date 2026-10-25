"""PowerSchool REST source field maps (Phase 4, N.2).

Only the resources **OneRoster 1.2 cannot carry** live here: attendance and
fees.  Rostering (students/staff) rides the OneRoster profile — see
:mod:`connectors.powerschool.connector` and
:mod:`connectors.oneroster.rest_binding` — so these maps cover the
``/ws/v1/attendance`` and ``/ws/v1/fees`` shapes of the PowerSchool REST API.

Source key → canonical key.  Pure data; no imports from sis_adapter so the
registry can load these without an import cycle.  The property names follow
the published PowerSchool REST resource model (camelCase).  Confirming exact
casing against a partner sandbox is a field-map edit, not a connector rewrite.
"""

from __future__ import annotations


def _alias(*names: str, dest: str) -> dict[str, str]:
    return {name: dest for name in names}


# GET {POWERSCHOOL_BASE_URL}/ws/v1/attendance
ATTENDANCE_FIELD_MAP: dict[str, str] = {
    **_alias("studentId", "student_reference", "StudentId", dest="source_student_id"),
    **_alias("date", "attendanceDate", "attendance_date", dest="attendance_date"),
    **_alias("attendanceCode", "code", "status", "attendanceStatus",
             dest="status"),
    **_alias("periodId", "period", "periodName", "sectionId", dest="period"),
}

# GET {POWERSCHOOL_BASE_URL}/ws/v1/fees
FEE_FIELD_MAP: dict[str, str] = {
    **_alias("studentId", "student_reference", "StudentId", dest="source_student_id"),
    **_alias("feeId", "invoiceNumber", "reference", "feeName",
             dest="invoice_number"),
    **_alias("term", "termName", "billingPeriod", "academicTerm",
             dest="term_label"),
    **_alias("amountDue", "amount", "totalDue", "balance", dest="amount_due"),
    **_alias("amountPaid", "paid", "totalPaid", dest="amount_paid"),
    **_alias("dueDate", "invoiceDueDate", dest="due_date"),
    **_alias("status", "feeStatus", "invoiceStatus", dest="status"),
    **_alias("daysOverdue", "overdueDays", dest="days_overdue"),
}

# Distinctive keys used for payload auto-detection.
DETECT_KEYS = frozenset(
    list(ATTENDANCE_FIELD_MAP) + list(FEE_FIELD_MAP)
) | {"attendance", "fees", "feeTransactions", "attendanceRecords"}
