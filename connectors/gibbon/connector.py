"""Gibbon connector (#0) — the native connector that already runs (C.3).

The ``fetch_*`` SQL pulls moved here from ``gibbon_bridge.py``, which stays
as a thin shim so the RUNBOOK commands do not break.  Honours ``school_id``
and the F.8 org literals (env with demo defaults — never hardcoded secrets).

Phase 4 (N.3) — incremental sync + tombstones
---------------------------------------------
* **Incremental:** per-resource watermarks in :class:`connectors.state.StateStore`
  so a second run transfers nothing new (``pull()`` twice → 0 rows).
    - persons   → monotonic ``gibbonPersonID`` insert watermark
    - attendance→ ``COALESCE(timestampTaken, TIMESTAMP(date))``
    - fees      → ``GREATEST(timestampCreator, timestampUpdate)``
  ``gibbonPerson`` has **no updated-at column** (``lastTimestamp`` is last
  login, not last edit) — that is a Gibbon schema fact, not a choice.  New
  people arrive via the id watermark; edits to existing people are caught by
  the **status sweep** below and by a full resync (``--full``).
* **Tombstones:** a student deactivated in Gibbon must deactivate in MSOE,
  not linger as active.  Every pull runs a two-column status probe
  (``gibbonPersonID``, ``status``) diffed against the status map recorded on
  the previous run — only *changed* rows are fetched and emitted with
  ``is_active=False`` (a plain ``UPDATE gibbonPerson SET status='Left'`` is
  caught even when no audit row was written).  People who vanish from the
  source entirely surface as ``meta['vanished_*']`` id lists; the ingest
  path flips only ``is_active`` / ``is_available`` for those ids — never a
  full-row overwrite.  The probe emits nothing when nothing changed, so the
  incremental guarantee holds.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import pymysql

from connectors.base import CanonicalBatch, SourceConnector
from connectors.gibbon.field_maps import DETECT_KEYS
from connectors.state import default_store

GIBBON_MYSQL = {
    "host": os.environ.get("GIBBON_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("GIBBON_MYSQL_PORT", "3306")),
    "user": os.environ.get("GIBBON_MYSQL_USER", "gibbon"),
    "password": os.environ.get("GIBBON_MYSQL_PASSWORD", "msoe_gibbon_2026"),
    "database": os.environ.get("GIBBON_MYSQL_DB", "gibbon"),
    "connect_timeout": 8,
    "charset": "utf8mb4",
}

# Client-configurable identity literals (F.8 — env with demo defaults).
_ORG_NAME = os.environ.get("MSOE_SCHOOL_NAME", "MSOE Academy Dubai")
_ORG_EMIRATE = os.environ.get("MSOE_EMIRATE", "Dubai")
_ORG_CURRICULUM = os.environ.get("MSOE_CURRICULUM", "British")
_ORG_PARENT_EMAIL = os.environ.get("MSOE_PARENT_EMAIL", "parent@msoe.ae")
_ORG_PARENT_PHONE = os.environ.get("MSOE_PARENT_PHONE", "+971500000000")

# State keys (per school_id).
WM_PERSONS = "watermark.persons"
WM_ATTENDANCE = "watermark.attendance"
WM_FEES = "watermark.fees"
STATUS_MAP_STUDENTS = "student_status_map"
STATUS_MAP_STAFF = "staff_status_map"

_STUDENT_ROLE = "003"
_STAFF_ROLE = "002"


def gibbon_conn():
    return pymysql.connect(**GIBBON_MYSQL)


def _as_id(value: Any) -> str:
    """Gibbon ids are ZEROFILL ints — normalise to a plain digit string."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


