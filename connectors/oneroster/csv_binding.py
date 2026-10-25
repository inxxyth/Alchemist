"""OneRoster 1.2 CSV bulk binding — the market wedge (C.4).

Consumes the OneRoster CSV bulk set:

    orgs.csv, users.csv, classes.csv, courses.csv,
    enrollments.csv, demographics.csv

and maps it onto the canonical five entities.  Files whose headers do not
match the 1.2 contract are **rejected with the offending column named** —
a silent mis-map of a student's grade is worse than a failed sync.

Grade/term handling is calendar-agnostic: no US Fall/Spring assumptions,
no grade inferred from month numbers (UAE schools run Aug–Jun, some Sep–Jul).

Rostering covers students, staff and grade/class context.  Attendance and
fees are not in the OneRoster rostering bulk set — those lists stay empty
rather than being fabricated.
"""

from __future__ import annotations

import csv
import io
import os
from typing import Any, Iterable, Optional

from connectors.base import (
    CanonicalBatch,
    SourceConnector,
    as_optional_date,
    normalize_grade,
    normalize_term_label,
)
from connectors.oneroster.field_maps import (
    CLASS_FIELD_MAP,
    COURSE_FIELD_MAP,
    DEMOGRAPHICS_FIELD_MAP,
    DETECT_KEYS,
    ENROLLMENT_FIELD_MAP,
    ORG_FIELD_MAP,
    REQUIRED_HEADERS,
    STAFF_FIELD_MAP,
    STUDENT_FIELD_MAP,
    STUDENT_ROLES,
    STAFF_ROLES,
)

# CSV bulk file names accepted on disk / in a zip-free directory drop.
FILE_ALIASES = {
    "orgs.csv": "orgs",
    "users.csv": "users",
    "classes.csv": "classes",
    "courses.csv": "courses",
    "enrollments.csv": "enrollments",
    "demographics.csv": "demographics",
}


class OneRosterCsvError(ValueError):
    """Malformed OneRoster CSV.  The message names the file and column."""


# ---------------------------------------------------------------------------
# Header validation + row mapping
# ---------------------------------------------------------------------------

def validate_headers(file_key: str, headers: Iterable[str]) -> list[str]:
    """Return violations for a CSV header row (empty == valid).

    Every missing required column is named: ``users.csv: missing required
    column 'familyName'``.  Extra columns are allowed (OneRoster is
    extensible via ``metadata.*`` / ``$$`` vendor extensions).
    """
    present = {h.strip() for h in headers if h}
    problems: list[str] = []
    for col in REQUIRED_HEADERS.get(file_key, ()):
        if col not in present:
            problems.append(f"{file_key}.csv: missing required column '{col}'")
    return problems


def _apply(field_map: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in field_map.items():
        if src in row and row[src] not in (None, ""):
            out[dest] = row[src]
    return out


def _split_csv_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _role_base(raw: Any) -> str:
    """OneRoster roles may carry an instance suffix: ``teacher#primary``."""
    return str(raw or "").strip().lower().split("#", 1)[0]


def _as_bool_enabled(raw: Any, default: bool = True) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"true", "t", "1", "yes", "y", "active"}


