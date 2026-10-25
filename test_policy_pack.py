"""Policy-pack library tests (Phase 5, P.1–P.6) — SYNTHETIC data.

Covers
------
* **P.1** the FORMAT.md grammar: H1 → Policy.title, Document Reference in the
  first 20 lines → Policy.reference, ``## N. Heading`` → Standard,
  ``### Strand X — Name`` (em-dash) → Competency, body → text_content (4000)
* **P.1** ``validate_policy_pack.py``: 0 violations on the three MSOE packs,
  and every violation of a deliberately broken pack reported with file:line
* **P.2** the three maps come from ``pack.config.yml`` via :mod:`policy_pack`
  and are no longer hardcoded in ``build_knowledge_graph`` / ``sis_adapter``
* **P.3** two packs scoped by school_id coexist without MERGE collisions,
  including two schools holding an **identical** Standard.code as two nodes
* **P.4** reloading a changed document supersedes the old Policy instead of
  overwriting it — both revisions stay queryable
* **P.6** the 4-hop
  ``(Student)-[:HAS_RISK]->(RiskFlag)-[:ADDRESSED_BY]->(Standard)-[:PART_OF]->(Policy)``
  resolves for a school whose clause headings are NOT "Campus Health", driven
  by the config map, and resolves differently for MSOE's pack in the same graph

Parts 1–3 are pure parsing/config and need no sockets.  Parts 4–6 need a live
Neo4j (they load packs into it) and fail loudly if it is unreachable — that is
the point of P.3/P.4/P.6.  All pack fixtures are SYNTHETIC.

Run (from the container network, msoe-api image for neo4j + psycopg2)::

    python -u test_policy_pack.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import policy_pack  # noqa: E402
import validate_policy_pack  # noqa: E402
from policy_pack import (  # noqa: E402
    PackConfigError,
    clause_keyword_for_risk,
    iter_pack_documents,
    load_pack_config,
    pack_content_hash,
    register_school_pack,
    risk_policy_entry,
)

REPO = os.path.dirname(os.path.abspath(__file__))
MSOE_PACK = os.path.join(REPO, "policy_vault")
FIXTURE_PACK = os.path.join(REPO, "fixtures", "policy_packs", "meridian_british_school")
FIXTURE_SCHOOL = "meridian_fixture"

RESULTS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name:<62} {detail}")
    return bool(condition)


def section(title: str) -> None:
    print()
    print(f"PART {title}")


# ===========================================================================
# Part 1 — the FORMAT.md grammar (no sockets)
# ===========================================================================

def part1() -> None:
    section("1 — FORMAT.md grammar: what build_knowledge_graph parses")

    docs = iter_pack_documents(MSOE_PACK)
    ok("pack glob is flat + sorted, minus pack documentation",
       [p.name for p in docs] == sorted([
           "khda_adek_health_safety_protocols.md",
           "moe_ai_curriculum_framework.md",
           "staff_safeguarding_handbook.md",
       ]),
       f"{[p.name for p in docs]}")
    ok("README.md / FORMAT.md are never parsed as policy documents",
       not any(p.name in policy_pack.PACK_DOC_FILENAMES for p in docs)
       and not any("README" in p.name or "FORMAT" in p.name for p in docs))

    # Import the real parser — the spec is worthless if the loader disagrees.
    import build_knowledge_graph as bkg
    parsed = {p.source_file: p for p in bkg.load_all_policies(MSOE_PACK)}

    hs = parsed["khda_adek_health_safety_protocols.md"]
    ok("H1 -> Policy.title",
       hs.title == "KHDA / ADEK Health & Safety Protocols", repr(hs.title))
    ok("**Document Reference:** (line 3, within 20) -> Policy.reference",
       hs.reference == "MSOE-HS-2026-02", repr(hs.reference))
    ok("## 4. Campus Health Services -> Standard clause 4 'Campus Health Services'",
       any(s.clause_number == "4" and s.heading == "Campus Health Services"
           for s in hs.sections),
       f"{len(hs.sections)} sections")
    ok("Standard code = reference§clause_number",
       f"{hs.reference}§4" == "MSOE-HS-2026-02§4")
    ok("clause body -> Standard.text_content (non-empty, under 4000)",
       all(0 < len(s.content) <= 4000 for s in hs.sections),
       f"max body={max(len(s.content) for s in hs.sections)}")

    moe = parsed["moe_ai_curriculum_framework.md"]
    ok("### Strand A — Name (em-dash) -> Competency code 'StrandA'",
       [c["code"] for c in moe.competencies] == [
           "StrandA", "StrandB", "StrandC", "StrandD", "StrandE", "StrandF"],
       f"{[c['code'] for c in moe.competencies]}")
    ok("competency description body is non-empty",
       all(c["description"] for c in moe.competencies))
    ok("non-clause H2 (appendix) is absorbed into the last clause body",
       "Appendix A" in moe.sections[-1].content,
       f"last clause {moe.sections[-1].clause_number!r} body "
       f"{len(moe.sections[-1].content)} chars")

    # Document-reference window is exactly 20 lines (FORMAT.md §2.2).
    late = os.path.join(tempfile.mkdtemp(prefix="pack_lateref_"), "late.md")
    with open(late, "w", encoding="utf-8") as fh:
        fh.write("# Late Reference Pack\n\n")
        for i in range(24):
            fh.write(f"filler line {i}\n")
        fh.write("**Document Reference:** TOO-LATE-01\n\n## 1. Only Clause\nbody\n")
    parsed_late = bkg.parse_policy_file(Path(late))
    ok("Document Reference after line 20 is NOT captured",
       parsed_late.reference == "", repr(parsed_late.reference))


# ===========================================================================
# Part 2 — validate_policy_pack.py (no sockets)
# ===========================================================================

def part2() -> None:
    section("2 — validator: 0 violations on the MSOE packs, file:line on a bad one")

    findings = validate_policy_pack.validate_path(Path(MSOE_PACK))
    violations = [f for f in findings if f.level == validate_policy_pack.VIOLATION]
    ok("policy_vault/ -> 0 violations", not violations,
       f"{len(violations)} violations, "
       f"{sum(1 for f in findings if f.level == validate_policy_pack.WARNING)} warnings")

    fixture_findings = validate_policy_pack.validate_path(Path(FIXTURE_PACK))
    fixture_violations = [f for f in fixture_findings
                          if f.level == validate_policy_pack.VIOLATION]
    ok("fixture pack -> 0 violations", not fixture_violations,
       f"{len(fixture_findings)} findings")

    # Two deliberately broken documents: the H1 violations are mutually
    # exclusive on one file (no-h1 vs first-heading-not-h1), so each gets
    # its own.  The config is broken both ways the validator can see it:
    # a policy_file that does not exist, and a keyword that matches nothing.
    bad_dir = tempfile.mkdtemp(prefix="pack_bad_")
    with open(os.path.join(bad_dir, "pack.config.yml"), "w", encoding="utf-8") as fh:
        fh.write(
            "schema_version: 1\n"
            "DEPARTMENTS:\n"
            "  - {name: 'X', full_name: 'X', jurisdiction: 'Y'}\n"
            "POLICY_DEPT_RULES:\n"
            "  - {match: 'a', dept: 'X'}\n"
            "RISK_POLICY_MAP:\n"
            "  FEE_OVERDUE:\n"
            "    policy_file: 'does_not_exist'\n"
            "    clause_keyword: 'Nowhere'\n"
            "  VACCINATION_EXPIRED:\n"
            "    policy_file: 'broken'\n"
            "    clause_keyword: 'Nowhere Either'\n"
        )
    with open(os.path.join(bad_dir, "broken.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "## 1. Clause Before Any H1\n"          # no H1 anywhere in this file
            "body\n"
            "**Document Reference:** BAD-01\n"       # line 3 -> inside window
            "\n## 1.Heading\n"                       # bad clause H2 (no space)
            "swallowed\n"
            "\n## 2. Good Clause\n"
            "text\n"
            "\n### Strand A - Wrong Dash\n"          # hyphen, not em-dash
            "not a competency\n"
            "\n### Strand G — Out Of Range\n"        # letter G
            "not a competency\n"
            "\n## 3. First Clause\n"
            "\n## 3. Duplicate Number\n"             # duplicate clause number
            "dup\n"
        )
    with open(os.path.join(bad_dir, "late_title.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "## 1. Clause Before The H1\n"           # first heading is not an H1
            "body\n"
            "# Second Thought\n"                     # H1 arrives too late
            "\n**Document Reference:** BAD-02\n"
            "\n## 2. Only Real Clause\n"
            "body\n"
        )
    bad_findings = validate_policy_pack.validate_path(Path(bad_dir))
    codes = {f.code for f in bad_findings if f.level == validate_policy_pack.VIOLATION}
    expected = {
        "no-h1", "first-heading-not-h1", "bad-clause-h2", "bad-strand",
        "duplicate-clause", "config-unknown-policy-file", "config-keyword-unmatched",
    }
    ok("broken pack -> every violation class reported",
       expected.issubset(codes), f"got={sorted(codes)}")

    with_loc = [f for f in bad_findings if f.level == validate_policy_pack.VIOLATION
                and f.line >= 1 and f.path.name]
    ok("violations carry filename + line",
       bool(with_loc) and all(re.match(r"^\S+:\d+:", f.render()) for f in with_loc),
       with_loc[0].render() if with_loc else "none")

    # The silent-fallback trap gap #8 names: a keyword matching no heading.
    unmatched = [f for f in bad_findings if f.code == "config-keyword-unmatched"]
    ok("clause_keyword that matches no heading is a violation (no silent fallback)",
       bool(unmatched) and "Nowhere Either" in unmatched[0].message,
       unmatched[0].message[:80] if unmatched else "missing")


# ===========================================================================
# Part 3 — config is the one source of truth (no sockets)
# ===========================================================================

def part3() -> None:
    section("3 — RISK_POLICY_MAP / POLICY_DEPT_RULES / DEPARTMENTS come from config")

    cfg = load_pack_config(MSOE_PACK)
    ok("pack.config.yml loads DEPARTMENTS", len(cfg.departments) == 8,
       f"{len(cfg.departments)} departments")
    ok("pack.config.yml loads POLICY_DEPT_RULES", len(cfg.policy_dept_rules) == 9,
       f"{len(cfg.policy_dept_rules)} rules")

    # The exact values that used to be hardcoded in build_knowledge_graph.
    ok("RISK_POLICY_MAP preserved across the extraction",
       cfg.risk_policy_map == {
           "VACCINATION_EXPIRED": {"policy_file": "khda_adek_health_safety_protocols",
                                   "clause_keyword": "Campus Health"},
           "CHRONIC_ATTENDANCE": {"policy_file": "khda_adek_health_safety_protocols",
                                  "clause_keyword": "Campus Health"},
           "FEE_OVERDUE": {"policy_file": "staff_safeguarding_handbook",
                           "clause_keyword": "Purpose"},
       },
       f"{sorted(cfg.risk_policy_map)}")

    import build_knowledge_graph as bkg
    import sis_adapter
    leftover = [n for n in ("RISK_POLICY_MAP", "POLICY_DEPT_RULES", "DEPARTMENTS")
                if n in vars(bkg)]
    ok("build_knowledge_graph no longer defines the three maps",
       not leftover, f"still defined: {leftover}")

    # P.2's whole point: the ADDRESSED_BY heading is no longer a CASE of
    # literals inside sis_adapter — those exact strings were the old arms.
    src = open(os.path.join(REPO, "sis_adapter.py"), encoding="utf-8").read()
    hardcoded = [kw for kw in ("'Campus Health'", "'Attendance'", "'Finance'",
                               "'Compliance'") if kw in src]
    ok("sis_adapter no longer hardcodes ADDRESSED_BY heading keywords",
       not hardcoded and "policy_pack.link_risk_to_clause" in src,
       f"leftover literals={hardcoded}")

    # Same risk type resolves to different clauses per school — by config.
    ok("default school resolves MSOE headings from config",
       clause_keyword_for_risk("default", "VACCINATION_EXPIRED") == "Campus Health"
       and clause_keyword_for_risk("default", "FEE_OVERDUE") == "Purpose")
    register_school_pack(FIXTURE_SCHOOL, FIXTURE_PACK)
    ok("fixture school resolves NON-MSOE headings from its own config",
       clause_keyword_for_risk(FIXTURE_SCHOOL, "VACCINATION_EXPIRED") == "Pupil Health Centre"
       and clause_keyword_for_risk(FIXTURE_SCHOOL, "CHRONIC_ATTENDANCE") == "Attendance and Punctuality"
       and clause_keyword_for_risk(FIXTURE_SCHOOL, "FEE_OVERDUE") == "Bursary and Fee Collection",
       str({rt: clause_keyword_for_risk(FIXTURE_SCHOOL, rt)
            for rt in ("VACCINATION_EXPIRED", "CHRONIC_ATTENDANCE", "FEE_OVERDUE")}))
    ok("a school with no pack gets NO mapping (nothing is invented)",
       risk_policy_entry("nonexistent_school", "FEE_OVERDUE") is None)

    try:
        load_pack_config(os.path.join(tempfile.gettempdir(), "no_such_pack_dir"))
        ok("pack without pack.config.yml is refused", False, "no error raised")
    except PackConfigError as exc:
        ok("pack without pack.config.yml is refused", "not found" in str(exc), str(exc)[:70])


# ===========================================================================
# Graph helpers (Parts 4-6)
# ===========================================================================

def _neo4j():
    from neo4j import GraphDatabase
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "msoe_graph_2026")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    return driver


def _load_pack(driver, pack_dir: str, school_id: str) -> None:
    """Load a pack through the real builder path (additive, never wipes)."""
    import build_knowledge_graph as bkg
    cfg = load_pack_config(pack_dir)
    register_school_pack(school_id, pack_dir)
    with driver.session() as session:
        for row in cfg.departments:
            session.run(
                "MERGE (d:Department {name: $name}) "
                "SET d.full_name = $full_name, d.jurisdiction = $jurisdiction",
                **row)
    policies = bkg.load_all_policies(pack_dir)
    bkg.ingest_policies(driver, policies, cfg, school_id=school_id)


def _write_probe_pack(root: str, reference: str, clause4_heading: str,
                      marker: str) -> str:
    """A minimal valid pack (FORMAT.md) used for the revision / code-clash proofs."""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "governing_code.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# Synthetic Governing Code\n"
            "\n"
            f"**Document Reference:** {reference}\n"
            "**Effective Date:** 01 September 2026\n"
            "\n"
            "---\n"
            "\n"
            "## 1. Purpose\n"
            f"Marker: {marker}. Synthetic probe document.\n"
            "\n"
            "## 2. Scope\n"
            "Synthetic scope clause.\n"
            "\n"
            "## 3. Responsibilities\n"
            "Synthetic responsibilities clause.\n"
            "\n"
            f"## 4. {clause4_heading}\n"
            f"Marker: {marker}. Synthetic health clause.\n"
            "\n"
        )
    with open(os.path.join(root, "pack.config.yml"), "w", encoding="utf-8") as fh:
        fh.write(
            "schema_version: 1\n"
            "pack_id: 'synthetic-probe'\n"
            "DEPARTMENTS:\n"
            "  - {name: 'Probe', full_name: 'Probe Regulator', jurisdiction: 'SYNTHETIC'}\n"
            "POLICY_DEPT_RULES:\n"
            "  - {match: 'Synthetic', dept: 'Probe'}\n"
            "RISK_POLICY_MAP:\n"
            "  VACCINATION_EXPIRED:\n"
            "    policy_file: 'governing_code'\n"
            f"    clause_keyword: '{clause4_heading}'\n"
        )
    return root


# ===========================================================================
# Part 4 — two packs coexist scoped by school_id (P.3)
# ===========================================================================

def _clear_school(driver, school_id: str) -> None:
    """Drop only this test's own synthetic school — makes the suite re-runnable.

    This is the test cleaning up after itself, not the loader wiping: the
    loader's rule (never wipe an existing pack) is asserted separately below.
    """
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.school_id = $sid "
            "DETACH DELETE n", sid=school_id)


def part4(driver) -> None:
    section("4 — P.3 two packs coexist scoped by school_id, no MERGE collision")

    _clear_school(driver, "code_clash")
    _load_pack(driver, MSOE_PACK, "default")
    _load_pack(driver, FIXTURE_PACK, FIXTURE_SCHOOL)

    with driver.session() as session:
        baseline = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]

        rows = [dict(r) for r in session.run(
            "MATCH (p:Policy) WHERE p.status IS NULL OR p.status = 'active' "
            "RETURN p.title AS title, p.school_id AS school_id, p.source_file AS sf "
            "ORDER BY p.school_id, p.title")]
        schools = {(r["title"], r["school_id"]) for r in rows}
        ok("MATCH (p:Policy) RETURN p.title, p.school_id -> both packs present",
           any(s == "default" and "Health" in t for t, s in schools)
           and any(s == FIXTURE_SCHOOL and t == "Pupil Welfare and Attendance Code"
                   for t, s in schools),
           f"{sorted(schools)}")
        ok("each Policy carries the right school_id",
           all(r["school_id"] in ("default", FIXTURE_SCHOOL, "rev_probe", "code_clash")
               for r in rows),
           f"{[r['school_id'] for r in rows]}")

        # Identical Standard.code under two schools must be two nodes, not one.
        clash = os.path.join(tempfile.mkdtemp(prefix="pack_clash_"), "probe")
        _write_probe_pack(clash, reference="MSOE-HS-2026-02",
                          clause4_heading="Synthetic Health Centre",
                          marker="clash-v1")
        _load_pack(driver, clash, "code_clash")
        shared = [dict(r) for r in session.run(
            "MATCH (s:Standard {code: $code}) RETURN s.school_id AS school_id",
            code="MSOE-HS-2026-02§1")]
        ok("identical Standard.code across school_ids stays two distinct nodes",
           len(shared) == 2 and {r["school_id"] for r in shared} == {"default", "code_clash"},
           f"{sorted(r['school_id'] for r in shared)} for code MSOE-HS-2026-02§1")

        count_default = session.run(
            "MATCH (s:Standard {school_id: 'default', code: 'MSOE-HS-2026-02§1'}) "
            "RETURN count(s) AS n").single()["n"]
        ok("loading a second pack did not overwrite the first",
           count_default == 1, f"default's MSOE-HS-2026-02§1 count={count_default}")

        # Additive reload: loading one pack may only ever grow the graph.
        after = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        ok("graph only grew (loading never wipes an existing pack)",
           after >= baseline and after > 0,
           f"{baseline} -> {after} nodes")


# ===========================================================================
# Part 5 — P.6 the 4-hop for non-MSOE clause headings
# ===========================================================================

def part5(driver) -> None:
    section("5 — P.6 4-hop path resolves for non-MSOE clause headings")

    import sis_adapter

    student = {
        "source_student_id": "MBS-9001",
        "first_name": "Test",
        "last_name": "Pupil",
        "grade_level": 8,
        "school_name": "Meridian British School (SYNTHETIC)",
        "emirate": "Dubai",
        "school_id": FIXTURE_SCHOOL,
    }
    summary = sis_adapter.sync_students_to_graph(
        [student], {"MBS-9001": ["VACCINATION_EXPIRED"]}, school_id=FIXTURE_SCHOOL)
    ok("fixture student + risk synced through sis_adapter",
       summary["students_upserted"] == 1 and summary["risk_edges"] == 1,
       str(summary))

    cypher = """
        MATCH (s:Student)-[:HAS_RISK]->(rf:RiskFlag)-[:ADDRESSED_BY]->(std:Standard)
              -[:PART_OF]->(p:Policy)
        WHERE s.school_id = $sid AND rf.type = 'VACCINATION_EXPIRED'
          AND coalesce(p.status, 'active') = 'active'
        RETURN std.heading AS heading, std.code AS code, p.title AS policy,
               p.school_id AS school_id
    """
    with driver.session() as session:
        rows = [dict(r) for r in session.run(cypher, sid=FIXTURE_SCHOOL)]

    ok("4-hop path resolves for the fixture school",
       len(rows) == 1, f"{rows}")
    if rows:
        r = rows[0]
        ok("clause heading is the fixture's 'Pupil Health Centre', NOT 'Campus Health'",
           r["heading"] == "Pupil Health Centre" and "Campus Health" not in r["heading"],
           f"heading={r['heading']!r}")
        ok("clause is the fixture's own Standard under the fixture's Policy",
           r["code"] == "MBS-WEL-2026-01§4"
           and r["policy"] == "Pupil Welfare and Attendance Code"
           and r["school_id"] == FIXTURE_SCHOOL,
           f"{r['code']} <- {r['policy']} ({r['school_id']})")

    # The same risk type must resolve differently for MSOE's pack in the SAME
    # graph — proof it is the config map, not a hardcoded heading, doing the work.
    import build_knowledge_graph as bkg
    bkg.link_risks_to_standards(driver, school_id="default")
    with driver.session() as session:
        default_rows = [dict(r) for r in session.run(cypher, sid="default")]
    ok("same risk resolves to MSOE's 'Campus Health' under school_id=default",
       bool(default_rows)
       and all(r["heading"] == "Campus Health Services" for r in default_rows)
       and all(r["code"] == "MSOE-HS-2026-02§4" for r in default_rows),
       f"{[r['heading'] for r in default_rows]}")

    # FEE_OVERDUE resolves to a third, different heading via its own config row.
    sis_adapter.sync_students_to_graph(
        [{**student, "source_student_id": "MBS-9002"}],
        {"MBS-9002": ["FEE_OVERDUE"]}, school_id=FIXTURE_SCHOOL)
    with driver.session() as session:
        fee_rows = [dict(r) for r in session.run(
            cypher.replace("VACCINATION_EXPIRED", "FEE_OVERDUE"), sid=FIXTURE_SCHOOL)]
    ok("FEE_OVERDUE -> 'Bursary and Fee Collection' (third distinct heading)",
       len(fee_rows) == 1 and fee_rows[0]["heading"] == "Bursary and Fee Collection"
       and fee_rows[0]["code"] == "MBS-WEL-2026-01§7",
       f"{[r['heading'] for r in fee_rows]}")


# ===========================================================================
# Part 6 — P.4 pack revision tracking
# ===========================================================================

def part6(driver) -> None:
    section("6 — P.4 reload of a changed pack supersedes, never overwrites")

    root = os.path.join(tempfile.mkdtemp(prefix="pack_rev_"), "probe")
    _write_probe_pack(root, reference="PROBE-REV-001",
                      clause4_heading="Clinical Services",
                      marker="revision-A")
    school = "rev_probe"
    _clear_school(driver, school)          # re-runnable: only this probe's school
    _load_pack(driver, root, school)

    with driver.session() as session:
        before = [dict(r) for r in session.run(
            "MATCH (p:Policy {school_id: $sid}) RETURN p.title AS title, "
            "p.content_hash AS h, p.status AS status, p.revision AS rev, "
            "p.loaded_at AS loaded_at",
            sid=school)]
    ok("first load -> one active Policy with content_hash + loaded_at",
       len(before) == 1 and before[0]["status"] == "active"
       and before[0]["h"] and before[0]["loaded_at"] and before[0]["rev"] == 1,
       f"{before}")
    hash_a = before[0]["h"] if before else ""

    clause4_a = ""
    with driver.session() as session:
        rec = session.run(
            "MATCH (s:Standard {school_id: $sid, code: 'PROBE-REV-001§4'}) "
            "RETURN s.text_content AS t", sid=school).single()
        clause4_a = rec["t"] if rec else ""

    # --- revise the document (client sends a new version) ----------------
    _write_probe_pack(root, reference="PROBE-REV-001",
                      clause4_heading="Clinical Services", marker="revision-B")
    hash_b = pack_content_hash(os.path.join(root, "governing_code.md"))
    ok("the revision really changed the content hash",
       hash_a != hash_b and bool(hash_a) and bool(hash_b),
       f"{hash_a[:10]}… -> {hash_b[:10]}…")

    _load_pack(driver, root, school)

    with driver.session() as session:
        after = [dict(r) for r in session.run(
            "MATCH (p:Policy {school_id: $sid}) RETURN p.title AS title, "
            "p.content_hash AS h, p.status AS status, p.revision AS rev, "
            "p.superseded_at AS superseded_at, "
            "p.superseded_by_hash AS superseded_by "
            "ORDER BY p.revision", sid=school)]
        clause4_b = session.run(
            "MATCH (s:Standard {school_id: $sid, code: 'PROBE-REV-001§4', "
            "policy_hash: $h}) RETURN s.text_content AS t",
            sid=school, h=hash_b).single()
        clause4_b = clause4_b["t"] if clause4_b else ""
        live_paths = [dict(r) for r in session.run(
            "MATCH (rf:RiskFlag)-[:ADDRESSED_BY]->(s:Standard)-[:PART_OF]->(p:Policy) "
            "WHERE p.school_id = $sid AND coalesce(p.status,'active') = 'active' "
            "RETURN p.content_hash AS h", sid=school)]

    ok("old Policy marked superseded, new Policy present — both queryable",
       len(after) == 2
       and {r["status"] for r in after} == {"superseded", "active"}
       and {r["h"] for r in after} == {hash_a, hash_b},
       f"{[(r['status'], r['rev'], r['h'][:8]) for r in after]}")

    old = next((r for r in after if r["h"] == hash_a), None)
    new = next((r for r in after if r["h"] == hash_b), None)
    ok("superseded revision records when + by what",
       bool(old) and old["superseded_at"] and old["superseded_by"] == hash_b,
       f"superseded_at={old and old['superseded_at']} "
       f"by={old and str(old['superseded_by'])[:8]}")
    ok("new revision is numbered 2 and active",
       bool(new) and new["rev"] == 2 and new["status"] == "active",
       f"rev={new and new['rev']} status={new and new['status']}")

    ok("clause text of BOTH revisions is still readable (not overwritten)",
       "revision-A" in clause4_a and "revision-B" in clause4_b
       and clause4_a != clause4_b,
       f"A={clause4_a[:40]!r} B={clause4_b[:40]!r}")

    # A risk raised AFTER the revision must be ADDRESSED_BY the active
    # revision's clause — not the superseded one.  This is the difference
    # between "both revisions are queryable" and "the old one still governs".
    import sis_adapter
    sis_adapter.sync_students_to_graph(
        [{"source_student_id": "REV-1", "first_name": "Probe", "last_name": "Pupil",
          "grade_level": 9, "school_name": "Synthetic Probe School",
          "emirate": "Dubai", "school_id": school}],
        {"REV-1": ["VACCINATION_EXPIRED"]}, school_id=school)
    with driver.session() as session:
        live_paths = [dict(r) for r in session.run(
            "MATCH (rf:RiskFlag)-[:ADDRESSED_BY]->(s:Standard)-[:PART_OF]->(p:Policy) "
            "WHERE p.school_id = $sid "
            "RETURN p.content_hash AS h, p.status AS status, s.policy_hash AS sh",
            sid=school)]
    ok("live 4-hop links to the active revision, never the superseded one",
       bool(live_paths)
       and all(r["h"] == hash_b and r["status"] == "active" and r["sh"] == hash_b
               for r in live_paths),
       f"{[(r['status'], r['h'][:8], str(r['sh'])[:8]) for r in live_paths]}")

    # Idempotent reload of unchanged content must NOT create a third revision.
    _load_pack(driver, root, school)
    with driver.session() as session:
        unchanged = session.run(
            "MATCH (p:Policy {school_id: $sid}) RETURN count(p) AS n", sid=school
        ).single()["n"]
    ok("reloading unchanged content adds no revision",
       unchanged == 2, f"{unchanged} Policy nodes after idempotent reload")

    shutil.rmtree(os.path.dirname(root), ignore_errors=True)


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    print("=" * 72)
    print("POLICY-PACK LIBRARY TEST SUITE (Phase 5)")
    print("=" * 72)

    part1()
    part2()
    part3()

    driver = None
    try:
        driver = _neo4j()
        print()
        print(f"Neo4j reachable ({os.environ.get('NEO4J_URI', 'bolt://localhost:7687')})")
        import build_knowledge_graph as bkg
        bkg.drop_legacy_constraints(driver)
        bkg.backfill_school_id(driver)
        bkg.create_constraints(driver)
        part4(driver)
        part5(driver)
        part6(driver)
    except Exception as exc:
        ok("Neo4j graph parts (P.3/P.4/P.6) require a reachable Neo4j", False,
           f"{type(exc).__name__}: {exc}")
    finally:
        if driver is not None:
            driver.close()

    print()
    print("=" * 72)
    passed = sum(1 for _n, c, _d in RESULTS if c)
    total = len(RESULTS)
    print(f"SUMMARY: {passed}/{total} passed")
    print("=" * 72)
    for name, condition, detail in RESULTS:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {name:<62} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
