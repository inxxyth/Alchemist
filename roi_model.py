"""ROI instrumentation for Alchemist engagements.

Purpose
-------
Alchemist sells automation to K-12 schools on the claim that it returns
**legitimate** time and closes real risk. That claim has to be measurable
and challengeable, not marketing copy. This module records what each
automated run actually did and converts it into:

* **hours_returned**  — staff time the school did not have to spend
* **estimated_value** — hours_returned x an explicit, editable rate
* **risk_coverage**   — cases surfaced with a governing clause (a count of
                        coverage, never dressed up as dollars saved)

Honesty rules baked in
----------------------
1. We count *coordination and drafting time we removed*. We do **not**
   claim "cost avoided" on items the school still pays for (a substitute
   teacher's wage, a fee write-off, an avoided inspection fine). Those
   figures cannot be defended in a meeting, and an agency that inflates
   them loses the room.
2. Every minute-per-unit figure is conservative and lives in one dict so
   the client can see it, argue with it, and override it per engagement.
3. The report always returns its own assumptions alongside the numbers.
   A principal must be able to re-do the multiplication on paper.

Persistence
-----------
Events land in PostgreSQL (``roi_events``) via ``CREATE TABLE IF NOT EXISTS``
— additive only, never destructive. The engagement report is the evidence
trail you invoice against, so it has to survive a restart.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Configuration — same env contract as sis_adapter
# ---------------------------------------------------------------------------

PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}
PG_CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "5"))

# ---------------------------------------------------------------------------
# Task units — minutes of manual work removed per unit
# ---------------------------------------------------------------------------

# role keys into DEFAULT_ROLE_RATES_AED.
TASK_UNITS: dict[str, dict[str, Any]] = {
    "parent_notification_drafted": {
        "minutes": 4.0,
        "role": "admin",
        "service": "audit.compliance",
        "basis": (
            "Draft, policy-check and review one parent compliance "
            "notification by hand."
        ),
    },
    "policy_clause_matched": {
        "minutes": 6.0,
        "role": "compliance",
        "service": "audit.compliance",
        "basis": (
            "Locate and verify the governing policy clause for a risk case "
            "across the school's policy pack."
        ),
    },
    "student_risk_reviewed": {
        "minutes": 2.0,
        "role": "admin",
        "service": "audit.compliance",
        "basis": (
            "Triage one student's attendance, fee and vaccination records "
            "against the risk thresholds."
        ),
    },
    "substitute_cover_coordinated": {
        "minutes": 12.0,
        "role": "admin",
        "service": "operations.substitute-dispatch",
        "basis": (
            "Identify cover for one leave request: check availability and "
            "subject match, then contact candidates. "
            "NOTE: the school still pays the substitute — this is "
            "coordination time only, not wage avoided."
        ),
    },
    "lesson_gap_analysis": {
        "minutes": 20.0,
        "role": "coordinator",
        "service": "curriculum.harmonize",
        "basis": (
            "Curriculum coordinator gap analysis of one lesson plan against "
            "the MoE AI Competencies."
        ),
    },
    "executive_briefing_composed": {
        "minutes": 30.0,
        "role": "leadership",
        "service": "executive.query",
        "basis": (
            "Pull reports, cross-reference attendance/fee/staff data and "
            "write a sourced briefing for leadership."
        ),
    },
}

# Illustrative fully-loaded hourly costs in AED. Editable per engagement —
# the report always echoes whatever was used.
DEFAULT_ROLE_RATES_AED: dict[str, float] = {
    "admin": 100.0,
    "teacher": 120.0,
    "compliance": 150.0,
    "coordinator": 180.0,
    "leadership": 250.0,
}

CURRENCY_DEFAULT = os.environ.get("MSOE_CURRENCY", "AED")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS roi_events (
    roi_event_id    BIGSERIAL PRIMARY KEY,
    school_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    engagement_id   VARCHAR(64) NOT NULL DEFAULT 'default',
    service         VARCHAR(64) NOT NULL,
    unit            VARCHAR(64) NOT NULL,
    qty             NUMERIC(12,2) NOT NULL DEFAULT 1,
    minutes_each    NUMERIC(8,2) NOT NULL,
    role            VARCHAR(32) NOT NULL,
    minutes_total   NUMERIC(12,2) NOT NULL,
    detail          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE roi_events ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_roi_events_engagement ON roi_events (engagement_id);
CREATE INDEX IF NOT EXISTS idx_roi_events_service ON roi_events (service);
CREATE INDEX IF NOT EXISTS idx_roi_events_created ON roi_events (created_at);
"""


def _connect():
    return psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)


