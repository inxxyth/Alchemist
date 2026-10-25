"""MSOE Knowledge Graph builder.

Solves the graph cold-start problem by:
  1. Connecting to local Neo4j and creating uniqueness constraints.
  2. Parsing a policy pack's markdown into Policy / Standard / Competency nodes.
  3. Bridging flagged SIS records from PostgreSQL into Student / RiskFlag nodes.
  4. Running a multi-hop verification query and printing a summary report.

Usage:
    python build_knowledge_graph.py                # MERGE-only (non-destructive)
    python build_knowledge_graph.py --pack DIR     # load a specific policy pack
    python build_knowledge_graph.py --school-id S  # scope the load to one school
    python build_knowledge_graph.py --wipe         # DETACH DELETE then rebuild
    python build_knowledge_graph.py --wipe-only    # DETACH DELETE and exit

The pack markdown format is specified in ``policy_vault/FORMAT.md`` and checked
by ``validate_policy_pack.py``.  The three maps that used to be hardcoded here
(``RISK_POLICY_MAP``, ``POLICY_DEPT_RULES``, ``DEPARTMENTS``) now live in
``<pack>/pack.config.yml`` and are read through ``policy_pack`` — the same
module ``sis_adapter`` uses, so the ADDRESSED_BY link is resolved identically
on both paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
from neo4j import GraphDatabase

import policy_pack
from policy_pack import (
    PackConfig,
    iter_pack_documents,
    load_pack_config,
    pack_content_hash,
    risk_policy_entry,
)

# ---------------------------------------------------------------------------
# Config — all credentials from env (F.5). Demo defaults preserved.
# ---------------------------------------------------------------------------

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "msoe_graph_2026")

PG_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "msoe_db"),
    "user": os.environ.get("PGUSER", "msoe_admin"),
    "password": os.environ.get("PGPASSWORD", "msoe_secure_2026"),
}

POLICY_VAULT = Path(__file__).resolve().parent / "policy_vault"

# The three maps that used to be hardcoded here — RISK_POLICY_MAP,
# POLICY_DEPT_RULES and DEPARTMENTS — now live in <pack>/pack.config.yml and
# are read through policy_pack.load_pack_config().  See policy_vault/FORMAT.md
# §3 and policy_vault/README.md.

# Risk type → SQL predicate (students meeting any of these are ingested)
RISK_QUERIES: dict[str, str] = {
    "CHRONIC_ATTENDANCE": """
        SELECT s.student_id, s.emirates_id, s.first_name, s.last_name,
               s.grade_level, s.curriculum_track, s.school_name, s.emirate,
               s.parent_email,
               ROUND(100.0 * SUM(CASE WHEN a.status IN ('present','late')
                                      THEN 1 ELSE 0 END) / COUNT(*), 2) AS attendance_rate
        FROM students s
        JOIN attendance_logs a ON a.student_id = s.student_id
        GROUP BY s.student_id, s.emirates_id, s.first_name, s.last_name,
                 s.grade_level, s.curriculum_track, s.school_name, s.emirate,
                 s.parent_email
        HAVING SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)::decimal
               / COUNT(*) < 0.85
        ORDER BY s.student_id
    """,
    "VACCINATION_EXPIRED": """
        SELECT s.student_id, s.emirates_id, s.first_name, s.last_name,
               s.grade_level, s.curriculum_track, s.school_name, s.emirate,
               s.parent_email,
               s.vaccination_status, s.vaccination_expiry
        FROM students s
        WHERE s.vaccination_status IN ('expired', 'missing')
        ORDER BY s.student_id
    """,
    "FEE_OVERDUE": """
        SELECT DISTINCT s.student_id, s.emirates_id, s.first_name, s.last_name,
               s.grade_level, s.curriculum_track, s.school_name, s.emirate,
               s.parent_email,
               MAX(f.days_overdue) OVER (PARTITION BY s.student_id) AS max_days_overdue
        FROM students s
        JOIN fee_ledgers f ON f.student_id = s.student_id
        WHERE f.status = 'overdue' AND f.days_overdue > 60
        ORDER BY s.student_id
    """,
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PolicySection:
    clause_number: str
    heading: str
    content: str


@dataclass
class ParsedPolicy:
    title: str
    reference: str
    source_file: str
    content_hash: str = ""          # sha256 of the document bytes (revision key)
    sections: list[PolicySection] = field(default_factory=list)
    competencies: list[dict[str, str]] = field(default_factory=list)  # MoE AI strands


@dataclass
class StudentRisk:
    student_id: int
    emirates_id: str
    first_name: str
    last_name: str
    grade_level: int
    curriculum_track: str
    school_name: str
    emirate: str
    parent_email: str
    risk_type: str
    risk_detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Policy parser
# ---------------------------------------------------------------------------

def parse_policy_file(path: Path) -> ParsedPolicy:
    """Deterministically parse a policy-pack markdown file into sections.

    The grammar this implements is specified in ``policy_vault/FORMAT.md``;
    the regexes come from :mod:`policy_pack` so the spec, the validator and
    this loader cannot drift apart.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Title: first H1  (FORMAT.md §2.1)
    title = path.stem.replace("_", " ").title()
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # Document reference: **Document Reference:** XXX in the first 20 lines
    reference = ""
    for line in lines[: policy_pack.DOC_REFERENCE_WINDOW]:
        m = policy_pack.DOC_REFERENCE_RE.search(line)
        if m:
            reference = m.group(1).strip()
            break

    policy = ParsedPolicy(
        title=title,
        reference=reference,
        source_file=path.name,
        content_hash=pack_content_hash(path),
    )

    # Split on H2 clause headings (## N. Title)  (FORMAT.md §2.3)
    section_starts: list[tuple[int, str, str]] = []  # (line_idx, clause_no, heading)
    for i, line in enumerate(lines):
        m = policy_pack.CLAUSE_H2_RE.match(line.strip())
        if m:
            section_starts.append((i, m.group(1), m.group(2).strip()))

    for idx, (start, clause_no, heading) in enumerate(section_starts):
        end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        body_lines = lines[start + 1 : end]
        # Strip trailing horizontal rules / appendix separators for body
        content = "\n".join(body_lines).strip()
        # Collapse excessive blank lines
        content = re.sub(r"\n{3,}", "\n\n", content)
        policy.sections.append(
            PolicySection(clause_number=clause_no, heading=heading, content=content)
        )

    # Extract ### Strand / ### sub-headings as Competencies (FORMAT.md §2.5)
    current_pos = 0
    for m in policy_pack.STRAND_H3_RE.finditer(text):
        strand_code = m.group(1).strip()
        strand_name = m.group(2).strip()
        # Content until next ### or ##
        rest = text[m.end():]
        next_heading = re.search(r"^#{2,3}\s", rest, re.MULTILINE)
        body = rest[: next_heading.start()] if next_heading else rest
        body = re.sub(r"\n{3,}", "\n\n", body.strip())
        policy.competencies.append(
            {
                "code": strand_code.replace(" ", ""),  # e.g. StrandA
                "name": strand_name,
                "description": body,
            }
        )
        current_pos = m.end()
    _ = current_pos  # silence unused warning if no strands

    return policy


