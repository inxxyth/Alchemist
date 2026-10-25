"""iSAMS native connector (Phase 4, N.1) — strongest UAE market signal.

iSAMS is the cloud MIS running through the British/IB belt in the Emirates
(INTEGRATION-PLAN.md §2–3).  This connector is the **Tier 2 native** route:
OAuth2 against iSAMS's identity host, rostering + the data OneRoster cannot
carry (attendance, finance).

Live-sandbox status
-------------------
The iSAMS developer portal (``developer.isams.com``) is **partner-gated** —
no sandbox credentials were available at build time.  The connector is built
to the documented request/response contract below and verified against
synthetic fixtures (``fixtures/isams/``) with an injected transport.  The
live-sandbox check is recorded BLOCKED in ``IMPLEMENTATION-LEDGER.md`` with
that reason.  Nothing here invents a live result.

Request/response contract
-------------------------
Auth: OAuth2 client-credentials (``grant_type=client_credentials``) against
``https://identity.isams.com/connect/token``.  Env var NAMES (never values):

    ISAMS_BASE_URL    — API root (e.g. https://{school}.isams.com/api)
    ISAMS_CLIENT_ID   — client id from the iSAMS integration marketplace
    ISAMS_CLIENT_SECRET
    ISAMS_TENANT      — tenant/school identifier (token audience + school map)

Resources (``GET {ISAMS_BASE_URL}/{resource}``, Bearer token, JSON):

    students   → {"students":   [ {StudentId, FirstName, Surname, …} ]}
    staff      → {"staff":      [ {StaffId, FirstName, Surname, …} ]}
    contacts   → {"contacts":   [ {ContactId, StudentId, Email, …} ]}
    attendance → {"attendance": [ {StudentId, Date, Code, Session, …} ]}
    behaviour  → {"behaviour":  [ {StudentId, Date, Type, …} ]}
    academic   → {"academic":   [ {StudentId, NCYear, Term, …} ]}
    finance    → {"finance":    [ {StudentId, InvoiceNumber, AmountDue, …} ]}

Paging: ``limit``/``offset`` and/or RFC 5988 ``Link`` ``rel="next"``.
Delta: ``?since=`` (ISO-8601) where the provider offers it — ignored
otherwise, and the run watermark is still recorded so a second pull is
incremental.  Timeout + retry with backoff; 401/403 → connector
``UNHEALTHY``, core services stay up (same degrade rule as DeterministicLLM).

Canonical mapping (the five entities)
-------------------------------------
students   → Student   (+ contacts join for parent/guardian fields)
staff      → Staff
attendance → Attendance  **period rows collapse to one row per student per
             day** (``attendance_logs`` keys on (student_id, date)) with a
             documented severity order: ``absent > excused > late > present``
finance    → Fee
behaviour  → **no canonical entity exists**.  The engine's five entities
             carry no behaviour record and one is NOT fabricated; incident
             counts land in ``batch.meta['behaviour']`` for risk scoring.
academic   → grade/term context only (enriches Student.grade_level and Fee
             .term_label; no rows of its own)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Callable, Optional

from connectors.base import (
    CanonicalBatch,
    SourceConnector,
    as_optional_date,
    normalize_grade,
    normalize_term_label,
)
from connectors.isams.field_maps import (
    ACADEMIC_FIELD_MAP,
    ATTENDANCE_FIELD_MAP,
    BEHAVIOUR_FIELD_MAP,
    CONTACT_FIELD_MAP,
    FINANCE_FIELD_MAP,
    STAFF_FIELD_MAP,
    STUDENT_FIELD_MAP,
)
from connectors.state import StateStore, default_store

# iSAMS identity host — the OAuth2 token endpoint (not a data resource).
IDENTITY_TOKEN_URL = "https://identity.isams.com/connect/token"

DEFAULT_SCOPE = "api"
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RETRIES = 3

# Resource → (list key, field map).  Walked by pull(); behaviour/academic are
# context-only (see module docstring).
RESOURCES = (
    ("students", "students", STUDENT_FIELD_MAP),
    ("staff", "staff", STAFF_FIELD_MAP),
    ("contacts", "contacts", CONTACT_FIELD_MAP),
    ("attendance", "attendance", ATTENDANCE_FIELD_MAP),
    ("behaviour", "behaviour", BEHAVIOUR_FIELD_MAP),
    ("academic", "academic", ACADEMIC_FIELD_MAP),
    ("finance", "finance", FINANCE_FIELD_MAP),
)

ENVELOPE_KEYS = {
    "students": ("students", "student", "Students"),
    "staff": ("staff", "Staff", "teachers"),
    "contacts": ("contacts", "Contacts", "parents"),
    "attendance": ("attendance", "Attendance", "attendanceRecords"),
    "behaviour": ("behaviour", "Behaviour", "behaviourIncidents"),
    "academic": ("academic", "Academic", "enrolments", "enrollments"),
    "finance": ("finance", "Finance", "invoices", "feeTransactions"),
}

DELTA_STATE_KEY = "delta.since"

# Day-collapse severity for period-level attendance (worst status wins).
_SEVERITY = {"present": 0, "late": 1, "excused": 2, "absent": 3}

# iSAMS-style single-letter attendance codes (documented code set).  Unknown
# codes fall through to sis_adapter._normalize_status — never guessed.
_ATT_CODE = {
    "P": "present", "PRESENT": "present",
    "L": "late", "LT": "late", "LATE": "late",
    "N": "absent", "A": "absent", "ABS": "absent", "ABSENT": "absent",
    "M": "excused", "MED": "excused", "MEDICAL": "excused",
    "E": "excused", "EXC": "excused", "EXCUSED": "excused",
    "C": "late",  # late after registration closes
}

Transport = Callable[..., tuple[int, dict[str, str], bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      timeout: float, body: Optional[bytes] = None,
                      ) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body or None, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


class ISAMSError(RuntimeError):
    """iSAMS transport failed after retries, or the envelope broke the contract."""


# ---------------------------------------------------------------------------
# Mapping: iSAMS resource rows → canonical batch
# ---------------------------------------------------------------------------

def _apply(field_map: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in field_map.items():
        if src in row and row[src] not in (None, ""):
            out[dest] = row[src]
    return out


def _as_bool(raw: Any, default: bool = True) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"true", "t", "1", "yes", "y", "active"}


def _normalize_gender(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"M", "MALE", "BOY"}:
        return "M"
    if s in {"F", "FEMALE", "GIRL"}:
        return "F"
    return "U"


def _is_active_from(row: dict[str, Any], mapped: dict[str, Any]) -> bool:
    """IsActive flag wins; else Status string; else active (iSAMS default)."""
    if "is_active" in mapped:
        return _as_bool(mapped.get("is_active"), default=True)
    status = str(mapped.get("status") or row.get("Status") or "").strip().lower()
    if status in {"inactive", "left", "leaver", "deleted", "off", "withdrawn"}:
        return False
    return True


def map_isams_objects(
    students: list[dict[str, Any]],
    staff: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    behaviour: list[dict[str, Any]],
    academic: list[dict[str, Any]],
    finance: list[dict[str, Any]],
    school_id: str = "default",
    school_id_map: Optional[dict[str, str]] = None,
    source: str = "isams",
) -> CanonicalBatch:
    """Map iSAMS resource objects onto :class:`CanonicalBatch`.

    Pure mapping — no I/O, no secret handling.  Attendance/finance are the
    reason this connector exists (OneRoster rostering carries neither).
    """
    from sis_adapter import _normalize_status  # late import: sis_adapter loads our field maps

    school_id_map = school_id_map or {}
    batch = CanonicalBatch(source=source, school_id=school_id)
    errors = batch.errors

    def resolve_school(hint: Any) -> str:
        if hint is not None and str(hint) in school_id_map:
            return school_id_map[str(hint)]
        return school_id

    # -- academic: grade + term context (no rows of its own) -----------------
    grade_by_student: dict[str, int] = {}
    term_seen: dict[str, bool] = {}
    for raw in academic:
        mapped = _apply(ACADEMIC_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if sid and mapped.get("grade_level"):
            grade = normalize_grade(mapped["grade_level"], default=0)
            if grade:
                grade_by_student[sid] = grade
        if mapped.get("term_label"):
            term_seen[normalize_term_label(mapped["term_label"])] = True
    term_labels = list(term_seen)

    # -- contacts: parent/guardian fields on the student ---------------------
    parent_by_student: dict[str, dict[str, Any]] = {}
    for raw in contacts:
        mapped = _apply(CONTACT_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if not sid:
            errors.append("contacts: row missing StudentId")
            continue
        parent = {
            "parent_full_name": " ".join(filter(None, [
                str(mapped.get("first_name") or ""),
                str(mapped.get("last_name") or ""),
            ])).strip() or "Parent/Guardian",
            "parent_email": str(mapped.get("email") or ""),
            "parent_phone": str(mapped.get("phone") or ""),
        }
        # Primary contacts win; the first contact is the fallback.
        if _as_bool(mapped.get("is_primary"), default=False) or sid not in parent_by_student:
            if _as_bool(mapped.get("is_primary"), default=False) or not parent_by_student.get(sid):
                parent_by_student[sid] = parent

    # -- students ------------------------------------------------------------
    for raw in students:
        mapped = _apply(STUDENT_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if not sid:
            errors.append("students: row missing StudentId")
            continue
        parent = parent_by_student.get(sid, {})
        batch.students.append({
            "school_id": resolve_school(raw.get("SchoolId") or raw.get("schoolId")),
            "source_student_id": sid,
            # National id only when the source really carries one (F.4).
            "emirates_id": mapped.get("emirates_id") or None,
            "first_name": mapped.get("first_name") or "Unknown",
            "last_name": mapped.get("last_name") or "Student",
            "gender": _normalize_gender(mapped.get("gender")),
            "date_of_birth": as_optional_date(mapped.get("date_of_birth")),
            "grade_level": normalize_grade(
                mapped.get("grade_level"), default=grade_by_student.get(sid, 1)),
            "curriculum_track": raw.get("Curriculum") or raw.get("curriculum") or "British",
            "school_name": mapped.get("school_name") or "iSAMS School",
            "emirate": mapped.get("emirate") or "Dubai",
            "nationality": mapped.get("nationality") or "UAE",
            "parent_full_name": parent.get("parent_full_name") or "Parent/Guardian",
            "parent_email": parent.get("parent_email") or "",
            "parent_phone": parent.get("parent_phone") or "",
            "vaccination_status": mapped.get("vaccination_status") or "up_to_date",
            "vaccination_expiry": as_optional_date(mapped.get("vaccination_expiry")),
            "enrollment_date": as_optional_date(mapped.get("enrollment_date")),
            "is_active": _is_active_from(raw, mapped),
        })

    # -- staff ---------------------------------------------------------------
    for raw in staff:
        mapped = _apply(STAFF_FIELD_MAP, raw)
        sid = str(mapped.get("source_staff_id") or "")
        if not sid:
            errors.append("staff: row missing StaffId")
            continue
        full_name = str(mapped.get("full_name") or " ".join(filter(None, [
            str(mapped.get("first_name") or ""),
            str(mapped.get("last_name") or ""),
        ])).strip() or "Teacher")
        subjects = mapped.get("subjects") or []
        if isinstance(subjects, str):
            subjects = [s.strip() for s in subjects.split(",") if s.strip()]
        # STAFF_FIELD_MAP lands IsActive on is_available; reuse the shared
        # flag logic, but let Status="Left" count when no IsActive is present.
        check = dict(mapped)
        if mapped.get("is_available") is not None:
            check["is_active"] = mapped["is_available"]
        batch.staff.append({
            "school_id": resolve_school(raw.get("SchoolId") or raw.get("schoolId")),
            "source_staff_id": sid,
            "full_name": full_name,
            "email": str(mapped.get("email") or ""),
            "role": str(mapped.get("role") or "teacher")[:32],
            "subjects": list(subjects),
            "is_available": _is_active_from(raw, check),
            "max_periods": int(raw.get("MaxPeriods") or 6),
            "assigned": int(raw.get("AssignedPeriods") or 0),
        })

    # -- attendance: period rows → one row per (student, day) ----------------
    by_day: dict[tuple[str, str], dict[str, Any]] = {}
    period_rows = 0
    for raw in attendance:
        mapped = _apply(ATTENDANCE_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        day = as_optional_date(mapped.get("attendance_date"))
        if not sid or day is None:
            errors.append(f"attendance: row missing StudentId/Date "
                          f"(StudentId={raw.get('StudentId')!r} Date={raw.get('Date')!r})")
            continue
        period_rows += 1
        status = str(mapped.get("status") or "Absent")
        # Period-level collapse: worst status of the day wins.  Real SISs
        # record per session; attendance_logs keys (student_id, date).
        normalized = _ATT_CODE.get(status.strip().upper()) or _normalize_status(status)
        key = (sid, day.isoformat())
        prev = by_day.get(key)
        if prev is None or _SEVERITY[normalized] > _SEVERITY[prev["status"]]:
            by_day[key] = {
                "school_id": resolve_school(raw.get("SchoolId") or raw.get("schoolId")),
                "source_student_id": sid,
                "attendance_date": day,
                "status": normalized,
            }
    batch.attendance.extend(by_day.values())

    # -- finance -------------------------------------------------------------
    seen_invoices: set[tuple[str, str]] = set()
    for raw in finance:
        mapped = _apply(FINANCE_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if not sid:
            errors.append("finance: row missing StudentId")
            continue
        invoice = str(mapped.get("invoice_number") or "")
        if not invoice:
            errors.append(f"finance: student {sid}: row missing InvoiceNumber")
            continue
        key = (sid, invoice)
        if key in seen_invoices:
            continue  # idempotent: one fee row per student per invoice
        seen_invoices.add(key)
        try:
            amount_due = float(mapped.get("amount_due") or 0)
        except (TypeError, ValueError):
            amount_due = 0.0
        try:
            amount_paid = float(mapped.get("amount_paid") or 0)
        except (TypeError, ValueError):
            amount_paid = 0.0
        try:
            days_overdue = max(0, int(mapped.get("days_overdue") or 0))
        except (TypeError, ValueError):
            days_overdue = 0
        status = str(mapped.get("status") or "").strip().lower()
        if status not in {"current", "partial", "overdue", "paid", "waived"}:
            if amount_paid <= 0 and amount_due > 0:
                status = "overdue" if days_overdue else "current"
            elif amount_paid < amount_due:
                status = "overdue" if days_overdue else "partial"
            else:
                status = "paid"
        batch.fees.append({
            "school_id": resolve_school(raw.get("SchoolId") or raw.get("schoolId")),
            "source_student_id": sid,
            "invoice_number": invoice,
            "term_label": normalize_term_label(
                mapped.get("term_label") or (term_labels[0] if term_labels else None)),
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "due_date": as_optional_date(mapped.get("due_date")),
            "status": status,
            "days_overdue": days_overdue,
        })

    # -- behaviour: honesty note, no fabricated entity -----------------------
    incident_counts: dict[str, int] = defaultdict(int)
    for raw in behaviour:
        mapped = _apply(BEHAVIOUR_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if not sid:
            errors.append("behaviour: row missing StudentId")
            continue
        incident_counts[sid] += 1
    batch.meta["behaviour"] = {
        "incidents": sum(incident_counts.values()),
        "students_with_incidents": dict(incident_counts),
        "note": "the canonical five carry no behaviour entity — counts only, "
                "never fabricated rows",
    }
    batch.meta["academic_terms"] = term_labels
    batch.meta["contacts_joined"] = len(parent_by_student)
    batch.meta["attendance_periods_seen"] = period_rows
    return batch


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class ISAMSConnector(SourceConnector):
    """iSAMS REST consumer (OAuth2 client-credentials, read-only pulls).

    Config keys:
      ``transport``     — injectable transport (tests)
      ``state_store``   — run/watermark store (default: Postgres, else memory)
      ``school_id_map`` — {"isams-school-id": "our-school-id"}
      ``token_url``     — override the identity host token endpoint (tests)
      ``page_size`` / ``timeout`` / ``retries`` — transport tuning
    """

    dialect = "isams"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self.transport: Transport = config.get("transport") or _urllib_transport
        self.state_store: StateStore = config.get("state_store") or default_store()
        self._store_kind = type(self.state_store).__name__
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.rows_in_last_sync: dict[str, int] = {}
        self.delta_since: Optional[str] = None

    # -- credentials (env NAMES only — never values) ------------------------

    @staticmethod
    def _env(name: str) -> str:
        return os.environ.get(name, "")

    def _missing_env(self) -> list[str]:
        names = ["ISAMS_BASE_URL", "ISAMS_CLIENT_ID", "ISAMS_CLIENT_SECRET", "ISAMS_TENANT"]
        return [n for n in names if not self._env(n)]

    def _base_url(self) -> str:
        return self._env("ISAMS_BASE_URL") or str(self.config.get("base_url") or "")

    def _school_id_map(self) -> dict[str, str]:
        raw = self._env("ISAMS_SCHOOL_ID_MAP") or ""
        env_map: dict[str, str] = {}
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    env_map = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                pass  # health() reports it; never crash a pull over config
        config_map = self.config.get("school_id_map") or {}
        return {**env_map, **{str(k): str(v) for k, v in config_map.items()}}

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """OAuth2 client-credentials against the iSAMS identity host."""
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        missing = self._missing_env()
        if missing:
            raise ISAMSError("iSAMS credentials not configured (need env vars: "
                             + ", ".join(missing) + ")")
        token_url = str(self.config.get("token_url") or IDENTITY_TOKEN_URL)
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._env("ISAMS_CLIENT_ID"),
            "client_secret": self._env("ISAMS_CLIENT_SECRET"),
            "tenant": self._env("ISAMS_TENANT"),
            "scope": str(self.config.get("scope") or DEFAULT_SCOPE),
        }).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _resp_headers, raw = self.transport("POST", token_url, headers,
                                                    self._timeout(), body)
        if status != 200:
            raise ISAMSError(f"token request failed: HTTP {status}")
        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError as exc:
            raise ISAMSError("token response is not JSON") from exc
        token = payload.get("access_token")
        if not token:
            raise ISAMSError("token response has no access_token")
        self._token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in") or 600)
        return self._token

    # -- transport ----------------------------------------------------------

    def _timeout(self) -> float:
        return float(self.config.get("timeout") or DEFAULT_TIMEOUT_S)

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        """GET with timeout + retry/backoff.  Retries 5xx and transport errors."""
        base = self._base_url().rstrip("/")
        if not url.startswith(base):
            raise ISAMSError(f"refusing non-iSAMS URL: {url[:80]}")
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        retries = int(self.config.get("retries") or DEFAULT_RETRIES)
        last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                status, resp_headers, body = self.transport("GET", url, headers,
                                                            self._timeout())
                if status < 500:
                    return status, resp_headers, body
                last_exc = ISAMSError(f"HTTP {status} from {url}")
            except Exception as exc:
                last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        raise ISAMSError(f"GET {url} failed after {retries} attempts: {last_exc}")

    @staticmethod
    def _next_link(headers: dict[str, str]) -> Optional[str]:
        link = headers.get("Link") or headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part or "rel=next" in part:
                start, end = part.find("<"), part.find(">")
                if 0 <= start < end:
                    return part[start + 1:end]
        return None

    @staticmethod
    def _unwrap_envelope(resource: str, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            raise ISAMSError(
                f"{resource}: response envelope must be an object or list, "
                f"got {type(payload).__name__}")
        for key in ENVELOPE_KEYS.get(resource, (resource,)):
            if key in payload and isinstance(payload[key], list):
                return [r for r in payload[key] if isinstance(r, dict)]
        raise ISAMSError(
            f"{resource}: response envelope has none of {ENVELOPE_KEYS.get(resource)} "
            f"(keys: {sorted(payload.keys())[:8]})")

    def _fetch_resource(self, resource: str, since: Optional[str]) -> list[dict[str, Any]]:
        base = self._base_url().rstrip("/")
        if not base:
            raise ISAMSError("ISAMS_BASE_URL is not configured")
        page_size = int(self.config.get("page_size") or DEFAULT_PAGE_SIZE)
        rows: list[dict[str, Any]] = []
        offset = 0
        url: Optional[str] = f"{base}/{resource}?limit={page_size}&offset={offset}"
        if since:
            url += f"&since={urllib.parse.quote(since)}"
        seen_urls: set[str] = set()
        while url:
            if url in seen_urls:
                break  # a broken Link loop must not spin forever
            seen_urls.add(url)
            status, headers, body = self._get(url)
            if status == 404:
                # 404 = this tenant does not expose the resource.  pull()
                # decides fatal (students) vs. visible-but-tolerated.
                raise ISAMSError(f"resource not available (HTTP 404)")
            if status in (401, 403):
                raise ISAMSError(f"HTTP {status} authorising {resource}")
            if status >= 400:
                raise ISAMSError(f"HTTP {status} from {url}: {body[:200]!r}")
            payload = json.loads(body.decode() or "{}")
            chunk = self._unwrap_envelope(resource, payload)
            rows.extend(chunk)
            next_link = self._next_link(headers)
            if next_link:
                url = next_link
            elif len(chunk) >= page_size:
                offset += page_size
                url = f"{base}/{resource}?limit={page_size}&offset={offset}"
                if since:
                    url += f"&since={urllib.parse.quote(since)}"
            else:
                url = None
        return rows

    # -- SourceConnector ----------------------------------------------------

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        if payload.get("dialect") == "isams":
            return True
        if payload.get("source") == "isams":
            return True
        # Resource-shaped webhook bodies: {"students": [{...StudentId...}], …}
        for key in ("students", "contacts", "attendance", "behaviour",
                    "academic", "finance", "staff"):
            rows = payload.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                if "StudentId" in rows[0] or "StaffId" in rows[0]:
                    return True
        keys = set(payload.keys())
        # Strict: StudentId + a second iSAMS-shaped key — generic keys like
        # "Id" or "Email" must never claim another provider's payload.
        return "StudentId" in keys and bool(keys & {"NCYear", "Surname", "FormGroup",
                                                    "InvoiceNumber", "AttendanceCode"})

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Pull every resource; delta via ``since`` or the recorded watermark."""
        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        try:
            anchor = since
            if anchor is None:
                try:
                    anchor = self.state_store.get(self.dialect, self.school_id,
                                                  DELTA_STATE_KEY)
                except Exception:
                    anchor = None
            self.delta_since = anchor

            files: dict[str, list[dict[str, Any]]] = {}
            read_counts: dict[str, int] = {}
            for resource, key, _fmap in RESOURCES:
                try:
                    rows = self._fetch_resource(resource, anchor)
                except ISAMSError as exc:
                    # Optional resources degrade; the core roster must succeed.
                    if resource in ("students",):
                        raise
                    batch.errors.append(f"{resource}: {exc}")
                    rows = []
                files[key] = rows
                read_counts[resource] = len(rows)

            mapped = map_isams_objects(
                files.get("students", []), files.get("staff", []),
                files.get("contacts", []), files.get("attendance", []),
                files.get("behaviour", []), files.get("academic", []),
                files.get("finance", []),
                school_id=self.school_id,
                school_id_map=self._school_id_map(),
                source=self.dialect,
            )
            mapped.errors.extend(batch.errors)
            # Canonical entity counts — the same keys Gibbon reports, so the
            # status endpoint reads the same across connectors (N.5).  Raw
            # per-resource reads stay visible in meta (period rows, etc.).
            self.rows_in_last_sync = {
                "students": len(mapped.students),
                "staff": len(mapped.staff),
                "attendance": len(mapped.attendance),
                "fees": len(mapped.fees),
                "tombstones": sum(1 for s in mapped.students
                                  if not s.get("is_active")),
            }
            mapped.meta["resources_read"] = dict(read_counts)
            self.watermark = self._next_watermark()
            try:
                self.state_store.put(self.dialect, self.school_id,
                                     DELTA_STATE_KEY, self.watermark)
            except Exception:
                pass
            mapped.meta["delta_since"] = anchor
            self._mark_success()
            return mapped
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def _next_watermark(self) -> str:
        """New delta anchor: now, in UTC ISO-8601 (second pull is incremental)."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Real-time iSAMS event bodies carry resource-shaped lists."""
        try:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            def pick(key: str) -> list[dict[str, Any]]:
                val = data.get(key) or payload.get(key) or []
                return [r for r in val if isinstance(r, dict)] if isinstance(val, list) else []

            files = {key: pick(key) for _res, key, _fmap in RESOURCES}
            if not any(files.values()):
                raise ISAMSError("payload carries no iSAMS resources")
            batch = map_isams_objects(
                files["students"], files["staff"], files["contacts"],
                files["attendance"], files["behaviour"], files["academic"],
                files["finance"],
                school_id=self.school_id,
                school_id_map=self._school_id_map(),
                source=self.dialect,
            )
            self._mark_success()
            return batch
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["mode"] = "pull"
        base["rows_in_last_sync"] = dict(self.rows_in_last_sync)
        base["state_store"] = self._store_kind
        base["delta_since"] = self.delta_since
        base["partner_portal"] = "developer.isams.com (partner-gated)"
        missing = self._missing_env()
        if missing:
            base["status"] = "NOT_CONFIGURED"
            base["missing_env"] = missing
        elif self.last_error:
            base["status"] = "UNHEALTHY"
        else:
            base["status"] = "OK"
        return base
