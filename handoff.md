# MSOE — Session Handoff (Warm Start)

**Project root:** `C:\Users\notti\OneDrive\Documents\Alchemist`
**Last session:** 2026-10-23
**State:** Road-To-AlchemistV1 **Phases 1–6 DONE — complete.** Phase 6 closed the compliance pack: four counsel-review templates in `docs/compliance/` (data residency + retention table, DPIA, DPA, subprocessors — UAE PDPL Federal Decree-Law No. 45 of 2021, sourced, no certification claimed), a durable `audit_log` answering "who approved this parent letter?", a **dry-run-safe** `retention_sweep.py` (allowlist `attendance_logs` + `audit_log`, students never deleted), `residency_check.py` wired into `/health` (`data_region`, allowed values `uae-north`/`uae-central`), and the `MSOE_LLM_EXTERNAL=off` kill switch (proven to beat a configured key; all call sites degrade to `DeterministicLLM`). Confirmation re-verify green 2026-10-23: `py_compile` OK, `/health` carries `data_region` + `llm.external_allowed`, retention dry-run deletes nothing (students 543→543), smoke **9/9**. **The next move is commercial, not technical: Discovery at a real school.**
**How to run it:** see `RUNBOOK.md` (bring-up → bridge → 4 services → troubleshooting).
**This host note:** Phases 2–3 ran on a Linux box with **podman** (no docker CLI). Same containers/network (`msoe_default`); volume mounts need `:z` (SELinux). Fresh instance required a one-time bootstrap (synthetic seed via `sis_adapter` + `build_knowledge_graph.py`) — see §7.

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
| `sis_adapter.py` | Canonical normalisers + PG/Neo4j upserts + webhook router + `ingest_batch` |
| `connectors/` | **Phase 2 connector framework** — registry, base contract, manifest validator |
| `connectors/state.py` | **Phase 3** durable delta-token store (Postgres `connector_state`) |
| `connectors/gibbon/` | Gibbon connector #0 (fetch_* moved from gibbon_bridge) |
| `connectors/oneroster/` | OneRoster 1.2 CSV + REST bindings (the market wedge) |
| `connectors/graph_education/` | **Phase 3** Microsoft Graph Education / SDS (read-only) |
| `connectors/openeducat/`, `connectors/canvas/` | Legacy dialect field maps + webhook connectors |
| `test_oneroster.py` | OneRoster connector tests (63 assertions) |
| `test_graph_education.py` | Graph Education connector tests (43 assertions) |
| `fixtures/oneroster/` | SYNTHETIC OneRoster 1.2 bulk set (6 CSV files, labelled) |
| `fixtures/graph_education/` | SYNTHETIC Graph v1.0 responses (6 JSON fixtures, labelled) |
| `gibbon_bridge.py` | Thin shim → GibbonConnector (RUNBOOK commands unchanged) |
| `gibbon_bootstrap_settings.py` | Gibbon org settings + local-mock admin bootstrap (idempotent) |
| `gibbon_seed_demo_ops.py` | Additive Gibbon attendance/finance demo seed (tables were empty) |
| `gibbon_install_msoe_module.py` | Registers the MSOE Dashboard module/action/permission rows |
| `gibbon/modules/MSOE Dashboard/` | Gibbon custom module: iframe panel → MSOE `/docs` + `/health` |
| `GIBBON-LOGIN.md` | Local Gibbon login + org settings documentation |
| `RUNBOOK.md` | Operator runbook — bring-up, bridge, 4 services, troubleshooting |
| `IMPLEMENTATION-LEDGER.md` | Task coverage ledger (proof of coverage) |
| `main.py` | App (env-configured PG/Neo4j; LLM + SIS routes wired) |
| `docker-compose.yml` | 5-service unified stack (now passes `OPENROUTER_API_KEY`, `MSOE_API_URL`) |
| `Dockerfile` | msoe-api image (now COPYs `connectors/`) |
| `gibbon_build/Dockerfile` | Slim PHP image for gibbon_app |
| `gibbon/` | Full Gibbon v31 source + config.php + vendor/ |
| `start_msoe.py` | Legacy host startup (prefer docker compose) |
| `test_api.py` | Smoke suite (honours `BASE` env) |
| `client.manifest.example.yml` | Connector-manifest schema (C.2) + F.8 config surface |

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
- [x] **Road-To-AlchemistV1 Phase 6 (Compliance pack — UAE residency, DPIA/DPA, audit)** — K.1–K.7 DONE 2026-10-23. The artefacts GEMS/Taaleem/PACE procurement asks for in meeting one now exist in `docs/compliance/`. Road-To-AlchemistV1 is **complete**.

### Next unblocked tasks
All Road-To-AlchemistV1 phases are DONE. What remains is BLOCKED on user action:
1. **Publish the proof book** *(BLOCKED — user action)* — `ALCHEMIST-PROOF-BOOK.html` is complete and openable locally. To host it as a shareable claude.ai Artifact: unset `ANTHROPIC_API_KEY` for the session (or `claude /logout` → answer "No" to API-key approval → `/login` with a Claude subscription), then republish.
2. **Clean host sockets** *(BLOCKED — user action)* — reboot Windows when convenient to clear ~31k FinWait2/Closing sockets blocking host urllib.

