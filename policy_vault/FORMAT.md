# Policy-pack format specification

**Status:** normative for the parser in `build_knowledge_graph.py`.
**Audience:** anyone authoring or reviewing a client policy pack.
**Validator:** `python validate_policy_pack.py <pack-dir-or-file>` reports every
deviation from this document as `filename:line: LEVEL code: message`.

This document specifies the markdown pack format **as the engine parses it
today**. The format is the reuse asset: it is specified here, not replaced.
Do not change the markdown shape without changing the parser and this file
together.

---

## 1. Pack layout

A **pack** is a directory:

```
<pack_dir>/
    pack.config.yml      # required — DEPARTMENTS / POLICY_DEPT_RULES / RISK_POLICY_MAP
    *.md                 # policy documents — the pack format specified below
    README.md            # author documentation — never parsed as a policy document
    FORMAT.md            # this file          — never parsed as a policy document
```

| Rule | Detail |
|------|--------|
| Document location | `<pack_dir>/*.md`, **flat** (no subdirectories) |
| Enumeration | **sorted** `Path.glob("*.md")` — load order is filename order, and it is deterministic |
| Excluded names | `README.md`, `FORMAT.md` are pack documentation and are never parsed as policy documents |
| Case | the glob is case-sensitive on Linux: `Policy.md` is a policy document, `readme.md` is **not** in the exclusion set |

A pack with no policy documents is a violation (`no-documents`).

### 1.1 Scope: one pack per school

A pack is loaded for exactly one `school_id` (see §5). Two packs may live in
one Neo4j instance because every node identity is scoped by `school_id` — a
code or title used by one school never MERGEs onto another school's node.

---

## 2. Policy document rules

Every `*.md` policy document parses into one `:Policy` node, a set of
`:Standard` nodes, and optionally `:Competency` nodes. The mapping is:

| Markdown construct | Becomes | Property |
|--------------------|---------|----------|
| First H1 (`# Title`) | `:Policy` | `title` |
| `**Document Reference:**` in the first **20** lines | `:Policy` | `reference` |
| File name | `:Policy` | `source_file` |
| File bytes (sha256) | `:Policy` | `content_hash` |
| `## N. Heading` H2 | `:Standard` | `clause_number` = `N`, `heading` = `Heading` |
| Body between clause H2s | `:Standard` | `text_content` (truncated to **4000** chars) |
| `### Strand X — Name` H3 | `:Competency` | `code` = `StrandX`, `name` = `Name` |
| Body after a strand H3 | `:Competency` | `description` (truncated to **2000** chars) |

### 2.1 Title (H1 → `Policy.title`)

```
# KHDA / ADEK Health & Safety Protocols
```

- The **first** heading of the document must be an H1: a line beginning with
  `# ` (hash + space).
- Its text after `# ` is the `Policy.title`, trimmed.
- Exactly one H1 per document. A second H1 is a violation: only the first is
  kept, and the rest is silently absorbed into a clause body.
- If no H1 exists the loader falls back to the filename stem with `_` → space
  and title-casing. That fallback is almost always wrong (it will not match
  `POLICY_DEPT_RULES`), so the validator reports `no-h1` as a violation.

### 2.2 Document reference (`Policy.reference`)

```
**Document Reference:** MSOE-HS-2026-02
```

- Matched by the regex `\*\*Document Reference:\*\*\s*(.+)`.
- Only lines **1–20** (1-based) are scanned; the first match wins.
- Beyond line 20 the loader never sees it (`doc-ref-late` violation).
- The reference is the base of every Standard code (§2.4). Keep it short,
  stable and unique per document within the school.

Other header metadata in that window (`**Effective Date:**`, `**Owner:**`,
`**Jurisdiction:**`, …) is **not** parsed. It is human-readable provenance and
the Discovery convention — put it above the first `---`, unstructured.

### 2.3 Clause sections (H2 → `:Standard`)

```
## 4. Campus Health Services
```

- Pattern, matched against the **stripped** line:
  `^##\s+(\d+)\.\s+(.+)$` — literally: `##`, whitespace, an **integer**,
  a **period**, **whitespace**, then a non-empty heading.
- The integer is `Standard.clause_number` (kept as a string; `§4` and `§04`
  are different codes — leading zeros are warned about, not normalised).
- The heading text is `Standard.heading`. This is what `RISK_POLICY_MAP`
  matches its `clause_keyword` against (§3.3).
- Numbering is per document and should be dense from 1. Duplicate numbers are
  a violation: two clauses with the same number produce the same Standard code
  and MERGE into one node.

**H2s that do not match** never become Standards:

