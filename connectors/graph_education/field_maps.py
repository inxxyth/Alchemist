"""Microsoft Graph Education field maps (v1.0) — source property → canonical.

Shapes (see ``educationUser`` / ``educationSchool`` / ``educationClass`` on
learn.microsoft.com):

* ``educationUser``    — id, displayName, givenName, surname, mail,
                         primaryRole ("student" | "teacher"), studentId,
                         student{birthDate, gender, externalId, grade},
                         teacher{externalId}
* ``educationSchool``  — id, displayName, externalSource
* ``educationClass``   — id, displayName, classCode, externalName, externalId,
                         grade, course{displayName, subject}, term{displayName}
* class ``/members``   — educationUser subset with ``roles``

Mapping is deliberately tolerant of optional/deprecated property spellings
(``grade`` vs ``student.grade``) because Graph education shapes drift between
edu-licence SKUs — a missing property must degrade to a default, never crash.

Assignments/submissions are **out of scope for the compliance engine** —
see G.4; they are not mapped into the canonical five.
"""

from __future__ import annotations

STUDENT_FIELD_MAP = {
    "id": "source_student_id",
    "studentId": "sis_id",
    "givenName": "first_name",
    "surname": "last_name",
    "displayName": "display_name",
    "mail": "parent_email",          # roster has no parent column; refined later
    "primaryRole": "role",
    "grade": "grade_level",
}

STAFF_FIELD_MAP = {
    "id": "source_staff_id",
    "givenName": "first_name",
    "surname": "last_name",
    "displayName": "display_name",
    "mail": "email",
    "primaryRole": "role",
}

# educationUser.student (nested) → demographics
STUDENT_PROFILE_FIELD_MAP = {
    "birthDate": "date_of_birth",
    "gender": "gender",
    "externalId": "external_id",
    "grade": "grade_level",
}

ORG_FIELD_MAP = {
    "id": "source_org_id",
    "displayName": "school_name",
}

CLASS_FIELD_MAP = {
    "id": "source_class_id",
    "displayName": "class_title",
    "classCode": "class_code",
    "externalName": "external_name",
    "externalId": "external_id",
    "grade": "grade_level",
    "schoolId": "school_id_hint",
}

DETECT_KEYS = frozenset({"primaryRole", "studentId", "educationClass",
                         "givenName", "surname", "displayName"})

STUDENT_ROLES = frozenset({"student"})
STAFF_ROLES = frozenset({"teacher", "staff", "aide", "administrator"})

# Gender spellings seen across edu SKUs / SDS profiles.
_GENDER_MAP = {
    "m": "M", "male": "M",
    "f": "F", "female": "F",
}
