# MSOE — Session Handoff (Warm Start)

**Project root:** `C:\Users\notti\OneDrive\Documents\Alchemist`
**Last session:** 2026-10-18
**State:** Full stack LIVE with the **live LLM enabled** (`llm.provider=openrouter`, Nemotron via OpenRouter). MSOE smoke suite **9/9 PASS** (suite_elapsed=50.4s). Gibbon SIS configured (org name/currency + admin login), bridged (students/staff/attendance/fees), and wired via the **MSOE Dashboard** module. Knowledge-graph layer verified (674 Student / 323 HAS_RISK / 4-hop Student→RiskFlag→Standard→Policy paths) and the executive service returns `GROUNDED` with cited evidence rows. Compliance-audit latency fixed (220s → 28s). **Closing sales demo built:** `ALCHEMIST-PROOF-BOOK.html` — school-shaped walkthrough of a live run (citation chain vs stock n8n workflow, GROUNDED vs REFUSED, 55 staged / 0 executed, ROI ledger with assumptions). Next: shareable publish (login), then portable per-client deployment.
**How to run it:** see `RUNBOOK.md` (bring-up → bridge → 4 services → troubleshooting).

---

## 0. Quick start (next session — do this first)

```powershell
cd C:\Users\notti\OneDrive\Documents\Alchemist

# 1. Ensure Docker Desktop is running (docker info)
docker compose up -d

# 2. Verify
docker ps --format "table {{.Names}}\t{{.Status}}"
# Expect: msoe_postgres, msoe_neo4j, msoe_api, gibbon_db, gibbon_app — all Up/healthy

# 3. MSOE smoke (run FROM docker network — host Python hits WinError 10048)
docker run --rm --network msoe_default `
  -v C:\Users\notti\OneDrive\Documents\Alchemist\test_api.py:/tmp/test_api.py `
  -e BASE=http://msoe_api:8000 `
  python:3.12-slim python -u /tmp/test_api.py

# 4. URLs
#   MSOE API docs : http://127.0.0.1:8000/docs
#   MSOE health   : http://127.0.0.1:8000/health
#   Gibbon SIS    : http://127.0.0.1:8080
```

**Do NOT** run `python -u test_api.py` on the Windows host — ephemeral ports are exhausted (WinError 10048 cascade from ~31k stuck FinWait2 sockets). Always use the Docker network client above.

---

## 1. Live stack (5 containers)

| Container | Image | Port | Role |
|-----------|-------|------|------|
| `msoe_postgres` | postgres:16 | 5432 | MSOE SIS relational store (`msoe_db`) |
| `msoe_neo4j` | neo4j:community | 7474 / 7687 | Knowledge graph |
| `msoe_api` | msoe-api:latest | 8000 | FastAPI agentic backend (4 services + SIS + approvals) |
| `gibbon_db` | mysql:8.0 | 127.0.0.1:3306 | Gibbon MySQL (`gibbon`) |
| `gibbon_app` | gibbon-app:latest | 8080 | Gibbon SIS v31.0.00 (PHP) |

Single compose file: `docker-compose.yml` (network `msoe_default`).

### Credentials (local mock only — never hardcode secrets in prod)

| Service | Host/Port | User | Password |
|---------|-----------|------|----------|
| MSOE Postgres | localhost:5432 | `msoe_admin` | `msoe_secure_2026` |
| Neo4j | localhost:7687 | `neo4j` | `msoe_graph_2026` |
| Gibbon MySQL | 127.0.0.1:3306 | `gibbon` / root | `msoe_gibbon_2026` / `msoe_gibbon_root_2026` |
| MSOE API | 127.0.0.1:8000 | — | — |
| Gibbon UI | 127.0.0.1:8080 | — | see §5 |

---

## 2. MSOE Agentic Backend (`main.py`)

Four institutional services, human-in-the-loop guardrails, Graph-Grounded RAG.

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/health` | GET | PG + Neo4j + LLM status |
| `/api/v1/audit/compliance` | POST | Risk students → policy clauses → parent notification drafts |
| `/api/v1/curriculum/harmonize` | POST | Lesson plan vs MoE AI Competencies |
| `/api/v1/operations/substitute-dispatch` | POST | Leave → substitute candidates, staged PENDING_HUMAN_APPROVAL |
| `/api/v1/executive/query` | POST | Graph-Grounded RAG; `REFUSED_INSUFFICIENT_DATA` if no paths |
| `/api/v1/approvals/pending` | GET | Pending human approvals |
| `/api/v1/approvals/{id}/execute` | POST | `{decision: approve\|reject}` |
| `/api/v1/sis/status` | GET | Adapter schema/dialect probe |
| `/api/v1/sis/webhook` | POST | Real-time LMS hooks (Gibbon/Canvas/OpenEduCat dialects) |
| `/api/v1/sis/sync` | POST | Batched Students/Attendance/Fees/Staff |

**LLM (`llm_client.py`):**
- OpenRouter OpenAI-compatible client → `https://openrouter.ai/api/v1`
- Default model: `nvidia/nemotron-3.5-lightning` (fallback `nvidia/nemotron-3.5-super-vl`)
- Env: `OPENROUTER_API_KEY` (NOT set yet — deterministic fallback in use)
- Zero-hallucination: generative calls constrained + post-validated against graph evidence

**SIS adapter (`sis_adapter.py`):**
- Dialects: `gibbon` | `openeducat` | `canvas` → MSOE tables + Neo4j Student/RiskFlag

