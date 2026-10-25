"""Gibbon SIS → MSOE bridge (thin shim — C.3).

The fetch/mapping implementation lives in :mod:`connectors.gibbon`; this
script stays so the RUNBOOK commands keep working unchanged::

  python gibbon_bridge.py --full
  python gibbon_bridge.py --students-only

Maps Gibbon (MySQL) tables into the MSOE relational store and knowledge
graph via :mod:`sis_adapter`:

  gibbonPerson              → students / staff
  gibbonAttendanceLogPerson → attendance_logs
  gibbonFinanceInvoice      → fee_ledgers  (best-effort)
  gibbonStaff               → staff

Honours ``school_id`` and the F.8 org literals (env with demo defaults).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import sis_adapter
from connectors.gibbon.connector import GIBBON_MYSQL, GibbonConnector, gibbon_conn

# Re-export the connection config so existing imports keep working.
__all__ = ["GIBBON_MYSQL", "gibbon_conn", "map_students_to_canonical", "sync_all", "main"]


def map_students_to_canonical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy helper — canonical projection of raw Gibbon student rows."""
    out = []
    for r in rows:
        out.append({
            "source_student_id": r.get("source_student_id"),
            "emirates_id": r.get("emirates_id"),
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "gender": r.get("gender"),
            "date_of_birth": r.get("date_of_birth"),
            "grade_level": r.get("grade_level"),
            "curriculum_track": r.get("curriculum_track"),
            "school_name": r.get("school_name"),
            "emirate": r.get("emirate"),
            "nationality": r.get("nationality"),
            "parent_full_name": r.get("parent_full_name"),
            "parent_email": r.get("parent_email"),
            "parent_phone": r.get("parent_phone"),
            "vaccination_status": r.get("vaccination_status"),
            "enrollment_date": r.get("enrollment_date"),
            "is_active": bool(r.get("is_active", True)),
        })
    return out


def sync_all(students_only: bool = False,
             school_id: str = sis_adapter.DEFAULT_SCHOOL_ID,
             full: bool = False) -> dict[str, Any]:
    """Pull from Gibbon via the connector and upsert through sis_adapter.

    Incremental by default (N.3): the connector carries watermarks in
    ``connector_state``, so a second run transfers 0 new rows.  Pass
    ``full=True`` (``--full``) for a complete snapshot + watermark reset.
    """
    config: dict[str, Any] = {"full": full}
    if students_only:
        config["entities"] = ["students"]
    connector = GibbonConnector(school_id=school_id, **config)
    batch = connector.pull()
    summary = sis_adapter.ingest_batch(batch, sync_graph=True)
    summary["source"] = "gibbon"
    summary["watermark"] = connector.watermark
    summary["rows_in_last_sync"] = dict(connector.rows_in_last_sync)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Gibbon → MSOE bridge")
    parser.add_argument("--full", action="store_true",
                        help="full snapshot (ignore + reset watermarks)")
    parser.add_argument("--students-only", action="store_true")
    args = parser.parse_args()
    try:
        result = sync_all(students_only=args.students_only, full=args.full)
    except Exception as exc:
        print(f"[bridge] FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