The next *unblocked* move is commercial, not technical: **run Discovery at a real school** (see *Business frame* below).

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
| 2 | Connector framework + OneRoster 1.2 | DONE | 2026-10-23 | All C.1–C.7 DONE. Registry live (5 dialects), Gibbon connector #0 + shim preserved, OneRoster CSV+REST consumers with school_id stamping, manifest schema validated (names-not-values). test_oneroster.py 63/63, smoke 9/9. REST paging (Link + limit/offset), 401→UNHEALTHY degrade proven with injected transport |
| 3 | Microsoft Graph Education / SDS | DONE | 2026-10-23 | All G.1–G.6 DONE. `graph_education` connector (Graph v1.0 read-only, OAuth2 app-only), delta tokens per school_id in durable `connector_state` (Postgres), 401→UNHEALTHY degrade, **no Graph write call exists** (asserted on every recorded request + grep). test_graph_education.py 43/43, test_oneroster.py 63/63 regression, smoke 9/9. Permissions + SDS principal note documented in INTEGRATION-PLAN.md §3.1–3.2 |
| 4 | Native connectors (iSAMS → PowerSchool; Gibbon as #0) | DONE | 2026-10-23 | All N.1–N.5 DONE. `isams` + `powerschool` + `csv_drop` behind the registry; Gibbon incremental (monotonic watermark 2770) + tombstones proven live (deactivate → `is_active=false`, flag-only, watermark unmoved); N.5 status view per connector per school_id. test_isams 63/63, test_powerschool 43/43, test_csv_drop 48/48, Tier 1 regression 63/63 + 43/43, smoke 9/9. **iSAMS live check BLOCKED: partner-gated developer portal, no sandbox credentials** (contract tests recorded). PowerSchool live check NOT RUN (no credentials) |
| 5 | Policy-pack library | DONE | 2026-10-23 | All P.1–P.6 DONE. `policy_vault/FORMAT.md` (spec) + `validate_policy_pack.py` (0 violations on the three MSOE packs); the three maps moved to `pack.config.yml` and are the single source for `build_knowledge_graph` **and** `sis_adapter` (the old hardcoded `CASE … THEN 'Campus Health'/'Attendance'/'Finance'/'Compliance'` is gone — the two modules used to disagree); packs scoped by `school_id` (identical Standard.code across schools stays two nodes); reload of a revised document supersedes the old Policy (`rev=1 superseded by=…` / `rev=2 active`, both queryable). `test_policy_pack.py` **44/44**, smoke **9/9**. 4-hop resolves for non-MSOE headings (`Pupil Health Centre` vs `Campus Health`) via config |
| 6 | Compliance pack (UAE residency, DPIA/DPA, audit) | DONE | 2026-10-23 | All K.1–K.7 DONE. **K.1** `docs/compliance/DATA-RESIDENCY.md` (position paper + retention table per data category). **K.2** `DPIA-TEMPLATE.md` (minors; lawful basis = contract/legal obligation, NOT consent; OpenRouter called out). **K.3** `DPA-TEMPLATE.md` (breach "immediately upon becoming aware" — PDPL Art. 9 standard). **K.4** `SUBPROCESSORS.md` + `MSOE_LLM_EXTERNAL` kill switch (off → api_key discarded, `chat()` raises, all call sites degrade to `DeterministicLLM`; proven to beat a dummy configured key). **K.5** `audit_log.py` + `GET /api/v1/audit/log` (fingerprints, never secret values; cross-school → 403 + recorded). **K.6** `retention_sweep.py` (allowlist exactly `attendance_logs`+`audit_log`, dry-run default, `--apply` required, min 30d, students never deleted — 543→543). **K.7** `residency_check.py` + `/health` `data_region`/`residency` block. Close-out bookkeeping + confirmation re-verify green (see QS.11 / K.1–K.7 in `IMPLEMENTATION-LEDGER.md`) |

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

> **Status:** DONE (2026-10-18)
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

> **Status:** DONE (2026-10-23)
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

> **Status:** DONE (2026-10-23)
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

> **Status:** DONE (2026-10-23)
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

> **Status:** DONE (2026-10-23)
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

> **Status:** NEXT
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

### CONTINUATION PROMPT (paste this next session — Phase 6 close-out)

> Session 2026-10-23 implemented and verified Phase 6 in full, then ended
> before the IID bookkeeping (ledger + handoff close-out). The code is
> in place and green — this prompt finishes the phase. Do NOT paste the
> original PROMPT above.

```
Execute the Road-To-AlchemistV1 Phase 6 CLOSE-OUT (continuation) from
handoff.md. Phase 6 was implemented and fully verified on 2026-10-23;
this session does the IID bookkeeping + one confirmation re-verify.
Read INTEGRATION-PLAN.md §4 and §9. Do NOT re-implement — K.1–K.7 are
delivered and green. If a confirmation check fails, fix the cause
(never weaken assertions).

STATE AT HANDOVER (2026-10-23, Linux/podman host)

  All K.1–K.7 implemented. Every VERIFY command in the original PROMPT
  passed. Files delivered (all uncommitted working-tree changes):

    docs/compliance/DATA-RESIDENCY.md   new — K.1 position paper + K.6
                                        retention table per data category
    docs/compliance/DPIA-TEMPLATE.md    new — K.2 (minors, lawful basis =
                                        contract/legal obligation, NOT
                                        consent; OpenRouter called out)
    docs/compliance/DPA-TEMPLATE.md     new — K.3 (breach "immediately
                                        upon becoming aware" quoted from
                                        PDPL Art. 9 standard)
    docs/compliance/SUBPROCESSORS.md    new — K.4 register + the LLM
                                        question (payload minimisation,
                                        temp 0.0, graph post-validation,
                                        DeterministicLLM fallback,
                                        MSOE_LLM_EXTERNAL=off switch)
    audit_log.py                        new — K.5 audit module (DDL +
                                        record/query; fingerprints, never
                                        secret values)
    retention_sweep.py                  new — K.6 (allowlist of exactly
                                        attendance_logs + audit_log;
                                        dry-run default; --apply required;
                                        min horizon 30d guard; prints
                                        students before/after)
    residency_check.py                  new — K.7 (MSOE_DATA_REGION must
                                        be uae-north / uae-central; fails
                                        loudly if unset)
    main.py                             audit hooks (auth_rejection in the
                                        middleware, approval_decision +
                                        side_effect in execute_approval,
                                        sync_run in sis sync/webhook),
                                        GET /api/v1/audit/log?school_id=&since=
                                        (cross-school -> 403 + recorded),
                                        /health gains data_region +
                                        residency block
    llm_client.py                       MSOE_LLM_EXTERNAL kill switch
                                        (off -> api_key discarded at
                                        construction, chat() raises, all
                                        call sites degrade to
                                        DeterministicLLM; status() reports
                                        external_allowed); also
                                        summarize_violations now sends a
                                        minimised payload (no emirates_id
                                        / contact / health detail)
    schema.sql                          audit_log table + indexes
    docker-compose.yml                  MSOE_DATA_REGION (default
                                        uae-north), MSOE_LLM_EXTERNAL
                                        (default on), MSOE_RETENTION_DAYS
                                        (default 365)
    Dockerfile                          COPYs audit_log.py,
                                        retention_sweep.py,
                                        residency_check.py

  EVIDENCE CAPTURED 2026-10-23 (write these into the ledger rows):

    1. podman exec msoe_api python -m py_compile main.py
       retention_sweep.py audit_log.py residency_check.py llm_client.py
       -> PY_COMPILE OK
    2. GET /health -> status healthy, data_region "uae-north",
       residency {configured true, allowed true, allowed_values
       [uae-north, uae-central]}, llm.external_allowed true,
       llm.temperature 0.0
    3. MSOE_LLM_EXTERNAL=off recreate -> /health llm.external_allowed
       false, provider deterministic; POST /api/v1/audit/compliance
       -> PENDING_HUMAN_APPROVAL, 8/8 non-empty draft bodies
       (DeterministicLLM path). Extra in-process proof with a DUMMY
       OPENROUTER_API_KEY set alongside off: client.api_key None,
       chat() raises "MSOE_LLM_EXTERNAL=off — external inference is
       disabled by configuration" — the switch beats a configured key.
       (Note: no real OPENROUTER_API_KEY on this host — external-LLM
       live path was not exercised; switch + fallback were.)
    4. Audit proof: approved ACT-00003 (reviewer "Principal (demo
       reviewer)") -> GET /api/v1/audit/log?school_id=default shows
       approval_decision SEND_PARENT_NOTIFICATION APPROVED at
       2026-10-23T12:40:56Z + side_effect SIMULATED. invalid_api_key
       rejections land in ?school_id=unauthenticated with sha256
       fingerprints (never the key). Cross-school query -> 403 and is
       itself recorded. Event totals at handover: approval_decision 5,
       auth_rejection 3, side_effect 3, sync_run 7 (sync rows from the
       OneRoster live-ingest part of the regression).
    5. retention_sweep.py: SYNTHETIC probe fixture (school_id
       'retention_probe', student_id 1122 — labelled, left in place):
       2 old attendance rows (2024-01-15 / 2024-02-20) + 1 recent
       (2026-10-20); 2 old audit rows (-400d / -380d) + 1 recent.
       Dry-run listed exactly 4 candidates; --apply deleted exactly 4;
       students 543 -> 543 (UNCHANGED); recent rows survived.
       --apply --days 1 refused (min 30d, exit 2).
    6. test_api.py -> SUMMARY: 9/9 passed TWICE (once with
       MSOE_LLM_EXTERNAL=off, once restored to on; suite_elapsed 1.1s).
       Stack left in the DEFAULT state: external on, region uae-north.
    7. Doc sourcing check: all 4 docs cite Federal Decree-Law No. 45 of
       2021 + named articles/sources + a Sources section with the DLA
       Piper PDPL link and the Azure geographies link. No certification
       claimed (the only "certified" strings are the explicit disclaimer
       and the DPA deletion-certificate clause). No real secret, student
       record or client/school name in any doc (grep verified).
    8. Full regression: test_policy_pack 44/44, test_oneroster 63/63,
       test_graph_education 43/43, test_isams 63/63,
       test_powerschool 43/43, test_csv_drop 48/48. No assertion
       weakened; test_api.py untouched.

  HOST OPERATING NOTES (this Linux box — differs from the Windows
  quick-start in §0):

    - podman, no docker CLI. `podman compose` works via podman-compose,
      BUT `podman-compose up -d --force-recreate api` HANGS forever:
      depends_on service_healthy is never satisfied (containers report
      Health.Status None under this podman setup). Working recipe:
      `podman rm -f msoe_api && podman-compose up -d --no-deps api`
    - Postgres/Neo4j ports are NOT published to the host. Run every test
      from the container network, e.g.:
      podman run --rm --network msoe_default -v $PWD:/app:z \
        -e NEO4J_URI=bolt://msoe_neo4j:7687 -e NEO4J_USER=neo4j \
        -e NEO4J_PASSWORD=msoe_graph_2026 -e PGHOST=msoe_postgres \
        -e PGPORT=5432 -e PGDATABASE=msoe_db -e PGUSER=msoe_admin \
        -e PGPASSWORD=msoe_secure_2026 \
        -e BASE=http://msoe_api:8000 \
        -e MSOE_API_KEYS=default:demo-api-key-2026 \
        msoe-api:latest python -u test_api.py
    - test_api.py and the Part-4 live-ingest blocks of test_oneroster /
      test_graph_education REQUIRE -e MSOE_API_KEYS=default:demo-api-
      key-2026. Without it they env-refuse (counts drop to 54 / 38) —
      that is the test declining to run uncredentialed, not a regression.
    - Image rebuild: `podman build -t msoe-api:latest .` from repo root.

TASKS

  L.1 IMPLEMENTATION-LEDGER.md
    Add rows K.1–K.7 (one per original task ID) + QS.11 (Phase 6 verify
    sweep), all DONE, using the EVIDENCE above as the exact
    command/output column. Update the Coverage summary counts.
    K.1 DATA-RESIDENCY.md · K.2 DPIA-TEMPLATE.md · K.3 DPA-TEMPLATE.md
    · K.4 SUBPROCESSORS.md + MSOE_LLM_EXTERNAL switch · K.5 audit_log
    table + GET /api/v1/audit/log · K.6 retention_sweep.py + retention
    table in DATA-RESIDENCY.md · K.7 residency_check.py + /health wire.

  L.2 handoff.md close-out
    - Phase status tracker row 6 -> DONE (2026-10-23), notes = K.1–K.7
      summary
    - Rewrite the top **State:** line: Phases 1–6 DONE, Road-To-AlchemistV1
      complete; next move is commercial (Discovery at a real school)
    - Add a **Done since last handoff** bullet for Phase 6: what the four
      documents are, the kill switch + /health proof, audit_log answer to
      "who approved this parent letter?", dry-run-safe retention sweep,
      residency proof — plus honesty notes: (a) docs are templates for
      counsel, not legal advice, and claim no certification; (b) no real
      secret/student/school name in any compliance doc; (c) the
      retention-probe fixture (school_id 'retention_probe', student
      "Retention"/"Probe", SYNTHETIC) is still in the DB as evidence —
      remove manually if a clean demo DB is wanted, the sweep will never
      delete it; (d) no real OPENROUTER_API_KEY on this host, so the live
      external-LLM path was not exercised this session (switch + fallback
      proven instead); (e) parent-notification dispatch remains a
      simulated relay — nothing is actually emailed
    - §5 Next unblocked tasks: mark the Phase 6 item done; the remaining
      items are the two BLOCKED user-action ones (proof-book publish,
      Windows socket reboot)

  L.3 Confirmation re-verify (fast — proves the handover state holds)
    1. podman exec msoe_api python -m py_compile main.py
       retention_sweep.py -> OK
    2. GET /health -> data_region + llm.external_allowed present
    3. python retention_sweep.py -> dry-run, no deletion
    4. test_api.py -> SUMMARY: 9/9 passed
    If any check fails, FIX THE CAUSE — never weaken an assertion.

VERIFY (this close-out session)
  1. Ledger contains K.1–K.7 + QS.11 rows, all DONE, with exact commands
     and outputs (not "looks correct")
  2. handoff.md tracker row 6 = DONE and the State: line is rewritten
  3. Confirmation re-verify all green
  4. grep docs/compliance/ -> no certification claim, no real secret /
     student / client-school name

DO NOT
  - Re-implement or refactor what is already green (K.1–K.7 are done)
  - Claim a certification (ISO/SOC/PDPL-certified) we have not obtained
  - Put a real secret, real student record or real school name in any
    compliance document
  - Write legal advice — the four docs stay templates for counsel
  - Delete student rows under any retention rule
  - Weaken or skip any assertion to make a check pass
  - Commit, push or deploy (commit only if the user asks)

DEFINITION OF DONE
  Ledger and handoff reflect reality (Phase 6 DONE, Road-To-AlchemistV1
  complete), every ledger row carries exact evidence, confirmation
  re-verify green, honesty notes recorded.
```

---

## After Phase 6

All six phases DONE means the engine is integratable: safe to point at a real client's data, speaking OneRoster and Microsoft Graph, with iSAMS/PowerSchool/Gibbon natives behind a registry, policy packs as reusable library assets, and the compliance artefacts procurement asks for.

**That is the end of Road-To-AlchemistV1.** The next move is commercial, not technical: run Discovery at a real school. If the engine productises from there, the shelved multi-tenant SaaS thread becomes the discussion — Phase 1's `school_id` work is what makes it possible.

### Done since last handoff
- **Road-To-AlchemistV1 Phase 6 complete (2026-10-23, K.1–K.7) — the compliance pack exists, and Road-To-AlchemistV1 is DONE.** These are the artefacts GEMS/Taaleem/PACE procurement asks for in meeting one. **The four documents** (in `docs/compliance/`): `DATA-RESIDENCY.md` — the position paper ("student data stays in the UAE", one instance per client group, `school_id` campus separation) plus the **retention table per data category** (K.6 policy); `DPIA-TEMPLATE.md` — data-protection impact assessment for high-risk processing of minors' data, lawful basis **contract / legal obligation, not consent from a minor**, with the OpenRouter external-inference question called out; `DPA-TEMPLATE.md` — data-processing agreement whose breach clause quotes the PDPL Art. 9 standard **"immediately upon becoming aware"**; `SUBPROCESSORS.md` — the register (Azure UAE North/Central as the deployment; the LLM drafting path with its minimised payload) answering the "does student text go to a model provider?" question honestly. **Kill switch + `/health` proof:** `MSOE_LLM_EXTERNAL=off` discards the API key at construction, `chat()` raises, and every call site degrades to `DeterministicLLM` — proven to beat a *configured* key (dummy `OPENROUTER_API_KEY` alongside `off` → `client.api_key` is None, `chat()` raises "external inference is disabled by configuration"); `/health` reports `llm.external_allowed` so a client can verify the switch is off. **`audit_log` answers "who approved this parent letter?":** `approval_decision`, `side_effect`, `auth_rejection` and `sync_run` events are durable in Postgres with reviewer, action, timestamp and outcome; secrets are **fingerprinted (sha256), never recorded**; a cross-school audit query is refused 403 *and the refusal is itself audited* — `GET /api/v1/audit/log?school_id=&since=`. **Retention sweep is dry-run-safe:** `retention_sweep.py` allowlists exactly `attendance_logs` + `audit_log`, dry-runs by default, needs `--apply`, refuses horizons under 30 days, and prints students before/after (543 → 543, never deleted). **Residency proof:** `residency_check.py` requires `MSOE_DATA_REGION` ∈ {`uae-north`, `uae-central`} and fails loudly if unset; `/health` carries `data_region` + the `residency` block — the residency answer is machine-checkable, not a promise in a document. Evidence: `IMPLEMENTATION-LEDGER.md` K.1–K.7 / QS.11.
- **Honesty notes (Phase 6):** (a) the four documents are **templates for review by qualified legal counsel — not legal advice**, and they **claim no certification** (the only "certified" strings are the explicit "none of those certifications has been obtained" disclaimer and the DPA's deletion-certificate clause); (b) **no real secret, student record or client/school name appears in any compliance document** (grep-verified); (c) the retention-probe fixture (`school_id 'retention_probe'`, student "Retention"/"Probe", SYNTHETIC) is **still in the database as evidence** — remove it manually if a clean demo DB is wanted; the sweep will never delete it (it is not in the sweep's allowlist and is not a student-row delete path); (d) **no real `OPENROUTER_API_KEY` on the Phase 6 host**, so the live external-LLM path was not exercised this session — the kill switch + DeterministicLLM fallback were proven instead; (e) **parent-notification dispatch remains a simulated relay — nothing is actually emailed**.
- **Road-To-AlchemistV1 Phase 5 complete (2026-10-23, P.1–P.6) — the policy pack is now a library asset.** This is the phase that makes engagement #3 cost ~30% of engagement #1. **P.1** `policy_vault/FORMAT.md` specifies exactly what `build_knowledge_graph.py` parses (H1 → `Policy.title`, `**Document Reference:**` in the first 20 lines → `Policy.reference`, `## N. Heading` → `Standard`, `### Strand X — Name` em-dash → `Competency`, body → `text_content` capped 4000) and `validate_policy_pack.py` reports every violation with `filename:line` — **0 violations on the three MSOE packs** (2 warnings, both legitimately naming the MoE pack's appendices). Its regexes are imported from `policy_pack`, so spec and parser cannot drift. **P.2** `RISK_POLICY_MAP` / `POLICY_DEPT_RULES` / `DEPARTMENTS` moved out of Python into `policy_vault/pack.config.yml`. This fixed a real divergence: `sis_adapter`'s hardcoded `CASE` mapped `CHRONIC_ATTENDANCE`→'Attendance' and `FEE_OVERDUE`→'Finance' while `RISK_POLICY_MAP` said 'Campus Health'/'Purpose' — the two modules disagreed about which clause governed a risk. Both now resolve through `policy_pack.link_risk_to_clause`. **P.3** packs are scoped by `school_id` (uniqueness widened to `(title, school_id, content_hash)` / `(code, school_id, policy_hash)`); loading is additive — never wipes. **P.4** content hash + `loaded_at` + `revision` on every Policy; a revised document **supersedes** rather than overwrites, so an inspector can see which clause version governed a past decision. **P.5** `policy_vault/README.md` is the authoring guide (Word/PDF → markdown conversion table, the four parser traps, checklist). **P.6** the 4-hop resolves for a school whose clause headings are NOT "Campus Health" — driven by its own `pack.config.yml`. Evidence + exact query output in `IMPLEMENTATION-LEDGER.md` P.1–P.6 / QS.10.
- **Honesty notes (Phase 5):** the fixture pack under `fixtures/policy_packs/meridian_british_school/` is SYNTHETIC and labelled as such — it exists to prove non-MSOE headings resolve, not to describe a real school. The markdown pack format was **specified, not rewritten** (it is the reuse asset). `test_api.py` is untouched and still 9/9. No assertion was weakened to make a check pass.
- **Road-To-AlchemistV1 Phase 4 complete (2026-10-23, N.1–N.5).** Three new native/fallback connectors behind the registry (`isams`, `powerschool`, `csv_drop`) plus a hardened Gibbon. **iSAMS** (`connectors/isams/`): OAuth2 client-credentials against `identity.isams.com`, resources students/staff/contacts/attendance/behaviour/academic/finance → canonical five; attendance collapses period rows to the `(student_id, date)` day key (severity absent > excused > late > present), finance is idempotent on `(sid, invoice)`, **behaviour is meta-only — no canonical entity, rows never fabricated**. **PowerSchool** (`connectors/powerschool/`): thin config profile over `connectors/oneroster/` (`{PS}/ims/oneroster/v1p2` roster root, `{PS}/oauth/access_token`, `POWERSCHOOL_*` env names) with bespoke code only for what OneRoster misses — `/ws/v1/attendance` + `/ws/v1/fees` on the same OAuth token. **Gibbon polish**: incremental sync (monotonic person-ID insert watermark + status sweep + timestamp watermarks) and tombstones (status change → full row `is_active=false`; vanished id → flag-only UPDATE that preserves the name). `python gibbon_bridge.py` remains the shim. **CSV drop** (`connectors/csv_drop/`): documented column contract, file/row rejections name file+line+column, sha256 per-file idempotent re-import, and a `school_id` column in a drop file is ignored + logged so stamping cannot be bypassed. **N.5:** `GET /api/v1/sis/status` returns per connector per school_id `last_success_at, last_error, rows_in_last_sync, watermark, mode`. Evidence + the Gibbon watermark-monotonicity bug fix in `IMPLEMENTATION-LEDGER.md` N.1–N.5 / QS.9.
- **Honesty record (Phase 4 live checks):** iSAMS live-sandbox check **BLOCKED — "partner-gated developer portal, no sandbox credentials"** (developer.isams.com is partner-gated; the connector is built to the documented contract and tested against SYNTHETIC fixtures only — no live result is claimed). PowerSchool live check **NOT RUN** — no sandbox credentials in this environment. Tier 1 connectors unbroken (test_oneroster 63/63, test_graph_education 43/43, smoke 9/9).
- **Road-To-AlchemistV1 Phase 3 complete (2026-10-23, G.1–G.6).** `connectors/graph_education/` — Microsoft Graph v1.0 roster consumer (read-only): `/education/schools`, `/education/users`, `/education/classes`, `/education/classes/{id}/members`, OAuth2 app-only against the login host with scope `https://graph.microsoft.com/.default`. **Least privilege is architectural:** the package has one Graph call site (`_graph_get`) and it only ever sends GET; the only POST in the package is the token exchange at `login.microsoftonline.com`. Asserted on every recorded request in tests + grep proof. Permissions documented in `INTEGRATION-PLAN.md` §3.1 (`EducationRoster.Read.All` required, `EducationAssignments.Read.All` optional). Delta tokens per school_id persist in Postgres `connector_state` — a second pull is incremental. 401 → connector UNHEALTHY, services stay up. Evidence in `IMPLEMENTATION-LEDGER.md` G.1–G.6.
- **Principal-facing SDS note (`INTEGRATION-PLAN.md` §3.2)** — where the school already runs School Data Sync, the roster is already in Entra ID and the connector never touches the SIS. Written for a non-technical principal AND a group IT lead. Google Classroom noted as future work only (§3.3).
- **Road-To-AlchemistV1 Phase 2 complete (2026-10-23, C.1–C.7).** Connector registry (`connectors/`) with 5 dialects; field maps moved out of `sis_adapter` into per-connector modules; Gibbon is connector #0 (`gibbon_bridge.py` is now a thin shim — RUNBOOK commands unchanged); OneRoster 1.2 CSV consumer (bulk set with joins: demographics → dob/sex, enrolment→class→course → grade fallback, orgs → school_name) and REST consumer (OAuth2 client-credentials, Link + limit/offset paging, `since` delta, timeout/retry, degrade to UNHEALTHY). Both `/api/v1/sis/sync` and `/api/v1/sis/webhook` accept `oneroster-csv`/`oneroster-rest`; `/api/v1/sis/status` lists every connector with health. `test_oneroster.py` 63/63, smoke 9/9. Evidence in `IMPLEMENTATION-LEDGER.md` C.1–C.7.
- **Client-manifest schema (C.2)** documented in `client.manifest.example.yml` and validated by `connectors/manifest.py` — manifests hold env-var NAMES, never values; secret-shaped literals are rejected by the validator.
- **Honesty note baked into tests:** OneRoster 1.2 *rostering* carries no attendance/fees entities — the connector emits none rather than fabricating rows (asserted). Grade/term handling is calendar-agnostic (UAE Aug–Jun and Sep–Jul schools; no US-season rewrites). Graph roster has no national id — `emirates_id` stays NULL rather than fabricated.
- **OpenRouter key is live.** `OPENROUTER_API_KEY` is set in `./.env` (user-filled; value never read or logged). `/health` → `llm.provider=openrouter`, `api_key_configured: true`, `last_provider: openrouter`, `last_error: null`. `.gitignore` excludes `.env`.
- **Compliance-audit latency fixed** (see `IMPLEMENTATION-LEDGER.md` T5.7): parent-notification drafts now run concurrently under a wall-clock budget, falling back to `DeterministicLLM` for stragglers. Full 50-student audit went from a 60s timeout (~220s serial) to **28s**, all 55 bodies populated.
- **Neo4j `ORDER BY` warning fixed** (T5.8): `s.student_name` → `s.full_name` (2 query sites).
- **Runbook correction:** `docker compose up -d --force-recreate api` — the compose *service* is `api`, not `msoe_api`.
- **Closing sales demo built** (`DEMO.1`, `DEMO.2`). Live run captured to `demo_data.json` and rendered as `ALCHEMIST-PROOF-BOOK.html` — the school-shaped walkthrough where the citation chain beats a stock n8n workflow. It shows: the 4-hop `verified_path` (Student → RiskFlag → Standard → Policy) with Neo4j node IDs; `GROUNDED` executive answer vs `REFUSED_INSUFFICIENT_DATA`; guardrail **55 staged / 0 executed** with a real `ACT-00001` ticket and drafted parent letter; ROI ledger 14.63 h / AED 1,771.67 with assumptions + method tables exposed. All figures are from that run, labelled as synthetic demo data. Open the file in a browser to view.

---

## 6. Machine-specific landmines (do not regress)

| Issue | Workaround |
|-------|------------|
| Host `urllib`/`http.client` → **WinError 10048** (Windows) | Run tests from Docker network with `BASE=http://msoe_api:8000` |
| PG `127.0.0.1:5432` handshake hangs on Windows | Use `localhost` or connect via Docker network (`msoe_postgres`) |
| Docker **build context** on OneDrive → Access denied | Build from `%TEMP%\msoe_build` and `%TEMP\gibbon_build` |
| `docker cp` from OneDrive path → Access denied | Copy to `%TEMP` first, then `docker cp` |
| Debian mirror unreachable during Docker build | Slim PHP image: no apt; busybox unzip for Composer |
| PowerShell mangles `$` / `<` / quotes | Prefer script files + `docker exec`; not inline python -c |
| Host curl to :8000 may deliver headers but hang on body | Prefer Docker network for API clients |
| **Linux host has podman, not docker** (no compose provider) | Use `podman` commands directly: `podman network create msoe_default`, `podman run --network msoe_default …`. Images: `postgres:16`, `neo4j:community`, build `msoe-api:latest` from `Dockerfile` |
| **SELinux blocks volume mounts** → `can't open file … Permission denied` | Mount with `:z` relabel: `-v $PWD/test_api.py:/tmp/test_api.py:ro,z` |
| **Fresh instance has empty DB/graph** → smoke fails on data, not code | One-time bootstrap: synthetic seed through `sis_adapter.sync_*` (never `seed_sis.py` — it DROPs the Phase-1 schema) + `build_knowledge_graph.py` (MERGE). Grade 8 must appear in the chronic-attendance + fee-overdue cohorts or the executive query correctly returns `REFUSED` |
| `gibbon/` source dir may be empty in a fresh checkout | Gibbon containers can't run; the connector reports `UNHEALTHY` — MSOE core is unaffected |

---

## 7. Verified evidence (this session — Phases 2–3, 2026-10-23)

```
PHASE 3 VERIFY (2026-10-23)

VERIFY 1  docker exec msoe_api python -m py_compile main.py sis_adapter.py
          connectors/*.py connectors/*/*.py  → PY_COMPILE OK

VERIFY 2  test_graph_education.py (container network, BASE=http://msoe_api:8000)
          → SUMMARY: 43/43 passed
          Part 1 canonical mapping + school_id stamping (class-membership hints)
          Part 2 delta-token persistence (memory + Postgres), paging, 401,
                least privilege (every Graph call GET-asserted)
          Part 3 live: sis/status lists graph_education; graph_education
                sync dialect accepted; delta token survives a store restart

VERIFY 3  test_oneroster.py → SUMMARY: 63/63 passed   (no regression)

VERIFY 4  test_api.py → SUMMARY: 9/9 passed   suite_elapsed=1.9s

VERIFY 5  GET /api/v1/sis/status → connectors include:
          graph_education {status: NOT_CONFIGURED,
                           missing_env: [MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID,
                                         MS_GRAPH_CLIENT_SECRET],
                           state_store: PgStateStore,
                           least_privilege: EducationRoster.Read.All (application)}

VERIFY 6  grep -ri "graph.microsoft.com" connectors/graph_education/
          → GRAPH_BASE / GRAPH_SCOPE constants + docstrings only.
          All transport call sites:
            POST → https://login.microsoftonline.com/.../token   (OAuth2 token)
            GET  → GRAPH_BASE  (the single _graph_get call site)
          No PATCH/POST/DELETE to Graph anywhere in the package.
```

```
PHASE 2 VERIFY (2026-10-23)

test_oneroster.py → SUMMARY: 63/63 passed
test_api.py       → SUMMARY: 9/9 passed   suite_elapsed=1.6s
psql (school_id='oneroster_fixture_school'):
  students 6 · staff 2 · attendance_logs 2
  usr-1001 present / usr-1002 absent on 2026-10-01
```

Bootstrap on this fresh host (synthetic data, additive): 120 students /
5280 attendance / 24 fees / 12 staff via `sis_adapter.sync_*`, then
`build_knowledge_graph.py` MERGE → 50 Student / 3 RiskFlag / 74 HAS_RISK,
4-hop paths confirmed. `seed_sis.py` was deliberately NOT run (it DROPs the
Phase-1 schema).

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

## 2026-10-24 — F1–F6 functional-fix pass (FX.1–FX.6) — HANDOFF

**State:** Road-To-AlchemistV1 Phases 1–6 DONE (all suites green at handover:
test_policy_pack 44/44, test_oneroster 63/63, test_graph_education 43/43,
test_isams 63/63, test_powerschool 43/43, test_csv_drop 48/48, test_api 9/9).
Working tree uncommitted — normal here. **F1–F6 pass: FX.1/FX.2/FX.3/FX.5/FX.6
DONE and green with pre-fix evidence; FX.4 behaviour fixed, 2 snapshot asserts
left (test zip artefact — diagnosed). Read `IMPLEMENTATION-LEDGER.md` rows
FX.4 + QS.12 and Improvement.md "Session progress — 2026-10-24" FIRST — they
carry the exact remaining steps. Everything below is historical (written when
the pass had not started).**

### Done since last handoff (2026-10-24 session)

- Read Improvement.md Part 2 in full (it carries the rationale). Defect sites
  located: `main.py:479-518` (pg_risk_students, COALESCE at line 500),
  `main.py:1338-1448` (substitute_dispatch: teacher lookup 1347-1354,
  candidate query 1359-1372, `recommended = candidates[0]` at 1389),
  `main.py:1218` (utcnow), `retention_sweep.py:64-67`, staff DDL
  `schema.sql:108-131`.
- **Pre-fix DB baseline captured** (use as the final-verify comparison):
  `SELECT COUNT(*) FROM students` → **543**, staff → **61**,
  attendance_logs → **5286**. staff has 10 columns, NO is_agency/day_rate yet.
- Zero-attendance survey (shapes FX.4): **418/543** students have zero
  attendance rows, **0** of them vax/fee-flagged; **52** students match the
  compliance WHERE clause today.
- Stack brought up on this Linux/podman host (msoe_postgres, msoe_neo4j,
  msoe_api all up). API image **built + running from the still-defective
  tree** (`msoe-api:latest`, image id `9fdfc3ed8f99`) — this live build is
  the PRE-FIX baseline. **Do not rebuild before the pre-fix evidence run.**
- Progress notes mirrored in Improvement.md ("Session progress — 2026-10-24").

### How to continue (next session — execute in this order)

The task is the F1–F6 functional-fix pass ONLY (ledger IDs **FX.1–FX.6**;
NOT F.* — that prefix is Phase 1 Foundations). CORRECTNESS pass: do NOT
implement Part 3 services (S1–S10). The only Part-3 adjacency allowed is
FX.3's ranking ladder (it IS the bug fix). No analytics, no reports, no ROI
units, no "savings"/"saved"/"cheaper"/"AED" language in code, comments or API
responses — FX.3 is ordering only.

Files to touch: `main.py`, `schema.sql`, `retention_sweep.py`, (maybe
`sis_adapter.py` only if staff columns must be written — prefer not), plus
NEW `test_fixes.py`. `test_api.py` is UNTOUCHED and must stay 9/9.

**STEP 0 — write test_fixes.py FIRST and run it ONCE pre-fix.** SYNTHETIC
fixtures, labelled like the other suites (TEST-/SYNTHETIC- names,
`@synthetic.example` emails), in a dedicated `fx_fixture_school` so the
seeded default cohort is untouched. Run from the container network:

    podman run --rm --network msoe_default -v $PWD:/app:z \
      -e NEO4J_URI=bolt://msoe_neo4j:7687 -e NEO4J_USER=neo4j \
      -e NEO4J_PASSWORD=msoe_graph_2026 -e PGHOST=msoe_postgres \
      -e PGPORT=5432 -e PGDATABASE=msoe_db -e PGUSER=msoe_admin \
      -e PGPASSWORD=msoe_secure_2026 \
      -e BASE=http://msoe_api:8000 \
      -e MSOE_API_KEYS=default:demo-api-key-2026 \
      msoe-api:latest python -u test_fixes.py

(MSOOE_API_KEYS is REQUIRED or the live parts env-refuse.) Save the failing
output — every new assertion must fail before its fix and pass after. That
pre-fix run is the evidence the tests are real.

**The six fixes (full designs are in Improvement.md Part 2 + the original
task brief; summary here):**

- **FX.1** teacher match (`main.py:1349` area): normalise both sides (strip,
  casefold, collapse whitespace); exact match on normalised full_name first;
  else substring match ONLY if it returns exactly one row; 0 matches → 404
  naming the submitted name; ≥2 → 409 listing candidate names. Never
  `LIMIT 1` on a fuzzy match. Escape `%`, `_`, `\` in any LIKE/ILIKE input.
  Test: exact hit works; "Johnny" with both "John Smith" and "Johnny Cash"
  on the roll → 409; a name with a literal `%` does not match the world.
- **FX.2** subject vocabulary (`main.py:1366` area): one module-level
  `SUBJECT_ALIASES` (normalised variant → canonical token) +
  `SUBJECT_GROUPS` (canonical → shared group) in main.py, explicitly
  enumerated keys only — no fuzzy matching, no LLM, no config file.
  Precedence: canonical equality → alias → group. Response reports
  `match_rule` per candidate ("exact"|"alias"|"group"). Test: "Maths" finds
  a teacher with ["Mathematics"] and vice versa; "Art" vs ["Mathematics"]
  still yields NO candidates (honest empty — do NOT over-merge).
- **FX.3** cost-aware recommendation (`main.py:1389` area): additive schema
  only — `ALTER TABLE staff ADD COLUMN IF NOT EXISTS is_agency BOOLEAN NOT
  NULL DEFAULT false;` and `... day_rate NUMERIC(10,2);` in schema.sql
  (source of truth) + runtime ensure-schema via the existing
  ADD COLUMN IF NOT EXISTS idiom. Ranking ladder, deterministic: internal
  (is_agency=false) before agency; then lower day_rate (NULL last within
  class); then larger remaining capacity; then full_name ASC. Response gains
  per-candidate `cover_class` ("internal"|"agency") + `day_rate`, and
  `recommendation_reason` (e.g. "internal cover, capacity 4, no day rate").
  Test: agency with huge capacity LOSES to internal with less capacity;
  ordering deterministic across runs; students and every other table
  untouched.
- **FX.4** zero-attendance blind spot (`main.py:500`): return
  `attendance_records` (count) alongside the rate; attendance_rate becomes
  float|None (None = no data, NOT 100.0). Zero-row students join the review
  as risk_type **DATA_GAP**. Locked-down notification rule: DATA_GAP is an
  internal ops flag only — it MAY appear in the compliance `violations` list
  (audit visibility) but the notification drafting loop SKIPS it; no
  SEND_PARENT_NOTIFICATION is ever staged for a data-gap-only student. Order
  DATA_GAP cases AFTER real risk cases in the response. Existing risk types
  keep their exact current behaviour. Test: zero-attendance synthetic student
  → visible with DATA_GAP and NO parent notification staged; a student with
  real attendance rows behaves exactly as before.
  **Two session-locked design notes:** (1) `pg_risk_students` LIMITs to
  max_students (default 50) — with 418 data-gap students in the WHERE clause,
  ORDER BY must put real-risk rows first so the limit cannot crowd out
  vax/attendance/fee findings. (2) "byte-identical to pre-fix" for the seeded
  cohort means the pre-existing findings + their notification set are
  identical — generated_at (FX.5) and the additive attendance_records /
  DATA_GAP fields (FX.4) are expected deltas. Run the same compliance request
  pre/post and diff; normalise those three deltas; record the exact diff in
  the ledger. If an existing expectation changes, FIX THE FIXTURE, never the
  assertion.
- **FX.5** naive timestamps (`main.py:1218`): `datetime.now(timezone.utc)
  .isoformat()` (produces +00:00). Grep main.py for any remaining utcnow /
  naive datetime.now() in response builders and fix those too. Test:
  generated_at matches `^\d{4}-\d{2}-\d{2}T.*\+00:00$` on touched endpoints.
- **FX.6** two clocks (`retention_sweep.py:66-67`): one
  `now = datetime.now(timezone.utc)`; date cutoff from `now.date()`;
  timestamp cutoff from `now`. Both still printed in dry-run output. Test:
  assert the pure cutoff function (prefer this over running --apply at all);
  dry-run still deletes nothing; `--apply --days 1` still refused (exit 2,
  min 30d).

**Fixture/cleanup nuance (decided this session):** the "never delete student
rows" rule governs production/seed data and the retention sweep. test_fixes.py
may clean up ONLY the exact SYNTHETIC rows it inserted itself (dedicated
`fx_fixture_school`, matched by the exact IDs it created) — that is the same
self-cleanup idiom as test_policy_pack._clear_school. The end-of-session
invariant is: default-school student rows and the 543-count baseline for
non-fixture data unchanged; never a blanket DELETE FROM students.

**After the fixes land:**
1. Rebuild + recreate (podman-compose up --force-recreate HANGS here — never
   use it):
       podman build -t msoe-api:latest .
       podman rm -f msoe_api && podman-compose up -d --no-deps api
2. `podman exec msoe_api python -m py_compile main.py retention_sweep.py
   test_fixes.py` → PY_COMPILE OK.
3. Post-fix test_fixes.py → SUMMARY: N/N passed (the run command is above).
4. Full regression with exact counts: test_policy_pack 44/44,
   test_oneroster 63/63, test_graph_education 43/43, test_isams 63/63,
   test_powerschool 43/43, test_csv_drop 48/48, test_api 9/9.
5. psql via `podman exec msoe_postgres psql -U msoe_admin -d msoe_db -c "…"`:
   students count unchanged from the 543 baseline (never delete student
   rows); staff gained ONLY the two new columns (is_agency, day_rate).
6. One curl proof of FX.1 live: ambiguous teacher name → 409 with candidate
   names in the body.

**Bookkeeping before session end:** IMPLEMENTATION-LEDGER.md rows FX.1–FX.6 +
QS.12 (this verify sweep), all DONE, evidence = the exact commands/outputs
INCLUDING the pre-fix failure output; update the Coverage summary. handoff.md:
one "Done since last handoff" bullet — the six fixes, the DATA_GAP-is-internal
rule, the pre-fix-failure evidence note. Do not rewrite the State: line's
Phase status (Road-To-AlchemistV1 remains complete); extend it with one clause
noting F1–F6 fixed.

**DO NOT:** implement S1–S10 or any Part 3/Part 4 work; weaken, skip, delete
or reorder any existing assertion (if a fix seems to require changing an
existing expectation, the fix is wrong — change the fix or the fixture);
delete student rows under any circumstance (beyond a suite's own synthetic
fixture rows as scoped above); say "saved"/"cheaper"/"avoided"/"AED" in code,
comments or API responses; touch the connector framework, policy packs, or
the compliance docs; commit/push/deploy (commit only if the user asks).

**Definition of done:** all six fixes in with the designs above;
test_fixes.py green with its pre-fix failure evidence attached; full
regression green at exact counts; students count unchanged; ledger
FX.1–FX.6 + QS.12 carry exact commands and outputs; handoff bullet written.

---

**End of handoff.** Next engineer: start at §0, then pick §5 next tasks — or,
for the active work, the "2026-10-24 — F1–F6 functional-fix pass" section
immediately above.
