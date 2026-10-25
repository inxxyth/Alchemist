"""iSAMS REST API source field maps (Phase 4, N.1).

Source key → canonical key.  Pure data; no imports from sis_adapter so the
registry can load these without an import cycle.

iSAMS's public API surface is partner-gated (``developer.isams.com`` sits
behind OAuth) — these maps encode the **documented resource contract** the
connector is built to (see :mod:`connectors.isams.connector`): the resource
names below are the ones the engagement requires (students, staff, contacts,
attendance, behaviour, academic, finance) and the property names follow the
published iSAMS data model (PascalCase, with camelCase aliases accepted).
Confirming the exact property casing against a partner sandbox is a
field-map edit, not a connector rewrite.
"""

from __future__ import annotations

# Accept both "StudentId" and "studentId"-style properties.
def _alias(*names: str, dest: str) -> dict[str, str]:
    return {name: dest for name in names}


STUDENT_FIELD_MAP: dict[str, str] = {
    **_alias("StudentId", "Id", "studentId", dest="source_student_id"),
    **_alias("NationalId", "EmiratesId", "nationalId", dest="emirates_id"),
    **_alias("FirstName", "GivenName", "firstName", dest="first_name"),
    **_alias("Surname", "LastName", "FamilyName", "surname", dest="last_name"),
    **_alias("Gender", "gender", dest="gender"),
    **_alias("DateOfBirth", "Dob", "BirthDate", "dateOfBirth", dest="date_of_birth"),
    **_alias("NCYear", "YearGroup", "YearGroupId", "SchoolYear", "ncYear",
             dest="grade_level"),
    **_alias("Curriculum", "CurriculumTrack", dest="curriculum_track"),
    **_alias("SchoolName", "School", dest="school_name"),
    **_alias("Emirate", dest="emirate"),
    **_alias("Nationality", dest="nationality"),
    **_alias("VaccinationStatus", dest="vaccination_status"),
    **_alias("VaccinationExpiry", dest="vaccination_expiry"),
    **_alias("AdmissionDate", "StartDate", "EnrollmentDate", "admissionDate",
             dest="enrollment_date"),
    **_alias("IsActive", "Active", "isActive", dest="is_active"),
    **_alias("Status", dest="status"),
}

CONTACT_FIELD_MAP: dict[str, str] = {
    **_alias("ContactId", "Id", dest="source_contact_id"),
    **_alias("StudentId", "studentId", dest="source_student_id"),
    **_alias("FirstName", "GivenName", dest="first_name"),
    **_alias("Surname", "LastName", "FamilyName", dest="last_name"),
    **_alias("Email", "EmailAddress", dest="email"),
    **_alias("Mobile", "Phone", "PhoneNumber", "Telephone", dest="phone"),
    **_alias("Relationship", "Relation", dest="relationship"),
    **_alias("IsPrimaryContact", "PrimaryContact", dest="is_primary"),
}

ATTENDANCE_FIELD_MAP: dict[str, str] = {
    **_alias("StudentId", "studentId", dest="source_student_id"),
    **_alias("Date", "AttendanceDate", "date", dest="attendance_date"),
    **_alias("Code", "AttendanceCode", "Status", "AttendanceStatus", dest="status"),
    **_alias("Session", "Period", "PeriodName", dest="period"),
}

STAFF_FIELD_MAP: dict[str, str] = {
    **_alias("StaffId", "Id", "TeacherId", "staffId", dest="source_staff_id"),
    **_alias("FirstName", "GivenName", dest="first_name"),
    **_alias("Surname", "LastName", "FamilyName", dest="last_name"),
    **_alias("FullName", "DisplayName", "Name", dest="full_name"),
    **_alias("Email", "EmailAddress", dest="email"),
    **_alias("Role", "JobTitle", "role", dest="role"),
    **_alias("Subjects", "TeachingSubjects", dest="subjects"),
    **_alias("IsActive", "Active", "isActive", dest="is_available"),
    **_alias("Status", dest="status"),
}

FINANCE_FIELD_MAP: dict[str, str] = {
    **_alias("StudentId", "studentId", dest="source_student_id"),
    **_alias("InvoiceNumber", "InvoiceId", "Reference", "invoiceNumber",
             dest="invoice_number"),
    **_alias("Term", "TermName", "BillingPeriod", "AcademicTerm", dest="term_label"),
    **_alias("AmountDue", "Amount", "TotalDue", "amountDue", dest="amount_due"),
    **_alias("AmountPaid", "Paid", "TotalPaid", "amountPaid", dest="amount_paid"),
    **_alias("DueDate", "InvoiceDueDate", "dueDate", dest="due_date"),
    **_alias("Status", "InvoiceStatus", dest="status"),
    **_alias("DaysOverdue", "daysOverdue", dest="days_overdue"),
}

ACADEMIC_FIELD_MAP: dict[str, str] = {
    **_alias("StudentId", "studentId", dest="source_student_id"),
    **_alias("NCYear", "YearGroup", "YearGroupId", "SchoolYear", dest="grade_level"),
    **_alias("Term", "TermName", "AcademicTerm", dest="term_label"),
    **_alias("FormGroup", "Class", "TeachingGroup", dest="form_group"),
}

BEHAVIOUR_FIELD_MAP: dict[str, str] = {
    **_alias("StudentId", "studentId", dest="source_student_id"),
    **_alias("Date", "IncidentDate", dest="incident_date"),
    **_alias("Type", "Category", "BehaviourType", dest="incident_type"),
    **_alias("Points", "Severity", dest="points"),
    **_alias("Comment", "Notes", dest="comment"),
}

# Distinctive keys used for payload auto-detection.
DETECT_KEYS = frozenset(
    list(STUDENT_FIELD_MAP) + list(CONTACT_FIELD_MAP) + list(ATTENDANCE_FIELD_MAP)
    + list(STAFF_FIELD_MAP) + list(FINANCE_FIELD_MAP)
) | {"behaviour", "academic", "finance", "contacts", "attendance"}
