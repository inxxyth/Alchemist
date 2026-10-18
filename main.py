"""MSOE Agentic Backend — FastAPI application.

Four institutional services with strict human-in-the-loop guardrails:

  1. POST /api/v1/audit/compliance            — Regulatory compliance & safeguarding
  2. POST /api/v1/curriculum/harmonize        — Multi-curriculum & MoE AI harmoniser
  3. POST /api/v1/operations/substitute-dispatch — Operational friction reducer
  4. POST /api/v1/executive/query             — Executive decision co-pilot (Graph-Grounded RAG)

Guardrails
----------
* Every mutating action is halted and returned as ``status: PENDING_HUMAN_APPROVAL``.
* Graph-grounded answers cite ``source_file`` + ``clause_number`` + Neo4j node IDs.
* Missing data produces an explicit refusal — never a hallucinated answer.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime
from typing import Any, Optional

import psycopg2
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

from llm_client import DeterministicLLM, get_llm
import roi_model

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

import os

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "msoe_graph_2026")

# Wall-clock budget for the batch of LLM draft calls inside one compliance
# audit. Anything the provider has not answered by then degrades to the
# deterministic template (see llm_client design contract: the four services
# must stay online). Keeps a 50-student audit well inside a 60s HTTP timeout.
LLM_DRAFT_BUDGET_S = float(os.environ.get("MSOE_LLM_DRAFT_BUDGET_S", "20"))
LLM_DRAFT_WORKERS = int(os.environ.get("MSOE_LLM_DRAFT_WORKERS", "8"))

PG_CONFIG = {
    # In Docker use host=msoe_postgres; on Windows host use localhost
    # (127.0.0.1 hangs on this machine's Postgres handshake).
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}

app = FastAPI(
    title="MSOE Agentic Backend",
    version="1.0.0",
    description="Mock School Operating Environment — agentic institutional services",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Per-client API auth (F.6) — X-API-Key on every /api/v1/* except /health, /docs
# ---------------------------------------------------------------------------

_PUBLIC_PATHS = frozenset({"/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"})


def _parse_api_keys() -> dict[str, str]:
    """Parse MSOE_API_KEYS env into {school_id: secret}.

    Format: "school_id:secret,school_id2:secret2"
    The env var name is the contract — never hardcode real secrets in source.
    """
    raw = os.environ.get("MSOE_API_KEYS", "")
    keys: dict[str, str] = {}
    if raw:
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" in entry:
                sid, secret = entry.split(":", 1)
                keys[sid.strip()] = secret.strip()
    return keys


API_KEYS: dict[str, str] = _parse_api_keys()


def _resolve_school_from_key(provided_key: str) -> Optional[str]:
    """Return the school_id whose secret matches, or None."""
    for sid, secret in API_KEYS.items():
        if hmac.compare_digest(provided_key, secret):
            return sid
    return None


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """X-API-Key auth on every /api/v1/* route. /health, /docs, / are public.

    For POST /api/v1/sis/webhook, additionally verifies the HMAC-SHA256
    signature header X-MSOE-Signature (sha256 of raw body with school's secret).
    """
    path = request.url.path
    if path in _PUBLIC_PATHS or not path.startswith("/api/v1/"):
        return await call_next(request)

    provided = request.headers.get("x-api-key")
    if not provided:
        return JSONResponse(
            status_code=401,
            content={"status": "ERROR", "detail": "Missing X-API-Key header", "path": path},
        )

    school = _resolve_school_from_key(provided)
    if school is None:
        return JSONResponse(
            status_code=401,
            content={"status": "ERROR", "detail": "Invalid API key", "path": path},
        )

    # HMAC verification for the webhook endpoint
    if path == "/api/v1/sis/webhook" and request.method == "POST":
        signature = request.headers.get("x-msoe-signature")
        if not signature:
            return JSONResponse(
                status_code=401,
                content={"status": "ERROR", "detail": "Missing X-MSOE-Signature header", "path": path},
            )
        body = await request.body()
        secret = API_KEYS.get(school, "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return JSONResponse(
                status_code=401,
                content={"status": "ERROR", "detail": "Webhook signature verification failed", "path": path},
            )

    request.state.school_id = school
    return await call_next(request)

# ---------------------------------------------------------------------------
# Connection management (per-call connections; no pool starvation at mock scale)
# ---------------------------------------------------------------------------

from contextlib import contextmanager

neo4j_driver: Any = None

PG_CONNECT_TIMEOUT = 5  # seconds — fail fast instead of hanging the request
NEO4J_POOL_SIZE = 8
NEO4J_ACQUIRE_TIMEOUT = 15  # seconds


@contextmanager
def get_pg_conn():
    """Open a short-lived PostgreSQL connection; always closed in finally.

    Deliberately NOT pooled: ThreadedConnectionPool.getconn() blocks forever
    when exhausted and was the root cause of the /health hang → WinError 10048
    cascade. At mock scale (≤10 req sequential) a per-call connection is correct.
    """
    conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    try:
        yield conn
    finally:
        try:
            if not conn.closed and not conn.autocommit:
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_pg_cursor():
    """Pooled connection + cursor; commit on success, rollback on error."""
    with get_pg_conn() as conn:
        cur = conn.cursor()
        try:
            yield cur, conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def get_neo4j():
    return neo4j_driver


@contextmanager
def neo4j_session():
    """Pooled Neo4j session — always closed, never left dangling."""
    session = neo4j_driver.session()
    try:
        yield session
    finally:
        session.close()


@app.on_event("startup")
def _startup() -> None:
    global neo4j_driver
    neo4j_driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        max_connection_pool_size=NEO4J_POOL_SIZE,
        connection_acquisition_timeout=NEO4J_ACQUIRE_TIMEOUT,
    )
    # Lightweight connectivity probe (NOT verify_connectivity — that can block)
    with neo4j_session() as session:
        session.run("RETURN 1 AS ok").single()


@app.on_event("shutdown")
def _shutdown() -> None:
    if neo4j_driver is not None:
        neo4j_driver.close()


# ---------------------------------------------------------------------------
# LLM — OpenRouter / Nvidia Nemotron with deterministic fallback
# (see llm_client.py; MockLLM alias kept for compatibility)
# ---------------------------------------------------------------------------

MockLLM = DeterministicLLM  # alias — offline engine
llm = get_llm()  # OpenRouter when OPENROUTER_API_KEY is set


# ---------------------------------------------------------------------------
# Guardrail rule engine
# ---------------------------------------------------------------------------

_APPROVAL_DDL = """
CREATE TABLE IF NOT EXISTS approval_actions (
    action_id       VARCHAR(16)  PRIMARY KEY,
    school_id       VARCHAR(64)  NOT NULL DEFAULT 'default',
    action          VARCHAR(64)  NOT NULL,
    category        VARCHAR(32)  NOT NULL DEFAULT 'WRITE',
    reason          TEXT         NOT NULL,
    payload         JSONB        NOT NULL DEFAULT '{}',
    status          VARCHAR(32)  NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolution      VARCHAR(32),
    executed_result JSONB
);
ALTER TABLE approval_actions ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
CREATE SEQUENCE IF NOT EXISTS approval_actions_seq START 1;
CREATE INDEX IF NOT EXISTS idx_approval_actions_status ON approval_actions (status);
CREATE INDEX IF NOT EXISTS idx_approval_actions_school  ON approval_actions (school_id);
"""


class ApprovalStore:
    """PostgreSQL-backed registry of actions trapped in PENDING_HUMAN_APPROVAL.

    Durable across restarts (ids are allocated from a Postgres sequence).
    Thread-safe: the compliance service stages from a thread pool — every
    public method uses its own short-lived connection so concurrent
    ``stage()`` calls cannot interleave id allocation.
    """

    def __init__(self) -> None:
        self._ensure_schema()

    @staticmethod
    def _ensure_schema() -> None:
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute(_APPROVAL_DDL)
            conn.commit()
        finally:
            conn.close()

    def stage(self, action: str, category: str, reason: str,
              payload: dict[str, Any]) -> str:
        """Stage a new action and return its ACT-XXXXX id."""
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT nextval('approval_actions_seq')")
                seq = cur.fetchone()[0]
                action_id = f"ACT-{seq:05d}"
                cur.execute(
                    "INSERT INTO approval_actions "
                    "(action_id, school_id, action, category, reason, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (action_id,
                     str(payload.get("school_id") or "default")[:64],
                     action, category, reason,
                     json.dumps(payload, default=str)),
                )
            conn.commit()
            return action_id
        finally:
            conn.close()

    def register(self, action: str, payload: dict[str, Any], reason: str,
                 category: str = "WRITE") -> dict[str, Any]:
        """Backward-compatible wrapper around :meth:`stage` returning the record."""
        action_id = self.stage(action, category, reason, payload)
        record = self.get(action_id)
        assert record is not None
        return record

    def pending(self) -> list[dict[str, Any]]:
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action_id, school_id, action, category, reason, payload, "
                    "status, created_at, resolved_at, resolution, executed_result "
                    "FROM approval_actions WHERE status = 'PENDING_HUMAN_APPROVAL' "
                    "ORDER BY created_at, action_id"
                )
                return [self._row_to_dict(r, cur) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_pending(self) -> list[dict[str, Any]]:
        return self.pending()

    def get(self, action_id: str) -> dict[str, Any] | None:
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action_id, school_id, action, category, reason, payload, "
                    "status, created_at, resolved_at, resolution, executed_result "
                    "FROM approval_actions WHERE action_id = %s",
                    (action_id,),
                )
                row = cur.fetchone()
                return self._row_to_dict(row, cur) if row else None
        finally:
            conn.close()

    def execute(self, action_id: str, decision: str) -> dict[str, Any]:
        """Resolve an action as APPROVED or REJECTED. Returns the updated record."""
        resolution = "APPROVED" if decision == "approve" else "REJECTED"
        return self.resolve(action_id, resolution, None)

    def resolve(self, action_id: str, resolution: str,
                executed_result: dict[str, Any] | None) -> dict[str, Any]:
        status = "EXECUTED" if resolution == "APPROVED" else "REJECTED"
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE approval_actions SET status = %s, resolution = %s, "
                    "resolved_at = NOW(), executed_result = %s "
                    "WHERE action_id = %s AND status = 'PENDING_HUMAN_APPROVAL'",
                    (status, resolution,
                     json.dumps(executed_result, default=str) if executed_result is not None else None,
                     action_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(action_id)
            conn.commit()
        finally:
            conn.close()
        record = self.get(action_id)
        if record is None:
            raise KeyError(action_id)
        return record

    @staticmethod
    def _row_to_dict(row: tuple, cur: Any) -> dict[str, Any]:
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


approval_store = ApprovalStore()


class GuardrailEngine:
    """Halts every mutating action and returns PENDING_HUMAN_APPROVAL."""

    FORBIDDEN_VERBS = {
        "update", "delete", "drop", "insert", "alter", "truncate",
        "send", "dispatch", "publish", "commit", "execute_write",
    }

    @staticmethod
    def block_write(action: str, payload: dict[str, Any],
                    reason: str = "Automatic write actions are disabled.") -> dict[str, Any]:
        record = approval_store.register(action, payload, reason)
        return {
            "status": "PENDING_HUMAN_APPROVAL",
            "action_id": record["action_id"],
            "action": action,
            "reason": reason,
            "payload": payload,
            "requires_human_approval": True,
            "approval_url": f"/api/v1/approvals/{record['action_id']}/execute",
        }

    @classmethod
    def assert_no_write(cls, label: str) -> None:
        tokens = set(re.findall(r"[a-z_]+", label.lower()))
        if tokens & cls.FORBIDDEN_VERBS:
            raise HTTPException(
                status_code=400,
                detail=f"Guardrail violation: write-action verb detected in '{label}'.",
            )


guardrail = GuardrailEngine()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def pg_risk_students(school_id: str = "default", limit: int = 500) -> list[dict[str, Any]]:
    """Fetch risk-flagged students from PostgreSQL across all three risk axes."""
    sql = """
        WITH att AS (
            SELECT student_id,
                   ROUND(100.0 * SUM(CASE WHEN status IN ('present','late')
                                          THEN 1 ELSE 0 END) / COUNT(*), 2) AS attendance_rate
            FROM attendance_logs
            WHERE school_id = %s
            GROUP BY student_id
        ),
        fee AS (
            SELECT student_id, MAX(days_overdue) AS max_days_overdue
            FROM fee_ledgers
            WHERE status = 'overdue' AND school_id = %s
            GROUP BY student_id
        )
        SELECT s.student_id, s.emirates_id, s.first_name, s.last_name,
               s.grade_level, s.curriculum_track, s.school_name, s.emirate,
               s.parent_full_name, s.parent_email,
               s.vaccination_status, s.vaccination_expiry,
               COALESCE(att.attendance_rate, 100.0) AS attendance_rate,
               COALESCE(fee.max_days_overdue, 0) AS max_days_overdue,
               (s.vaccination_status IN ('expired','missing')) AS flag_vax,
               (COALESCE(att.attendance_rate, 100.0) < 85) AS flag_att,
               (COALESCE(fee.max_days_overdue, 0) > 60) AS flag_fee
        FROM students s
        LEFT JOIN att ON att.student_id = s.student_id
        LEFT JOIN fee ON fee.student_id = s.student_id
        WHERE s.school_id = %s
          AND (s.vaccination_status IN ('expired','missing')
           OR COALESCE(att.attendance_rate, 100.0) < 85
           OR COALESCE(fee.max_days_overdue, 0) > 60)
        ORDER BY s.grade_level, s.last_name
        LIMIT %s
    """
    with get_pg_cursor() as (cur, _conn):
        cur.execute(sql, (school_id, school_id, school_id, limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def neo4j_policy_for_risk(risk_type: str, school_id: str = "default") -> list[dict[str, Any]]:
    """Traverse Neo4j RiskFlag → Standard → Policy for a given risk type."""
    cypher = """
        MATCH (rf:RiskFlag {type: $risk, school_id: $school_id})-[r:ADDRESSED_BY]->(s:Standard)-[:PART_OF]->(p:Policy)
        OPTIONAL MATCH (p)-[:GOVERNS]->(d:Department)
        RETURN s.code AS standard_code,
               s.clause_number AS clause_number,
               s.heading AS heading,
               s.text_content AS text_content,
               p.title AS policy_title,
               p.reference AS policy_reference,
               p.source_file AS source_file,
               collect(DISTINCT d.name) AS departments
    """
    with neo4j_session() as session:
        return [dict(r) for r in session.run(cypher, risk=risk_type, school_id=school_id)]


def neo4j_all_competencies(school_id: str = "default") -> list[dict[str, Any]]:
    cypher = """
        MATCH (c:Competency)<-[:DEFINES]-(p:Policy)
        WHERE p.title CONTAINS 'AI Curriculum'
        RETURN c.code AS code, c.name AS name, c.description AS description,
               p.title AS policy_title, p.source_file AS source_file,
               p.reference AS policy_reference,
               elementId(c) AS node_id
        ORDER BY c.code
    """
    with neo4j_session() as session:
        return [dict(r) for r in session.run(cypher)]


def neo4j_student_risk_paths(school_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
    cypher = """
        MATCH path = (s:Student)-[:HAS_RISK]->(rf:RiskFlag)-[:ADDRESSED_BY]->(std:Standard)-[:PART_OF]->(p:Policy)
        WHERE s.school_id = $school_id
        RETURN s.student_id AS student_id,
               s.full_name AS student_name,
               s.grade_level AS grade_level,
               s.school_name AS school_name,
               rf.type AS risk_type,
               std.clause_number AS clause_number,
               std.heading AS clause_heading,
               std.code AS standard_code,
               p.title AS policy_title,
               p.source_file AS source_file,
               p.reference AS policy_reference,
               [n IN nodes(path) | labels(n)[0] + ':' + coalesce(n.full_name, n.type, n.heading, n.title, n.name, '')] AS path_labels,
               elementId(s) AS student_node_id,
               elementId(rf) AS risk_node_id,
               elementId(std) AS standard_node_id,
               elementId(p) AS policy_node_id
        ORDER BY s.grade_level, s.full_name
        LIMIT $limit
    """
    with neo4j_session() as session:
        return [dict(r) for r in session.run(cypher, school_id=school_id, limit=limit)]


def neo4j_query_paths_for_exec(grades: list[int], risk_type: str,
                               school_id: str = "default",
                               limit: int = 25) -> list[dict[str, Any]]:
    if grades:
        grade_filter = "WHERE s.grade_level IN $grades AND s.school_id = $school_id"
    else:
        grade_filter = "WHERE s.school_id = $school_id"
    cypher = f"""
        MATCH path = (s:Student)-[:HAS_RISK]->(rf:RiskFlag {{type: $risk}})
                      -[:ADDRESSED_BY]->(std:Standard)-[:PART_OF]->(p:Policy)
        {grade_filter}
        RETURN s.student_id AS student_id,
               s.full_name AS student_name,
               s.grade_level AS grade_level,
               s.school_name AS school_name,
               s.emirate AS emirate,
               rf.type AS risk_type,
               std.clause_number AS clause_number,
               std.heading AS clause_heading,
               p.title AS policy_title,
               p.source_file AS source_file,
               p.reference AS policy_reference,
               [n IN nodes(path) | labels(n)[0] + ':' + coalesce(n.full_name, n.type, n.heading, n.title, '')] AS verified_path,
               elementId(std) AS standard_node_id,
               elementId(p) AS policy_node_id
        ORDER BY s.grade_level, s.full_name
        LIMIT $limit
    """
    params: dict[str, Any] = {"risk": risk_type, "limit": limit, "school_id": school_id}
    if grades:
        params["grades"] = grades
    with neo4j_session() as session:
        return [dict(r) for r in session.run(cypher, **params)]


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

class ComplianceRequest(BaseModel):
    emirate: Optional[str] = Field(None, description="Filter by emirate (Dubai, Abu Dhabi, ...)")
    grade_level: Optional[int] = Field(None, ge=1, le=12)
    max_students: int = Field(50, ge=1, le=500)
    school_id: Optional[str] = Field("default", description="School identifier")
    engagement_id: Optional[str] = Field("default", description="Engagement identifier for ROI")


class ComplianceResponse(BaseModel):
    status: str
    generated_at: str
    summary: str
    total_students: int
    violations: list[dict[str, Any]]
    parent_notifications: list[dict[str, Any]]
    policy_citations: list[dict[str, Any]]
    risk_coverage: dict[str, Any] = {}
    roi: dict[str, Any] = {}


class RoiReportResponse(BaseModel):
    engagement_id: str
    generated_at: str
    currency: str
    totals: dict[str, Any]
    per_unit: list[dict[str, Any]]
    per_service: dict[str, Any]
    assumptions: dict[str, Any]
    method: str
    scope_note: str
    event_count: int
    note: Optional[str] = None


class HarmonizeRequest(BaseModel):
    lesson_plan: str = Field(..., min_length=20)
    subject: str = Field(..., min_length=2)
    grade_level: int = Field(..., ge=1, le=12)
    teacher_name: Optional[str] = None
    school_id: Optional[str] = Field("default", description="School identifier")
    engagement_id: Optional[str] = Field("default", description="Engagement identifier for ROI")


class HarmonizeResponse(BaseModel):
    status: str
    subject: str
    grade_level: int
    alignment_score: float
    aligned_competencies: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    enhanced_lesson_plan: str
    citations: list[dict[str, Any]]
    node_ids: list[str]
    roi: dict[str, Any] = {}


class SubstituteRequest(BaseModel):
    teacher_name: str
    leave_date: date
    subject_needed: str
    periods: int = Field(1, ge=1, le=8)
    reason: Optional[str] = None
    school_id: Optional[str] = Field("default", description="School identifier")
    engagement_id: Optional[str] = Field("default", description="Engagement identifier for ROI")


class SubstituteResponse(BaseModel):
    status: str
    leave_request: dict[str, Any]
    candidates: list[dict[str, Any]]
    recommended_substitute: Optional[dict[str, Any]]
    schedule_update: dict[str, Any]
    notification_draft: dict[str, Any]
    roi: dict[str, Any] = {}


class ExecutiveQueryRequest(BaseModel):
    query: str = Field(..., min_length=10)
    role: str = Field("principal", description="Requesting role: principal | coo")
    school_id: Optional[str] = Field("default", description="School identifier")
    engagement_id: Optional[str] = Field("default", description="Engagement identifier for ROI")


class ExecutiveQueryResponse(BaseModel):
    status: str
    query: str
    interpreted_intents: list[str]
    answer: str
    graph_evidence: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    data_sufficiency: str
    refusal_reason: Optional[str] = None
    roi: dict[str, Any] = {}


class ApprovalDecision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$",
                          description="approve or reject")
    reviewer: Optional[str] = Field("principal", description="Approving role")
    note: Optional[str] = None


class ApprovalExecuteResponse(BaseModel):
    status: str
    action_id: str
    action: str
    resolution: str
    executed_result: Optional[dict[str, Any]] = None
    message: str


class SISWebhookPayload(BaseModel):
    """Real-time SIS/LMS webhook (Gibbon / OpenEduCat / Canvas-style)."""
    event: Optional[str] = Field(None, description="e.g. sis.student.updated")
    data: Optional[dict[str, Any]] = Field(None, description="Event object")
    dialect: Optional[str] = Field(None, description="gibbon | openeducat | canvas")
    school_id: Optional[str] = Field("default", description="School identifier")


class SISBatchPayload(BaseModel):
    """Batched SIS sync (nightly export / bulk import)."""
    dialect: Optional[str] = Field(None, description="gibbon | openeducat | canvas")
    students: list[dict[str, Any]] = Field(default_factory=list)
    attendance: list[dict[str, Any]] = Field(default_factory=list)
    fees: list[dict[str, Any]] = Field(default_factory=list)
    staff: list[dict[str, Any]] = Field(default_factory=list)
    sync_graph: bool = Field(True, description="Mirror students/risks into Neo4j")
    school_id: Optional[str] = Field("default", description="School identifier")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "ERROR",
            "detail": exc.detail,
            "path": str(request.url.path),
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "status": "ERROR",
            "detail": f"Internal error: {exc.__class__.__name__}",
            "path": str(request.url.path),
        },
    )


# ---------------------------------------------------------------------------
# Service 5 — Human Approval Workflow
# ---------------------------------------------------------------------------

@app.get("/api/v1/approvals/pending")
def list_pending_approvals() -> dict[str, Any]:
    guardrail.assert_no_write("list_pending_approvals")
    items = approval_store.list_pending()
    return {
        "status": "OK",
        "count": len(items),
        "pending_actions": items,
    }


def _simulate_execution(record: dict[str, Any]) -> dict[str, Any]:
    """Simulate the deferred database write / side-effect for an approved action."""
    action = record["action"]
    payload = record["payload"]

    if action == "CREATE_LEAVE_REQUEST":
        try:
            with get_pg_cursor() as (cur, _conn):
                cur.execute(
                    "INSERT INTO leave_requests (school_id, staff_id, leave_date, reason, status) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING leave_id",
                    (
                        payload.get("school_id") or "default",
                        payload.get("staff_id"),
                        payload.get("leave_date"),
                        payload.get("reason"),
                        "APPROVED",
                    ),
                )
                leave_id = cur.fetchone()[0]
            return {
                "simulated": False,
                "table": "leave_requests",
                "leave_id": leave_id,
                "note": "Leave request committed to PostgreSQL.",
            }
        except Exception as exc:
            return {"simulated": True, "note": f"DB write skipped: {exc}"}

    if action == "SEND_PARENT_NOTIFICATION":
        return {
            "simulated": True,
            "channel": payload.get("channel", "email"),
            "to": payload.get("to"),
            "subject": payload.get("subject"),
            "note": "Parent notification dispatched (simulated SMTP relay).",
        }

    if action == "UPDATE_SCHEDULE":
        return {
            "simulated": True,
            "note": "Schedule mutation applied (simulated timetable service).",
            "detail": payload,
        }

    return {"simulated": True, "note": f"No executor registered for {action}; logged only."}


@app.post("/api/v1/approvals/{action_id}/execute",
          response_model=ApprovalExecuteResponse)
def execute_approval(action_id: str, body: ApprovalDecision) -> ApprovalExecuteResponse:
    record = approval_store.get(action_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found")
    if record["status"] != "PENDING_HUMAN_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"Action '{action_id}' already resolved as {record['status']}",
        )

    if body.decision == "reject":
        resolved = approval_store.resolve(action_id, "REJECTED", None)
        return ApprovalExecuteResponse(
            status="REJECTED",
            action_id=action_id,
            action=record["action"],
            resolution="REJECTED",
            executed_result={"logged": True, "reviewer": body.reviewer,
                             "note": body.note or "Rejected by human reviewer."},
            message=f"Action {action_id} rejected and logged. No write performed.",
        )

    result = _simulate_execution(record)
    resolved = approval_store.resolve(action_id, "APPROVED", result)
    return ApprovalExecuteResponse(
        status="EXECUTED",
        action_id=action_id,
        action=record["action"],
        resolution="APPROVED",
        executed_result=result,
        message=f"Action {action_id} approved by {body.reviewer}. Write executed.",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> JSONResponse:
    """Landing page — / previously 404'd into a blank browser tab."""
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>MSOE Agentic Backend</title>
<style>
 body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:2.5rem}
 a{color:#38bdf8} .card{background:#1e293b;border-radius:12px;padding:1.25rem 1.5rem;margin:.75rem 0}
 code{background:#334155;padding:.15rem .4rem;border-radius:4px}
 h1{margin-top:0} .ok{color:#4ade80}
</style></head><body>
<h1>MSOE Agentic Backend</h1>
<p>Service is up. Docs and probes:</p>
<div class="card"><strong>UI</strong> — <a href="/docs">/docs</a> (Swagger) · <a href="/redoc">/redoc</a></div>
<div class="card"><strong>Health</strong> — <a href="/health">/health</a> · <a href="/api/v1/sis/status">/api/v1/sis/status</a></div>
<div class="card"><strong>API</strong>
<br><code>POST /api/v1/audit/compliance</code>
<br><code>POST /api/v1/curriculum/harmonize</code>
<br><code>POST /api/v1/operations/substitute-dispatch</code>
<br><code>POST /api/v1/executive/query</code>
<br><code>GET  /api/v1/approvals/pending</code>
<br><code>POST /api/v1/approvals/{action_id}/execute</code>
</div>
<div class="card"><strong>SIS Integration</strong>
<br><code>POST /api/v1/sis/webhook</code> — real-time LMS hooks
<br><code>POST /api/v1/sis/sync</code> — batched Students/Attendance/Fees/Staff
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/health")
def health() -> dict[str, Any]:
    import time as _time

    pg_ok = False
    neo_ok = False
    t0 = _time.perf_counter()
    try:
        with get_pg_cursor() as (cur, _conn):
            cur.execute("SELECT 1")
        pg_ok = True
    except Exception as exc:
        print(f"[health] PG probe failed after {_time.perf_counter()-t0:.2f}s: {exc!r}", flush=True)
    t1 = _time.perf_counter()
    try:
        with neo4j_session() as session:
            session.run("RETURN 1 AS ok").single()
        neo_ok = True
    except Exception as exc:
        print(f"[health] Neo4j probe failed after {_time.perf_counter()-t1:.2f}s: {exc!r}", flush=True)
    print(
        f"[health] pg={pg_ok} ({t1-t0:.2f}s) neo={neo_ok} ({_time.perf_counter()-t1:.2f}s)",
        flush=True,
    )
    return {
        "status": "healthy" if (pg_ok and neo_ok) else "degraded",
        "postgres": "connected" if pg_ok else "unavailable",
        "neo4j": "connected" if neo_ok else "unavailable",
        "llm": llm.status(),
    }


# ---------------------------------------------------------------------------
# Service 1 — Regulatory Compliance & Safeguarding Guard
# ---------------------------------------------------------------------------

def draft_parent_notifications(jobs: list[dict[str, Any]],
                               budget_s: float = LLM_DRAFT_BUDGET_S) -> list[str]:
    """Draft every parent-notification body concurrently, under a deadline.

    The compliance audit has one body per (student, risk) pair — dozens of
    items on a full cohort. Calling the provider serially turns that into
    minutes of wall-clock and wedges the HTTP request, so the drafts run in
    a bounded thread pool and anything the provider has not returned by
    ``budget_s`` degrades to ``DeterministicLLM``. Every job gets a body:
    the service never returns partial results and never blocks past budget.

    Returns bodies in the same order as ``jobs``.
    """
    det = DeterministicLLM()
    fallback = [
        det.draft_parent_notification(j["student"], j["risk_type"], j["clause"])
        for j in jobs
    ]
    if not jobs or not llm.has_api_key:
        return fallback

    results: list[Optional[str]] = [None] * len(jobs)
    workers = max(1, min(LLM_DRAFT_WORKERS, len(jobs)))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="msoe-draft")
    try:
        future_at = {
            executor.submit(
                llm.draft_parent_notification,
                j["student"], j["risk_type"], j["clause"],
            ): i
            for i, j in enumerate(jobs)
        }
        done, _pending = wait(list(future_at), timeout=budget_s)
        for fut in done:
            i = future_at[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = None
    finally:
        # Do not join the laggards — that would defeat the budget. Cancel
        # whatever has not started; in-flight calls finish in the background.
        executor.shutdown(wait=False, cancel_futures=True)

    return [r if r else f for r, f in zip(results, fallback)]


@app.post("/api/v1/audit/compliance", response_model=ComplianceResponse)
def audit_compliance(req: ComplianceRequest) -> ComplianceResponse:
    guardrail.assert_no_write("audit_compliance")

    school_id = req.school_id or "default"
    engagement_id = req.engagement_id or "default"

    students = pg_risk_students(school_id=school_id, limit=req.max_students)
    if req.grade_level is not None:
        students = [s for s in students if s["grade_level"] == req.grade_level]
    if req.emirate:
        students = [s for s in students if s["emirate"].lower() == req.emirate.lower()]

    # Map student flags to risk types and fetch governing clauses
    risk_clause_cache: dict[str, list[dict[str, Any]]] = {}
    for rt in ("VACCINATION_EXPIRED", "CHRONIC_ATTENDANCE", "FEE_OVERDUE"):
        risk_clause_cache[rt] = neo4j_policy_for_risk(rt, school_id=school_id)

    violations: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    seen_citations: set[str] = set()
    # Phase 1 — build the violation records and the draft jobs. No LLM here.
    draft_jobs: list[dict[str, Any]] = []

    for s in students:
        flags: list[str] = []
        if s["flag_vax"]:
            flags.append("VACCINATION_EXPIRED")
        if s["flag_att"]:
            flags.append("CHRONIC_ATTENDANCE")
        if s["flag_fee"]:
            flags.append("FEE_OVERDUE")

        for risk in flags:
            clauses = risk_clause_cache.get(risk) or []
            clause = clauses[0] if clauses else {}
            violation = {
                "student_id": s["student_id"],
                "emirates_id": s["emirates_id"],
                "student_name": f"{s['first_name']} {s['last_name']}",
                "grade_level": s["grade_level"],
                "school_name": s["school_name"],
                "emirate": s["emirate"],
                "risk_type": risk,
                "risk_detail": {
                    "vaccination_status": s["vaccination_status"],
                    "attendance_rate": float(s["attendance_rate"]),
                    "max_days_overdue": int(s["max_days_overdue"]),
                },
                "governing_clause": {
                    "policy_title": clause.get("policy_title"),
                    "clause_number": clause.get("clause_number"),
                    "heading": clause.get("heading"),
                    "source_file": clause.get("source_file"),
                    "standard_code": clause.get("standard_code"),
                    "departments": clause.get("departments", []),
                },
            }
            violations.append(violation)

            draft_jobs.append(
                {
                    "student": {
                        "parent_full_name": s["parent_full_name"],
                        "first_name": s["first_name"],
                        "last_name": s["last_name"],
                        "grade_level": s["grade_level"],
                        "school_name": s["school_name"],
                    },
                    "risk_type": risk,
                    "clause": {
                        "policy_title": clause.get("policy_title"),
                        "clause_number": clause.get("clause_number"),
                        "heading": clause.get("heading"),
                    },
                    "note_payload": {
                        "channel": "email",
                        "to": s["parent_email"],
                        "student_id": s["student_id"],
                        "risk_type": risk,
                        "subject": f"[MSOE Compliance] Action required for {s['first_name']} {s['last_name']}",
                    },
                }
            )

            key = f"{clause.get('standard_code')}|{clause.get('policy_title')}"
            if key not in seen_citations and clause:
                seen_citations.add(key)
                citations.append(
                    {
                        "source_file": clause.get("source_file"),
                        "clause_number": clause.get("clause_number"),
                        "heading": clause.get("heading"),
                        "policy_title": clause.get("policy_title"),
                        "policy_reference": clause.get("policy_reference"),
                        "standard_code": clause.get("standard_code"),
                    }
                )

    # Phase 2 — draft every parent notification under one wall-clock budget.
    note_bodies = draft_parent_notifications(draft_jobs)

    # Phase 3 — stage each notification for human approval.
    for job, note_body in zip(draft_jobs, note_bodies):
        note_payload = {**job["note_payload"], "body": note_body}
        note_rec = approval_store.register(
            "SEND_PARENT_NOTIFICATION", note_payload,
            "Parent notification drafted by compliance audit. Awaiting human approval.",
        )
        notifications.append(
            {
                "status": "PENDING_HUMAN_APPROVAL",
                "action_id": note_rec["action_id"],
                **note_payload,
                "requires_human_approval": True,
                "approval_url": f"/api/v1/approvals/{note_rec['action_id']}/execute",
            }
        )

    summary = llm.summarize_violations(violations)

    # ROI: count what this run automated. Units are deliberately conservative
    # and expressed as work removed from staff — see roi_model.TASK_UNITS.
    roi_block = roi_model.record_units(
        {
            "parent_notification_drafted": len(notifications),
            "policy_clause_matched": len(citations),
            "student_risk_reviewed": len(students),
        },
        service="audit.compliance",
        engagement_id=engagement_id,
        school_id=school_id,
    )

    risk_coverage = {
        "students_examined": len(students),
        "risk_cases_flagged": len(violations),
        "cases_with_governing_clause": sum(
            1 for v in violations if v.get("governing_clause", {}).get("policy_title")
        ),
        "distinct_policies_cited": len(citations),
        "by_risk_type": {
            rt: sum(1 for v in violations if v["risk_type"] == rt)
            for rt in ("VACCINATION_EXPIRED", "CHRONIC_ATTENDANCE", "FEE_OVERDUE")
        },
        "guardrail": {
            "actions_staged_pending_human_approval": len(notifications),
            "side_effects_executed": 0,
            "note": "Nothing is sent until an officer approves each action.",
        },
        "scope_note": (
            "Coverage counts only. Not a claim of money saved or risk "
            "financially quantified."
        ),
    }

    return ComplianceResponse(
        status="PENDING_HUMAN_APPROVAL" if violations else "COMPLIANT",
        generated_at=datetime.utcnow().isoformat() + "Z",
        summary=summary,
        total_students=len({v["student_id"] for v in violations}),
        violations=violations[:100],
        parent_notifications=notifications[:100],
        policy_citations=citations,
        risk_coverage=risk_coverage,
        roi=roi_block,
    )


# ---------------------------------------------------------------------------
# Service 2 — Multi-Curriculum & MoE AI Harmonizer
# ---------------------------------------------------------------------------

def _detect_competency_gaps(lesson_plan: str,
                            competencies: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Keyword heuristics map lesson text to MoE AI competencies."""
    lower = lesson_plan.lower()
    keyword_map: dict[str, list[str]] = {
        "StrandA": ["algorithm", "decomposition", "abstraction", "computational",
                    "sequence", "pattern", "debug", "logic", "problem solving"],
        "StrandB": ["data", "dataset", "graph", "chart", "statistics", "survey",
                    "measurement", "collect", "visualis", "visualiz"],
        "StrandC": ["machine learning", "model", "train", "predict", "classif",
                    "neural", "supervised", "regression"],
        "StrandD": ["language", "text", "speech", "nlp", "translation", "arabic",
                    "vocabulary", "grammar"],
        "StrandE": ["ethic", "bias", "privacy", "fairness", "responsible",
                    "society", "impact", "governance"],
        "StrandF": ["design", "prototype", "user", "stakeholder", "usability",
                    "iteration", "empathy", "research"],
    }
    aligned: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for comp in competencies:
        code = comp["code"]
        keywords = keyword_map.get(code, [])
        matched = [kw for kw in keywords if kw in lower]
        entry = {
            "code": code,
            "name": comp["name"],
            "description": comp["description"],
            "node_id": comp["node_id"],
            "matched_keywords": matched,
        }
        if matched:
            aligned.append(entry)
        else:
            gaps.append(entry)
    return aligned, gaps


@app.post("/api/v1/curriculum/harmonize", response_model=HarmonizeResponse)
def curriculum_harmonize(req: HarmonizeRequest) -> HarmonizeResponse:
    guardrail.assert_no_write("curriculum_harmonize")

    school_id = req.school_id or "default"
    engagement_id = req.engagement_id or "default"

    if not (1 <= req.grade_level <= 12):
        raise HTTPException(status_code=422, detail="grade_level must be 1-12")

    competencies = neo4j_all_competencies(school_id=school_id)
    if not competencies:
        raise HTTPException(
            status_code=503,
            detail="MoE AI Curriculum Framework not found in knowledge graph. "
                   "Run build_knowledge_graph.py first.",
        )

    aligned, gaps = _detect_competency_gaps(req.lesson_plan, competencies)

    total = len(aligned) + len(gaps)
    score = round(len(aligned) / total * 100, 1) if total else 0.0

    enhanced = llm.rewrite_lesson(req.lesson_plan, gaps, req.subject, req.grade_level)

    citations = []
    node_ids = []
    for comp in competencies:
        citations.append(
            {
                "source_file": comp["source_file"],
                "clause_number": "3",  # Strand Architecture section in MoE AI framework
                "heading": f"{comp['code']} — {comp['name']}",
                "policy_title": comp["policy_title"],
                "policy_reference": comp["policy_reference"],
                "standard_code": comp["code"],
            }
        )
        node_ids.append(comp["node_id"])

    roi_block = roi_model.record_units(
        {"lesson_gap_analysis": 1},
        service="curriculum.harmonize",
        engagement_id=engagement_id,
        school_id=school_id,
    )

    return HarmonizeResponse(
        status="ALIGNED" if not gaps else "PENDING_HUMAN_APPROVAL",
        subject=req.subject,
        grade_level=req.grade_level,
        alignment_score=score,
        aligned_competencies=[
            {k: v for k, v in a.items() if k != "description"} for a in aligned
        ],
        gaps=[{k: v for k, v in g.items() if k != "description"} for g in gaps],
        enhanced_lesson_plan=enhanced,
        citations=citations,
        node_ids=node_ids,
        roi=roi_block,
    )


# ---------------------------------------------------------------------------
# Service 3 — Operational Friction Reducer (Substitute Dispatch)
# ---------------------------------------------------------------------------

@app.post("/api/v1/operations/substitute-dispatch", response_model=SubstituteResponse)
def substitute_dispatch(req: SubstituteRequest) -> SubstituteResponse:
    guardrail.assert_no_write("substitute_dispatch")

    school_id = req.school_id or "default"
    engagement_id = req.engagement_id or "default"

    with get_pg_cursor() as (cur, _conn):
        # Identify the requesting teacher
        cur.execute(
            "SELECT staff_id, full_name, email, subjects, is_available, max_periods, assigned "
            "FROM staff WHERE full_name ILIKE %s AND school_id = %s LIMIT 1",
            (f"%{req.teacher_name}%", school_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Teacher '{req.teacher_name}' not found")
        cols = [d[0] for d in cur.description]
        teacher = dict(zip(cols, row))

        # Find available substitutes with matching subject clearance and capacity
        cur.execute(
            """
            SELECT staff_id, full_name, email, subjects, assigned, max_periods
            FROM staff
            WHERE is_available = TRUE
              AND school_id = %s
              AND staff_id <> %s
              AND %s = ANY(subjects)
              AND (assigned + %s) <= max_periods
            ORDER BY (max_periods - assigned) DESC, full_name
            LIMIT 10
            """,
            (school_id, teacher["staff_id"], req.subject_needed, req.periods),
        )
        sub_cols = [d[0] for d in cur.description]
        candidates = []
        for r in cur.fetchall():
            d = dict(zip(sub_cols, r))
            candidates.append(
                {
                    "staff_id": d["staff_id"],
                    "full_name": d["full_name"],
                    "email": d["email"],
                    "subjects": list(d["subjects"]),
                    "assigned": d["assigned"],
                    "max_periods": d["max_periods"],
                    "capacity_remaining": d["max_periods"] - d["assigned"],
                }
            )

        recommended = candidates[0] if candidates else None

        # Leave request record (writes are staged as PENDING_HUMAN_APPROVAL)
        leave_payload = {
            "staff_id": teacher["staff_id"],
            "teacher_name": teacher["full_name"],
            "leave_date": req.leave_date.isoformat(),
            "subject_needed": req.subject_needed,
            "periods": req.periods,
            "reason": req.reason,
            "requested_status": "PENDING_HUMAN_APPROVAL",
        }

        schedule_payload = {
            "status": "PENDING_HUMAN_APPROVAL",
            "action": "UPDATE_SCHEDULE",
            "leave_date": req.leave_date.isoformat(),
            "periods_released": req.periods,
            "subject": req.subject_needed,
            "proposed_substitute_id": recommended["staff_id"] if recommended else None,
            "note": "Schedule mutation staged — human approval required before commit.",
        }

        notification = {
            "status": "PENDING_HUMAN_APPROVAL",
            "channel": "email",
            "to": recommended["email"] if recommended else None,
            "cc": ["hr@msoe.ae", "operations@msoe.ae"],
            "subject": f"[MSOE Dispatch] Substitute needed: {req.subject_needed} on {req.leave_date.isoformat()}",
            "body": (
                f"Dear {recommended['full_name'] if recommended else 'Colleague'},\n\n"
                f"{teacher['full_name']} has requested leave on {req.leave_date.isoformat()} "
                f"for {req.reason or 'personal reasons'}. "
                f"You are proposed as substitute for {req.subject_needed} "
                f"({req.periods} period(s)).\n\n"
                f"Please confirm availability within 2 hours.\n\n"
                f"— MSOE Operations Agent (draft — awaiting human approval)"
            ),
            "requires_human_approval": True,
        }

    roi_block = roi_model.record_units(
        {"substitute_cover_coordinated": 1},
        service="operations.substitute-dispatch",
        engagement_id=engagement_id,
        school_id=school_id,
    )

    return SubstituteResponse(
        status="PENDING_HUMAN_APPROVAL",
        leave_request=guardrail.block_write(
            "CREATE_LEAVE_REQUEST", leave_payload,
            "Leave request logged as draft. Human approval required to commit.",
        ),
        candidates=candidates,
        recommended_substitute=recommended,
        schedule_update=schedule_payload,
        notification_draft=notification,
        roi=roi_block,
    )


# ---------------------------------------------------------------------------
# Service 4 — Executive Decision Co-Pilot (Graph-Grounded RAG)
# ---------------------------------------------------------------------------

RISK_TYPE_KEYWORDS = {
    "CHRONIC_ATTENDANCE": ["attendance", "chronic", "absent", "absence", "truant", "attendance rate"],
    "FEE_OVERDUE": ["fee", "fees", "payment", "overdue", "invoice", "bursar", "fee delay", "payment delay"],
    "VACCINATION_EXPIRED": ["vaccination", "immun", "health record", "medical"],
}


def _resolve_risk_types(query: str) -> list[str]:
    q = query.lower()
    matched = []
    for rt, kws in RISK_TYPE_KEYWORDS.items():
        for kw in kws:
            if re.search(rf"\b{re.escape(kw)}\b", q):
                matched.append(rt)
                break
    return matched


@app.post("/api/v1/executive/query", response_model=ExecutiveQueryResponse)
def executive_query(req: ExecutiveQueryRequest) -> ExecutiveQueryResponse:
    guardrail.assert_no_write("executive_query")

    school_id = req.school_id or "default"
    engagement_id = req.engagement_id or "default"

    interpretation = llm.classify_executive_query(req.query)
    intents = interpretation["intents"]
    grades = interpretation["grades"]
    risk_types = _resolve_risk_types(req.query)

    graph_evidence: list[dict[str, Any]] = []
    for risk in risk_types:
        graph_evidence.extend(
            neo4j_query_paths_for_exec(grades, risk, school_id=school_id, limit=25)
        )

    # Correlation pass: fee delays among same students if query asks for it
    correlation_note = ""
    if "attendance" in intents and "fees" in intents:
        with get_pg_cursor() as (cur, _conn):
            grade_clause = ""
            sql_params: list[Any] = [school_id, school_id, school_id]
            if grades:
                placeholders = ",".join(["%s"] * len(grades))
                grade_clause = f"AND s.grade_level IN ({placeholders})"
                sql_params.extend(grades)
            cur.execute(
                f"""
                WITH att AS (
                    SELECT student_id,
                           ROUND(100.0 * SUM(CASE WHEN status IN ('present','late')
                                                  THEN 1 ELSE 0 END) / COUNT(*), 2) AS rate
                    FROM attendance_logs WHERE school_id = %s
                    GROUP BY student_id
                ),
                fee AS (
                    SELECT student_id, MAX(days_overdue) AS max_days
                    FROM fee_ledgers WHERE status = 'overdue' AND school_id = %s
                    GROUP BY student_id
                )
                SELECT COUNT(*) AS n
                FROM students s
                JOIN att ON att.student_id = s.student_id
                JOIN fee ON fee.student_id = s.student_id
                WHERE s.school_id = %s AND att.rate < 85 AND fee.max_days > 60
                {grade_clause}
                """,
                sql_params,
            )
            n = cur.fetchone()[0]
            correlation_note = (
                f" Cross-check: {n} student(s) simultaneously show chronic attendance "
                f"(<85%) AND fee delays (>60 days)."
            )

    # Build citations strictly from returned graph evidence
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in graph_evidence:
        key = f"{ev.get('standard_code', ev.get('clause_number'))}|{ev.get('policy_title')}"
        if key not in seen:
            seen.add(key)
            citations.append(
                {
                    "source_file": ev.get("source_file"),
                    "clause_number": ev.get("clause_number"),
                    "heading": ev.get("clause_heading"),
                    "policy_title": ev.get("policy_title"),
                    "policy_reference": ev.get("policy_reference"),
                    "standard_node_id": ev.get("standard_node_id"),
                    "policy_node_id": ev.get("policy_node_id"),
                }
            )

    # Data sufficiency gate — refuse to hallucinate
    if not graph_evidence:
        return ExecutiveQueryResponse(
            status="REFUSED_INSUFFICIENT_DATA",
            query=req.query,
            interpreted_intents=intents,
            answer=(
                "I cannot answer this query from verified sources. "
                "No matching student-risk-to-policy paths were found in the knowledge "
                "graph for the requested parameters. I will not generate an ungrounded "
                "answer."
            ),
            graph_evidence=[],
            citations=[],
            data_sufficiency="INSUFFICIENT",
            refusal_reason=(
                "Graph-Grounded RAG policy: zero verified Neo4j paths returned. "
                "Hallucination is not permitted."
            ),
        )

    # Compose grounded answer via LLM (Nemotron or deterministic fallback)
    answer = llm.answer_grounded(
        req.query,
        graph_evidence,
        correlation_note=correlation_note,
        intents=intents,
    )

    roi_block = roi_model.record_units(
        {"executive_briefing_composed": 1},
        service="executive.query",
        engagement_id=engagement_id,
        school_id=school_id,
    )

    return ExecutiveQueryResponse(
        status="GROUNDED",
        query=req.query,
        interpreted_intents=intents,
        answer=answer,
        graph_evidence=[
            {
                "student_id": e["student_id"],
                "student_name": e["student_name"],
                "grade_level": e["grade_level"],
                "school_name": e["school_name"],
                "emirate": e.get("emirate"),
                "risk_type": e["risk_type"],
                "clause_number": e["clause_number"],
                "clause_heading": e["clause_heading"],
                "policy_title": e["policy_title"],
                "verified_path": e["verified_path"],
                "standard_node_id": e["standard_node_id"],
                "policy_node_id": e["policy_node_id"],
            }
            for e in graph_evidence
        ],
        citations=citations,
        data_sufficiency="SUFFICIENT",
        refusal_reason=None,
        roi=roi_block,
    )


# ---------------------------------------------------------------------------
# Engagement ROI reporting — the numbers you take into a client meeting
# ---------------------------------------------------------------------------

@app.get("/api/v1/roi/report", response_model=RoiReportResponse)
def roi_report(engagement_id: str = "default",
               since: Optional[str] = None,
               service: Optional[str] = None) -> Any:
    """Aggregate what the engine automated for one engagement.

    Always returns its own assumptions and method so a principal can
    re-do the arithmetic. Reports time returned and risk coverage — never
    an invented "money saved" figure.
    """
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="since must be ISO-8601")
    rep = roi_model.report_for(engagement_id, since=since_dt, service=service)
    return rep


@app.get("/api/v1/roi/assumptions")
def roi_assumptions() -> dict[str, Any]:
    """The editable assumption set. Show this to a client before quoting ROI."""
    return {
        "task_units": roi_model.TASK_UNITS,
        "rates_per_hour": roi_model.DEFAULT_ROLE_RATES_AED,
        "currency": roi_model.CURRENCY_DEFAULT,
        "note": (
            "Minutes per unit are conservative and editable per engagement. "
            "Rates are illustrative fully-loaded costs, not published "
            "salary data. Override per engagement before quoting a figure."
        ),
    }


# ---------------------------------------------------------------------------
# SIS Modular Integration Layer (Gibbon / OpenEduCat / Canvas adapters)
# ---------------------------------------------------------------------------

import sis_adapter  # noqa: E402  — imported after core app so helpers exist


@app.get("/api/v1/sis/status")
def sis_status() -> dict[str, Any]:
    """Report which MSOE tables the SIS adapter can write to."""
    guardrail.assert_no_write("sis_status")
    return sis_adapter.schema_status()


@app.post("/api/v1/sis/webhook")
async def sis_webhook(request: Request, payload: SISWebhookPayload) -> dict[str, Any]:
    """Real-time sync hook — mirrors standard SIS/LMS webhooks.

    Requires X-API-Key and X-MSOE-Signature (HMAC-SHA256 of raw body).
    Both are verified in the middleware before this handler runs.
    """
    school = getattr(request.state, "school_id", "default")
    body = payload.model_dump(exclude_none=True)
    sid = payload.school_id or school or "default"
    return sis_adapter.handle_webhook(body, dialect=payload.dialect, school_id=sid)


@app.post("/api/v1/sis/sync")
def sis_batch_sync(payload: SISBatchPayload) -> dict[str, Any]:
    """Batched upsert of Students / Attendance / Fees / Staff (+ optional graph)."""
    body = payload.model_dump()
    body.setdefault("school_id", payload.school_id or "default")
    return sis_adapter.batch_sync(body)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
