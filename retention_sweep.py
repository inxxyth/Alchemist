"""Retention sweep (Phase 6 / K.6) — delete only what is past its horizon.

Sweeps two tables, and only these two:

    attendance_logs  (attendance_date)
    audit_log        (occurred_at)

Rows older than the configured horizon are deleted. **Students are never
deleted** — not by this script, not under any retention rule. The sweep
hard-codes its table allowlist; anything outside it is unreachable.

Usage (run inside the API container):

    python retention_sweep.py              # DRY-RUN: lists candidates, deletes nothing
    python retention_sweep.py --apply      # deletes over-horizon rows only

Horizon: ``MSOE_RETENTION_DAYS`` (default 365), overridable with ``--days N``.
``--apply`` refuses horizons under 30 days as a footgun guard.

The retention policy per data category is documented in
``docs/compliance/DATA-RESIDENCY.md``.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg2

PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}
PG_CONNECT_TIMEOUT = 5

RETENTION_ENV = "MSOE_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 365
MIN_APPLY_DAYS = 30  # footgun guard: --apply refuses anything shorter

# The ONLY tables this sweep may touch. (table, time column, kind)
ALLOWED_TABLES: dict[str, tuple[str, str]] = {
    "attendance_logs": ("attendance_date", "date"),
    "audit_log": ("occurred_at", "timestamptz"),
}

SAMPLE_LIMIT = 5


def horizon_days(cli_days: int | None) -> int:
    if cli_days is not None:
        return int(cli_days)
    raw = os.environ.get(RETENTION_ENV, "").strip()
    if raw:
        return int(raw)
    return DEFAULT_RETENTION_DAYS


def cutoff_for(kind: str, days: int, now: datetime | None = None):
    """Cutoff for a column kind, derived from ONE clock (FX.6).

    ``now`` is a single aware UTC instant; the date cutoff is derived from
    ``now.date()`` and the timestamp cutoff from ``now`` itself, so both
    horizons always describe the same "today".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if kind == "date":
        return now.date() - timedelta(days=days)
    return now - timedelta(days=days)


def _candidates(cur, table: str, column: str, kind: str, cutoff) -> tuple[int, list[tuple]]:
    """Count over-horizon rows and sample a few for the dry-run listing."""
    cur.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} < %s",
        (cutoff,),
    )
    count = cur.fetchone()[0]
    sample: list[tuple] = []
    if count:
        id_col = "attendance_id" if table == "attendance_logs" else "id"
        cur.execute(
            f"SELECT {id_col}, {column} FROM {table} WHERE {column} < %s "
            f"ORDER BY {column} ASC LIMIT {SAMPLE_LIMIT}",
            (cutoff,),
        )
        sample = cur.fetchall()
    return count, sample


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete attendance_logs / audit_log rows past the retention horizon. "
                    "Dry-run by default; --apply required to delete. Never touches students."
    )
    parser.add_argument("--apply", action="store_true",
                        help="actually delete over-horizon rows (default: dry-run)")
    parser.add_argument("--days", type=int, default=None,
                        help=f"horizon in days (default: {RETENTION_ENV} env, "
                             f"then {DEFAULT_RETENTION_DAYS})")
    args = parser.parse_args()

    try:
        days = horizon_days(args.days)
    except ValueError:
        print(f"ERROR: invalid horizon value (check --days / {RETENTION_ENV})", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"retention_sweep mode={mode}  horizon_days={days}  env={RETENTION_ENV}")
    if args.apply and days < MIN_APPLY_DAYS:
        print(
            f"ERROR: refusing --apply with horizon {days}d — minimum is {MIN_APPLY_DAYS}d. "
            f"This guard exists so a typo cannot wipe the history tables.",
            file=sys.stderr,
        )
        return 2

    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    deleted_total = 0
    try:
        with conn.cursor() as cur:
            # Proof line: students are counted before and after and never deleted.
            cur.execute("SELECT COUNT(*) FROM students")
            students_before = cur.fetchone()[0]

            # FX.6 — one clock for every cutoff in this run.
            now = datetime.now(timezone.utc)
            for table, (column, kind) in ALLOWED_TABLES.items():
                cutoff = cutoff_for(kind, days, now=now)
                count, sample = _candidates(cur, table, column, kind, cutoff)
                print(f"\n[{table}] column={column}  cutoff={cutoff}  over_horizon_rows={count}")
                for row_id, when in sample:
                    print(f"    candidate id={row_id}  {column}={when}")
                if count > len(sample):
                    print(f"    … and {count - len(sample)} more")

                if args.apply and count:
                    cur.execute(
                        f"DELETE FROM {table} WHERE {column} < %s",
                        (cutoff,),
                    )
                    deleted = cur.rowcount
                    deleted_total += deleted
                    print(f"    DELETED {deleted} row(s) from {table}")
                elif args.apply:
                    print("    nothing to delete")
                else:
                    print("    (dry-run — nothing deleted; re-run with --apply)")

            cur.execute("SELECT COUNT(*) FROM students")
            students_after = cur.fetchone()[0]

        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    print(f"\nstudents before={students_before} after={students_after} "
          f"({'UNCHANGED — sweep never deletes students' if students_before == students_after else 'CHANGED — THIS IS A BUG'})")
    if students_before != students_after:
        return 1
    if not args.apply:
        print("DRY-RUN complete — no rows deleted. Re-run with --apply to execute.")
    else:
        print(f"APPLY complete — {deleted_total} over-horizon row(s) deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
