# Policy-pack library

A **policy pack** is one client's governing documents, in the markdown format the
knowledge-graph loader already understands. Packs are the reuse asset: engagement
#3 costs a fraction of engagement #1 because a new client is a new pack
directory, not a code change.

This directory is the **MSOE reference pack** — three real regulatory documents
plus the config that links school risks to their clauses. Use it as the worked
example when authoring a new client pack.

| File | Role |
| --- | --- |
| `FORMAT.md` | Normative spec: document rules, `pack.config.yml` schema, revision tracking, node identity |
| `pack.config.yml` | The three maps (`DEPARTMENTS`, `POLICY_DEPT_RULES`, `RISK_POLICY_MAP`) — the one source of truth for clause linking |
| `khda_adek_health_safety_protocols.md` | Policy document (clauses → `:Standard`) |
| `moe_ai_curriculum_framework.md` | Policy document (clauses → `:Standard`, `### Strand` → `:Competency`) |
| `staff_safeguarding_handbook.md` | Policy document |

`README.md` and `FORMAT.md` are pack *documentation* — the loader and the
validator never parse them as policy documents.

---

## Authoring a new client pack

### 1. Create the directory

```
policy_vaults/<client_name>/
    pack.config.yml
    <document>.md          # one file per governing policy
```

Start by copying this directory and deleting the markdown documents. Keep
`pack.config.yml` as a shape reference. The synthetic fixture under
`fixtures/policy_packs/meridian_british_school/` is a second, fully worked
example with deliberately different clause headings.

### 2. Convert each Word/PDF policy into markdown

The client's policies arrive as Word or PDF. Convert them by hand into the
markdown format — do **not** invent a new format, and do not expect the loader
to read binary documents. The conversion is mechanical:

| Source document | Markdown form | Lands on |
| --- | --- | --- |
| Document title | `# Title` (first line) | `:Policy.title` |
| Reference number | `**Document Reference:** ABC-123` (within first 20 lines) | `:Policy.reference` |
| Numbered clause | `## 4. Heading` (integer, period, space) | `:Standard` with code `ABC-123§4` |
| Strand / sub-competency | `### Strand A — Name` (em-dash **U+2014**) | `:Competency` code `StrandA` |
| Clause body | plain text under the clause | `:Standard.text_content` (first 4000 chars) |

Rules the validator enforces — run it before loading anything:

```bash
python validate_policy_pack.py policy_vaults/<client_name>/
```

It must report **0 violations**. Warnings are advisory (e.g. an appendix that
gets absorbed into the previous clause body). Every finding carries
`filename:line`.

The traps worth knowing before you start (all in `FORMAT.md` §2):

* `## 4.1 Sub-clause` is **not** a clause — it parses as clause 4 with heading
  `1 Sub-clause`. Sub-numbers do not exist in this format.
* A hyphen `-` instead of the em-dash `—` in a strand heading silently drops
  the competency.
* `**Document Reference:**` after line 20 is ignored entirely.
* Non-clause H2s (appendices, "Annex") become no node; their text is absorbed
  into the previous clause body.

### 3. Write `pack.config.yml`

```yaml
schema_version: 1
pack_id: "acme-british-school"

DEPARTMENTS:                # regulators -> :Department, linked by GOVERNS
  - {name: "DfE", full_name: "UK Department for Education", jurisdiction: "UK"}

POLICY_DEPT_RULES:          # Policy title keyword -> department name
  - {match: "Welfare", dept: "DfE"}

RISK_POLICY_MAP:            # risk type -> which clause addresses it
  VACCINATION_EXPIRED:
    policy_file: "pupil_welfare_code"        # <file stem>.md in this pack
    clause_keyword: "Pupil Health Centre"    # must appear in some clause heading
```

`RISK_POLICY_MAP` is the whole point of the pack. Each risk type names the
document **and** the clause heading that governs it. The loader picks the
lowest-numbered clause whose heading contains `clause_keyword` (case-insensitive);
if none matches it falls back to the document's first clause. The validator
rejects a keyword that matches nothing — there is no silent fallback to another
school's clause.

Risk types are the ones `sis_adapter.compute_risk_flags` produces today:
`VACCINATION_EXPIRED`, `CHRONIC_ATTENDANCE`, `FEE_OVERDUE`.

### 4. Point a school at the pack

One school (the default one) uses `MSOE_POLICY_PACK`; every other school goes in
`MSOE_POLICY_PACK_MAP`, a JSON object of `school_id` → pack directory. Both are
set in `docker-compose.yml` and documented in `client.manifest.example.yml`.

```bash
MSOE_POLICY_PACK=policy_vaults/acme \
MSOE_POLICY_PACK_MAP='{"campus_a": "policy_vaults/acme_campus_a"}'
```

A school in neither map gets **no** pack. That is deliberate: a risk is never
linked to a clause from someone else's policy.

### 5. Load it

```bash
docker exec msoe_api python build_knowledge_graph.py \
    --pack policy_vaults/<client_name> --school-id <school_id>
```

Loading is **additive** — it never wipes an existing pack. Two schools' packs
coexist in one graph: every node is scoped by `school_id`, so the same Standard
code under two schools is two distinct nodes.

### 6. Revisions

When the client reissues a document, edit the markdown and reload. The loader
hashes the file and, if the bytes changed, marks the old `:Policy` `superseded`
(stamping `superseded_at` + `superseded_by_hash`) and adds a new one at
`revision + 1`. Both stay queryable, so an inspector can see which clause version
governed a past decision. Reloading unchanged bytes adds no revision.

```cypher
// which version of a clause governed this decision?
MATCH (d:Decision)-[:GOVERNED_BY]->(s:Standard)-[:PART_OF]->(p:Policy)
RETURN p.title, p.revision, p.status, p.loaded_at, p.superseded_at
ORDER BY p.revision DESC
```

---

## Checklist for a new pack

- [ ] `pack.config.yml` present and valid
- [ ] Every document starts with `# Title` and has `**Document Reference:**` within 20 lines
- [ ] Every clause is `## N. Heading` — no sub-numbers, no stray H2s
- [ ] Strands (if any) use `### Strand A — Name` with a real em-dash
- [ ] Every `RISK_POLICY_MAP` entry points at a real file and a heading that exists
- [ ] `python validate_policy_pack.py <pack>` reports 0 violations
- [ ] `MSOE_POLICY_PACK` / `MSOE_POLICY_PACK_MAP` point the right school at the pack
- [ ] Loaded with `--school-id <school_id>`, and the 4-hop path resolves:

```cypher
MATCH (s:Student)-[:HAS_RISK]->(rf:RiskFlag)-[:ADDRESSED_BY]->(std:Standard)
      -[:PART_OF]->(p:Policy)
WHERE p.school_id = '<school_id>'
RETURN s.full_name, rf.type, std.heading, p.title
```
