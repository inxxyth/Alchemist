"""OneRoster 1.2 field maps (CSV bulk binding + REST/JSON binding).

Column headers and REST property names are the same identifiers in OneRoster
1.2 (``sourcedId``, ``givenName``, …), so one map serves both bindings.

Required-column sets follow the 1EdTech OneRoster 1.2 CSV bulk set:
  orgs.csv, users.csv, classes.csv, courses.csv, enrollments.csv,
  demographics.csv

Grade/term handling is calendar-agnostic (see ``connectors.base``):
UAE schools run Aug–Jun and some run Sep–Jul — no US-season assumptions.

Rostering gives us students, staff and grade/class context.  Attendance and
fees are NOT in the OneRoster rostering bulk set (they live in Gradebook /
a school's finance system) — those entity lists stay empty for this
connector rather than being invented.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Required headers per file (1EdTech OneRoster 1.2 CSV bulk set)
# ---------------------------------------------------------------------------

REQUIRED_HEADERS: dict[str, tuple[str, ...]] = {
    "orgs": ("sourcedId", "status", "name", "type"),
    "users": ("sourcedId", "status", "givenName", "familyName", "role"),
    "classes": ("sourcedId", "status", "title", "courseSourcedId"),
    "courses": ("sourcedId", "status", "title"),
    "enrollments": ("sourcedId", "status", "classSourcedId", "userSourcedId", "role"),
    "demographics": ("sourcedId", "status", "birthDate", "sex"),
}

# ---------------------------------------------------------------------------
# Source key → canonical key
# ---------------------------------------------------------------------------

# users.csv (role=student) → students
STUDENT_FIELD_MAP = {
    "sourcedId": "source_student_id",
    "givenName": "first_name",
    "familyName": "last_name",
    "middleName": "middle_name",
    "email": "parent_email",          # no parent column in OneRoster; refined by agents
    "grade": "grade_level",
    "identifier": "emirates_id",      # national identifier where the school maps it here
    "enabledUser": "is_active",
    "orgSourcedIds": "org_sourced_ids",
}

# users.csv (role=teacher) → staff
STAFF_FIELD_MAP = {
    "sourcedId": "source_staff_id",
    "givenName": "first_name",
    "familyName": "last_name",
    "email": "email",
    "enabledUser": "is_available",
    "orgSourcedIds": "org_sourced_ids",
}

# demographics.csv → student demographics
DEMOGRAPHICS_FIELD_MAP = {
    "birthDate": "date_of_birth",
    "sex": "gender",
}

# orgs.csv → school identity
ORG_FIELD_MAP = {
    "sourcedId": "source_org_id",
    "name": "school_name",
    "type": "org_type",
    "identifier": "org_identifier",
    "parentSourcedId": "parent_org_id",
}

# classes.csv → class context (attendance context + grade_level source)
CLASS_FIELD_MAP = {
    "sourcedId": "source_class_id",
    "title": "class_title",
    "courseSourcedId": "course_id",
    "classType": "class_type",
    "schoolSourcedIds": "school_ids",
    "termSourcedIds": "term_ids",
    "subjects": "subjects",
    "periods": "periods",
}

# courses.csv → course/grade context
COURSE_FIELD_MAP = {
    "sourcedId": "source_course_id",
    "title": "course_title",
    "courseCode": "course_code",
    "grades": "grades",
    "subjects": "subjects",
    "orgSourcedIds": "org_sourced_ids",
}

# enrollments.csv → student↔class links (grade/attendance context)
ENROLLMENT_FIELD_MAP = {
    "sourcedId": "source_enrollment_id",
    "classSourcedId": "class_id",
    "schoolSourcedId": "school_id",
    "userSourcedId": "user_id",
    "role": "role",
    "beginDate": "begin_date",
    "endDate": "end_date",
}

FILE_FIELD_MAPS: dict[str, dict[str, str]] = {
    "orgs": ORG_FIELD_MAP,
    "users": STUDENT_FIELD_MAP,
    "classes": CLASS_FIELD_MAP,
    "courses": COURSE_FIELD_MAP,
    "enrollments": ENROLLMENT_FIELD_MAP,
    "demographics": DEMOGRAPHICS_FIELD_MAP,
}

# Distinctive keys for payload auto-detection.
DETECT_KEYS = frozenset({"sourcedId", "classSourcedId", "userSourcedId",
                         "givenName", "familyName"})

# OneRoster role vocabulary (case-insensitive match on the role field).
STUDENT_ROLES = frozenset({"student"})
STAFF_ROLES = frozenset({"teacher", "aide", "administrator", "proctor",
                         "parent", "guardian", "relative", "adult"})

# Columns that must NEVER be treated as free-text secrets in a manifest or log.
_NOISY_COLUMNS = ("password", "passwordHash", "passwordSalt")