| Example | Validator | What the parser does |
|---------|-----------|----------------------|
| `## 1.Heading` (no space after the period) | `bad-clause-h2` **violation** | not a clause; text absorbed into the previous clause |
| `## 1 Heading` (no period) | `bad-clause-h2` **violation** | as above |
| `## 4.1 Subsection` | parses as clause `4` with heading `1 Subsection` | `suspicious-clause-heading` warning |
| `## Appendix A — Glossary` | `non-clause-h2` **warning** | no Standard; text absorbed into the previous clause's body |
| `## Strand A — Name` (should be H3) | `strand-wrong-level` **violation** | no Competency, no Standard |

Absorption is the important quirk: the body of the **last** clause runs to end
of file, so trailing appendices land inside that clause's `text_content`. In
the MSOE reference pack, `moe_ai_curriculum_framework.md` §10's body includes
the Appendix A/B content for exactly this reason.

### 2.4 Standard codes and `text_content`

```
code         = f"{reference or title}§{clause_number}"      e.g. MSOE-HS-2026-02§4
text_content = body after the H2 up to the next clause H2 (or EOF)
               stripped, runs of 3+ blank lines collapsed to one,
               then truncated to 4000 characters
```

- The body **includes** any nested `###` subsections and their text.
- The `---` horizontal rules and everything else stay in the body verbatim.
- Truncation is silent in the loader and a `truncated-section` **warning** in
  the validator — write clause bodies under 4000 chars.

### 2.5 Competency strands (H3 → `:Competency`)

```
### Strand A — Computational Thinking
```

- Pattern: `^###\s+(Strand\s+[A-F])\s+—\s+(.+)$`
  - the word `Strand`, whitespace, a single letter **A–F**
  - then whitespace, an **em-dash `—` (U+2014)**, whitespace, then the name
  - a hyphen `-` (U+002D) or en-dash `–` (U+2013) does **not** match
- `code` = the strand token with spaces removed → `StrandA` … `StrandF`.
- `description` = body up to the next `##` or `###` heading (an `####` heading
  does **not** stop it), collapsed and truncated to **2000** chars.
- Strand letter outside A–F, a missing name, or the wrong dash character is a
  `bad-strand` **violation** — the line is silently not a Competency.

Strands are the MoE AI-competency shape. A client pack with no competencies
simply has no `### Strand …` lines.

---

## 3. `pack.config.yml`

Required, in the pack directory. This is the **one source of truth** for the
three maps that were previously hardcoded in `build_knowledge_graph.py` and
(divergently) in `sis_adapter.sync_students_to_graph`. Loaded by
`policy_pack.load_pack_config()`.

```yaml
schema_version: 1
pack_id: "msoe-uae-reference"

DEPARTMENTS:          # -> (:Department) nodes
  - {name: "KHDA", full_name: "Knowledge and Human Development Authority", jurisdiction: "Dubai"}

POLICY_DEPT_RULES:    # -> (p:Policy)-[:GOVERNS]->(d:Department)
  - {match: "Health", dept: "KHDA"}

RISK_POLICY_MAP:      # -> (rf:RiskFlag)-[:ADDRESSED_BY]->(s:Standard)
  VACCINATION_EXPIRED:
    policy_file: "khda_adek_health_safety_protocols"
    clause_keyword: "Campus Health"
```

### 3.1 `DEPARTMENTS`

List of `{name, full_name, jurisdiction}`. Upserted as `:Department` nodes,
uniquely keyed on `name` (departments are shared across schools — they are
regulators, not client data). All three fields are required.

### 3.2 `POLICY_DEPT_RULES`

List of `{match, dept}`. For each rule, if `match` appears as a
**case-insensitive substring** of the Policy title, a
`(p)-[:GOVERNS]->(d:Department {name: dept})` edge is MERGEd. `dept` must
name an entry in `DEPARTMENTS` (enforced at load). A policy may match several
rules and therefore govern several departments.

### 3.3 `RISK_POLICY_MAP`

Mapping of risk type → `{policy_file, clause_keyword}`:

| Key | Meaning |
|-----|---------|
| `policy_file` | The pack document **stem** — the `.md` basename without the extension |
| `clause_keyword` | Case-insensitive substring matched against `Standard.heading` |

Resolution, identical in `build_knowledge_graph.py` and
`sis_adapter.sync_students_to_graph`:

1. find the `:Policy` for `policy_file` + the target `school_id`, current
   revision only (§4);
2. among its Standards, take the one whose `heading` contains
   `clause_keyword`, ordered by `clause_number` ascending, `LIMIT 1`;
3. if none matches, fall back to that document's **first** clause.

Step 3 is the silent mis-link gap #8 warns about, so
`validate_policy_pack.py` reports `config-keyword-unmatched` as a **violation**
whenever a `clause_keyword` matches no heading in the named document, and
`config-unknown-policy-file` when `policy_file` names no document in the pack.

A risk type absent from `RISK_POLICY_MAP` gets **no** `ADDRESSED_BY` edge.
Nothing is invented.