**Gibbon bridge (`gibbon_bridge.py`):**
```bash
docker exec msoe_api python gibbon_bridge.py              # full: students+staff+attendance+fees+graph
docker exec msoe_api python gibbon_bridge.py --students-only
```
Last successful run: **391 students** + **47 staff** + **420 attendance** + **37 fees** upserted to MSOE Postgres; 391 Student nodes to Neo4j.

---

## 3. Gibbon SIS (open-source school software)

- Cloned to `gibbon/` (tag **v31.0.00** from GibbonEdu/core)
- Schema + demo loaded: **208 tables**, demo data present (~414 students / ~47 teachers in `gibbonPerson`)
- `gibbon/config.php` written (points at `gibbon_db`)
- Vendor/ autoload present (`vendor/autoload.php` OK)
- UI at **http://127.0.0.1:8080** — serves Gibbon v31 Default theme (verified 200 + HTML)
- PHP image is slim: mysqli + pdo_mysql + bcmath only (no apt — Debian mirror unreachable from Docker). Composer used busybox unzip at `/usr/local/bin/unzip`.
- **Org settings applied** — `organisationName=MSOE Academy Dubai`, `systemName=MSOE SIS`, `currency=AED د.إ`. UI title is now `MSOE - MSOE SIS`.
- **Admin login** — see `GIBBON-LOGIN.md` (local mock `msoe_admin` / `msoe_admin_2026`). Created by `gibbon_bootstrap_settings.py` using Gibbon's own `sha256(salt+password)` scheme.

### Why Gibbon (vs OpenEduCat)
Purpose-built for international schools (UAE/British/IB), free core, lightweight PHP, active community. OpenEduCat is ERP-heavier and Python/Odoo-shaped.

---

## 4. New / key files

| Path | Purpose |
|------|---------|
| `llm_client.py` | OpenRouter + Nemotron + DeterministicLLM fallback + grounding validator |
| `sis_adapter.py` | SIS dialect translation + PG/Neo4j upserts + webhook router |
| `gibbon_bridge.py` | Gibbon MySQL → MSOE via sis_adapter (students/staff/attendance/fees) |
| `gibbon_bootstrap_settings.py` | Gibbon org settings + local-mock admin bootstrap (idempotent) |
| `gibbon_seed_demo_ops.py` | Additive Gibbon attendance/finance demo seed (tables were empty) |
| `gibbon_install_msoe_module.py` | Registers the MSOE Dashboard module/action/permission rows |
| `gibbon/modules/MSOE Dashboard/` | Gibbon custom module: iframe panel → MSOE `/docs` + `/health` |
| `GIBBON-LOGIN.md` | Local Gibbon login + org settings documentation |
| `RUNBOOK.md` | Operator runbook — bring-up, bridge, 4 services, troubleshooting |
| `IMPLEMENTATION-LEDGER.md` | Task coverage ledger (proof of coverage) |
| `main.py` | App (env-configured PG/Neo4j; LLM + SIS routes wired) |
| `docker-compose.yml` | 5-service unified stack (now passes `OPENROUTER_API_KEY`, `MSOE_API_URL`) |
| `Dockerfile` | msoe-api image |
| `gibbon_build/Dockerfile` | Slim PHP image for gibbon_app |
| `gibbon/` | Full Gibbon v31 source + config.php + vendor/ |
| `start_msoe.py` | Legacy host startup (prefer docker compose) |
| `test_api.py` | Smoke suite (honours `BASE` env) |

---

## 5. Definition of done / next tasks

### Already green
- [x] LLM client + deterministic fallback + grounding
- [x] SIS adapter (3 dialects) + webhook/sync endpoints
- [x] All 4 services wired
- [x] Unified Docker stack (MSOE + Gibbon)
- [x] Gibbon schema+demo loaded, UI serves, vendor installed
- [x] Gibbon→MSOE student/staff bridge (391 / 47)
- [x] Smoke suite 9/9 (via docker network)
- [x] Gibbon org settings + admin login (`GIBBON-LOGIN.md`) — was: empty " - Gibbon" title
- [x] Gibbon attendance/finance modules active + bridge pulls attendance (420) + fees (37)
- [x] MSOE Dashboard Gibbon module (iframe → MSOE `/docs`, `/health`, approvals)

### Next unblocked tasks
1. **Road-To-AlchemistV1 Phase 1** — see the **Road-To-AlchemistV1** section below. One phase per session.
2. **Publish the proof book** *(BLOCKED — user action)* — `ALCHEMIST-PROOF-BOOK.html` is complete and openable locally. To host it as a shareable claude.ai Artifact: unset `ANTHROPIC_API_KEY` for the session (or `claude /logout` → answer "No" to API-key approval → `/login` with a Claude subscription), then republish.
3. **Clean host sockets** *(BLOCKED — user action)* — reboot Windows when convenient to clear ~31k FinWait2/Closing sockets blocking host urllib.

### Business frame (do not lose this)
**Alchemist = B2B AI Automation Agency for K-12.** Not SaaS. Differentiator: automations *grounded in the client's knowledge graph* + *guardrailed* (irreversible actions stage for human approval). Competes against saturated n8n/Make shops. Unit of sale = Discovery → Pilot → Retainer. **MSOE is the delivery engine and the sales demo, not the licensed SKU.**

SaaS thread (multi-tenancy, pooled RLS, PDPL cert, GEMS enterprise sale) is **shelved, not deleted** — revisit only if the engine productises. The `school_id` work in Phase 1 is the cheap insurance that keeps that door open.

### ROI instrumentation (new)
`roi_model.py` + `GET /api/v1/roi/report` — turns each engine run into **hours returned** and a **challengeable estimated value**. Every service returns an `roi` block; events persist in Postgres `roi_events` (survives restart).