def ensure_roi_schema() -> None:
    """Create the roi_events table if missing. Additive only — never drops."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_units(units: dict[str, float],
                 service: str,
                 engagement_id: str = "default",
                 school_id: str = "default",
                 detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Persist one run's worth of automated work and return the ROI block.

    ``units`` maps a TASK_UNITS key to a quantity (e.g.
    ``{"parent_notification_drafted": 55}``). Unknown keys are ignored so a
    new call site can never crash a service over bookkeeping.
    """
    ensure_roi_schema()

    rows: list[tuple[str, float, float, str, float]] = []
    for unit, qty in units.items():
        spec = TASK_UNITS.get(unit)
        if not spec or not qty:
            continue
        q = float(qty)
        minutes_each = float(spec["minutes"])
        rows.append((
            unit, q, minutes_each, spec["role"], q * minutes_each,
        ))

    if not rows:
        return _empty_block(service)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            for unit, q, minutes_each, role, minutes_total in rows:
                cur.execute(
                    """
                    INSERT INTO roi_events
                        (school_id, engagement_id, service, unit, qty,
                         minutes_each, role, minutes_total, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (school_id, engagement_id, service, unit, q,
                     minutes_each, role, minutes_total,
                     psycopg2.extras.Json(detail or {})),
                )
        conn.commit()
    finally:
        conn.close()

    return _block_from_rows(service, rows)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fetch_events(engagement_id: str = "default",
                 since: Optional[datetime] = None,
                 service: Optional[str] = None) -> list[dict[str, Any]]:
    ensure_roi_schema()
    sql = (
        "SELECT service, unit, qty, minutes_each, role, minutes_total, created_at "
        "FROM roi_events WHERE engagement_id = %s"
    )
    params: list[Any] = [engagement_id]
    if since:
        sql += " AND created_at >= %s"
        params.append(since)
    if service:
        sql += " AND service = %s"
        params.append(service)
    sql += " ORDER BY created_at"

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = ["service", "unit", "qty", "minutes_each", "role",
                    "minutes_total", "created_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def build_report(events: Iterable[dict[str, Any]],
                 rates: Optional[dict[str, float]] = None,
                 currency: str = CURRENCY_DEFAULT) -> dict[str, Any]:
    """Turn raw events into a challengeable engagement report.

    The report always carries ``assumptions`` and ``method`` so the reader
    can redo the arithmetic. Nothing here is presented as a realised saving.
    """
    rates = rates or dict(DEFAULT_ROLE_RATES_AED)

    per_unit: dict[str, dict[str, Any]] = {}
    per_service: dict[str, dict[str, float]] = {}
    total_minutes = 0.0

    for ev in events:
        unit = ev["unit"]
        minutes = float(ev["minutes_total"] or 0)
        qty = float(ev["qty"] or 0)
        role = ev["role"]
        rate = rates.get(role, 0.0)
        total_minutes += minutes

        u = per_unit.setdefault(unit, {
            "unit": unit,
            "qty": 0.0,
            "minutes_each": float(ev["minutes_each"] or 0),
            "role": role,
            "rate_per_hour": rate,
            "minutes_total": 0.0,
            "value": 0.0,
            "basis": TASK_UNITS.get(unit, {}).get("basis", ""),
            "service": TASK_UNITS.get(unit, {}).get("service", ev["service"]),
        })
        u["qty"] += qty
        u["minutes_total"] += minutes
        u["value"] += (minutes / 60.0) * rate

        s = per_service.setdefault(ev["service"], {"minutes_total": 0.0, "value": 0.0})
        s["minutes_total"] += minutes
        s["value"] += (minutes / 60.0) * rate

    hours = total_minutes / 60.0
    value = sum(u["value"] for u in per_unit.values())

    return {
        "engagement_id": None,  # stamped by report_for()
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": currency,
        "totals": {
            "hours_returned": round(hours, 2),
            "estimated_value": round(value, 2),
            "unit_events": sum(int(u["qty"]) for u in per_unit.values()),
        },
        "per_unit": sorted(per_unit.values(), key=lambda x: -x["value"]),
        "per_service": per_service,
        "assumptions": {
            "rates_per_hour": rates,
            "task_units": {
                k: {"minutes": v["minutes"], "role": v["role"], "basis": v["basis"]}
                for k, v in TASK_UNITS.items()
            },
        },
        "method": (
            "hours_returned = sum(unit_qty x minutes_per_unit) / 60. "
            "estimated_value = hours_returned x that unit's role rate. "
            "Figures measure coordination and drafting time removed from "
            "staff. They do NOT claim wages avoided on substitutes, fee "
            "write-offs, or avoided regulatory penalties — the school still "
            "bears those. Rates are illustrative defaults and are editable "
            "per engagement; every figure above is reproducible from the "
            "assumptions block."
        ),
        "scope_note": (
            "Risk coverage (cases surfaced with a governing clause) is "
            "reported separately by the compliance service and is a measure "
            "of coverage, not of money saved."
        ),
    }


def report_for(engagement_id: str = "default",
               since: Optional[datetime] = None,
               service: Optional[str] = None,
               rates: Optional[dict[str, float]] = None) -> dict[str, Any]:
    events = fetch_events(engagement_id, since=since, service=service)
    rep = build_report(events, rates=rates)
    rep["engagement_id"] = engagement_id
    rep["event_count"] = len(events)
    if not events:
        rep["note"] = (
            "No ROI events recorded for this engagement yet. Run one of the "
            "four services — each run records what it automated."
        )
    return rep


# ---------------------------------------------------------------------------
# Block builders used by main.py call sites
# ---------------------------------------------------------------------------

def _empty_block(service: str) -> dict[str, Any]:
    return {
        "service": service,
        "hours_returned": 0.0,
        "estimated_value": 0.0,
        "currency": CURRENCY_DEFAULT,
        "units": [],
    }


def _block_from_rows(service: str,
                     rows: list[tuple[str, float, float, str, float]]) -> dict[str, Any]:
    rates = DEFAULT_ROLE_RATES_AED
    minutes_total = sum(r[4] for r in rows)
    value = sum((r[4] / 60.0) * rates.get(r[3], 0.0) for r in rows)
    return {
        "service": service,
        "hours_returned": round(minutes_total / 60.0, 3),
        "estimated_value": round(value, 2),
        "currency": CURRENCY_DEFAULT,
        "units": [
            {
                "unit": r[0],
                "qty": r[1],
                "minutes_each": r[2],
                "role": r[3],
                "minutes_total": r[4],
                "rate_per_hour": rates.get(r[3], 0.0),
                "basis": TASK_UNITS.get(r[0], {}).get("basis", ""),
            }
            for r in rows
        ],
        "method": (
            "hours_returned = sum(qty x minutes_each)/60; "
            "estimated_value = hours_returned x role rate. "
            "See /api/v1/roi/report for the full assumption set."
        ),
    }