---

## 4. Revision tracking

Each `:Policy` and the `:Standard`/`:Competency` nodes parsed from it carry:

| Property | On | Meaning |
|----------|----|---------|
| `content_hash` | `:Policy` | sha256 of the document bytes — the revision fingerprint |
| `loaded_at` | `:Policy` | ISO timestamp of the last load of this revision |
| `revision` | `:Policy` | 1-based counter, incremented on each content change |
| `status` | `:Policy` | `active` or `superseded` |
| `superseded_at` | `:Policy` | set when a later revision replaces this one |
| `superseded_by_hash` | `:Policy` | `content_hash` of the replacing revision |
| `policy_hash` | `:Standard` | `content_hash` of the owning document version |
| `policy_hash` | `:Competency` | ditto |

Reload semantics for a document with the same `(title, school_id)`:

| Incoming `content_hash` | Effect |
|-------------------------|--------|
| matches an existing version | that version is refreshed (`loaded_at`) and re-activated — a revert un-supersedes |
| matches a legacy node with no `content_hash` | **adopted in place**: the hash is written onto the existing node, nothing is superseded (additive migration) |
| differs from every version | previous `active` version(s) are marked `superseded`, and a **new** node is created |

The old Policy is therefore never overwritten or deleted. An inspector can see
which clause version governed a past decision:

```cypher
// every revision of a document, newest last
MATCH (p:Policy {school_id: $school_id, title: $title})
RETURN p.revision, p.status, p.content_hash, p.loaded_at,
       p.superseded_at, p.superseded_by_hash
ORDER BY p.revision;

// the clause text as it stood under a superseded revision
MATCH (p:Policy {school_id: $school_id, content_hash: $hash})<-[:PART_OF]-(s:Standard)
RETURN s.clause_number, s.heading, s.text_content;
```

Live 4-hop paths select the current revision only
(`WHERE coalesce(p.status, 'active') = 'active'`), so a superseded clause never
silently governs a new decision — and never disappears from the graph.

Identity is `(title, school_id, content_hash)` for `:Policy` and
`(code, school_id, policy_hash)` for `:Standard`, which is why two revisions of
the same document can coexist.

---

## 5. Loading

```bash
python build_knowledge_graph.py --pack policy_vault --school-id default
```

| Argument | Default | Meaning |
|----------|---------|---------|
| `--pack` | `policy_vault` | pack directory to load |
| `--school-id` | `default` | scope every MERGE to this school |
| `--wipe` | off | `DETACH DELETE` the whole graph first (destructive, opt-in) |
| `--wipe-only` | off | wipe and exit |

Loading is **additive**: it MERGEs, never wipes, and never removes another
school's or another revision's nodes.

School → pack resolution at sync time (`policy_pack.pack_dir_for_school`):

1. in-process registry — `register_school_pack(school_id, pack_dir)`
2. env `MSOE_POLICY_PACK_MAP` — JSON object `{"school_id": "/path/to/pack"}`
3. env `MSOE_POLICY_PACK` (default `policy_vault`) for `school_id` `default`
4. otherwise **no pack** — the risk is left unlinked rather than borrowing
   another school's clauses

A client manifest points at its pack via `policy_pack.path` (see
`client.manifest.example.yml`); that value is what `MSOE_POLICY_PACK` /
`MSOE_POLICY_PACK_MAP` carry into the engine.

---

## 6. Node identity and multi-school coexistence

| Label | Identity (uniqueness) |
|-------|----------------------|
| `:Policy` | `(title, school_id, content_hash)` |
| `:Standard` | `(code, school_id, policy_hash)` |
| `:Competency` | `(code, school_id)` |
| `:RiskFlag` | `(type, school_id)` |
| `:Student` | `(student_id, school_id)` |
| `:Department` | `name` — shared across schools (regulators) |

Two packs under two `school_id`s may therefore use identical document
references and clause numbers without colliding: the `school_id` in the key
keeps their nodes distinct. They may **not** share a node — that is what the
compound key prevents.

Nodes created before Phase 5 carry no `content_hash`/`school_id`; loading
adopts them in place (§4) and a one-pass backfill stamps
`school_id = 'default'` where the property is missing. No node is deleted by
the migration.

---

## 7. What the parser ignores

Useful when converting a Word/PDF policy (the Discovery deliverable) — these
are free to use for human structure and cost nothing:

- `**Key:** value` metadata lines other than `**Document Reference:**`
- `---` horizontal rules
- bullet lists, numbered lists, tables, blockquotes, bold/italic
- `####` and deeper headings (their text stays in the enclosing clause body)
- `###` headings that are not `### Strand A–F — Name` (subsections, e.g.
  `### 4.1 Clinical Staffing`)
- any HTML, images, links — the text content is stored as-is