class GibbonConnector(SourceConnector):
    """Gibbon v31 MySQL → canonical batch (students, staff, attendance, fees).

    Config keys:
      ``full``        — force a full snapshot (ignore + reset watermarks)
      ``entities``    — optional subset of {"students","staff","attendance","fees"}
      ``state_store`` — watermark/status-map store (default: Postgres, else memory)
    """

    dialect = "gibbon"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self.state_store = config.get("state_store") or default_store()
        self._store_kind = type(self.state_store).__name__
        self.rows_in_last_sync: dict[str, int] = {}

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        keys = set(payload.keys())
        return bool(DETECT_KEYS & keys)

    # -- raw pulls (moved from gibbon_bridge.fetch_*) ------------------------

    def fetch_students(self, limit: int = 5000,
                       since_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Student rows (all statuses — ``is_active`` carries the tombstone).

        Gibbon v31: role is gibbonRoleIDPrimary (Student=003); grade via
        gibbonStudentEnrolment → gibbonYearGroup.sequenceNumber.
        Emits source_student_id (gibbonPersonID) — the surrogate PK is
        assigned by the MSOE students table, not by the source system.

        ``since_id`` enables the insert watermark: only people with a
        greater ``gibbonPersonID`` transfer.  Rows carry ``is_active`` —
        ``status='Full'`` is active, anything else ('Left', 'Expected',
        'Pending Approval') is a tombstone, not a deletion.
        """
        where = ["p.gibbonRoleIDPrimary = " + _STUDENT_ROLE]
        params: list[Any] = []
        if since_id is not None:
            where.append("p.gibbonPersonID > %s")
            params.append(int(since_id))
        sql = f"""
            SELECT * FROM (
                SELECT
                    p.gibbonPersonID AS source_student_id,
                    COALESCE(NULLIF(p.studentID, ''), '') AS emirates_id,
                    p.firstName AS first_name,
                    p.surname AS last_name,
                    CASE p.gender WHEN 'M' THEN 'M' WHEN 'F' THEN 'F' ELSE 'U' END AS gender,
                    COALESCE(p.dob, '2015-01-01') AS date_of_birth,
                    COALESCE(MAX(yg.sequenceNumber), 7) AS grade_level_raw,
                    '{_ORG_CURRICULUM}' AS curriculum_track,
                    '{_ORG_NAME}' AS school_name,
                    '{_ORG_EMIRATE}' AS emirate,
                    COALESCE(MAX(NULLIF(p.address1Country, '')), 'UAE') AS nationality,
                    COALESCE(MAX(NULLIF(p.emailAlternate, '')), 'Parent/Guardian') AS parent_full_name,
                    COALESCE(MAX(NULLIF(p.email, '')), '{_ORG_PARENT_EMAIL}') AS parent_email,
                    COALESCE(MAX(NULLIF(p.phone1, '')), '{_ORG_PARENT_PHONE}') AS parent_phone,
                    'up_to_date' AS vaccination_status,
                    MAX(DATE(p.lastTimestamp)) AS enrollment_date,
                    MAX(CASE p.status WHEN 'Full' THEN 1 ELSE 0 END) AS is_active_raw
                FROM gibbonPerson p
                LEFT JOIN gibbonStudentEnrolment se
                       ON se.gibbonPersonID = p.gibbonPersonID
                LEFT JOIN gibbonYearGroup yg
                       ON yg.gibbonYearGroupID = se.gibbonYearGroupID
                WHERE {' AND '.join(where)}
                GROUP BY p.gibbonPersonID, p.firstName, p.surname, p.gender, p.dob,
                         p.studentID, p.status
            ) t
            LIMIT %s
        """
        params.append(limit)
        rows: list[dict[str, Any]] = []
        with gibbon_conn() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    raw = r.pop("grade_level_raw", 7)
                    try:
                        r["grade_level"] = min(12, max(1, int(str(raw).strip() or "7")))
                    except Exception:
                        r["grade_level"] = 7
                    r["is_active"] = bool(int(r.pop("is_active_raw", 1) or 0))
                    rows.append(r)
        return rows

    def fetch_staff(self, limit: int = 500,
                    since_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Staff rows; ``is_available`` false is the staff-side tombstone."""
        where = ["p.gibbonRoleIDPrimary = " + _STAFF_ROLE]
        params: list[Any] = []
        if since_id is not None:
            where.append("p.gibbonPersonID > %s")
            params.append(int(since_id))
        sql = f"""
            SELECT
                p.gibbonPersonID AS source_staff_id,
                CONCAT(p.firstName, ' ', p.surname) AS full_name,
                COALESCE(NULLIF(p.email, ''),
                         CONCAT('staff', p.gibbonPersonID, '@msoe.ae')) AS email,
                'teacher' AS role,
                'Mathematics' AS subjects,
                CASE p.status WHEN 'Full' THEN 1 ELSE 0 END AS is_available,
                6 AS max_periods,
                0 AS assigned
            FROM gibbonPerson p
            WHERE {' AND '.join(where)}
            LIMIT %s
        """
        params.append(limit)
        with gibbon_conn() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall())

    def fetch_attendance(self, limit: int = 5000,
                         since: Optional[str] = None) -> list[dict[str, Any]]:
        """Attendance pull from gibbonAttendanceLogPerson + gibbonAttendanceCode.

        Gibbon's ``type`` column on the log is Core/Additional — the status
        lives on ``gibbonAttendanceCode.name`` ("Present", "Present - Late",
        "Absent", ...).  Emits gibbon-shaped keys so the attendance field map
        can resolve them (``gibbonPersonID`` / ``date`` / ``name``).

        ``since`` filters on ``COALESCE(timestampTaken, TIMESTAMP(date))`` —
        the log row's real last-updated signal.
        """
        where = ["l.date IS NOT NULL"]
        params: list[Any] = []
        if since:
            where.append("COALESCE(l.timestampTaken, TIMESTAMP(l.date)) > %s")
            params.append(since)
        sql = f"""
            SELECT
                l.gibbonPersonID AS gibbonPersonID,
                l.date AS date,
                COALESCE(c.name, 'Absent') AS name,
                COALESCE(l.timestampTaken, TIMESTAMP(l.date)) AS last_updated
            FROM gibbonAttendanceLogPerson l
            LEFT JOIN gibbonAttendanceCode c
                   ON c.gibbonAttendanceCodeID = l.gibbonAttendanceCodeID
            WHERE {' AND '.join(where)}
            ORDER BY l.date DESC
            LIMIT %s
        """
        params.append(limit)
        try:
            with gibbon_conn() as conn:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql, tuple(params))
                    return list(cur.fetchall())
        except Exception as exc:
            print(f"[gibbon] attendance pull skipped: {exc}")
            return []

    def fetch_fees(self, limit: int = 5000,
                   since: Optional[str] = None) -> list[dict[str, Any]]:
        """Best-effort finance pull: gibbonFinanceInvoice → fee ledger rows.

        Gibbon invoices are addressed to an invoicee (parent/guardian).  Each
        invoice fans out to that family's children as one fee row per student.
        Emits gibbon-shaped keys for the fee field map.

        ``since`` filters on the invoice's create/update timestamps.
        """
        where = ["1=1"]
        params: list[Any] = []
        if since:
            where.append(
                "GREATEST(COALESCE(inv.timestampCreator, '1970-01-01'), "
                "COALESCE(inv.timestampUpdate, '1970-01-01')) > %s")
            params.append(since)
        sql = f"""
            SELECT
                fc.gibbonPersonID AS gibbonPersonID,
                CONCAT('INV-', inv.gibbonFinanceInvoiceID) AS invoice_number,
                COALESCE(bs.name, 'Term 1') AS term_label,
                COALESCE(SUM(invf.fee), 0) AS amount,
                COALESCE(inv.paidAmount, 0) AS amount_paid,
                COALESCE(inv.invoiceDueDate, CURDATE()) AS dueDate,
                inv.status AS status,
                GREATEST(DATEDIFF(CURDATE(), inv.invoiceDueDate), 0) AS days_overdue,
                GREATEST(COALESCE(inv.timestampCreator, '1970-01-01'),
                         COALESCE(inv.timestampUpdate, '1970-01-01')) AS last_updated
            FROM gibbonFinanceInvoice inv
            JOIN gibbonFinanceInvoicee ie
                   ON ie.gibbonFinanceInvoiceeID = inv.gibbonFinanceInvoiceeID
            JOIN gibbonFamilyAdult fa
                   ON fa.gibbonPersonID = ie.gibbonPersonID
            JOIN gibbonFamilyChild fc
                   ON fc.gibbonFamilyID = fa.gibbonFamilyID
            LEFT JOIN gibbonFinanceInvoiceFee invf
                   ON invf.gibbonFinanceInvoiceID = inv.gibbonFinanceInvoiceID
            LEFT JOIN gibbonFinanceBillingSchedule bs
                   ON bs.gibbonFinanceBillingScheduleID = inv.gibbonFinanceBillingScheduleID
            WHERE {' AND '.join(where)}
            GROUP BY inv.gibbonFinanceInvoiceID, fc.gibbonPersonID,
                     inv.paidAmount, inv.invoiceDueDate, inv.status, bs.name,
                     inv.timestampCreator, inv.timestampUpdate
            ORDER BY inv.invoiceDueDate DESC
            LIMIT %s
        """
        params.append(limit)
        try:
            with gibbon_conn() as conn:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql, tuple(params))
                    return list(cur.fetchall())
        except Exception as exc:
            print(f"[gibbon] fees pull skipped: {exc}")
            return []

    def fetch_person_status_maps(self) -> tuple[dict[str, bool], dict[str, bool]]:
        """Cheap change probe: ``({student_id: is_active}, {staff_id: is_active})``.

        Three columns, no joins — the sweep that makes tombstones work even
        when a status change wrote no audit row.  Emits nothing itself.
        """
        sql = """
            SELECT gibbonPersonID, status, gibbonRoleIDPrimary
            FROM gibbonPerson
            WHERE gibbonRoleIDPrimary IN (002, 003)
        """
        students: dict[str, bool] = {}
        staff: dict[str, bool] = {}
        with gibbon_conn() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql)
                for r in cur.fetchall():
                    target = students if int(r["gibbonRoleIDPrimary"]) == int(_STUDENT_ROLE) else staff
                    target[_as_id(r["gibbonPersonID"])] = r["status"] == "Full"
        return students, staff

    def fetch_persons_by_ids(self, ids: list[str]) -> tuple[list[dict[str, Any]],
                                                           list[dict[str, Any]]]:
        """Full student + staff rows for the given person ids (tombstone path)."""
        if not ids:
            return [], []
        id_list = ",".join(["%s"] * len(ids))
        params = tuple(_as_id(i) for i in ids)

        def _rows(role: str, sql_builder) -> list[dict[str, Any]]:
            with gibbon_conn() as conn:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql_builder(id_list), params)
                    out = list(cur.fetchall())
            for r in out:
                r["is_active" if role == _STUDENT_ROLE else "is_available"] = \
                    bool(int(r.get("is_active_raw", r.get("is_available_raw", 1)) or 0))
                r.pop("is_active_raw", None)
                r.pop("is_available_raw", None)
                if role == _STUDENT_ROLE:
                    raw = r.pop("grade_level_raw", 7)
                    try:
                        r["grade_level"] = min(12, max(1, int(str(raw).strip() or "7")))
                    except Exception:
                        r["grade_level"] = 7
            return out

        students = _rows(_STUDENT_ROLE, lambda id_list: f"""
            SELECT * FROM (
                SELECT
                    p.gibbonPersonID AS source_student_id,
                    COALESCE(NULLIF(p.studentID, ''), '') AS emirates_id,
                    p.firstName AS first_name,
                    p.surname AS last_name,
                    CASE p.gender WHEN 'M' THEN 'M' WHEN 'F' THEN 'F' ELSE 'U' END AS gender,
                    COALESCE(p.dob, '2015-01-01') AS date_of_birth,
                    COALESCE(MAX(yg.sequenceNumber), 7) AS grade_level_raw,
                    '{_ORG_CURRICULUM}' AS curriculum_track,
                    '{_ORG_NAME}' AS school_name,
                    '{_ORG_EMIRATE}' AS emirate,
                    COALESCE(MAX(NULLIF(p.address1Country, '')), 'UAE') AS nationality,
                    COALESCE(MAX(NULLIF(p.emailAlternate, '')), 'Parent/Guardian') AS parent_full_name,
                    COALESCE(MAX(NULLIF(p.email, '')), '{_ORG_PARENT_EMAIL}') AS parent_email,
                    COALESCE(MAX(NULLIF(p.phone1, '')), '{_ORG_PARENT_PHONE}') AS parent_phone,
                    'up_to_date' AS vaccination_status,
                    MAX(DATE(p.lastTimestamp)) AS enrollment_date,
                    MAX(CASE p.status WHEN 'Full' THEN 1 ELSE 0 END) AS is_active_raw
                FROM gibbonPerson p
                LEFT JOIN gibbonStudentEnrolment se
                       ON se.gibbonPersonID = p.gibbonPersonID
                LEFT JOIN gibbonYearGroup yg
                       ON yg.gibbonYearGroupID = se.gibbonYearGroupID
                WHERE p.gibbonPersonID IN ({id_list})
                GROUP BY p.gibbonPersonID, p.firstName, p.surname, p.gender, p.dob,
                         p.studentID, p.status
            ) t
        """)
        staff = _rows(_STAFF_ROLE, lambda id_list: f"""
            SELECT
                p.gibbonPersonID AS source_staff_id,
                CONCAT(p.firstName, ' ', p.surname) AS full_name,
                COALESCE(NULLIF(p.email, ''),
                         CONCAT('staff', p.gibbonPersonID, '@msoe.ae')) AS email,
                'teacher' AS role,
                'Mathematics' AS subjects,
                CASE p.status WHEN 'Full' THEN 1 ELSE 0 END AS is_available_raw,
                6 AS max_periods,
                0 AS assigned
            FROM gibbonPerson p
            WHERE p.gibbonPersonID IN ({id_list})
              AND p.gibbonRoleIDPrimary = {_STAFF_ROLE}
        """)
        return students, staff

    # -- incremental state ---------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        def raw(key: str) -> Optional[str]:
            try:
                return self.state_store.get(self.dialect, self.school_id, key)
            except Exception:
                return None

        def load_map(key: str) -> dict[str, bool]:
            try:
                parsed = json.loads(raw(key) or "{}")
            except ValueError:
                return {}
            if not isinstance(parsed, dict):
                return {}
            return {str(k): bool(v) for k, v in parsed.items()}

        return {
            "persons": raw(WM_PERSONS),
            "attendance": raw(WM_ATTENDANCE),
            "fees": raw(WM_FEES),
            "students": load_map(STATUS_MAP_STUDENTS),
            "staff": load_map(STATUS_MAP_STAFF),
        }

    def _save_state(self, *, persons: Optional[str] = None,
                    attendance: Optional[str] = None,
                    fees: Optional[str] = None,
                    students: Optional[dict[str, bool]] = None,
                    staff: Optional[dict[str, bool]] = None) -> None:
        try:
            if persons is not None:
                self.state_store.put(self.dialect, self.school_id, WM_PERSONS, persons)
            if attendance is not None:
                self.state_store.put(self.dialect, self.school_id, WM_ATTENDANCE, attendance)
            if fees is not None:
                self.state_store.put(self.dialect, self.school_id, WM_FEES, fees)
            if students is not None:
                self.state_store.put(self.dialect, self.school_id, STATUS_MAP_STUDENTS,
                                     json.dumps(students))
            if staff is not None:
                self.state_store.put(self.dialect, self.school_id, STATUS_MAP_STAFF,
                                     json.dumps(staff))
        except Exception as exc:
            # Losing a watermark costs a re-pull, never a failed sync.
            print(f"[gibbon] state save skipped: {exc}")

    # -- SourceConnector ----------------------------------------------------

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Incremental pull — full snapshot only when no watermark is stored.

        ``since`` (ISO-8601) overrides the attendance/fees watermark for one
        run; person changes still run through the status sweep because
        ``gibbonPerson`` has no person-level updated-at column.
        """
        import sis_adapter  # late import: sis_adapter loads our field maps

        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        state = self._load_state()
        force_full = bool(self.config.get("full")) or (
            since is None and state["persons"] is None)
        wanted = set(self.config.get("entities") or
                     {"students", "staff", "attendance", "fees"})

        try:
            new_person_wm = state["persons"]
            cur_students: dict[str, bool] = {}
            cur_staff: dict[str, bool] = {}

            # ---- people: insert watermark + status-sweep tombstones --------
            if "students" in wanted or "staff" in wanted:
                cur_students, cur_staff = self.fetch_person_status_maps()

                def _diff(cur: dict[str, bool], prev: dict[str, bool]
                          ) -> tuple[list[str], list[str]]:
                    changed = sorted(pid for pid, active in cur.items()
                                     if prev.get(pid) != active)
                    vanished = sorted(pid for pid in prev if pid not in cur)
                    return changed, vanished

                changed_students, vanished_students = _diff(cur_students, state["students"])
                changed_staff, vanished_staff = _diff(cur_staff, state["staff"])
                # Vanish lists only for entities this run is responsible for —
                # a students-only pull must not deactivate staff.
                batch.meta["vanished_student_source_ids"] = (
                    vanished_students if "students" in wanted else [])
                batch.meta["vanished_staff_source_ids"] = (
                    vanished_staff if "staff" in wanted else [])

                insert_since = None if force_full else (
                    int(state["persons"]) if state["persons"] else None)
                students_raw: list[dict[str, Any]] = []
                staff_raw: list[dict[str, Any]] = []
                if "students" in wanted:
                    students_raw = self.fetch_students(since_id=insert_since)
                if "staff" in wanted:
                    staff_raw = self.fetch_staff(since_id=insert_since)

                raw_student_ids = {_as_id(r.get("source_student_id"))
                                   for r in students_raw}
                raw_staff_ids = {_as_id(r.get("source_staff_id")) for r in staff_raw}

                # Status-changed people older than the insert watermark still
                # need a full row (the tombstone / reactivation path).
                need_students = [pid for pid in changed_students
                                 if pid not in raw_student_ids]
                need_staff = [pid for pid in changed_staff if pid not in raw_staff_ids]
                extra_students, extra_staff = self.fetch_persons_by_ids(
                    need_students + need_staff)
                if "students" in wanted:
                    students_raw.extend(extra_students)
                if "staff" in wanted:
                    staff_raw.extend(extra_staff)

                for row in students_raw:
                    mapped = sis_adapter.map_student(row, "gibbon", self.school_id)
                    # The gibbon student field map predates tombstones — carry
                    # is_active explicitly so deactivations reach the upsert.
                    mapped["is_active"] = bool(row.get("is_active", True))
                    if mapped.get("source_student_id") or mapped.get("emirates_id"):
                        batch.students.append(mapped)
                for row in staff_raw:
                    mapped = sis_adapter.map_staff(row, "gibbon", self.school_id)
                    if mapped.get("email"):
                        batch.staff.append(mapped)

                ids = [_as_id(r.get("source_student_id")) for r in students_raw] + \
                      [_as_id(r.get("source_staff_id")) for r in staff_raw]
                max_id = 0
                for pid in ids:
                    try:
                        max_id = max(max_id, int(pid))
                    except ValueError:
                        continue
                try:
                    prev_wm = int(state["persons"] or 0)
                except ValueError:
                    prev_wm = 0
                if force_full:
                    # Full snapshot: the watermark is whatever the snapshot
                    # reached (--full is a documented watermark reset).
                    new_person_wm = str(max_id) if max_id else state["persons"]
                elif max_id or prev_wm:
                    # Incremental: INSERT watermark is monotonic.  Status-change
                    # refetches carry old gibbonPersonIDs — they must never drag
                    # the watermark backwards (that would re-pull everyone above
                    # them on the next run).
                    new_person_wm = str(max(max_id, prev_wm))
                else:
                    new_person_wm = state["persons"]

            # ---- attendance / fees: timestamp watermarks ---------------------
            att_wm = since or (None if force_full else state["attendance"])
            if "attendance" in wanted:
                att_rows = self.fetch_attendance(since=att_wm)
                batch.meta["attendance_raw"] = len(att_rows)
                for row in att_rows:
                    last_upd = row.pop("last_updated", None)
                    mapped = sis_adapter.map_attendance(row, "gibbon", self.school_id)
                    if mapped.get("source_student_id") or mapped.get("student_id"):
                        batch.attendance.append(mapped)
                    if last_upd is not None:
                        text = str(last_upd)
                        if not batch.meta.get("attendance_wm") or \
                                text > str(batch.meta["attendance_wm"]):
                            batch.meta["attendance_wm"] = text

            fee_wm = since or (None if force_full else state["fees"])
            if "fees" in wanted:
                fee_rows = self.fetch_fees(since=fee_wm)
                batch.meta["fees_raw"] = len(fee_rows)
                for row in fee_rows:
                    last_upd = row.pop("last_updated", None)
                    mapped = sis_adapter.map_fee(row, "gibbon", self.school_id)
                    if mapped.get("source_student_id") or mapped.get("student_id"):
                        batch.fees.append(mapped)
                    if last_upd is not None:
                        text = str(last_upd)
                        if not batch.meta.get("fees_wm") or \
                                text > str(batch.meta["fees_wm"]):
                            batch.meta["fees_wm"] = text

            # ---- bookkeeping: watermarks + status maps -----------------------
            self._save_state(
                persons=new_person_wm,
                attendance=batch.meta.get("attendance_wm") or att_wm,
                fees=batch.meta.get("fees_wm") or fee_wm,
                students=cur_students if "students" in wanted else None,
                staff=cur_staff if "staff" in wanted else None,
            )
            self.watermark = new_person_wm
            self.rows_in_last_sync = {
                "students": len(batch.students),
                "staff": len(batch.staff),
                "attendance": len(batch.attendance),
                "fees": len(batch.fees),
                "tombstones": sum(1 for s in batch.students if not s.get("is_active")),
            }
            batch.meta["incremental"] = not force_full
            batch.meta["watermark"] = new_person_wm
            self._mark_success()
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Real-time Gibbon hooks reuse the canonical webhook router."""
        import sis_adapter

        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        try:
            data = payload.get("data") or payload
            event = str(payload.get("event") or "").lower()
            if "attendance" in event or "name" in data and "date" in data:
                batch.attendance.append(
                    sis_adapter.map_attendance(data, "gibbon", self.school_id))
            elif "fee" in event or "invoice" in data or "amount" in data:
                batch.fees.append(
                    sis_adapter.map_fee(data, "gibbon", self.school_id))
            elif "staff" in event or "gibbonStaffID" in data:
                batch.staff.append(
                    sis_adapter.map_staff(data, "gibbon", self.school_id))
            else:
                mapped = sis_adapter.map_student(data, "gibbon", self.school_id)
                if "is_active" in data:
                    mapped["is_active"] = bool(data["is_active"])
                batch.students.append(mapped)
            self._mark_success()
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["mode"] = "pull"
        base["rows_in_last_sync"] = dict(self.rows_in_last_sync)
        base["state_store"] = self._store_kind
        if self.watermark is None:
            try:
                self.watermark = self.state_store.get(self.dialect, self.school_id,
                                                      WM_PERSONS)
            except Exception:
                pass
        base["watermark"] = self.watermark
        try:
            conn = gibbon_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                base["status"] = "UNHEALTHY" if self.last_error else "OK"
            finally:
                conn.close()
        except Exception as exc:
            base["status"] = "UNHEALTHY"
            base["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return base