def load_all_policies(pack_dir: Path | str | None = None) -> list[ParsedPolicy]:
    """Parse every policy document in a pack (flat sorted *.md glob)."""
    root = Path(pack_dir) if pack_dir is not None else POLICY_VAULT
    files = iter_pack_documents(root)
    if not files:
        raise FileNotFoundError(f"No markdown files found in {root}")
    return [parse_policy_file(f) for f in files]


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def wipe_graph(driver: Any) -> None:
    with driver.session() as session:
        result = session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted")
        deleted = result.single()["deleted"]
        print(f"  Wiped {deleted} existing nodes (DETACH DELETE).")


# Constraints that predate Phase 5 and are strictly narrower than the identity
# keys below.  They are dropped (schema only — no node is deleted) so that two
# revisions of one document, or two schools' identical clause codes, can hold
# separate nodes instead of MERGE-colliding.  See FORMAT.md §4 and §6.
LEGACY_CONSTRAINT_NAMES = (
    "policy_title_school_unique",   # (title, school_id)  -> + content_hash
    "standard_code_school_unique",  # (code, school_id)   -> + policy_hash
    "competency_code_unique",       # (code)              -> + school_id
)

CONSTRAINTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("student_school_id_unique", "Student", ("student_id", "school_id")),
    ("policy_identity_unique", "Policy", ("title", "school_id", "content_hash")),
    ("standard_identity_unique", "Standard", ("code", "school_id", "policy_hash")),
    ("competency_code_school_unique", "Competency", ("code", "school_id")),
    ("department_name_unique", "Department", ("name",)),
    ("riskflag_type_school_unique", "RiskFlag", ("type", "school_id")),
]


