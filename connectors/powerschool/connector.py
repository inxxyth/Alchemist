"""PowerSchool connector (Phase 4, N.2) — a thin OneRoster profile, not a fork.

Where PowerSchool speaks OneRoster 1.2 — its ``/ims/oneroster/v1p2`` surface —
this connector **reuses** :class:`connectors.oneroster.rest_binding.OneRosterRestConnector`
with a config profile.  Bespoke code exists only where OneRoster has no
answer: **attendance and fees** (the same gap iSAMS fills — see
INTEGRATION-PLAN.md §3).

Contract
--------
Auth: OAuth2 client-credentials against ``{POWERSCHOOL_BASE_URL}/oauth/access_token``
— one token serves both the OneRoster surface and the PowerSchool REST
resources.  Env var NAMES (never values):

    POWERSCHOOL_BASE_URL     — PowerSchool server root (no trailing path)
    POWERSCHOOL_CLIENT_ID    — plugin / REST client id
    POWERSCHOOL_CLIENT_SECRET

Surfaces consumed:

    {base}/ims/oneroster/v1p2/{orgs,users,classes,courses,enrollments,demographics}
        → rostering (delegated to the OneRoster REST binding unchanged)
    {base}/ws/v1/attendance   → {"attendance": [ {studentId, date, …} ]}
    {base}/ws/v1/fees         → {"fees":       [ {studentId, feeId, …} ]}

Paging: ``limit``/``offset`` and/or RFC 5988 ``Link`` ``rel="next"`` on both
surfaces.  Delta: ``?since=`` on the attendance/fees resources, with the run
watermark recorded per school_id in :class:`connectors.state.StateStore` so a
second pull is incremental.  Timeout + retry with backoff; 401/403 →
connector ``UNHEALTHY``, core services stay up.

Canonical mapping (the five entities)
-------------------------------------
OneRoster users(role=student|teacher) → Student / Staff   [delegated]
attendance → Attendance  **period rows collapse to one row per student per
             day** (severity: ``absent > excused > late > present``)
fees       → Fee

Live-sandbox status
-------------------
No PowerSchool partner sandbox credentials were available at build time.
The connector is built to the documented request/response contract above and
verified against synthetic fixtures (``fixtures/powerschool/``) with an
injected transport.  No live result is claimed — ``test_powerschool.py``
records the gate honestly.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from connectors.base import (
    CanonicalBatch,
    SourceConnector,
    as_optional_date,
    normalize_term_label,
)
from connectors.oneroster.rest_binding import (
    OneRosterRestConnector,
    OneRosterRestError,
)
from connectors.powerschool.field_maps import (
    ATTENDANCE_FIELD_MAP,
    FEE_FIELD_MAP,
)
from connectors.state import StateStore, default_store

DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RETRIES = 3

# PowerSchool REST resources OneRoster cannot carry.
EXTRAS = (
    ("attendance", "attendance", ATTENDANCE_FIELD_MAP),
    ("fees", "fees", FEE_FIELD_MAP),
)

EXTRA_ENVELOPE_KEYS = {
    "attendance": ("attendance", "attendanceRecords", "Attendance"),
    "fees": ("fees", "feeTransactions", "Fees", "invoices"),
}

DELTA_STATE_KEY = "delta.since"

# Day-collapse severity for period-level attendance (worst status wins).
_SEVERITY = {"present": 0, "late": 1, "excused": 2, "absent": 3}

# PowerSchool attendance-code names (documented code set); unknown codes fall
# through to sis_adapter._normalize_status — never guessed.
_ATT_CODE = {
    "P": "present", "PRESENT": "present", "ADA": "present",
    "L": "late", "LT": "late", "LATE": "late", "TARDY": "late",
    "A": "absent", "ABS": "absent", "ABSENT": "absent", "N": "absent",
    "M": "excused", "MED": "excused", "MEDICAL": "excused",
    "E": "excused", "EXC": "excused", "EXCUSED": "excused",
}

Transport = Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def _urllib_transport(url: str, headers: dict[str, str],
                      timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


class PowerSchoolError(RuntimeError):
    """PowerSchool transport failed after retries, or the envelope broke the contract."""


# ---------------------------------------------------------------------------
# Mapping: PowerSchool extras → canonical batch (rostering is delegated)
# ---------------------------------------------------------------------------

def _apply(field_map: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in field_map.items():
        if src in row and row[src] not in (None, ""):
            out[dest] = row[src]
    return out


def map_powerschool_extras(
    attendance: list[dict[str, Any]],
    fees: list[dict[str, Any]],
    school_id: str = "default",
    school_id_map: Optional[dict[str, str]] = None,
    source: str = "powerschool",
) -> CanonicalBatch:
    """Map the PowerSchool-only resources (attendance, fees) to canonical rows.

    Pure mapping — no I/O.  Rostering is not handled here on purpose: it
    rides the OneRoster binding, which already maps users → Student/Staff.
    """
    from sis_adapter import _normalize_status  # late import: sis_adapter loads our field maps

    school_id_map = school_id_map or {}
    batch = CanonicalBatch(source=source, school_id=school_id)
    errors = batch.errors

    def resolve_school(hint: Any) -> str:
        if hint is not None and str(hint) in school_id_map:
            return school_id_map[str(hint)]
        return school_id

    # -- attendance: period rows → one row per (student, day) ----------------
    by_day: dict[tuple[str, str], dict[str, Any]] = {}
    period_rows = 0
    for raw in attendance:
        mapped = _apply(ATTENDANCE_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        day = as_optional_date(mapped.get("attendance_date"))
        if not sid or day is None:
            errors.append(
                f"attendance: row missing studentId/date "
                f"(studentId={raw.get('studentId')!r} date={raw.get('date')!r})")
            continue
        period_rows += 1
        status = str(mapped.get("status") or "Absent")
        # Period-level collapse: worst status of the day wins.  PowerSchool
        # records per period; attendance_logs keys (student_id, date).
        normalized = _ATT_CODE.get(status.strip().upper()) or _normalize_status(status)
        key = (sid, day.isoformat())
        prev = by_day.get(key)
        if prev is None or _SEVERITY[normalized] > _SEVERITY[prev["status"]]:
            by_day[key] = {
                "school_id": resolve_school(raw.get("schoolId")),
                "source_student_id": sid,
                "attendance_date": day,
                "status": normalized,
            }
    batch.attendance.extend(by_day.values())

    # -- fees ---------------------------------------------------------------
    seen_invoices: set[tuple[str, str]] = set()
    for raw in fees:
        mapped = _apply(FEE_FIELD_MAP, raw)
        sid = str(mapped.get("source_student_id") or "")
        if not sid:
            errors.append("fees: row missing studentId")
            continue
        invoice = str(mapped.get("invoice_number") or "")
        if not invoice:
            errors.append(f"fees: student {sid}: row missing feeId/invoiceNumber")
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
            "school_id": resolve_school(raw.get("schoolId")),
            "source_student_id": sid,
            "invoice_number": invoice,
            "term_label": normalize_term_label(mapped.get("term_label")),
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "due_date": as_optional_date(mapped.get("due_date")),
            "status": status,
            "days_overdue": days_overdue,
        })

    batch.meta["attendance_periods_seen"] = period_rows
    return batch


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class PowerSchoolConnector(SourceConnector):
    """PowerSchool: OneRoster profile for rostering + REST extras for
    attendance/fees.

    Config keys:
      ``transport``     — injectable GET transport (tests; shared by both
                          surfaces — OneRoster REST's transport protocol)
      ``state_store``   — run/watermark store (default: Postgres, else memory)
      ``school_id_map`` — {"ps-school-id": "our-school-id"}
      ``page_size`` / ``timeout`` / ``retries`` — transport tuning
    """

    dialect = "powerschool"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self.transport: Transport = config.get("transport") or _urllib_transport
        self.state_store: StateStore = config.get("state_store") or default_store()
        self._store_kind = type(self.state_store).__name__
        self.rows_in_last_sync: dict[str, int] = {}
        self.watermark: Optional[str] = None
        self.delta_since: Optional[str] = None
        # The thin profile: rostering is the OneRoster REST binding with a
        # PowerSchool config — not a reimplementation.
        self._roster = OneRosterRestConnector(
            school_id=school_id,
            credential_env_names=credential_env_names,
            base_url=self._roster_base_url(),
            token_url=self._token_url(),
            client_id_env="POWERSCHOOL_CLIENT_ID",
            client_secret_env="POWERSCHOOL_CLIENT_SECRET",
            transport=self.transport,
            page_size=config.get("page_size"),
            timeout=config.get("timeout"),
            retries=config.get("retries"),
            school_id_map=config.get("school_id_map"),
        )

    # -- credentials (env NAMES only — never values) ------------------------

    @staticmethod
    def _env(name: str) -> str:
        return os.environ.get(name, "")

    def _missing_env(self) -> list[str]:
        names = ["POWERSCHOOL_BASE_URL", "POWERSCHOOL_CLIENT_ID",
                 "POWERSCHOOL_CLIENT_SECRET"]
        return [n for n in names if not self._env(n)]

    def _base_url(self) -> str:
        return self._env("POWERSCHOOL_BASE_URL") or str(self.config.get("base_url") or "")

    def _roster_base_url(self) -> str:
        base = self._base_url().rstrip("/")
        return f"{base}/ims/oneroster/v1p2" if base else ""

    def _extras_base_url(self) -> str:
        base = self._base_url().rstrip("/")
        return f"{base}/ws/v1" if base else ""

    def _token_url(self) -> str:
        base = self._base_url().rstrip("/")
        return f"{base}/oauth/access_token" if base else ""

    def _school_id_map(self) -> dict[str, str]:
        raw = self._env("POWERSCHOOL_SCHOOL_ID_MAP") or ""
        env_map: dict[str, str] = {}
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    env_map = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                pass
        config_map = self.config.get("school_id_map") or {}
        return {**env_map, **{str(k): str(v) for k, v in config_map.items()}}

    # -- auth: one PowerSchool token serves both surfaces -------------------

    def _access_token(self) -> str:
        return self._roster._access_token()

    # -- transport ----------------------------------------------------------

    def _timeout(self) -> float:
        return float(self.config.get("timeout") or DEFAULT_TIMEOUT_S)

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        """GET with timeout + retry/backoff.  Retries 5xx and transport errors."""
        base = self._base_url().rstrip("/")
        if not base or not url.startswith(base):
            raise PowerSchoolError(f"refusing non-PowerSchool URL: {url[:80]}")
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        retries = int(self.config.get("retries") or DEFAULT_RETRIES)
        last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                status, resp_headers, body = self.transport(url, headers,
                                                            self._timeout())
                if status < 500:
                    return status, resp_headers, body
                last_exc = PowerSchoolError(f"HTTP {status} from {url}")
            except Exception as exc:
                last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        raise PowerSchoolError(f"GET {url} failed after {retries} attempts: {last_exc}")

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
            raise PowerSchoolError(
                f"{resource}: response envelope must be an object or list, "
                f"got {type(payload).__name__}")
        for key in EXTRA_ENVELOPE_KEYS.get(resource, (resource,)):
            if key in payload and isinstance(payload[key], list):
                return [r for r in payload[key] if isinstance(r, dict)]
        raise PowerSchoolError(
            f"{resource}: response envelope has none of "
            f"{EXTRA_ENVELOPE_KEYS.get(resource)} "
            f"(keys: {sorted(payload.keys())[:8]})")

    def _fetch_extras(self, resource: str, since: Optional[str]) -> list[dict[str, Any]]:
        base = self._extras_base_url()
        if not base:
            raise PowerSchoolError("POWERSCHOOL_BASE_URL is not configured")
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
                raise PowerSchoolError(f"resource not available (HTTP 404)")
            if status in (401, 403):
                raise PowerSchoolError(f"HTTP {status} authorising {resource}")
            if status >= 400:
                raise PowerSchoolError(f"HTTP {status} from {url}: {body[:200]!r}")
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
        if payload.get("dialect") == "powerschool":
            return True
        if payload.get("source") == "powerschool":
            return True
        # Extras-shaped webhook bodies: {"attendance": [{studentId, …}], …}
        for key in ("attendance", "fees", "feeTransactions", "attendanceRecords"):
            rows = payload.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                if "studentId" in rows[0] and "sourcedId" not in payload:
                    return True
        keys = set(payload.keys())
        # Strict: attendanceCode/feeId + studentId — generic keys never claim
        # another provider's payload.
        return "studentId" in keys and bool(keys & {"attendanceCode", "feeId",
                                                    "feeName", "periodId"})

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Roster via the OneRoster profile + attendance/fees via /ws/v1."""
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

            # 1. Rostering — delegated, unchanged.
            roster = self._roster.pull(since=anchor)
            if any(e.startswith(("users", "orgs")) for e in roster.errors):
                # Core roster failure (orgs/users) is a pull failure.
                raise PowerSchoolError(f"roster pull failed: {roster.errors[0]}")
            merged = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            merged.students.extend(roster.students)
            merged.staff.extend(roster.staff)
            merged.errors.extend(roster.errors)

            # 2. Extras — the OneRoster gap, bespoke.  Attendance is the
            # flagship (OneRoster cannot carry it) so its failure is fatal;
            # fees degrade where the plugin is absent.  Auth failures on any
            # surface are connector-level (one token serves both).
            files: dict[str, list[dict[str, Any]]] = {}
            read_counts: dict[str, int] = {}
            for resource, key, _fmap in EXTRAS:
                try:
                    rows = self._fetch_extras(resource, anchor)
                except (PowerSchoolError, OneRosterRestError) as exc:
                    msg = str(exc)
                    if "401" in msg or "403" in msg or resource == "attendance":
                        raise
                    merged.errors.append(f"{resource}: {exc}")
                    rows = []
                files[key] = rows
                read_counts[resource] = len(rows)
            extras = map_powerschool_extras(
                files.get("attendance", []), files.get("fees", []),
                school_id=self.school_id,
                school_id_map=self._school_id_map(),
                source=self.dialect,
            )
            merged.attendance.extend(extras.attendance)
            merged.fees.extend(extras.fees)
            merged.errors.extend(extras.errors)
            merged.meta["resources_read"] = dict(read_counts)
            merged.meta["roster_resources"] = dict(self._roster.rows_in_last_sync)

            # 3. Bookkeeping — entity keys match Gibbon/iSAMS (N.5).
            self.rows_in_last_sync = {
                "students": len(merged.students),
                "staff": len(merged.staff),
                "attendance": len(merged.attendance),
                "fees": len(merged.fees),
                "tombstones": sum(1 for s in merged.students
                                  if not s.get("is_active")),
            }
            self.watermark = self._next_watermark()
            try:
                self.state_store.put(self.dialect, self.school_id,
                                     DELTA_STATE_KEY, self.watermark)
            except Exception:
                pass
            merged.meta["delta_since"] = anchor
            self._mark_success()
            return merged
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def _next_watermark(self) -> str:
        """New delta anchor: now, in UTC ISO-8601 (second pull is incremental)."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Real-time bodies: OneRoster envelopes or attendance/fees events."""
        try:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            has_extras = any(
                isinstance(data.get(k), list)
                for k in ("attendance", "fees", "feeTransactions",
                          "attendanceRecords"))
            has_roster = any(
                k in data for k in ("users", "orgs", "classes", "courses",
                                    "enrollments", "demographics"))

            merged = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            if has_roster or not has_extras:
                roster = self._roster.webhook(data)
                merged.students.extend(roster.students)
                merged.staff.extend(roster.staff)
                merged.errors.extend(roster.errors)
            if has_extras:
                def pick(*keys: str) -> list[dict[str, Any]]:
                    for k in keys:
                        val = data.get(k)
                        if isinstance(val, list):
                            return [r for r in val if isinstance(r, dict)]
                    return []

                extras = map_powerschool_extras(
                    pick("attendance", "attendanceRecords"),
                    pick("fees", "feeTransactions"),
                    school_id=self.school_id,
                    school_id_map=self._school_id_map(),
                    source=self.dialect,
                )
                merged.attendance.extend(extras.attendance)
                merged.fees.extend(extras.fees)
                merged.errors.extend(extras.errors)
            if not (merged.students or merged.staff or merged.attendance
                    or merged.fees):
                raise PowerSchoolError("payload carries no PowerSchool resources")
            self._mark_success()
            return merged
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
        base["roster_profile"] = "oneroster-rest (OneRoster 1.2 — not a fork)"
        missing = self._missing_env()
        if missing:
            base["status"] = "NOT_CONFIGURED"
            base["missing_env"] = missing
        elif self.last_error:
            base["status"] = "UNHEALTHY"
        else:
            base["status"] = "OK"
        return base
