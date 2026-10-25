"""CSV drop connector (Phase 4, N.4) — Tier 3 universal fallback.

"Their IT won't do an API."  A directory the school drops CSV files into;
this connector watches it (``pull()`` scans — scheduling is the operator's
cron/loop, same as the Gibbon bridge), validates every file against the
documented column contract (:mod:`connectors.csv_drop.field_maps`), stamps
``school_id`` from its own config, and **rejects and logs** anything
malformed.  Re-import is idempotent: a file whose sha256 is unchanged since
the last run is skipped, so a re-drop transfers 0 new rows.

Rejection rules (nothing malformed is partially ingested):
* unknown file name   → file ignored + logged
* missing required column  → file rejected + logged (file + column)
* missing required value   → row rejected + logged (file + line + column)
* ``school_id`` column in a file → ignored + logged (the connector stamps it)

Env var NAMES (never values):

    CSV_DROP_DIR    — drop directory (config ``directory=`` overrides)

state: per-file sha256 digests in :class:`connectors.state.StateStore`, keyed
``sha256.<filename>`` per school_id — durable across restarts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import time
from typing import Any, Optional

from connectors.base import (
    CanonicalBatch,
    SourceConnector,
    as_optional_date,
    normalize_grade,
    normalize_term_label,
)
from connectors.csv_drop.field_maps import (
    COLUMN_CONTRACT,
    FILE_TO_ENTITY,
    STAMPED_COLUMNS,
)
from connectors.state import StateStore, default_store

__all__ = ["CsvDropConnector", "CsvDropError", "validate_rows", "map_csv_files"]


class CsvDropError(RuntimeError):
    """The drop directory is unusable (not a per-file/per-row rejection)."""


def _is_true(raw: Any, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"true", "t", "1", "yes", "y"}


def _float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _int(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pure mapping — no I/O
# ---------------------------------------------------------------------------

def validate_rows(entity: str, rows: list[dict[str, Any]],
                  filename: str = "?") -> tuple[list[dict[str, Any]], list[str]]:
    """Validate + coerce rows against the column contract.

    Returns (accepted_rows_with_canonical_types, rejection_log).  Every
    rejection names file + line + column so the school's IT can fix the drop.
    """
    required, optional = COLUMN_CONTRACT[entity]
    known = set(required) | set(optional)
    accepted: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):  # line 1 is the header
        clean = {k.strip(): v for k, v in row.items() if k is not None}
        bad = False
        for col in required:
            if str(clean.get(col) or "").strip() == "":
                errors.append(f"{filename}: line {i}: missing required column "
                              f"value '{col}'")
                bad = True
        for col in STAMPED_COLUMNS:
            if col in clean and str(clean.get(col) or "").strip() != "":
                errors.append(f"{filename}: line {i}: column '{col}' ignored "
                              f"(school_id is stamped by the connector)")
        if bad:
            continue  # one bad row must not take the file down
        out: dict[str, Any] = {k: v for k, v in clean.items() if k in known}
        accepted.append(out)
    return accepted, errors


def map_csv_files(files: dict[str, list[dict[str, Any]]],
                  school_id: str = "default",
                  source: str = "csv_drop") -> CanonicalBatch:
    """Map validated CSV rows onto :class:`CanonicalBatch`, stamped."""
    batch = CanonicalBatch(source=source, school_id=school_id)
    errors = batch.errors

    for row in files.get("students", []):
        gender_raw = str(row.get("gender") or "").strip().upper()
        gender = ("M" if gender_raw.startswith("M") else
                  "F" if gender_raw.startswith("F") else "U")
        batch.students.append({
            "school_id": school_id,
            "source_student_id": str(row.get("source_student_id") or "").strip(),
            "emirates_id": str(row.get("emirates_id") or "").strip() or None,
            "first_name": str(row.get("first_name") or "").strip(),
            "last_name": str(row.get("last_name") or "").strip(),
            "gender": gender,
            "date_of_birth": as_optional_date(row.get("date_of_birth")),
            "grade_level": normalize_grade(row.get("grade_level"), default=1),
            "curriculum_track": str(row.get("curriculum_track") or "").strip() or "British",
            "school_name": str(row.get("school_name") or "").strip() or "CSV School",
            "emirate": str(row.get("emirate") or "").strip() or "Dubai",
            "nationality": str(row.get("nationality") or "").strip() or "UAE",
            "parent_full_name": str(row.get("parent_full_name") or "").strip() or "Parent/Guardian",
            "parent_email": str(row.get("parent_email") or "").strip(),
            "parent_phone": str(row.get("parent_phone") or "").strip(),
            "vaccination_status": str(row.get("vaccination_status") or "").strip() or "up_to_date",
            "vaccination_expiry": as_optional_date(row.get("vaccination_expiry")),
            "enrollment_date": as_optional_date(row.get("enrollment_date")),
            "is_active": _is_true(row.get("is_active"), default=True),
        })

    for row in files.get("attendance", []):
        day = as_optional_date(row.get("attendance_date"))
        if day is None:
            errors.append("attendance: unparseable attendance_date "
                          f"{row.get('attendance_date')!r}")
            continue
        batch.attendance.append({
            "school_id": school_id,
            "source_student_id": str(row.get("source_student_id") or "").strip(),
            "attendance_date": day,
            "status": _status(row.get("status")),
        })

    seen_invoices: set[tuple[str, str]] = set()
    for row in files.get("fees", []):
        sid = str(row.get("source_student_id") or "").strip()
        invoice = str(row.get("invoice_number") or "").strip()
        key = (sid, invoice)
        if key in seen_invoices:
            continue  # idempotent: one fee row per student per invoice
        seen_invoices.add(key)
        amount_due = _float(row.get("amount_due"))
        amount_paid = _float(row.get("amount_paid"))
        days_overdue = max(0, _int(row.get("days_overdue"), 0))
        status = str(row.get("status") or "").strip().lower()
        if status not in {"current", "partial", "overdue", "paid", "waived"}:
            if amount_paid <= 0 and amount_due > 0:
                status = "overdue" if days_overdue else "current"
            elif amount_paid < amount_due:
                status = "overdue" if days_overdue else "partial"
            else:
                status = "paid"
        batch.fees.append({
            "school_id": school_id,
            "source_student_id": sid,
            "invoice_number": invoice,
            "term_label": normalize_term_label(row.get("term_label")),
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "due_date": as_optional_date(row.get("due_date")),
            "status": status,
            "days_overdue": days_overdue,
        })

    for row in files.get("staff", []):
        subjects = str(row.get("subjects") or "").strip()
        batch.staff.append({
            "school_id": school_id,
            "source_staff_id": str(row.get("source_staff_id") or "").strip(),
            "full_name": str(row.get("full_name") or "").strip(),
            "email": str(row.get("email") or "").strip(),
            "role": str(row.get("role") or "teacher").strip()[:32],
            "subjects": [s.strip() for s in subjects.split(",") if s.strip()],
            "is_available": _is_true(row.get("is_available"), default=True),
            "max_periods": _int(row.get("max_periods"), 6),
            "assigned": _int(row.get("assigned"), 0),
        })

    for row in files.get("leave_requests", []):
        day = as_optional_date(row.get("leave_date"))
        if day is None:
            errors.append("leave_requests: unparseable leave_date "
                          f"{row.get('leave_date')!r}")
            continue
        batch.leave_requests.append({
            "school_id": school_id,
            "source_staff_id": str(row.get("source_staff_id") or "").strip(),
            "leave_date": day,
            "periods": _int(row.get("periods"), 1),
            "reason": str(row.get("reason") or "").strip() or "unspecified",
            "subject_needed": str(row.get("subject_needed") or "").strip() or "any",
        })
    return batch


_STATUS_MAP = {
    "present": "present", "p": "present", "here": "present",
    "late": "late", "tardy": "late",
    "excused": "excused", "excuse": "excused", "medical": "excused",
    "absent": "absent", "a": "absent", "n": "absent",
}


def _status(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    return _STATUS_MAP.get(s, "absent" if s else "absent")


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class CsvDropConnector(SourceConnector):
    """Directory-scanning CSV drop (Tier 3).

    Config keys:
      ``directory``     — drop directory   [env: CSV_DROP_DIR]
      ``state_store``   — file-hash store  (default: Postgres, else memory)
      ``move_processed``— archive imported files into ``<dir>/processed/``
                          (default False — the school may re-drop freely)
    """

    dialect = "csv_drop"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self.state_store: StateStore = config.get("state_store") or default_store()
        self._store_kind = type(self.state_store).__name__
        self.rows_in_last_sync: dict[str, int] = {}
        self.watermark: Optional[str] = None

    # -- directory (path only — never secret-bearing) -----------------------

    def _directory(self) -> str:
        return str(self.config.get("directory") or os.environ.get("CSV_DROP_DIR") or "")

    # -- SourceConnector ----------------------------------------------------

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        if payload.get("dialect") == "csv_drop":
            return True
        if payload.get("source") == "csv_drop":
            return True
        # Webhook shape: {"file": "students.csv", "rows": [...]} or a bare
        # {"students": [...]}-style body carrying contract column names.
        if payload.get("file") in FILE_TO_ENTITY and isinstance(payload.get("rows"), list):
            return True
        keys = set(payload.keys())
        return bool({"source_student_id", "source_staff_id"} & keys) and \
            bool({"attendance_date", "invoice_number", "first_name",
                  "full_name", "leave_date"} & keys)

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Scan the drop directory once.  ``since`` is unused: the drop is a
        snapshot; idempotency comes from the per-file sha256, not a delta."""
        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        try:
            directory = self._directory()
            if not directory or not os.path.isdir(directory):
                raise CsvDropError(
                    f"drop directory not found: {directory or '(unset)'} "
                    f"(set CSV_DROP_DIR or pass directory=)")

            accepted_files: dict[str, list[dict[str, Any]]] = {}
            meta_files: dict[str, Any] = {}
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if not os.path.isfile(path) or not name.lower().endswith(".csv"):
                    continue
                entity = FILE_TO_ENTITY.get(name.lower())
                if entity is None:
                    batch.errors.append(f"{name}: unknown file name — ignored "
                                       f"(contract names: "
                                       f"{', '.join(sorted(FILE_TO_ENTITY))})")
                    continue

                with open(path, "rb") as fh:
                    raw = fh.read()
                digest = hashlib.sha256(raw).hexdigest()
                state_key = f"sha256.{name.lower()}"
                try:
                    previous = self.state_store.get(self.dialect, self.school_id,
                                                    state_key)
                except Exception:
                    previous = None
                if previous == digest:
                    meta_files[name] = {"status": "skipped", "sha256": digest,
                                        "reason": "unchanged since last import"}
                    continue

                rows, file_errors = self._parse_csv(entity, name, raw)
                batch.errors.extend(file_errors)
                if rows is None:
                    meta_files[name] = {"status": "rejected", "sha256": digest,
                                        "errors": len(file_errors)}
                    continue  # file-level rejection: do not record its hash
                validated, row_errors = validate_rows(entity, rows, filename=name)
                batch.errors.extend(row_errors)
                accepted_files.setdefault(entity, []).extend(validated)
                meta_files[name] = {
                    "status": "imported", "sha256": digest,
                    "rows": len(validated), "rejected_rows":
                        sum(1 for e in row_errors if "line" in e),
                }
                try:
                    self.state_store.put(self.dialect, self.school_id,
                                         state_key, digest)
                except Exception:
                    pass  # a lost bookkeeping write re-imports; upserts are idempotent

            mapped = map_csv_files(accepted_files, school_id=self.school_id,
                                   source=self.dialect)
            mapped.errors.extend(batch.errors)
            mapped.meta["files"] = meta_files
            self.rows_in_last_sync = {
                "students": len(mapped.students),
                "staff": len(mapped.staff),
                "attendance": len(mapped.attendance),
                "fees": len(mapped.fees),
                "leave_requests": len(mapped.leave_requests),
                "tombstones": sum(1 for s in mapped.students
                                  if not s.get("is_active")),
            }
            # A re-scan of unchanged files transfers nothing; the watermark
            # is the scan time for the status endpoint.
            self.watermark = self._scan_watermark(meta_files)
            self._mark_success()
            return mapped
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def _parse_csv(self, entity: str, name: str,
                   raw: bytes) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
        """Parse one file; (None, errors) means **file-level rejection**."""
        required, optional = COLUMN_CONTRACT[entity]
        known = set(required) | set(optional) | set(STAMPED_COLUMNS)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return None, [f"{name}: not valid UTF-8 ({exc})"]
        reader = csv.DictReader(io.StringIO(text))
        header = [h.strip() for h in (reader.fieldnames or []) if h is not None]
        if not header:
            return None, [f"{name}: empty or headerless file"]
        missing = [c for c in required if c not in header]
        if missing:
            return None, [f"{name}: missing required column(s) {missing} "
                          f"(contract requires: {list(required)})"]
        errors: list[str] = []
        for col in header:
            if col not in known:
                errors.append(f"{name}: column '{col}' ignored (not in the contract)")
        rows = [dict(r) for r in reader]
        return rows, errors

    @staticmethod
    def _scan_watermark(meta_files: dict[str, Any]) -> str:
        if not meta_files:
            return ""
        imported = sum(1 for m in meta_files.values()
                       if m.get("status") == "imported")
        return f"scan:{imported}imported:{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Push shape for SFTP listeners: {"file": "students.csv", "rows": […]}
        or an entity-keyed body ({"students": […], …}) of contract-shaped rows.
        """
        try:
            row_errors: list[str] = []
            if payload.get("file") in FILE_TO_ENTITY and isinstance(payload.get("rows"), list):
                entity = FILE_TO_ENTITY[str(payload["file"])]
                validated, errs = validate_rows(
                    entity, [r for r in payload["rows"] if isinstance(r, dict)],
                    filename=str(payload["file"]))
                row_errors.extend(errs)
                batch = map_csv_files({entity: validated},
                                      school_id=self.school_id, source=self.dialect)
            else:
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                files = {}
                for entity in COLUMN_CONTRACT:
                    val = data.get(entity)
                    if isinstance(val, list):
                        validated, errs = validate_rows(
                            entity, [r for r in val if isinstance(r, dict)],
                            filename=f"webhook:{entity}")
                        files[entity] = validated
                        row_errors.extend(errs)
                if not files:
                    raise CsvDropError("payload carries no contract-shaped rows")
                batch = map_csv_files(files, school_id=self.school_id,
                                      source=self.dialect)
            batch.errors.extend(row_errors)
            self._mark_success()
            return batch
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["mode"] = "watch"
        base["rows_in_last_sync"] = dict(self.rows_in_last_sync)
        base["state_store"] = self._store_kind
        base["watermark"] = self.watermark
        directory = self._directory()
        if directory:
            base["directory"] = directory  # path only — not secret-bearing
        missing = []
        if not directory:
            missing.append("CSV_DROP_DIR")
        elif not os.path.isdir(directory):
            base["status"] = "UNHEALTHY"
            base["last_error"] = f"drop directory not found: {directory}"
            return base
        if missing:
            base["status"] = "NOT_CONFIGURED"
            base["missing_env"] = missing
        elif self.last_error:
            base["status"] = "UNHEALTHY"
        else:
            base["status"] = "OK"
        return base