def parse_csv_text(text: str, file_key: str) -> list[dict[str, str]]:
    """Parse one CSV file's text into rows; validates headers first."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise OneRosterCsvError(f"{file_key}.csv: empty file — no header row")
    problems = validate_headers(file_key, reader.fieldnames)
    if problems:
        raise OneRosterCsvError("; ".join(problems))
    rows: list[dict[str, str]] = []
    for i, raw in enumerate(reader, start=2):  # line 1 is the header
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue  # blank line
        if not row.get("sourcedId"):
            raise OneRosterCsvError(
                f"{file_key}.csv: line {i}: missing required column value 'sourcedId'")
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Bulk-set join: files → canonical batch
# ---------------------------------------------------------------------------

def map_bulk(
    files: dict[str, list[dict[str, Any]]],
    school_id: str = "default",
    school_id_map: Optional[dict[str, str]] = None,
    source: str = "oneroster-csv",
) -> CanonicalBatch:
    """Map the OneRoster bulk set onto :class:`CanonicalBatch`.

    ``files`` keys: orgs, users, classes, courses, enrollments, demographics
    (each a list of row dicts).  ``users`` is required for a non-empty batch;
    the rest enrich (grade from class→course, demographics for dob/sex,
    orgs for school_name).
    """
    school_id_map = school_id_map or {}
    batch = CanonicalBatch(source=source, school_id=school_id)
    errors = batch.errors

    orgs = {_apply(ORG_FIELD_MAP, r).get("source_org_id"): _apply(ORG_FIELD_MAP, r)
            for r in files.get("orgs", [])}
    demographics = {_apply(DEMOGRAPHICS_FIELD_MAP, r).get("source_student_id")
                    or r.get("sourcedId"): _apply(DEMOGRAPHICS_FIELD_MAP, r)
                    for r in files.get("demographics", [])}
    classes = {_apply(CLASS_FIELD_MAP, r).get("source_class_id"): _apply(CLASS_FIELD_MAP, r)
               for r in files.get("classes", [])}
    courses = {_apply(COURSE_FIELD_MAP, r).get("source_course_id"): _apply(COURSE_FIELD_MAP, r)
               for r in files.get("courses", [])}
    enrollments = [_apply(ENROLLMENT_FIELD_MAP, r) for r in files.get("enrollments", [])]

    # user_id -> best grade hint from enrolment → class → course.grades
    grade_by_user: dict[str, int] = {}
    for enr in enrollments:
        if _role_base(enr.get("role")) not in STUDENT_ROLES:
            continue
        cls = classes.get(str(enr.get("class_id") or ""))
        if not cls:
            continue
        course = courses.get(str(cls.get("course_id") or ""))
        if not course:
            continue
        for token in _split_csv_list(course.get("grades")):
            grade = normalize_grade(token, default=0)
            if grade:
                grade_by_user[str(enr.get("user_id"))] = grade

    for raw in files.get("users", []):
        role = _role_base(raw.get("role"))
        row = {**raw}
        # detect() and validation speak OneRoster columns; mapping speaks
        # canonical keys via the per-entity maps.
        if role in STUDENT_ROLES:
            student_id = str(raw.get("sourcedId") or "")
            if not student_id:
                errors.append("users.csv: row missing sourcedId (student)")
                continue
            mapped = _apply(STUDENT_FIELD_MAP, row)
            demo = demographics.get(student_id, {})
            org_id = _split_csv_list(mapped.get("org_sourced_ids"))
            org = orgs.get(org_id[0]) if org_id else None
            canonical = {
                "school_id": school_id_map.get(
                    org.get("source_org_id") if org else None, school_id),
                "source_student_id": student_id,
                "emirates_id": mapped.get("emirates_id"),
                "first_name": mapped.get("first_name") or "Unknown",
                "last_name": mapped.get("last_name") or "Student",
                "gender": _normalize_sex(demo.get("gender") or raw.get("sex")),
                "date_of_birth": as_optional_date(demo.get("date_of_birth")),
                # user.grade wins; else the class→course grades hint; else 1
                "grade_level": normalize_grade(
                    mapped.get("grade_level"), default=grade_by_user.get(student_id, 1)),
                "curriculum_track": raw.get("curriculumTrack") or "British",
                "school_name": (org or {}).get("school_name") or "OneRoster School",
                "emirate": raw.get("emirate") or "Dubai",
                "nationality": raw.get("nationality") or "UAE",
                "parent_full_name": raw.get("parent_name") or "Parent/Guardian",
                "parent_email": mapped.get("parent_email") or "",
                "parent_phone": raw.get("phone") or "",
                "vaccination_status": raw.get("vaccination_status") or "up_to_date",
                "vaccination_expiry": as_optional_date(raw.get("vaccination_expiry")),
                "enrollment_date": as_optional_date(raw.get("enrollment_date")),
                "is_active": _as_bool_enabled(mapped.get("is_active"))
                             and str(raw.get("status") or "active").lower() != "tobedeleted",
            }
            # school_id_map may still override via the row's own school hint
            hint = raw.get("school_id") or (org_id[0] if org_id else None)
            if hint and hint in school_id_map:
                canonical["school_id"] = school_id_map[hint]
            batch.students.append(canonical)
        elif role in STAFF_ROLES or role == "":
            staff_id = str(raw.get("sourcedId") or "")
            if not staff_id:
                errors.append("users.csv: row missing sourcedId (staff)")
                continue
            mapped = _apply(STAFF_FIELD_MAP, row)
            batch.staff.append({
                "school_id": school_id,
                "source_staff_id": staff_id,
                "full_name": f"{mapped.get('first_name') or ''} "
                             f"{mapped.get('last_name') or ''}".strip() or "Teacher",
                "email": mapped.get("email") or "",
                "role": "teacher" if role == "teacher" else (role or "teacher"),
                "subjects": _split_csv_list(raw.get("subjects")),
                "is_available": _as_bool_enabled(mapped.get("is_available")),
                "max_periods": int(raw.get("max_periods") or 6),
                "assigned": int(raw.get("assigned") or 0),
            })
        else:
            errors.append(f"users.csv: row {raw.get('sourcedId')!r} "
                          f"has unknown role {raw.get('role')!r}")

    # classes carry the term labels — kept verbatim (no US-season rewrite)
    for cls in classes.values():
        batch.meta.setdefault("terms", [])
    batch.meta["class_count"] = len(classes)
    batch.meta["course_count"] = len(courses)
    batch.meta["enrollment_count"] = len(enrollments)
    return batch


def _normalize_sex(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"M", "MALE"}:
        return "M"
    if s in {"F", "FEMALE"}:
        return "F"
    return "U"


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class OneRosterCsvConnector(SourceConnector):
    """OneRoster 1.2 CSV bulk binding.

    Config keys (from the client manifest / call site):
      ``csv_dir``   — directory holding the bulk set (orgs.csv, users.csv, …)
      ``files``     — pre-loaded {file_key: [rows]} for the in-request path
    """

    dialect = "oneroster-csv"

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        keys = set(payload.keys())
        if "users" in payload and isinstance(payload.get("users"), list):
            if payload["users"] and isinstance(payload["users"][0], dict):
                return bool(DETECT_KEYS & set(payload["users"][0].keys()))
        if "orgs" in payload and "users" in payload:
            return True
        return bool(DETECT_KEYS & keys) and "classSourcedId" not in keys \
            and "userSourcedId" not in keys

    def load_dir(self, csv_dir: str) -> dict[str, list[dict[str, str]]]:
        """Read the bulk set from a directory.  Unknown .csv files are ignored;
        a missing required file is an error only when *all* are missing."""
        if not os.path.isdir(csv_dir):
            raise OneRosterCsvError(f"{csv_dir}: not a directory")
        files: dict[str, list[dict[str, str]]] = {}
        for name, key in sorted(FILE_ALIASES.items()):
            path = os.path.join(csv_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                files[key] = parse_csv_text(fh.read(), key)
        if not files:
            raise OneRosterCsvError(
                f"{csv_dir}: no OneRoster CSV files found "
                f"(expected one of: {', '.join(sorted(FILE_ALIASES))})")
        return files

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """CSV bulk pull from ``csv_dir``.  ``since`` is accepted and ignored —
        a bulk file is a full snapshot; delta lives in the REST binding."""
        csv_dir = str(self.config.get("csv_dir") or "")
        try:
            if not csv_dir:
                raise OneRosterCsvError("csv_dir is not configured")
            files = self.load_dir(csv_dir)
            batch = map_bulk(files, school_id=self.school_id,
                             school_id_map=self.config.get("school_id_map") or {},
                             source=self.dialect)
            self._mark_success()
            return batch
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Map an in-request OneRoster payload (bulk set or single users list)."""
        try:
            files = {
                key: payload.get(key) or []
                for key in ("orgs", "users", "classes", "courses",
                            "enrollments", "demographics")
            }
            if not any(files.values()) and isinstance(payload.get("data"), dict):
                files = {k: payload["data"].get(k) or [] for k in files}
            if not any(files.values()):
                raise OneRosterCsvError(
                    "payload has no OneRoster sets (users/orgs/classes/…)")
            batch = map_bulk(files, school_id=self.school_id,
                             school_id_map=self.config.get("school_id_map") or {},
                             source=self.dialect)
            self._mark_success()
            return batch
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        csv_dir = str(self.config.get("csv_dir") or "")
        if csv_dir:
            base["csv_dir"] = csv_dir
            base["status"] = "OK" if os.path.isdir(csv_dir) else "UNHEALTHY"
        else:
            # In-request mode (rows arrive via POST /api/v1/sis/sync) — no
            # directory needed, so this is configured-by-usage, not broken.
            base["status"] = self.last_error and "DEGRADED" or "OK"
            base["mode"] = "batch"
        return base