**Honesty rule:** it measures coordination/drafting time removed from staff. It does **not** claim wages avoided, fee write-offs, or fines avoided — those cannot be defended in a client meeting. The report always returns its own `assumptions` + `method`. See `GET /api/v1/roi/assumptions` to tune per engagement.

---

# Road-To-AlchemistV1

**Source of truth for *why*:** `INTEGRATION-PLAN.md` (repo root). This section is the *how* — one self-contained execution prompt per phase.

## Method — Iterative & Incremental Development

Strict loop. **One phase per session. Do not start the next phase in the same session.**

```
Fresh session
  → read handoff.md §0 quick start, then §Road-To-AlchemistV1
  → pick the next unexecuted phase (lowest number whose status is TODO)
  → paste that phase's PROMPT block into the session
  → EXECUTE every task in the phase (no silent skips)
  → VERIFY with the phase's exact commands
  → if a check fails, FIX THE CAUSE (never weaken assertions / never suppress)
  → update IMPLEMENTATION-LEDGER.md (one row per task ID)
  → update handoff.md (status line + mark the phase DONE and the next one NEXT)
  → SESSION END
  → fresh session → repeat with the next phase
```

**Phase status tracker** (update this as you go):

| Phase | Name | Status | Session date | Notes |
|-------|------|--------|--------------|-------|
| 1 | Foundations | DONE | 2026-10-18 | All F.1–F.8 DONE. Smoke 9/9. Approvals durable (108 persist across restart). Auth 401 on missing key. Multi-school isolation verified (campus_a/campus_b same invoice). ROI regression green. Graph rebuilt with school_id scoping |
| 2 | Connector framework + OneRoster 1.2 | TODO | — | Market wedge |
| 3 | Microsoft Graph Education / SDS | TODO | — | Works without SIS credentials |
| 4 | Native connectors (iSAMS → PowerSchool; Gibbon as #0) | TODO | — | Needs iSAMS partner sandbox |
| 5 | Policy-pack library | TODO | — | Makes engagement #3 cost 30% of #1 |
| 6 | Compliance pack (UAE residency, DPIA/DPA, audit) | TODO | — | Sales artefacts |

**Rules that apply to every phase session:**
- Coverage: implement every task. Mark BLOCKED with a concrete reason, never leave blank, never delete.
- Evidence: DONE only with exact files changed AND exact command/output. "Looks correct" is not evidence.
- Never print, commit, log or embed real secret values. Env var names only.
- Never run destructive SQL against a live database. Author migrations as files. `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` only.
- Never suppress a failing check with `any`, `@ts-ignore`, `ignoreBuildErrors`, disabled rules, deleted tests or weakened assertions.
- Never push, force-push, reset or deploy. Commit only if asked.
- Tests always run from the Docker network (`BASE=http://msoe_api:8000`) — host `urllib` hits WinError 10048.

---

## PHASE 1 — Foundations

> **Status:** TODO
> **Session objective:** Make the engine safe to point at a real client's data. No connectors yet — just the substrate every connector will stand on.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 1 (Foundations) from handoff.md.
Read INTEGRATION-PLAN.md §5-6 first for the why. Follow the IID loop in
handoff.md §Road-To-AlchemistV1. Implement every task below. Mark anything
you cannot finish BLOCKED with a concrete reason — do not skip silently.

TASKS

F.1 Durable ApprovalStore
  Replace the in-memory ApprovalStore in main.py with a PostgreSQL-backed
  store (table `approval_actions`), same public API:
    stage(action, category, reason, payload) -> str  (ACT-XXXXX ids)
    pending() -> list
    execute(action_id, decision) -> dict
  DDL must be CREATE TABLE IF NOT EXISTS. Ids must survive a restart.
  Keep thread-safety (the compliance service stages from a thread pool).

F.2 Commit missing DDL to version control
  Add `schema.sql` at repo root holding CREATE TABLE IF NOT EXISTS for:
  students, attendance_logs, fee_ledgers, staff, leave_requests,
  roi_events, approval_actions.
  `staff` and `leave_requests` currently have NO DDL anywhere in the repo
  but are read/written — derive their columns from sis_adapter.sync_staff
  and the leave_requests INSERT in main.py. Never DROP anything.

F.3 school_id scoping end to end
  Add `school_id VARCHAR(64) NOT NULL DEFAULT 'default'` to students,
  attendance_logs, fee_ledgers, staff, leave_requests, approval_actions,
  roi_events. Add it to Neo4j Student/RiskFlag/Policy/Standard nodes as a
  property. Thread it through:
    - sis_adapter map_* / sync_* / handle_webhook / batch_sync
    - main.py request models (ComplianceRequest, HarmonizeRequest,
      SubstituteRequest, ExecutiveQueryRequest, SISWebhookPayload,
      SISBatchPayload) as an optional `school_id` field defaulting to
      'default'
    - the four services' PG/Neo4j queries (filter on school_id)
  Backwards compatible: with no school_id supplied, behaviour is exactly
  as today.

F.4 Fix global-unique collisions that corrupt multi-school data
  - fee_ledgers.invoice_number: UNIQUE -> UNIQUE (school_id, invoice_number)
  - students.emirates_id: UNIQUE -> UNIQUE (school_id, emirates_id);
    REMOVE the hash-synthesised mock emirates_id fallback in sis_adapter
    (make it NULL instead — a fake national ID occupying a unique key is a
    data-integrity bug)
  - staff.email: UNIQUE -> UNIQUE (school_id, email)
  - gibbon_bridge currently maps gibbonPersonID -> student_id PK. Keep the
    surrogate key but stop treating the source id as the PK: store the
    source native id in `source_student_id` and upsert on
    (school_id, source_student_id). Same for staff.

F.5 Non-destructive graph load
  build_knowledge_graph.py currently DETACH DELETEs the whole graph on every
  run. Make that opt-in only behind --wipe (keep the existing --wipe-only).
  Default run must MERGE. Scope every MERGE by school_id.
  Also make it read NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD and PG_* from
  env (it currently hardcodes credentials).

F.6 Per-client API auth + signed webhooks
  - Add `X-API-Key` auth to every /api/v1/* route except /health and
    /docs. Key(s) come from env `MSOE_API_KEYS` (comma-separated, format
    `school_id:secret`). Compare with hmac.compare_digest.
  - Add HMAC signature verification to POST /api/v1/sis/webhook using
    header `X-MSOE-Signature` (sha256 of the raw body with the school's
    secret). Reject mismatches with 401.
  - Do NOT invent a real secret value anywhere. Env var name is the contract.
  - Update test_api.py to send the header (read the key from env; if unset,
    the smoke run must fail loudly rather than silently skip auth).

F.7 Wire engagement_id through the ROI write path
  roi_model already has engagement_id as an indexed column and
  GET /api/v1/roi/report already filters on it. Stamp it at the four
  record_units(...) call sites in main.py from the request's school_id
  (or an explicit engagement_id field on the request models).

F.8 Config-not-literals
  Move these out of source and into env with a client-manifest default:
    - gibbon_bootstrap_settings.py ORG_SETTINGS (org name, short name,
      currency, email, system name)
    - gibbon_bridge.py SQL literals ('MSOE Academy Dubai', 'Dubai',
      'British', parent@msoe.ae, +971500000000)
    - sis_adapter.py defaults ("MSOE Academy", "Dubai", "parent@msoe.ae",
      "staff@msoe.ae")
    - roi_model.py CURRENCY_DEFAULT
  Add `client.manifest.example.yml` at repo root documenting every key.
  Do not remove the current values as defaults — the local demo must keep
  working unchanged.

VERIFY (exact commands — all must pass)
  1. docker compose up -d
  2. docker exec msoe_api python -m py_compile main.py sis_adapter.py
     gibbon_bridge.py roi_model.py build_knowledge_graph.py
     gibbon_bootstrap_settings.py            -> PY_COMPILE OK
  3. docker run --rm --network msoe_default \
       -v <repo>\test_api.py:/tmp/test_api.py \
       -e BASE=http://msoe_api:8000 python:3.12-slim \
       python -u /tmp/test_api.py
       -> SUMMARY: 9/9 passed   (auth header now sent)
  4. Restart-proof approvals:
       docker compose restart api
       GET /api/v1/approvals/pending  -> count > 0 persists across restart
  5. Auth rejection proof:
       curl -X POST http://msoe_api:8000/api/v1/sis/sync -d '{}'   (no key)
       -> HTTP 401
  6. Multi-school isolation proof:
       POST /api/v1/sis/sync with school_id="campus_a" and a student,
       then again with school_id="campus_b" and the SAME invoice_number
       -> both rows exist, neither overwrites the other
  7. GET /api/v1/roi/report?engagement_id=default -> still returns the
     historical events (no regression)

DEFINITION OF DONE
  Every task marked DONE in IMPLEMENTATION-LEDGER.md with the exact files
  and the exact command output above. Smoke 9/9. No secrets in source.
  Nothing dropped from any table.

DO NOT
  - Build any connector yet (that is Phase 2)
  - Multi-tenant pooled RLS (shelved — we are doing per-client instances)
  - Weaken a test to make it pass
  - Delete or rewrite seed data
```

---

## PHASE 2 — Connector framework + OneRoster 1.2

> **Status:** TODO
> **Session objective:** Turn the dialect maps into a real connector registry, and build the market wedge — one OneRoster consumer that covers PowerSchool, iSAMS and anything certified.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 2 (Connector framework + OneRoster 1.2)
from handoff.md. Read INTEGRATION-PLAN.md §3, §5.2-5.3 first. Follow the
IID loop. Phase 1 must already be DONE — verify its ledger rows first.

TASKS

C.1 Connector registry
  Create `connectors/` package:
    connectors/__init__.py       registry + load_manifest()
    connectors/base.py           abstract SourceConnector with:
        detect(payload) -> bool
        pull(since) -> CanonicalBatch
        webhook(payload) -> CanonicalBatch
        health() -> dict
    connectors/manifest.py       per-client YAML schema + validator
  Refactor sis_adapter.py's four *_FIELD_MAPS dicts into per-connector
  field maps under connectors/<dialect>/. Keep sis_adapter's canonical
  normalisers (_normalize_status, _normalize_vaccination,
  _normalize_curriculum, _as_grade, _as_date) and its sync_* upserts —
  they are the target, not the source.

C.2 Client manifest
  Schema (document in client.manifest.example.yml):
    client_id, school_id_map, dialect, credential_env_names[],
    sync.mode (pull|webhook|batch|sftp), sync.schedule,
    policy_pack.path, engagement_id, currency, org_name
  A connector resolves its credentials from the named env vars only —
  never from the manifest itself. Manifests hold NAMES, never values.

C.3 Gibbon becomes connector #0
  Move gibbon_bridge.py's fetch_* functions into connectors/gibbon/.
  Keep `python gibbon_bridge.py` working as a thin shim so the RUNBOOK
  does not break. It must now honour school_id and the org literals from
  Phase 1 F.8.

C.4 OneRoster 1.2 consumer (CSV binding first)
  connectors/oneroster/ implementing SourceConnector.
  Support the OneRoster CSV bulk set at minimum:
    orgs.csv, users.csv, classes.csv, courses.csv, enrollments.csv,
    demographics.csv
  Map to the canonical five entities. Validate against the OneRoster
  column headers; reject files that don't match with a clear error naming
  the bad column.
  Grade/term handling must not assume a US calendar — UAE schools run
  Aug-Jun and some run Sep-Jul.

C.5 OneRoster 1.2 REST binding
  Same connector, REST mode: OAuth2 client-credentials against the
  provider's OneRoster endpoint. Endpoints needed:
    /orgs /users /classes /courses /enrollments /demographics
  Support paging (Link header or limit/offset per the 1.2 spec) and a
  `since`/delta filter where the provider offers one.
  Timeout + retry with backoff. Degrade to DeterministicLLM-style
  behaviour on failure: the service stays up, the connector reports
  unhealthy in /api/v1/sis/status.

C.6 Wire into the existing HTTP contracts
  POST /api/v1/sis/sync and /api/v1/sis/webhook accept
  `dialect: "oneroster-csv" | "oneroster-rest"` in addition to the
  existing three. GET /api/v1/sis/status lists every registered connector
  and its health.

C.7 Tests
  Add `test_oneroster.py` with a checked-in fixture CSV set (synthetic
  data, clearly labelled). Assert: correct canonical rows out, correct
  school_id stamping, correct rejection of a malformed CSV, correct
  envelope for a REST payload.

VERIFY (exact commands)
  1. docker exec msoe_api python -m py_compile connectors/*.py \
       sis_adapter.py main.py                      -> PY_COMPILE OK
  2. docker run --rm --network msoe_default -v <repo>:/work -w /work \
       -e BASE=http://msoe_api:8000 python:3.12-slim \
       python -u test_oneroster.py                 -> all assertions pass
  3. Existing smoke still green:
       ... test_api.py -> SUMMARY: 9/9 passed
  4. GET /api/v1/sis/status -> lists gibbon + oneroster-csv +
     oneroster-rest with health
  5. OneRoster fixture ingest -> SELECT from students/attendance_logs
     shows rows stamped with the fixture's school_id

DEFINITION OF DONE
  Connector registry live, Gibbon is connector #0, OneRoster CSV+REST
  consumer ingests fixtures into the canonical model with school_id,
  smoke still 9/9. Ledger rows present.

DO NOT
  - Build iSAMS / PowerSchool natives (that is Phase 4)
  - Build the Graph/SDS route (that is Phase 3)
  - Hardcode any OneRoster endpoint URL or credential
  - Break the existing gibbon/openeducat/canvas dialect names
```

---

## PHASE 3 — Microsoft Graph Education / SDS

> **Status:** TODO
> **Session objective:** The route that works when IT will only grant a Graph application permission and will not open the SIS at all. Often the only door open at a group.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 3 (Microsoft Graph Education / SDS)
from handoff.md. Read INTEGRATION-PLAN.md §3 first — especially the SDS
page: SDS ingests OneRoster/CSV from any SIS into Entra ID and exposes
roster to third-party apps via a no-cost API. We consume THAT, not the
SIS. Follow the IID loop. Phase 2 must be DONE.

TASKS

G.1 connectors/graph_education/ implementing SourceConnector
  Microsoft Graph v1.0 education endpoints:
    /education/schools
    /education/classes and /education/classes/{id}/members
    /education/users (roles: student, teacher)
    /education/classes/{id}/assignments   (optional, for curriculum work)
  Auth: OAuth2 client-credentials (app-only) against
  https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
  with scope https://graph.microsoft.com/.default
  Env names (never values): MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID,
  MS_GRAPH_CLIENT_SECRET, MS_GRAPH_SCHOOL_ID_MAP

G.2 Scope and least privilege
  Document the exact Graph application permissions required:
    EducationRoster.Read.All (application) — rosters
    EducationAssignments.Read.All (application) — optional
  Do NOT request write permissions. We read rosters and stage actions;
  we never write back to the school's directory. Put this in the
  permissions section of INTEGRATION-PLAN.md.

G.3 Delta + paging
  Support @odata.deltaLink / deltaToken where Graph offers it for
  education resources, else $top/$skiptoken paging. Record the delta
  token per school_id so a second pull is incremental.

G.4 Map to canonical five entities
  educationStudent -> Student (plus demographics where present)
  educationTeacher -> Staff
  educationClass / educationEnrollment -> attendance context + grade_level
  Assignment/submission data is out of scope for the compliance engine —
  store it only if the curriculum service needs it.

G.5 SDS / Entra identity note
  Document (in INTEGRATION-PLAN.md) that where the school already runs
  SDS, the roster is already in Entra and this connector never touches
  the SIS — that is the selling point to a group IT lead. Include the
  one-paragraph explanation a non-technical principal can read.

G.6 Tests
  test_graph_education.py with a mocked Graph response fixture set
  (synthetic). Assert canonical mapping, school_id stamping, delta-token
  persistence, 401 handling, and that no write endpoint is ever called.

VERIFY (exact commands)
  1. docker exec msoe_api python -m py_compile connectors/*.py -> OK
  2. docker run ... python -u test_graph_education.py -> all pass
  3. test_oneroster.py still passes (no regression)
  4. test_api.py -> SUMMARY: 9/9 passed
  5. GET /api/v1/sis/status -> lists graph_education with health
  6. Grep proof of least privilege:
       grep -ri "graph.microsoft.com" connectors/graph_education/
     -> only GET-equivalent calls present, no PATCH/POST/DELETE to Graph

DEFINITION OF DONE
  Graph Education connector ingests a roster fixture into the canonical
  model, delta token persists, no Graph write calls exist anywhere,
  permissions documented, smoke 9/9.

DO NOT
  - Request Graph write permissions
  - Store client secrets in the manifest or in source
  - Build the Google Classroom variant (note it as future work only)
```

---

## PHASE 4 — Native connectors

> **Status:** TODO
> **Session objective:** Cover the schools that won't open OneRoster, and the data OneRoster doesn't carry — fees, behaviour, vaccination.
> **Pre-req you cannot code around:** iSAMS API access is partner-gated (`developer.isams.com` sits behind OAuth). If no sandbox exists at session start, build against a documented request/response contract and mark the live-sandbox check BLOCKED.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 4 (Native connectors) from handoff.md.
Read INTEGRATION-PLAN.md §2-3. Follow the IID loop. Phases 2 and 3 must
be DONE. If the iSAMS partner sandbox is not available, build to the
contract below and mark the live check BLOCKED with that reason.

TASKS

N.1 connectors/isams/ (priority 1 — strongest UAE signal)
  OAuth2 client-credentials against the iSAMS identity host
  (identity.isams.com). Resources: students, staff, contacts,
  attendance, behaviour, academic, finance.
  Map to the canonical five entities. Attendance and finance are the
  ones OneRoster cannot give us — get these right.
  Auth env names: ISAMS_BASE_URL, ISAMS_CLIENT_ID, ISAMS_CLIENT_SECRET,
  ISAMS_TENANT. Never hardcode.

N.2 connectors/powerschool/
  PowerSchool REST + OneRoster 1.2. Where PowerSchool speaks OneRoster,
  prefer reusing connectors/oneroster/ and treat this as a thin config
  profile rather than a new implementation. Only write bespoke code for
  what OneRoster misses.

N.3 connectors/gibbon/ polish
  Phase 2 made Gibbon connector #0. Add the two things it lacks:
    - incremental sync (watermark on last-updated, no full pull each run)
    - tombstones (a student deactivated in Gibbon must deactivate in
      MSOE, not linger forever)
  Keep `python gibbon_bridge.py` as a shim.

N.4 SFTP/CSV fallback hardening
  connectors/csv_drop/ — the Tier 3 answer to "our IT won't do an API".
  Watch a directory, validate against a documented column contract,
  stamp school_id, reject and log anything malformed. Idempotent re-import.

N.5 Connector health + operational visibility
  GET /api/v1/sis/status returns, per connector per school_id:
    last_success_at, last_error, rows_in_last_sync, watermark, mode
  This is what you demo to a client's IT lead.

VERIFY (exact commands)
  1. docker exec msoe_api python -m py_compile connectors/*.py -> OK
  2. python -u test_isams.py / test_powerschool.py / test_csv_drop.py
     -> all pass against fixtures
  3. test_oneroster.py + test_graph_education.py still pass
  4. test_api.py -> SUMMARY: 9/9 passed
  5. Incremental proof for Gibbon:
       run bridge twice; second run inserts 0 new rows (watermark works)
       deactivate a student in Gibbon; re-run; is_active flips to false
  6. GET /api/v1/sis/status shows health for every connector

DEFINITION OF DONE
  iSAMS + PowerSchool + hardened Gibbon + CSV drop all live behind the
  registry, incremental sync works, tombstones work, smoke 9/9.
  If the iSAMS sandbox was unavailable: everything else DONE, the
  live-sandbox check BLOCKED with "partner-gated developer portal,
  no sandbox credentials" and the contract-based tests recorded.

DO NOT
  - Fake a live iSAMS result you did not observe
  - Break the Tier 1 connectors
  - Add a connector that bypasses the school_id stamping
```

---

## PHASE 5 — Policy-pack library

> **Status:** TODO
> **Session objective:** This is what makes engagement #3 cost ~30% of engagement #1. Right now the policy pack format is implied by regexes and three maps are hardcoded to the MSOE pack. Write the spec down, make it config, make packs reusable.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 5 (Policy-pack library) from handoff.md.
Read INTEGRATION-PLAN.md §5.5 and §6 gap #8. Follow the IID loop.

TASKS

P.1 Write the policy-pack format spec
  Add policy_vault/FORMAT.md documenting exactly what build_knowledge_graph.py
  parses today (it currently lives only in parse_policy_file regexes):
    - file location: policy_vault/*.md (flat, sorted glob)
    - H1 first heading        -> Policy.title
    - **Document Reference:** in the first 20 lines -> Policy.reference
    - `## N. Heading` H2s     -> Standard sections (integer, period, space)
    - `### Strand X — Name`   -> Competencies (em-dash)
    - body between clause H2s -> Standard.text_content (truncated 4000)
  Add a validator script `validate_policy_pack.py` that checks a pack
  against the spec and reports every violation with the filename+line.

P.2 Make the hardcoded maps config
  Extract from build_knowledge_graph.py into
  policy_vault/pack.config.yml (or per-pack config):
    - RISK_POLICY_MAP  (risk type -> policy_file stem + clause_keyword)
    - POLICY_DEPT_RULES (title keyword -> department)
    - DEPARTMENTS
  Also fix sis_adapter.sync_students_to_graph's hardcoded ADDRESSED_BY
  heading keywords ('Campus Health'/'Attendance'/'Finance'/'Compliance')
  so they read the same config. One source of truth.

P.3 Per-client / per-school packs
  A client manifest points at a pack path. Packs are scoped by school_id
  when loaded into Neo4j. Two schools' packs must be able to coexist in
  one instance without MERGE collisions — that means Standard.code and
  Policy.title uniqueness becomes (school_id, code) / (school_id, title).
  Migration must be additive; never wipe an existing pack.

P.4 Pack revision tracking
  Store a content hash + loaded_at on each Policy node. When a client
  revises a document, reload marks the old Policy superseded rather than
  silently overwriting it, so an inspector can see which clause version
  governed a past decision.

P.5 Pack library
  Keep the three MSOE packs as the reference examples. Add
  policy_vault/README.md explaining how a new client pack is authored
  (the "convert their Word/PDF policy into the markdown spec" workflow is
  the Discovery deliverable).

P.6 Multi-hop verification for a second pack
  The 4-hop path
    (Student)-[:HAS_RISK]->(RiskFlag)-[:ADDRESSED_BY]->(Standard)-[:PART_OF]->(Policy)
  must resolve correctly for a school whose clause headings are NOT
  "Campus Health". Add a fixture pack with different headings and a test
  that proves the path still resolves via the config map.

VERIFY (exact commands)
  1. python validate_policy_pack.py policy_vault/ -> 0 violations on the
     three MSOE packs
  2. docker exec msoe_api python -m py_compile
       build_knowledge_graph.py sis_adapter.py -> OK
  3. Load the fixture pack alongside an existing one:
       MATCH (p:Policy) RETURN p.title, p.school_id
     -> both packs present, correct school_id, no collision
  4. 4-hop path resolves for the fixture pack's different headings
  5. test_api.py -> SUMMARY: 9/9 passed (existing packs still work)
  6. Revision proof: reload a changed pack -> old Policy marked
     superseded, new Policy present, both queryable

DEFINITION OF DONE
  FORMAT.md exists and is accurate, packs are config-driven, two packs
  coexist scoped by school_id, revisions are traceable, the 4-hop path
  works for non-MSOE headings, smoke 9/9.

DO NOT
  - Rewrite the markdown pack format (it is the reuse asset — spec it,
    don't replace it)
  - Wipe existing packs on load
  - Let two packs share a Standard.code across different school_ids
```

---

## PHASE 6 — Compliance pack (UAE residency, DPIA/DPA, audit)

> **Status:** TODO
> **Session objective:** The artefacts GEMS/Taaleem/PACE procurement will ask for in meeting one. Architecture must already make them true (Phase 1 did the scoping); this phase writes them down and proves them.

### PROMPT (paste this at session start)

```
Execute Road-To-AlchemistV1 Phase 6 (Compliance pack) from handoff.md.
Read INTEGRATION-PLAN.md §4 and §9 first. Follow the IID loop.
Phases 1-5 must be DONE.

TASKS

K.1 Data Residency position paper
  Add docs/compliance/DATA-RESIDENCY.md. State, in language a non-technical
  principal can read:
    - student data stays in the UAE (Azure UAE North / UAE Central)
    - one isolated instance per client group; competitors' data never
      commingle
    - cross-border transfer posture under PDPL Federal Decree-Law 45/2021
    - what happens if the PDPL Executive Regulations change the answer
  Cite the law and the Azure geography page. Do not overclaim certification.

K.2 DPIA template
  docs/compliance/DPIA-TEMPLATE.md — a working Data Protection Impact
  Assessment covering: processing purpose, categories of data subject
  (minors!), categories of personal data, lawful basis (contract /
  legal obligation — NOT consent from minors), retention, sub-processors
  (OpenRouter for LLM drafting — call this out explicitly), risks and
  mitigations, sign-off block.

K.3 DPA template
  docs/compliance/DPA-TEMPLATE.md — Data Processing Agreement a group
  legal counsel would recognise. Parties, scope, sub-processor list and
  notification, security measures, breach notification ("immediately upon
  becoming aware" — quote the PDPL standard), deletion/return on
  termination, audit rights.

K.4 Sub-processor register + the LLM question
  docs/compliance/SUBPROCESSORS.md. OpenRouter / the model provider is a
  sub-processor for parent-notification drafting. State:
    - what is sent (risk type + student name + school + clause — student-
      adjacent text)
    - temperature 0.0, post-validated against graph evidence
    - the DeterministicLLM fallback path, and that a client can require
      deterministic-only (env switch) with no external inference at all
  Implement that switch: MSOE_LLM_EXTERNAL=off forces DeterministicLLM
  and must be provable in /health.

K.5 Audit log
  Table `audit_log` (CREATE TABLE IF NOT EXISTS) recording every approval
  decision, every side effect executed, every sync run, every auth
  rejection: who (api key / school_id), what, when, outcome.
  GET /api/v1/audit/log?school_id=&since= returns it. This is the
  inspector's answer to "who approved this parent letter?".

K.6 Retention + deletion
  Document retention policy per data category in DATA-RESIDENCY.md.
  Implement a retention sweep script `retention_sweep.py` that deletes
  attendance_logs / audit_log rows older than the configured horizon,
  with the horizon from env (MSOE_RETENTION_DAYS). Dry-run by default;
  `--apply` required to actually delete. Never delete students.

K.7 Residency proof
  A verification script that asserts the deployment is where it claims
  to be: checks a config value `MSOE_DATA_REGION` is set to an allowed
  UAE region string, and fails loudly if unset. Wire it into /health.

VERIFY (exact commands)
  1. docker exec msoe_api python -m py_compile main.py retention_sweep.py
     -> OK
  2. GET /health -> includes data_region and llm.external_allowed
  3. MSOE_LLM_EXTERNAL=off docker compose up -d --force-recreate api
     -> /health shows llm.external_allowed=false and the compliance
        service still returns drafts (DeterministicLLM path)
  4. Audit log proof: approve one staged action, then
       GET /api/v1/audit/log?school_id=default
     -> shows the approval decision with timestamp
  5. python retention_sweep.py            -> dry-run, lists candidates
     python retention_sweep.py --apply    -> deletes only over-horizon rows
  6. test_api.py -> SUMMARY: 9/9 passed
  7. Every doc in docs/compliance/ names the PDPL article or source it
     relies on — no unsourced legal claims

DEFINITION OF DONE
  Four compliance documents exist and are internally consistent with the
  architecture, the LLM external-off switch is provable, audit_log is
  durable and queryable, retention sweep is dry-run-safe, /health reports
  data_region, smoke 9/9.

DO NOT
  - Claim a certification (ISO/SOC/PDPL-certified) we have not obtained
  - Put a real secret, real student record or real school name in any
    compliance document
  - Write legal advice — write templates for counsel to review
  - Delete student rows under any retention rule
```

---

## After Phase 6

All six phases DONE means the engine is integratable: safe to point at a real client's data, speaking OneRoster and Microsoft Graph, with iSAMS/PowerSchool/Gibbon natives behind a registry, policy packs as reusable library assets, and the compliance artefacts procurement asks for.

**That is the end of Road-To-AlchemistV1.** The next move is commercial, not technical: run Discovery at a real school. If the engine productises from there, the shelved multi-tenant SaaS thread becomes the discussion — Phase 1's `school_id` work is what makes it possible.

### Done since last handoff
- **OpenRouter key is live.** `OPENROUTER_API_KEY` is set in `./.env` (user-filled; value never read or logged). `/health` → `llm.provider=openrouter`, `api_key_configured: true`, `last_provider: openrouter`, `last_error: null`. `.gitignore` excludes `.env`.
- **Compliance-audit latency fixed** (see `IMPLEMENTATION-LEDGER.md` T5.7): parent-notification drafts now run concurrently under a wall-clock budget, falling back to `DeterministicLLM` for stragglers. Full 50-student audit went from a 60s timeout (~220s serial) to **28s**, all 55 bodies populated.
- **Neo4j `ORDER BY` warning fixed** (T5.8): `s.student_name` → `s.full_name` (2 query sites).
- **Runbook correction:** `docker compose up -d --force-recreate api` — the compose *service* is `api`, not `msoe_api`.
- **Closing sales demo built** (`DEMO.1`, `DEMO.2`). Live run captured to `demo_data.json` and rendered as `ALCHEMIST-PROOF-BOOK.html` — the school-shaped walkthrough where the citation chain beats a stock n8n workflow. It shows: the 4-hop `verified_path` (Student → RiskFlag → Standard → Policy) with Neo4j node IDs; `GROUNDED` executive answer vs `REFUSED_INSUFFICIENT_DATA`; guardrail **55 staged / 0 executed** with a real `ACT-00001` ticket and drafted parent letter; ROI ledger 14.63 h / AED 1,771.67 with assumptions + method tables exposed. All figures are from that run, labelled as synthetic demo data. Open the file in a browser to view.

---

## 6. Machine-specific landmines (do not regress)

| Issue | Workaround |
|-------|------------|
| Host `urllib`/`http.client` → **WinError 10048** | Run tests from Docker network with `BASE=http://msoe_api:8000` |
| PG `127.0.0.1:5432` handshake hangs on Windows | Use `localhost` or connect via Docker network (`msoe_postgres`) |
| Docker **build context** on OneDrive → Access denied | Build from `%TEMP%\msoe_build` and `%TEMP\gibbon_build` |
| `docker cp` from OneDrive path → Access denied | Copy to `%TEMP` first, then `docker cp` |
| Debian mirror unreachable during Docker build | Slim PHP image: no apt; busybox unzip for Composer |
| PowerShell mangles `$` / `<` / quotes | Prefer script files + `docker exec`; not inline python -c |
| Host curl to :8000 may deliver headers but hang on body | Prefer Docker network for API clients |

---

## 7. Verified evidence (this session)

```
SUMMARY: 9/9 passed   suite_elapsed=0.2s
  [PASS] GET /health … (all services + approvals)

docker compose ps → msoe_api, gibbon_app, msoe_postgres, msoe_neo4j, gibbon_db all Up/healthy

gibbon_bridge: students.upserted=391, staff.upserted=47, attendance.upserted=420, fees.upserted=37, graph.students_upserted=391
Gibbon UI: HTTP 200, title "MSOE - MSOE SIS", admin login → index.php OK
MSOE Dashboard module: HTTP 200, iframes to /docs + /health, menu entry present
```

Full task-by-task evidence: `IMPLEMENTATION-LEDGER.md`.

---

## 8. Smoke suite — what it covers

`test_api.py` (sequential, &lt;10 min):
1. GET /health  
2. POST compliance audit  
3. POST curriculum harmonize  
4. POST substitute dispatch  
5. POST executive query (grounded + refusal)  
6. GET approvals pending  
7. POST approvals execute (approve + reject)

Run via Docker (see §0). `BASE` env overrides `http://127.0.0.1:8000`.

---

**End of handoff.** Next engineer: start at §0, then pick §5 next tasks.
