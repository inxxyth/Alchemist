"""Durable connector state — delta tokens / watermarks per (connector, school_id).

Phase 3 (G.3): *Record the delta token per school_id so a second pull is
incremental.*  The token must survive a process restart — an in-memory dict
would silently re-pull a full roster every deploy and defeat the point.

Phase 4 (N.5): the same table records the **operational run snapshot** per
(connector, school_id) — ``last_success_at``, ``last_error``,
``rows_in_last_sync``, ``watermark`` — so ``GET /api/v1/sis/status`` can show
a client's IT lead real numbers after a restart, not just in-process memory.

Two implementations behind one interface:

* :class:`PgStateStore`   — Postgres ``connector_state`` (CREATE TABLE IF
  NOT EXISTS; additive only).  This is the production store.
* :class:`MemoryStateStore` — dict-backed, for tests and for hosts with no DB.

No secret handling: values are opaque provider tokens / run facts, never
credentials.  Never logged.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
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

DDL = """
CREATE TABLE IF NOT EXISTS connector_state (
    connector   VARCHAR(64)  NOT NULL,
    school_id   VARCHAR(64)  NOT NULL,
    state_key   VARCHAR(64)  NOT NULL,
    state_value TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (connector, school_id, state_key)
)
"""


class StateStore(ABC):
    """Key/value state scoped to (connector, school_id)."""

    @abstractmethod
    def get(self, connector: str, school_id: str, key: str) -> Optional[str]:
        ...

    @abstractmethod
    def put(self, connector: str, school_id: str, key: str, value: str) -> None:
        ...

    def list_school_ids(self, connector: str) -> list[str]:
        """School ids with any recorded state for ``connector`` (may be empty)."""
        return []


class MemoryStateStore(StateStore):
    """Dict-backed store — tests and DB-less hosts."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], str] = {}

    def get(self, connector: str, school_id: str, key: str) -> Optional[str]:
        return self._data.get((connector, school_id, key))

    def put(self, connector: str, school_id: str, key: str, value: str) -> None:
        self._data[(connector, school_id, key)] = value

    def list_school_ids(self, connector: str) -> list[str]:
        return sorted({sid for (c, sid, _k) in self._data if c == connector})


class PgStateStore(StateStore):
    """Postgres-backed store — survives restarts.  Additive DDL only."""

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config = dict(config or PG_CONFIG)

    def _connect(self):
        return psycopg2.connect(**self.config, connect_timeout=PG_CONNECT_TIMEOUT)

    def ensure_table(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)

    def get(self, connector: str, school_id: str, key: str) -> Optional[str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state_value FROM connector_state "
                        "WHERE connector = %s AND school_id = %s AND state_key = %s",
                        (connector, school_id, key),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        except psycopg2.Error:
            # A missing/unreachable store must not break the connector —
            # the caller falls back to a full pull and health() reports it.
            return None

    def put(self, connector: str, school_id: str, key: str, value: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO connector_state (connector, school_id, state_key, state_value, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (connector, school_id, state_key) DO UPDATE SET
                        state_value = EXCLUDED.state_value,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (connector, school_id, key, value, datetime.utcnow()),
                )

    def list_school_ids(self, connector: str) -> list[str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT school_id FROM connector_state "
                        "WHERE connector = %s ORDER BY school_id",
                        (connector,),
                    )
                    return [r[0] for r in cur.fetchall()]
        except psycopg2.Error:
            return []


# ---------------------------------------------------------------------------
# Operational run snapshot (N.5) — what /api/v1/sis/status reports per school
# ---------------------------------------------------------------------------

RUN_STATE_KEYS = ("last_success_at", "last_error", "rows_in_last_sync", "watermark")


def record_sync_run(store: StateStore, connector: str, school_id: str,
                    *, rows: Optional[dict[str, int]] = None,
                    watermark: Optional[str] = None,
                    error: Optional[str] = None) -> None:
    """Persist the outcome of one sync run for the status endpoint.

    ``error=None`` records success.  Values are operational facts only —
    never credentials, never row payloads.
    """
    now = datetime.utcnow().isoformat() + "Z"
    if error is None:
        store.put(connector, school_id, "last_success_at", now)
        store.put(connector, school_id, "last_error", "")
    else:
        store.put(connector, school_id, "last_error", str(error)[:300])
    if rows is not None:
        store.put(connector, school_id, "rows_in_last_sync",
                  json.dumps({str(k): int(v) for k, v in rows.items()}))
    if watermark is not None:
        store.put(connector, school_id, "watermark", str(watermark))


def read_sync_run(store: StateStore, connector: str, school_id: str) -> dict[str, Any]:
    """The durable run snapshot for (connector, school_id) — never raises."""
    def _get(key: str) -> Optional[str]:
        try:
            return store.get(connector, school_id, key)
        except Exception:
            return None

    rows_raw = _get("rows_in_last_sync") or ""
    try:
        rows = json.loads(rows_raw) if rows_raw else {}
        if not isinstance(rows, dict):
            rows = {}
    except ValueError:
        rows = {}
    error = _get("last_error") or ""
    return {
        "school_id": school_id,
        "last_success_at": _get("last_success_at") or None,
        "last_error": error or None,
        "rows_in_last_sync": rows,
        "watermark": _get("watermark") or None,
    }


_DEFAULT_STORE: Optional[StateStore] = None


def default_store() -> StateStore:
    """Production default: Postgres when reachable, else in-memory.

    Cached for the process so health probes don't re-open the database.
    ``/api/v1/sis/status`` surfaces which one is live via the connector
    health block — a silent in-memory fallback is reported, not hidden.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        try:
            store = PgStateStore()
            store.ensure_table()
            _DEFAULT_STORE = store
        except Exception:
            _DEFAULT_STORE = MemoryStateStore()
    return _DEFAULT_STORE
