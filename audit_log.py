"""Audit log (Phase 6 / K.5) — who did what, when, and with what outcome.

Durable record of every approval decision, every side effect executed,
every sync run and every auth rejection. This is the inspector's answer
to "who approved this parent letter?".

Table ``audit_log`` is created with ``CREATE TABLE IF NOT EXISTS`` (the
same idempotent-migration rule as ``schema.sql`` — never DROP, never
destructive). See ``docs/compliance/DATA-RESIDENCY.md`` for the retention
horizon applied by ``retention_sweep.py``.

This module never raises into a request path: an audit write failure is
printed loudly to stderr and swallowed so the platform keeps serving —
the failure stays visible in the container log instead of breaking the
request it was meant to record.

Secrets are never stored. Callers that only have an unverified API key
are recorded as a fingerprint (sha256 prefix), never the key itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2

PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}
PG_CONNECT_TIMEOUT = 5

TABLE = "audit_log"

# event_type vocabulary — the four classes K.5 requires.
EVENT_APPROVAL = "approval_decision"
EVENT_SIDE_EFFECT = "side_effect"
EVENT_SYNC = "sync_run"
EVENT_AUTH = "auth_rejection"
EVENT_TYPES = frozenset({EVENT_APPROVAL, EVENT_SIDE_EFFECT, EVENT_SYNC, EVENT_AUTH})

# school_id stamped on rows where no client school could be resolved
# (missing/invalid API key). Queriable via ?school_id=unauthenticated.
UNAUTHENTICATED = "unauthenticated"

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL    PRIMARY KEY,
    school_id   VARCHAR(64)  NOT NULL DEFAULT 'default',
    actor       VARCHAR(128) NOT NULL,
    event_type  VARCHAR(48)  NOT NULL,
    action      VARCHAR(128) NOT NULL,
    outcome     VARCHAR(64)  NOT NULL,
    detail      JSONB        NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_school ON audit_log (school_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_type   ON audit_log (event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_when   ON audit_log (occurred_at);
"""

_schema_ready = False


def key_fingerprint(secret: str) -> str:
    """Non-reversible identifier for an API key that failed verification.

    Never store or log the key itself — a sha256 prefix is enough to tell
    two distinct bad keys apart in an investigation.
    """
    digest = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:12]}"


def ensure_schema() -> None:
    """Create the audit table on first use (idempotent)."""
    global _schema_ready
    if _schema_ready:
        return
    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    try:
        with conn.cursor() as cur:
            cur.execute(AUDIT_DDL)
        conn.commit()
        _schema_ready = True
    finally:
        conn.close()


def record(event_type: str, action: str, outcome: str,
           actor: str = "unknown", school_id: str = "default",
           detail: Optional[dict[str, Any]] = None) -> bool:
    """Append one audit row. Returns True on success; never raises.

    ``event_type`` must be one of EVENT_TYPES. ``actor`` is the human
    reviewer name when one exists, else the caller's school_id, else a
    key fingerprint — never a secret value.
    """
    try:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown audit event_type: {event_type!r}")
        ensure_schema()
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (school_id, actor, event_type, action, outcome, detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        school_id or UNAUTHENTICATED,
                        actor or "unknown",
                        event_type,
                        action,
                        outcome,
                        json.dumps(detail or {}, default=str),
                    ),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        print(f"[audit] WRITE FAILED event={event_type} action={action}: {exc!r}",
              file=sys.stderr, flush=True)
        return False


def query(school_id: str, since: Optional[str] = None,
          limit: int = 200) -> list[dict[str, Any]]:
    """Return audit rows for one school, newest first.

    ``since`` accepts an ISO date or datetime (e.g. ``2026-10-01`` or
    ``2026-10-01T00:00:00Z``); rows are filtered on ``occurred_at >= since``.
    """
    ensure_schema()
    clauses = ["school_id = %s"]
    params: list[Any] = [school_id]
    if since:
        ts = _parse_since(since)
        clauses.append("occurred_at >= %s")
        params.append(ts)
    params.append(max(1, min(int(limit), 1000)))
    sql = (
        "SELECT id, school_id, actor, event_type, action, outcome, detail, occurred_at "
        f"FROM audit_log WHERE {' AND '.join(clauses)} "
        "ORDER BY occurred_at DESC, id DESC LIMIT %s"
    )
    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = []
            for raw in cur.fetchall():
                row = dict(zip(cols, raw))
                occurred = row.get("occurred_at")
                if isinstance(occurred, datetime):
                    row["occurred_at"] = occurred.astimezone(timezone.utc).isoformat()
                rows.append(row)
            return rows
    finally:
        conn.close()


def _parse_since(value: str) -> datetime:
    """ISO date/datetime -> aware UTC datetime. Raises ValueError if unparseable."""
    text = value.strip()
    if not text:
        raise ValueError("empty since")
    # Accept a bare date as midnight UTC of that day.
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