def drop_legacy_constraints(driver: Any) -> None:
    with driver.session() as session:
        for name in LEGACY_CONSTRAINT_NAMES:
            session.run(f"DROP CONSTRAINT {name} IF EXISTS")
    print("  Constraint migration: dropped legacy "
          f"({', '.join(LEGACY_CONSTRAINT_NAMES)}) — schema only, no nodes removed")


def create_constraints(driver: Any) -> None:
    with driver.session() as session:
        for name, label, props in CONSTRAINTS:
            key = ", ".join(f"n.{p}" for p in props)
            if len(props) == 1:
                session.run(
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{props[0]} IS UNIQUE"
                )
            else:
                session.run(
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE ({key}) IS UNIQUE"
                )
            print(f"  Constraint OK: {label}.({','.join(props)}) UNIQUE ({name})")

        # Full-text-ish index on Standard text for keyword lookups
        session.run(
            "CREATE INDEX standard_heading_idx IF NOT EXISTS "
            "FOR (s:Standard) ON (s.heading)"
        )
        print("  Index OK: Standard.heading")


def backfill_school_id(driver: Any, default_school_id: str = "default") -> int:
    """Additive migration: stamp school_id where Phase-1..4 nodes lack it.

    Nodes created before school scoping (notably ``:Competency``, which Phase 1
    did not touch) would otherwise MERGE onto a different identity key and be
    duplicated by the first load.  Sets a property; never deletes.
    """
    labels = ["Student", "RiskFlag", "Policy", "Standard", "Competency"]
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n)
            WHERE n.school_id IS NULL
              AND any(l IN labels(n) WHERE l IN $labels)
            SET n.school_id = $sid
            RETURN count(n) AS n
            """,
            labels=labels,
            sid=default_school_id,
        )
        n = result.single()["n"]
        print(f"  Backfill: school_id='{default_school_id}' stamped on {n} node(s)")
        return n


def ingest_departments(driver: Any, departments: list[dict[str, str]]) -> int:
    q = """
    UNWIND $rows AS row
    MERGE (d:Department {name: row.name})
    SET d.full_name = row.full_name,
        d.jurisdiction = row.jurisdiction
    RETURN count(d) AS n
    """
    with driver.session() as session:
        result = session.run(q, rows=departments)
        n = result.single()["n"]
        print(f"  Departments upserted: {n}")
        return n


def _load_policy_revision(session: Any, policy: ParsedPolicy, school_id: str
                          ) -> tuple[str, int, int]:
    """Merge one document as a revision.  Returns (action, revision, superseded).

    FORMAT.md §4:

    * identical ``content_hash`` -> refresh + re-activate (a revert un-supersedes)
    * legacy node with no hash   -> adopt it in place (additive migration)
    * new hash                   -> supersede the previous active version and
                                    create a new node alongside it

    The old Policy is never overwritten or deleted, so an inspector can always
    see which clause version governed a past decision.
    """
    title, h = policy.title, policy.content_hash
    loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    props = dict(
        title=title, school_id=school_id, hash=h, loaded_at=loaded_at,
        reference=policy.reference, source_file=policy.source_file,
        section_count=len(policy.sections),
    )

    exact = session.run(
        """
        MATCH (p:Policy {title: $title, school_id: $school_id, content_hash: $hash})
        RETURN p.revision AS revision
        """, **props).single()

    if exact is not None:
        action = "refreshed"
        revision = int(exact["revision"] or 1)
    else:
        legacy = session.run(
            """
            MATCH (p:Policy {title: $title, school_id: $school_id})
            WHERE p.content_hash IS NULL
            RETURN p.revision AS revision
            """, **props).single()
        if legacy is not None:
            action = "adopted"
            revision = int(legacy["revision"] or 1) or 1
            session.run(
                """
                MATCH (p:Policy {title: $title, school_id: $school_id})
                WHERE p.content_hash IS NULL
                SET p.content_hash = $hash, p.revision = $revision
                """, revision=revision, **props)
            # Bind pre-existing clause nodes to this document version so their
            # identity key is complete and a later revision does not re-use them.
            session.run(
                """
                MATCH (p:Policy {title: $title, school_id: $school_id,
                                 content_hash: $hash})<-[:PART_OF]-(s:Standard)
                WHERE s.policy_hash IS NULL
                SET s.policy_hash = $hash
                """, **props)
        else:
            action = "revised"
            max_rev = session.run(
                """
                MATCH (p:Policy {title: $title, school_id: $school_id})
                RETURN coalesce(max(p.revision), 0) AS mx
                """, **props).single()["mx"]
            revision = int(max_rev or 0) + 1

    # Exactly one active version per (title, school_id): retire the others.
    result = session.run(
        """
        MATCH (p:Policy {title: $title, school_id: $school_id})
        WHERE p.content_hash IS NOT NULL AND p.content_hash <> $hash
          AND coalesce(p.status, 'active') = 'active'
        SET p.status = 'superseded',
            p.superseded_at = $loaded_at,
            p.superseded_by_hash = $hash
        RETURN count(p) AS c
        """, **props)
    superseded = result.single()["c"]

    session.run(
        """
        MERGE (p:Policy {title: $title, school_id: $school_id, content_hash: $hash})
        SET p.revision = $revision,
            p.status = 'active',
            p.loaded_at = $loaded_at,
            p.superseded_at = null,
            p.superseded_by_hash = null,
            p.reference = $reference,
            p.source_file = $source_file,
            p.section_count = $section_count
        """, revision=revision, **props)

    return action, revision, superseded


def ingest_policies(driver: Any, policies: list[ParsedPolicy], config: PackConfig,
                    school_id: str = "default") -> tuple[int, int, int, int]:
    """Return (policy_count, standard_count, competency_count, governs_rel_count)."""
    policy_nodes = 0
    standard_nodes = 0
    competency_nodes = 0
    governs_rels = 0

    with driver.session() as session:
        for policy in policies:
            # Policy node — scoped by school_id + content_hash (revision-aware)
            action, revision, superseded = _load_policy_revision(
                session, policy, school_id)
            policy_nodes += 1
            print(f"    [{action}] {policy.title!r} rev={revision} "
                  f"hash={policy.content_hash[:12]}… "
                  f"({superseded} prior revision(s) superseded)")

            # GOVERNS → Department (from pack.config.yml POLICY_DEPT_RULES)
            for rule in config.policy_dept_rules:
                if rule["match"].lower() in policy.title.lower():
                    res = session.run(
                        """
                        MATCH (p:Policy {title: $title, school_id: $school_id,
                                         content_hash: $hash})
                        MATCH (d:Department {name: $dept})
                        MERGE (p)-[:GOVERNS]->(d)
                        RETURN count(*) AS c
                        """,
                        title=policy.title,
                        school_id=school_id,
                        hash=policy.content_hash,
                        dept=rule["dept"],
                    )
                    if res.single()["c"] > 0:
                        governs_rels += 1

            # Standard nodes from sections — scoped by school_id + policy_hash
            for section in policy.sections:
                code = f"{policy.reference or policy.title}§{section.clause_number}"
                session.run(
                    """
                    MERGE (s:Standard {code: $code, school_id: $school_id,
                                       policy_hash: $hash})
                    SET s.clause_number = $clause_number,
                        s.heading = $heading,
                        s.text_content = $text_content,
                        s.policy_title = $policy_title
                    WITH s
                    MATCH (p:Policy {title: $policy_title, school_id: $school_id,
                                     content_hash: $hash})
                    MERGE (s)-[:PART_OF]->(p)
                    """,
                    code=code,
                    school_id=school_id,
                    hash=policy.content_hash,
                    clause_number=section.clause_number,
                    heading=section.heading,
                    text_content=section.content[: policy_pack.TEXT_CONTENT_LIMIT],
                    policy_title=policy.title,
                )
                standard_nodes += 1

            # Competencies (from MoE AI strand architecture) — scoped by school_id
            for comp in policy.competencies:
                session.run(
                    """
                    MERGE (c:Competency {code: $code, school_id: $school_id})
                    SET c.name = $name,
                        c.description = $description,
                        c.framework = $framework,
                        c.policy_hash = $hash
                    WITH c
                    MATCH (p:Policy {title: $framework, school_id: $school_id,
                                     content_hash: $hash})
                    MERGE (p)-[:DEFINES]->(c)
                    """,
                    code=comp["code"],
                    school_id=school_id,
                    hash=policy.content_hash,
                    name=comp["name"],
                    description=comp["description"][: policy_pack.COMPETENCY_TEXT_LIMIT],
                    framework=policy.title,
                )
                competency_nodes += 1

        print(f"  Policies: {policy_nodes}, Standards: {standard_nodes}, "
              f"Competencies: {competency_nodes}, GOVERNS rels: {governs_rels}")

    return policy_nodes, standard_nodes, competency_nodes, governs_rels


# ---------------------------------------------------------------------------
# SIS bridge
# ---------------------------------------------------------------------------

def fetch_flagged_students() -> list[StudentRisk]:
    """Fetch one StudentRisk per (student, risk_type) pair from PostgreSQL."""
    try:
        conn = psycopg2.connect(**PG_CONFIG)
    except psycopg2.OperationalError as exc:
        raise RuntimeError(f"PostgreSQL connection failed: {exc}") from exc

    out: list[StudentRisk] = []
    try:
        with conn.cursor() as cur:
            for risk_type, sql in RISK_QUERIES.items():
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    data = dict(zip(cols, row))
                    out.append(
                        StudentRisk(
                            student_id=data["student_id"],
                            emirates_id=data["emirates_id"],
                            first_name=data["first_name"],
                            last_name=data["last_name"],
                            grade_level=data["grade_level"],
                            curriculum_track=data["curriculum_track"],
                            school_name=data["school_name"],
                            emirate=data["emirate"],
                            parent_email=data["parent_email"],
                            risk_type=risk_type,
                            risk_detail={
                                k: (str(v) if v is not None else None)
                                for k, v in data.items()
                                if k not in (
                                    "student_id", "emirates_id", "first_name",
                                    "last_name", "grade_level", "curriculum_track",
                                    "school_name", "emirate", "parent_email",
                                )
                            },
                        )
                    )
    finally:
        conn.close()
    return out


def ingest_students(driver: Any, risks: list[StudentRisk],
                    school_id: str = "default") -> tuple[int, int, int]:
    """Create Student + RiskFlag nodes and HAS_RISK edges. Returns counts."""
    student_ids: set[int] = set()
    risk_types: set[str] = set()
    has_risk_rels = 0

    with driver.session() as session:
        # Batch by risk type for clearer progress
        for risk_type in RISK_QUERIES:
            batch = [r for r in risks if r.risk_type == risk_type]
            if not batch:
                continue

            # Unique students for this risk — scoped by school_id
            session.run(
                """
                UNWIND $rows AS row
                MERGE (s:Student {student_id: row.student_id, school_id: $school_id})
                SET s.emirates_id = row.emirates_id,
                    s.first_name = row.first_name,
                    s.last_name = row.last_name,
                    s.full_name = row.first_name + ' ' + row.last_name,
                    s.grade_level = row.grade_level,
                    s.curriculum_track = row.curriculum_track,
                    s.school_name = row.school_name,
                    s.emirate = row.emirate,
                    s.parent_email = row.parent_email
                """,
                school_id=school_id,
                rows=[
                    {
                        "student_id": r.student_id,
                        "emirates_id": r.emirates_id,
                        "first_name": r.first_name,
                        "last_name": r.last_name,
                        "grade_level": r.grade_level,
                        "curriculum_track": r.curriculum_track,
                        "school_name": r.school_name,
                        "emirate": r.emirate,
                        "parent_email": r.parent_email,
                    }
                    for r in batch
                ],
            )
            student_ids.update(r.student_id for r in batch)

            # RiskFlag node (one per type) — scoped by school_id
            session.run(
                """
                MERGE (rf:RiskFlag {type: $type, school_id: $school_id})
                SET rf.description = $desc,
                    rf.severity = $severity
                """,
                type=risk_type,
                school_id=school_id,
                desc=RISK_DESCRIPTIONS.get(risk_type, risk_type),
                severity=RISK_SEVERITY.get(risk_type, "medium"),
            )
            risk_types.add(risk_type)

            # HAS_RISK edges with per-student detail
            result = session.run(
                """
                UNWIND $rows AS row
                MATCH (s:Student {student_id: row.student_id, school_id: $school_id})
                MATCH (rf:RiskFlag {type: row.risk_type, school_id: $school_id})
                MERGE (s)-[r:HAS_RISK]->(rf)
                SET r.detail = row.detail
                RETURN count(r) AS c
                """,
                school_id=school_id,
                rows=[
                    {
                        "student_id": r.student_id,
                        "risk_type": r.risk_type,
                        "detail": json.dumps(r.risk_detail, default=str),
                    }
                    for r in batch
                ],
            )
            has_risk_rels += result.single()["c"]
            print(f"  {risk_type}: {len(batch)} student-risk pairs")

    # Link students to Department via curriculum track / emirate (lightweight)
    with driver.session() as session:
        session.run(
            """
            MATCH (s:Student {school_id: $school_id})
            MATCH (d:Department {name: 'Student Welfare'})
            MERGE (s)-[:UNDER_JURISDICTION]->(d)
            """,
            school_id=school_id,
        )

    # Link RiskFlag → Standard via RISK_POLICY_MAP
    risk_std_rels = link_risks_to_standards(driver, school_id=school_id)

    print(f"  Students: {len(student_ids)}, RiskFlags: {len(risk_types)}, "
          f"HAS_RISK: {has_risk_rels}, risk->standard: {risk_std_rels}")
    return len(student_ids), len(risk_types), has_risk_rels + risk_std_rels


RISK_DESCRIPTIONS = {
    "VACCINATION_EXPIRED": "Student has expired or missing vaccination records",
    "CHRONIC_ATTENDANCE": "Student attendance rate below 85% threshold",
    "FEE_OVERDUE": "Outstanding fee payment more than 60 days overdue",
}

RISK_SEVERITY = {
    "VACCINATION_EXPIRED": "high",
    "CHRONIC_ATTENDANCE": "medium",
    "FEE_OVERDUE": "medium",
}


def link_risks_to_standards(driver: Any, school_id: str = "default") -> int:
    """Connect each RiskFlag to the Standard clause its pack config names.

    The mapping and the clause selection both come from
    :func:`policy_pack.link_risk_to_clause` — the same path
    ``sis_adapter.sync_students_to_graph`` takes, so a client pack with
    different clause headings links identically (FORMAT.md §3.3).
    """
    total = 0
    with driver.session() as session:
        for risk_type in RISK_QUERIES:
            entry = risk_policy_entry(school_id, risk_type)
            if not entry:
                print(f"  WARN: no RISK_POLICY_MAP entry for {risk_type} "
                      f"under school '{school_id}' — leaving risk unlinked "
                      f"(no clause is invented)")
                continue
            c = policy_pack.link_risk_to_clause(session, risk_type, school_id)
            total += c
            print(f"  {risk_type} -> {entry['policy_file']}.md "
                  f"(keyword '{entry['clause_keyword']}'): {c}")
    return total


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def count_nodes_and_rels(driver: Any) -> dict[str, int]:
    labels = ["Student", "Policy", "Standard", "Department", "RiskFlag", "Competency"]
    counts: dict[str, int] = {}
    with driver.session() as session:
        for label in labels:
            n = session.run(f"MATCH (n:{label}) RETURN count(n) AS n").single()["n"]
            if n > 0:
                counts[f"Node:{label}"] = n
        for r in session.run("MATCH ()-[rel]->() RETURN type(rel) AS t, count(rel) AS n"):
            counts[f"Rel:{r['t']}"] = r["n"]
    return counts


def run_test_query(driver: Any) -> list[dict[str, Any]]:
    """Multi-hop: Student -HAS_RISK-> RiskFlag -ADDRESSED_BY-> Standard -PART_OF-> Policy.

    Returns rows for risk-flagged students whose risk is addressed by a
    governing policy clause.  The clause is whatever the school's pack config
    names — no heading or title from any particular pack is assumed here.
    """
    cypher = """
    MATCH (s:Student)-[:HAS_RISK]->(rf:RiskFlag)-[:ADDRESSED_BY]->(std:Standard)-[:PART_OF]->(p:Policy)
    WHERE rf.type = 'VACCINATION_EXPIRED'
      AND coalesce(p.status, 'active') = 'active'
    RETURN s.full_name AS student,
           s.grade_level AS grade,
           s.school_name AS school,
           rf.type AS risk,
           std.clause_number AS clause,
           std.heading AS clause_heading,
           p.title AS governing_policy
    ORDER BY student
    LIMIT 10
    """
    with driver.session() as session:
        return [dict(r) for r in session.run(cypher)]


def run_secondary_test(driver: Any) -> list[dict[str, Any]]:
    """Alternative multi-hop: chronic attendance → its governing clause."""
    cypher = """
    MATCH (s:Student)-[:HAS_RISK]->(rf:RiskFlag)-[:ADDRESSED_BY]->(std:Standard)-[:PART_OF]->(p:Policy)
    WHERE rf.type = 'CHRONIC_ATTENDANCE'
      AND coalesce(p.status, 'active') = 'active'
    RETURN s.full_name AS student,
           rf.type AS risk,
           std.clause_number AS clause,
           std.heading AS clause_heading,
           p.title AS governing_policy
    ORDER BY student
    LIMIT 5
    """
    with driver.session() as session:
        return [dict(r) for r in session.run(cypher)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build MSOE Knowledge Graph")
    parser.add_argument(
        "--pack",
        default=str(POLICY_VAULT),
        help="policy pack directory to load (default: policy_vault)",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="DETACH DELETE all nodes before rebuilding (destructive)",
    )
    parser.add_argument(
        "--wipe-only",
        action="store_true",
        help="Only wipe existing graph data and exit",
    )
    parser.add_argument(
        "--school-id",
        default="default",
        help="School identifier for scoped MERGEs (default: 'default')",
    )
    args = parser.parse_args()
    school_id = args.school_id
    pack_dir = Path(args.pack)

    print("=" * 72)
    print("MSOE KNOWLEDGE GRAPH BUILDER")
    print("=" * 72)
    print(f"Neo4j : {NEO4J_URI}")
    print(f"Postgres: {PG_CONFIG['host']}:{PG_CONFIG['port']}/{PG_CONFIG['dbname']}")
    print(f"Policy pack: {pack_dir}")
    print(f"School ID: {school_id}")
    mode = "WIPE+REBUILD" if args.wipe else ("WIPE-ONLY" if args.wipe_only else "MERGE (non-destructive)")
    print(f"Mode: {mode}")

    try:
        config = load_pack_config(pack_dir)
    except policy_pack.PackConfigError as exc:
        print(f"[ERROR] pack configuration: {exc}")
        return 1
    # This run's pack governs this school for the rest of the process, so the
    # ADDRESSED_BY link resolves against it (see policy_pack.pack_dir_for_school).
    policy_pack.register_school_pack(school_id, pack_dir)
    print(f"Pack id: {config.pack_id}  "
          f"(DEPARTMENTS={len(config.departments)}, "
          f"POLICY_DEPT_RULES={len(config.policy_dept_rules)}, "
          f"RISK_POLICY_MAP={sorted(config.risk_policy_map)})")
    print()

    try:
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        driver.verify_connectivity()
        print("[1/6] Neo4j connectivity OK")
    except Exception as exc:
        print(f"[ERROR] Cannot connect to Neo4j: {exc}")
        return 1

    try:
        # --- Schema --------------------------------------------------------
        if args.wipe or args.wipe_only:
            print("[2/6] Wiping existing graph data ...")
            wipe_graph(driver)
        else:
            print("[2/6] Skipping wipe (use --wipe to clear before rebuild)")

        if args.wipe_only:
            print("Wipe-only mode complete.")
            driver.close()
            return 0

        print("[3/6] Creating uniqueness constraints / indexes ...")
        drop_legacy_constraints(driver)
        backfill_school_id(driver)          # additive: stamps 'default' where missing
        create_constraints(driver)

        # --- Policy pack ---------------------------------------------------
        print(f"[4/6] Parsing and ingesting policy pack {pack_dir.name} ...")
        policies = load_all_policies(pack_dir)
        print(f"  Parsed {len(policies)} policy documents:")
        for p in policies:
            print(f"    - {p.title} ({len(p.sections)} sections, "
                  f"{len(p.competencies)} competencies) [{p.source_file}] "
                  f"hash={p.content_hash[:12]}…")

        ingest_departments(driver, config.departments)
        p_n, s_n, c_n, g_r = ingest_policies(
            driver, policies, config, school_id=school_id)

        # --- SIS bridge ----------------------------------------------------
        print("[5/6] Bridging SIS risk-flagged students ...")
        risks = fetch_flagged_students()
        print(f"  Fetched {len(risks)} student-risk records from PostgreSQL")
        stud_n, rf_n, hr_r = ingest_students(driver, risks, school_id=school_id)

        # --- Verification --------------------------------------------------
        print("[6/6] Running verification queries ...")
        counts = count_nodes_and_rels(driver)
        test_rows = run_test_query(driver)
        secondary_rows = run_secondary_test(driver)

    except Exception as exc:
        print(f"[ERROR] Graph build failed: {exc}")
        import traceback
        traceback.print_exc()
        driver.close()
        return 1

    driver.close()

    # ---- Report ------------------------------------------------------------
    print()
    print("=" * 72)
    print("KNOWLEDGE GRAPH SEED REPORT")
    print("=" * 72)

    print("\nNode / relationship inventory:")
    total_nodes = 0
    total_rels = 0
    for key in sorted(counts.keys()):
        print(f"  {key:30s}  {counts[key]:>6}")
        if key.startswith("Node:"):
            total_nodes += counts[key]
        elif key.startswith("Rel:"):
            total_rels += counts[key]
    print(f"  {'TOTAL NODES':30s}  {total_nodes:>6}")
    print(f"  {'TOTAL RELATIONSHIPS':30s}  {total_rels:>6}")

    print("\nBuild tallies (this run):")
    print(f"  Policies ingested        : {p_n}")
    print(f"  Standards (clauses)      : {s_n}")
    print(f"  Competencies (AI strands): {c_n}")
    print(f"  GOVERNS relationships    : {g_r}")
    print(f"  Unique students linked   : {stud_n}")
    print(f"  RiskFlag types           : {rf_n}")
    print(f"  HAS_RISK + ADDRESSED_BY  : {hr_r}")

    print("\n--- TEST QUERY 1: VACCINATION_EXPIRED -> KHDA/ADEK Health clause ---")
    if test_rows:
        for row in test_rows:
            print(
                f"  {row['student']:<22} grade={row['grade']:<3} "
                f"risk={row['risk']:<20} clause={row['clause']:<3} "
                f"«{row['clause_heading']}» ← {row['governing_policy']}"
            )
        print(f"  -> {len(test_rows)} multi-hop paths confirmed.")
    else:
        print("  (no rows returned)")

    print("\n--- TEST QUERY 2: CHRONIC_ATTENDANCE -> governing clause ---")
    if secondary_rows:
        for row in secondary_rows:
            print(
                f"  {row['student']:<22} risk={row['risk']:<20} "
                f"clause={row['clause']:<3} «{row['clause_heading']}» "
                f"← {row['governing_policy']}"
            )
        print(f"  -> {len(secondary_rows)} multi-hop paths confirmed.")
    else:
        print("  (no rows returned)")

    print()
    print("=" * 72)
    print("GRAPH SEED COMPLETE — no errors.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
