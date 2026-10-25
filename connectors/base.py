"""Connector base contract — the target every source is mapped into.

A :class:`SourceConnector` turns some school SIS's native shape into a
:class:`CanonicalBatch` of rows keyed by the canonical entity fields that
:mod:`sis_adapter` upserts.  Connectors map **columns**; ``sis_adapter``
keeps the **normalisers and the upserts** (it is the target, not the source).

Canonical entities (unchanged since the first SIS adapter):
  Student · Attendance · Fee · Staff · LeaveRequest

Design rules for every connector implementation
-----------------------------------------------
* Pure mapping wherever possible — no secret handling, no hardcoded URLs.
* Credentials come from env vars **named in the client manifest**; never from
  the manifest itself (manifests hold NAMES, never values).
* ``health()`` never raises — a broken source reports ``UNHEALTHY`` and the
  core services stay up.
* Every row that reaches the upserts carries ``school_id``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Canonical row shapes — identity field maps ("canonical" dialect)
# ---------------------------------------------------------------------------
# These are also the keys :func:`sis_adapter.map_student` and friends emit.

CANONICAL_STUDENT_FIELDS = (
    "source_student_id", "emirates_id", "first_name", "last_name", "gender",
    "date_of_birth", "grade_level", "curriculum_track", "school_name",
    "emirate", "nationality", "parent_full_name", "parent_email",
    "parent_phone", "vaccination_status", "vaccination_expiry",
    "enrollment_date", "is_active", "school_id",
)

CANONICAL_ATTENDANCE_FIELDS = (
    "source_student_id", "student_id", "attendance_date", "status", "school_id",
)

CANONICAL_FEE_FIELDS = (
    "source_student_id", "student_id", "invoice_number", "term_label",
    "amount_due", "amount_paid", "due_date", "status", "days_overdue",
    "school_id",
)

CANONICAL_STAFF_FIELDS = (
    "source_staff_id", "full_name", "email", "role", "subjects",
    "is_available", "max_periods", "assigned", "school_id",
)

CANONICAL_LEAVE_FIELDS = (
    "source_staff_id", "staff_id", "leave_date", "periods", "reason",
    "subject_needed", "school_id",
)


def identity_map(fields: tuple[str, ...]) -> dict[str, str]:
    """Field map for rows that are already canonical (connector output)."""
    return {name: name for name in fields}


CANONICAL_STUDENT_MAP = identity_map(CANONICAL_STUDENT_FIELDS)
CANONICAL_ATTENDANCE_MAP = identity_map(CANONICAL_ATTENDANCE_FIELDS)
CANONICAL_FEE_MAP = identity_map(CANONICAL_FEE_FIELDS)
CANONICAL_STAFF_MAP = identity_map(CANONICAL_STAFF_FIELDS)
CANONICAL_LEAVE_MAP = identity_map(CANONICAL_LEAVE_FIELDS)


# ---------------------------------------------------------------------------
# Batch envelope
# ---------------------------------------------------------------------------

@dataclass
class CanonicalBatch:
    """What a connector hands back — already-canonical rows, stamped.

    ``errors`` collects per-row rejections (malformed input) without failing
    the whole pull: one bad CSV row must not take down a client's sync.
    """

    source: str = ""
    school_id: str = "default"
    students: list[dict[str, Any]] = field(default_factory=list)
    attendance: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    staff: list[dict[str, Any]] = field(default_factory=list)
    leave_requests: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "students": len(self.students),
            "attendance": len(self.attendance),
            "fees": len(self.fees),
            "staff": len(self.staff),
            "leave_requests": len(self.leave_requests),
            "errors": len(self.errors),
        }

    def is_empty(self) -> bool:
        return not (self.students or self.attendance or self.fees
                    or self.staff or self.leave_requests)


# ---------------------------------------------------------------------------
# Abstract source connector
# ---------------------------------------------------------------------------

class SourceConnector(ABC):
    """One client-facing data source (a SIS, a standard binding, a CSV drop).

    Subclasses set :attr:`dialect` to the name used in request payloads and
    in the client manifest (``gibbon`` | ``openeducat`` | ``canvas`` |
    ``oneroster-csv`` | ``oneroster-rest`` | …).
    """

    #: registry key / request ``dialect`` value
    dialect: str = ""

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        self.school_id = str(school_id or "default")[:64]
        self.credential_env_names = list(credential_env_names or [])
        self.config = config
        self.last_success_at: Optional[str] = None
        self.last_error: Optional[str] = None
        # N.5 operational visibility — every connector reports these.
        self.rows_in_last_sync: dict[str, int] = {}
        self.watermark: Optional[str] = None
        # Optional durable state (delta tokens / watermarks / run records).
        # Set by subclasses that sync incrementally; also injectable via
        # ``state_store=`` for tests and for the status endpoint's probes.
        self.state_store: Optional[Any] = config.get("state_store")

    # -- required -----------------------------------------------------------

    @classmethod
    @abstractmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        """Return True when ``payload`` looks like this connector's dialect.

        Used by the auto-detect path in :mod:`sis_adapter`.  Must be pure and
        cheap — it runs on every unlabelled webhook body.
        """

    @abstractmethod
    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Fetch from the source.  ``since`` (ISO-8601) enables delta pulls."""

    @abstractmethod
    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Map a real-time event payload into a :class:`CanonicalBatch`."""

    # -- provided -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Operational status.  Never raises — report, don't crash.

        The ``rows_in_last_sync`` / ``watermark`` / ``mode`` fields are the
        N.5 contract: everything a client's IT lead needs to see per run.
        """
        return {
            "dialect": self.dialect,
            "school_id": self.school_id,
            "status": "OK" if not self.last_error else "DEGRADED",
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "rows_in_last_sync": dict(self.rows_in_last_sync),
            "watermark": self.watermark,
            "mode": str(self.config.get("mode", "pull")),
            "credential_env_names": self.credential_env_names,
        }

    def _mark_success(self) -> None:
        self.last_error = None
        self.last_success_at = datetime.utcnow().isoformat() + "Z"
        self._persist_run()

    def _mark_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self._persist_run()

    def _persist_run(self) -> None:
        """Record the run snapshot durably when a state store is attached."""
        store = getattr(self, "state_store", None)
        if store is None:
            return
        try:
            from connectors.state import record_sync_run
            record_sync_run(
                store, self.dialect, self.school_id,
                rows=dict(self.rows_in_last_sync),
                watermark=self.watermark,
                error=self.last_error,
            )
        except Exception:
            pass  # status bookkeeping must never break a sync


# ---------------------------------------------------------------------------
# Grade / term handling — deliberately calendar-agnostic
# ---------------------------------------------------------------------------
# UAE schools run Aug–Jun and some run Sep–Jul.  Never rewrite a source term
# label into a US season ("Fall 2025") and never infer a grade from a month.

_UA_GRADE_TOKENS = {
    "kg": 1, "k": 1, "tk": 1, "jk": 1, "ps": 1, "pre-k": 1, "prek": 1,
    "foundation": 1, "fs1": 1, "fs2": 1, "yr1": 1, "year 1": 1,
    "ug": 12, "ungraded": 12, "other": 12,
}


def normalize_grade(raw: Any, default: int = 1) -> int:
    """Map OneRoster / SIS grade tokens onto MSOE grade 1–12.

    Tolerates ``"9"``, ``"09"``, ``"KG"``, ``"Year 7"``, ``"Grade 8"`` and
    refuses to guess from academic-year calendars.
    """
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    for token, grade in _UA_GRADE_TOKENS.items():
        if text == token:
            return grade
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if digits:
        try:
            return min(12, max(1, int(digits)))
        except ValueError:
            return default
    return default


def normalize_term_label(raw: Any, default: str = "Term 1") -> str:
    """Keep the source term label verbatim (truncated for the fee ledger).

    No US-season rewrites: a Sep–Jul school's ``"Semester 2 AY2025-26"``
    stays exactly that.
    """
    if raw is None:
        return default
    text = str(raw).strip()
    return text[:32] if text else default


def as_optional_date(value: Any) -> Optional[date]:
    """Parse a date-ish value; None when unparseable (callers keep their default)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
