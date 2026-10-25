# Improvement.md — Defects, Hardening & Growth Review

**Reviewed:** 2026-10-23 · **Scope:** the full MSOE/Alchemist platform as it stands after Road-To-AlchemistV1 (Phase 6 close-out) — `main.py`, `llm_client.py`, `audit_log.py`, `retention_sweep.py`, `residency_check.py`, `sis_adapter.py`, `build_knowledge_graph.py`, `policy_pack.py`, `connectors/`, `schema.sql`, `docker-compose.yml`, `Dockerfile`, `requirements.txt`, compliance docs vs code.

**Method:** static review of auth, tenancy, injection, secret handling, LLM data flows, schema, deployment config — cross-checked against what the compliance documents claim the platform does. Then a second pass for functional correctness and a product pass over where the platform leaves money unclaimed (grounded in `roi_model.py`'s honesty rules and rate card).

**In three parts:** **Part 1** — security & hardening findings (1–15). **Part 2** — functional bugs (F1–F6). **Part 3** — new services ranked by money saved (S1–S10), with re-doable math. **Part 4** — the packaging moves that make the purchase a no-brainer.

**Context that matters for severity:** this is a local demo/mock stack with synthetic data. The hardening items below are *not* bugs in the demo — they are the gaps between this stack and the posture the compliance docs describe ("campus A's data is filtered out of campus B's views **by the platform, not by convention**", residency "fails loudly"). A real client engagement inherits this code. Severity is rated against that bar.

---

## Summary

| # | Finding | Severity | Kind |
|---|---------|----------|------|
| 1 | Cross-school data isolation not enforced on service endpoints (read side) | **Critical** | Security |
| 2 | Cross-tenant write via `school_id` override in SIS sync/webhook | **Critical** | Security |
| 3 | Approvals: no school scoping — any key can approve/reject and enumerate all schools' parent notifications | **Critical** | Security |
| 4 | Default credentials in source + Postgres/Neo4j ports published on all interfaces | **High** | Security |
| 5 | CORS `allow_origins=["*"]` with `allow_credentials=True` | **High** | Security |
| 6 | Approver identity is free text — weak non-repudiation in the audit story | **Medium** | Security |
| 7 | Residency is reported, not enforced (docs say "fail loudly") | **Medium** | Security / doc drift |
| 8 | Unbounded request text + unvalidated `school_id` (DoS / LLM cost / key hygiene) | **Medium** | Robustness |
| 9 | No rate limiting on API-key auth | **Medium** | Security |
| 10 | LLM grounding check is substring-based; summary payload silently truncated | **Medium** | Correctness / LLM safety |
| 11 | `test_api.py` has no negative auth tests (401/403/cross-school uncovered) | **Medium** | Test gap |
| 12 | Dependencies unpinned; container runs as root; no TLS story | **Low** | Supply chain / hardening |
| 13 | `print()` logging, no request IDs; exception strings can reach clients | **Low** | Observability |
| 14 | Audit log is retention-swept at 365 days (accountability record deleted) | **Low** | Policy tension |
| 15 | Fragile patterns: f-string Cypher by label; silent env-parse crashes; key-parse quirks | **Low** | Code hygiene |

**What is already solid — do not regress it:** constant-time key comparison + HMAC webhook signatures; audit fingerprints never contain secret values; SQL/Cypher parameterised in all hot paths; retention sweep allowlist + student-count invariant; schema CHECKs/FKs/composite per-school uniques; data minimisation at the parent-draft call site; `MSOE_LLM_EXTERNAL` kill switch that beats a configured key; deterministic fallback; staged-approval guardrail with simulated parent dispatch. Details at the end.

**Beyond defects:** Part 2 lists 6 functional bugs (wrong-teacher matching, zero-attendance blind spot, clock inconsistencies). Part 3 ranks 10 new services by money — the addressable case for a 2,000-student UAE school is **AED 750k–1.7M/yr**, of which **AED 45k–135k is hard cash** (agency invoices not raised) and the rest is measured time and challengeable revenue. Part 4 is how to package it so the sale is a no-brainer.

---

## Critical

### 1. Cross-school isolation is not enforced on the data endpoints (read side)

The platform sells per-school isolation (`school_id` tagging, "campus A's data is filtered out of campus B's views by the platform"). The middleware authenticates a key to a school and stores it in `request.state.school_id` ([main.py:195](main.py#L195)), but the service handlers **take `school_id` from the request body and query it without ever comparing it to the caller**:

- [main.py:1063](main.py#L1063) — `audit_compliance`: `school_id = req.school_id or "default"`
- [main.py:1275](main.py#L1275) — `curriculum_harmonize`
- [main.py:1342](main.py#L1342) — `substitute_dispatch`
- [main.py:1477](main.py#L1477) — `executive_query`

A key scoped to `school_a` can pass `"school_id": "school_b"` and read school B's risk-flagged students (names, grades, parent names, attendance/fee risk — see the `pg_risk_students` SELECT at [main.py:496-502](main.py#L496-L502)), cohort counts, substitute staffing and graph evidence. Several of these handlers don't even take a `Request` argument, so they *cannot* check the caller today.

**The correct pattern already exists in this codebase** — `GET /api/v1/audit/log` refuses a cross-school query with 403 and audits the attempt ([main.py:1744-1757](main.py#L1744-L1757)). It is simply not applied elsewhere.

**Fix:** resolve `school_id` server-side everywhere: `school_id = request.state.school_id`, and if a body/query `school_id` is present and differs → 403 + `audit_log.record(..., outcome="cross_school_denied")`, exactly like the audit endpoint. Treat body `school_id` as a *filter hint* only, never as an authority. Add the same check to `roi_report`'s `engagement_id` scoping while there.

### 2. Cross-tenant write via `school_id` override in the SIS sync endpoints

The write side is worse — the payload's `school_id` **overrides** the authenticated school:

- [main.py:1676](main.py#L1676) — `sis_webhook`: `sid = payload.school_id or school or "default"`
- [main.py:1704](main.py#L1704) — `sis_batch_sync`: `body.setdefault("school_id", payload.school_id or "default")`

A webhook signed with school A's secret (signature verification is solid — [main.py:176-190](main.py#L176-L190)) can still upsert students, attendance and fees **into school B's namespace**, poisoning B's compliance cohorts and knowledge graph. The HMAC proves *which key* signed it; the code then lets the body choose *whose data it becomes*.

**Fix:** `sid = school` (from the key), always. If `payload.school_id` is present and ≠ `school`, reject with 403 + audit. Signed payloads that disagree with their signing key are exactly what the signature is there to catch.

### 3. Approvals: no school scoping, unfiltered pending list, forgeable reviewer

Three compounding issues in the human-approval workflow:

1. **`GET /api/v1/approvals/pending` returns every school's pending actions** ([main.py:803-811](main.py#L803-L811)) — `approval_store.pending()` has no `WHERE school_id = …` ([main.py:370-383](main.py#L370-L383)). Each row's `payload` includes the staged parent-notification `to` (parent email), student id and drafted body ([main.py:1135-1141](main.py#L1135-L1141)). Any valid API key from any school can harvest every other school's parent contact details.
2. **`POST /api/v1/approvals/{id}/execute` does not check that the action belongs to the caller's school** ([main.py:864-877](main.py#L864-L877)) — it reads `caller_school` and `action_school` and *records both in the audit detail* but never compares them. School A's key can approve or reject school B's staged parent letter.
3. **`reviewer` is client-supplied free text** with default `"principal"` ([main.py:731](main.py#L731)), and becomes the audit `actor` ([main.py:877](main.py#L877)). See finding 6.

**Fix:** filter `pending()`/`get()` by `school_id = request.state.school_id` (and 404 (not 403 — don't leak existence) on cross-school `action_id` lookups). Bind reviewer identity to the API key (a `MSOE_API_KEYS` value → `school_id:secret:role` or a separate principal map), never to body text.

---

## High

### 4. Default credentials in source, and Postgres/Neo4j published on all interfaces

- Hardcoded fallback passwords in **10+ source files** — e.g. [main.py:52](main.py#L52) `NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "msoe_graph_2026")`, [main.py:68](main.py#L68) `PGPASSWORD … "msoe_secure_2026"`; same fallbacks in `audit_log.py:37`, `retention_sweep.py:38`, `sis_adapter.py:54,58`, `build_knowledge_graph.py:54,61`, `connectors/state.py:37`, `roi_model.py:52`. Also [seed_sis.py:30](seed_sis.py#L30) and [probe_backends.py:7,20](probe_backends.py#L7) have them **with no env fallback at all**.
- [docker-compose.yml:12-13](docker-compose.yml#L12-L13) publishes Postgres as `"5432:5432"` and [docker-compose.yml:31-33](docker-compose.yml#L31-L33) publishes Neo4j browser + bolt (`7474`, `7687`) — **on every host interface**, unlike `gibbon_db` which correctly binds `127.0.0.1:3306` ([docker-compose.yml:109-110](docker-compose.yml#L109-L110)). Combined with the default passwords and `MSOE_API_KEYS` defaulting to `default:demo-api-key-2026` ([docker-compose.yml:65](docker-compose.yml#L65)), a copy of this compose file run on a reachable host exposes the whole student database to the network.

Fine for a local mock. Dangerous if this compose file is the starting point for a client deployment — which the commercial plan says it is.

**Fix:**
- Fail fast when the environment is production-like and a default credential is in play (same pattern `residency_check.py` uses — refuse loudly). At minimum: no password fallbacks in source; read env or crash.
- Bind DB ports to `127.0.0.1:` in compose (or drop them — the API reaches Postgres/Neo4j over the container network anyway; the host ports are only needed for local `psql`/browser debugging).
- Keep demo credentials in an `.env.example` / docs only. `.gitignore` already excludes `.env` — good.

### 5. Wide-open CORS with credentials

[main.py:77-82](main.py#L77-L82):

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Any origin may call the API from a browser with full method/header freedom. Auth here is header-based (X-API-Key) rather than cookie-based so classic CSRF is limited, but `*` + `allow_credentials=True` is a configuration red flag, and browsers disagree about how to treat it (reflecting the request origin with credentials is the dangerous outcome in some versions).

**Fix:** set `allow_origins` to the explicit UI origin(s) for a deployment (the Gibbon dashboard iframe origin, the proof-book host), `allow_credentials=False` unless cookies become the auth mechanism, and allow only the methods/headers actually used (`X-API-Key`, `X-MSOE-Signature`, `Content-Type`).

---

## Medium

### 6. Approver identity is free text — the audit answer is only as good as the claim

`GET /api/v1/audit/log` exists to answer "who approved this parent letter?" — but `actor` is whatever string the caller typed ([main.py:731](main.py#L731), [main.py:877](main.py#L877)). Any key holder can approve as `"Principal"`, `"Head of Safeguarding"` or anyone else. In the Phase 6 evidence the recorded reviewer was literally `"Principal (demo reviewer)"` — a self-assertion.

**Fix:** derive reviewer from the credential (per-key role/owner in the key map, or a signed identity header from a reverse proxy), and record the free-text field separately as `claimed_by` if it's still wanted. For a client-facing safeguarding story this is the difference between an audit log and a *defensible* audit log.

### 7. Residency is reported, not enforced — doc/code drift

`residency_check.py` fails loudly when run standalone (good). But in the API, `check_region()` is only consulted by `/health` ([main.py:995-1003](main.py#L995-L1003)): an invalid/unset region makes health report `degraded` and prints one line to stdout — **the API keeps serving all endpoints with student data**. `DATA-RESIDENCY.md` sells the residency answer as an absolute; a client whose contract says "no processing outside uae-north/uae-central" currently gets a dashboard signal, not an enforcement point.

**Fix:** at startup, if `residency_check_region()["allowed"]` is false, either refuse to start or gate every `/api/v1/*` route behind it (503 + audit), with an explicit `MSOE_RESIDENCY_ENFORCE=warn|block` switch if a lab mode is needed. One line of startup enforcement is worth more than a doc paragraph.

### 8. Unbounded request text and unvalidated `school_id`

Pydantic models bound numbers (`max_students` ≤ 500, `grade_level` 1–12) but the big strings only have `min_length` — no `max_length` on `lesson_plan`, `query`, `note`, `teacher_name`, `reason` ([main.py:633-733](main.py#L633-L733)). `school_id` has no pattern or length limit at all.

Impact: (a) multi-megabyte `lesson_plan`/`query` is copied into LLM prompts → cost/latency DoS on an unauthenticated-by-tenant key; (b) `school_id` is stamped into DB rows and Neo4j MERGE keys unbounded — a noisy or hostile value pollutes every composite key; (c) `teacher_name` flows into a `LIKE '%…%'` (parameterised — no injection, but unbounded scan).

**Fix:** `max_length` on every text field (e.g. `lesson_plan` ≤ 20k, `query` ≤ 2k, `note` ≤ 2k); `school_id: constr(pattern=r"^[A-Za-z0-9_-]{1,64}$")`; reject oversized bodies at the middleware.

### 9. No rate limiting on API-key auth

Keys are compared in constant time (good) but there is no throttling: unlimited 401 attempts against `/api/v1/*`. The audit log *records* rejections (good for forensics) but nothing slows an attacker guessing keys — and weak keys like the default `demo-api-key-2026` are one brute-force away.

**Fix:** per-IP and per-key attempt throttling (slowapi / a token bucket in the middleware), plus a lockout-and-audit on N consecutive `invalid_api_key` outcomes. Enforcement goes up for free if key strength is also enforced at parse time (finding 15).

### 10. LLM safety: weak grounding check, silent payload truncation

- `_validate_grounded_answer` ([llm_client.py:382-399](llm_client.py#L382-L399)) accepts an answer if **any one** evidence token (length > 3) appears as a substring. A prompt-injected or hallucinated answer that mentions the student's name and fabricates everything else passes. The per-call-site checks (e.g. name-or-clause match at [llm_client.py:454-458](llm_client.py#L454-L458)) are stronger but share the weakness.
- `summarize_violations` truncates the JSON payload at 6000 chars ([llm_client.py:422](llm_client.py#L422)) **without counting what was dropped** — a full-cohort summary will silently undercount, while the deterministic fallback would have been accurate.

**Fix:** for anything parent-facing, keep preferring `DeterministicLLM` (the current default without a key is right). If external drafting is enabled: validate on *structured* claims (all cited clause numbers present, no numbers absent from evidence), not substrings; send aggregates/counts for the summary instead of raw rows, or count and disclose "summarised N of M". Treat the LLM as untrusted input on the way back in — the staging guardrail already does the right thing downstream.

### 11. Test gap: the auth surface is untested negatively

`test_api.py` sends a valid `X-API-Key` on every request and asserts happy paths only (the only header use is at [test_api.py:52](test_api.py#L52)). None of the following are covered by the suite: 401 missing key, 401 invalid key, webhook signature missing/bad, cross-school 403 on `/api/v1/audit/log`, and — once fixed — cross-school 403s on the service endpoints. The Phase 6 evidence proved some of these by hand; hand proof isn't regression proof.

**Fix:** add a Part to `test_api.py` (or a `test_auth.py`) with negative cases for every finding in this document's Critical section. These tests should *fail before* the fixes land and pass after — that's the point.

---

## Low / hardening

### 12. Supply chain and container posture

- [requirements.txt](requirements.txt) is lower-bounds only (`fastapi>=0.115.0`, …) — no pins, no lockfile, no hashes. Rebuilds drift; a compromised newer release installs automatically. **Fix:** pin exact versions (`pip freeze` → `requirements.lock`) and rebuild deliberately.
- [Dockerfile](Dockerfile) runs as root (no `USER` directive) and copies the whole repo context including `fixtures/`. **Fix:** add a non-root user, copy only what the runtime needs (tests/fixtures belong in the test image, not the API image).
- **No TLS anywhere** in compose — API on plain `:8000`. Fine behind a local mock; a client deployment needs a reverse proxy (Caddy/nginx) with TLS and real `X-Forwarded-For` handling before any student data goes near it. `/docs`, `/redoc`, `/openapi.json` are public by design ([main.py:89](main.py#L89)) — consider disabling them outside lab mode.
- `gibbon_db` runs `--default-authentication-plugin=mysql_native_password` ([docker-compose.yml:113](docker-compose.yml#L113)) — legacy auth. It exists for the Gibbon PHP stack; isolate the MySQL container hard (it already binds localhost — keep it that way).

### 13. Observability

No `logging` module anywhere — `print()` to stdout (7 in `main.py`, 12 in `retention_sweep.py`, and `traceback.print_exc()` at [main.py:789](main.py#L789)). No request IDs, so an audit row can't be tied to an HTTP request in an incident. Exception strings are echoed to clients in places — e.g. `_simulate_execution` returns `"DB write skipped: {exc}"` ([main.py:841](main.py#L841)) and the sync handlers put `f"{exc.__class__.__name__}: {exc}"` into audit detail; psycopg2 messages can carry host/user/schema names.

**Fix:** stdlib `logging` with JSON lines (level, ts, request_id, school_id, path); propagate a `X-Request-ID`; log full exceptions server-side, return class name + request_id to the client (the generic 500 handler already returns only the class name — good; bring the other paths in line).

### 14. The audit log is deleted on the retention schedule

`retention_sweep.py` allowlists `audit_log` alongside `attendance_logs`, and `DATA-RESIDENCY.md:87` documents a 365-day horizon for it. That is a *defensible* storage-limitation answer (PDPL Art. 5), but the audit log is the accountability record for approvals of child-safeguarding actions — deleting it on the same schedule as attendance rows is a policy tension worth deciding consciously, not by default.

**Fix:** export audit rows to append-only cold storage (client-controlled) before the sweep deletes them, or run a longer horizon for `audit_log` than for operational tables. At minimum, make the two horizons independently configurable (`MSOE_RETENTION_DAYS_AUDIT`).

### 15. Fragile patterns and small bugs

| Where | Issue | Fix |
|---|---|---|
| [build_knowledge_graph.py:717](build_knowledge_graph.py#L717) | Cypher built by f-string with a label (`f"MATCH (n:{label}) …"`). The label list is hardcoded today so it is **not** injectable — but the pattern invites a future mistake. | Use `apoc` / `CALL { }` with parameters or at least assert `label.isidentifier()` against the known set. |
| [retention_sweep.py:55-62](retention_sweep.py#L55-L62) | `horizon_days()` calls `int(raw)` on env — a typo'd `MSOE_RETENTION_DAYS=365d` raises an unhandled `ValueError` traceback. | Catch and exit 2 with a one-line message (same style as the min-30d guard). |
| [main.py:92-107](main.py#L92-L107) | `_parse_api_keys` silently ignores malformed entries and lets a duplicate `school_id` overwrite an earlier one; empty secrets are storable. | Reject malformed entries loudly at startup; enforce secret minimum length; refuse empty secrets. |
| [main.py:862-877](main.py#L862-L877) | `actor = body.reviewer or caller_school` — with the free-text default `"principal"`, the `or` fallback is nearly dead code. | Part of finding 6. |
| `test_*.py` suite contract | Live-ingest blocks hard-require `MSOE_API_KEYS` or drop counts (54/38) — correct behaviour, but it reads like a regression in output. | Print an explicit `[SKIP: env]` banner so the drop is unmistakable. |

---

## Areas of improvement (beyond vulnerabilities)

1. **Consistency of the tenancy model.** One helper — `require_school(request, requested: str | None) -> str` — should be the only way any handler learns its school. Today each handler reinvents it, and four get it wrong. This is the single highest-leverage refactor in this document.
2. **CI.** There is no `.github/`, no pre-commit, no lint gate. The suite (`test_policy_pack` 44/44, `test_oneroster` 63/63, `test_graph_education` 43/43, `test_isams` 63/63, `test_powerschool` 43/43, `test_csv_drop` 48/48, smoke 9/9) is green *by hand*. A single workflow running `py_compile` + the suites on push would have caught the watermark bug in N.3 before a human did. Even a 20-line GitHub Actions file pays for itself immediately.
3. **Connection handling is documented as deliberate** (short-lived per-call PG connections to avoid pool deadlock — [main.py:212-214](main.py#L212-L214)). Good call at mock scale; at a real school's concurrency a bounded pool (`ThreadedConnectionPool` with a cap + `getconn(timeout)`) is the next step — the comment already names the pitfall.
4. **Podman/compose health-check landmine** (documented in `handoff.md` §6: `depends_on: service_healthy` never satisfied, `--force-recreate` hangs). A `scripts/up.sh` encoding the working recipe (`podman rm -f msoe_api && podman-compose up -d --no-deps api`) would stop the next session rediscovering it.
5. **`MSOE_API_KEYS` format** (`school_id:secret,school_id2:secret2`) is fragile to parse and can't carry roles (see findings 3/6). Moving to a small YAML/JSON key file (like `client.manifest.example.yml`, names-not-values) with `school_id`, `secret_env`, `role` would fix identity and tenancy in one move.
6. **Schema is in good shape** — but `leave_requests` lacks a `created_at`/audit link, and `approval_actions` stores payloads (parent email, body) in Postgres indefinitely; consider whether payload minimisation should also apply *at rest* for staged actions, not only on the LLM wire.
7. **Compliance docs are templates and say so — keep it that way.** They currently claim *less* than the code sometimes does (e.g. residency "machine-checkable at `/health`" is true; "fails loudly" is only true of the standalone script — finding 7). When hardening lands, re-run the doc-vs-code check so the two stay in lockstep; that consistency is itself a sales asset.

---

## What is already solid (do not regress)

- **Auth primitives:** `hmac.compare_digest` for key lookup ([main.py:115](main.py#L115)); HMAC-SHA256 webhook signatures over the raw body ([main.py:176-190](main.py#L176-L190)); unauthenticated bucket for rejections; keys resolved from env, not source.
- **Audit hygiene:** `key_fingerprint()` is sha256-truncated and never stores the key ([audit_log.py:73-80](audit_log.py#L73-L80)); audit writes fail soft-to-stderr rather than breaking the request path; cross-school audit reads are refused *and recorded*.
- **Injection:** every hot-path SQL statement is parameterised (`%s` + params); every hot-path Cypher call is parameterised (`session.run(cypher, **params)`, `UNWIND $rows`); the only f-string Cypher uses a hardcoded label list.
- **Retention safety:** hard-coded two-table allowlist, dry-run default, `--apply` required, min-30-day guard, and the students-before/after invariant ([retention_sweep.py:40-47](retention_sweep.py#L40-L47)). This is exemplary — use it as the template for any future deletion path.
- **Schema:** FKs with deliberate `ON DELETE CASCADE` only on child rows, CHECK constraints on enums/grades/amounts, composite per-school unique indexes (the `emirates_id`/`invoice_number` collision fixes), additive `CREATE TABLE IF NOT EXISTS` migrations.
- **LLM data minimisation is real at the call site:** the parent-draft job passes only `parent_full_name`, `first_name`, `last_name`, `grade_level`, `school_name` ([main.py:1122-1128](main.py#L1122-L1128)) — matching `SUBPROCESSORS.md`. No `emirates_id`, contacts or health detail on the wire. Keep this when refactoring.
- **Kill switch design:** `MSOE_LLM_EXTERNAL=off` discards the key at construction and raises in `chat()`, beating even a configured key; every call site degrades to `DeterministicLLM`; `status()` reports `external_allowed` honestly.
- **Guardrail posture:** irreversible actions stage for human approval; parent dispatch is explicitly a simulated relay (`_simulate_execution`), so nothing emails a real parent while the mock runs.
- **Error surface:** the generic 500 handler returns only the exception class name ([main.py:787-798](main.py#L787-L798)); `/health` reports env *names* for missing config, never values.

---

## Suggested order of work

1. **Tenancy helper + fixes** (findings 1, 2, 3) — one helper, six call sites, one negative test each. Everything else is cosmetic next to this.
2. **Credential/port posture** (finding 4) — compose binds, no default passwords in source, loud startup failure in non-lab mode.
3. **Identity binding for approvals** (findings 6) + **rate limiting** (finding 9) — makes the audit log defensible.
4. **Residency enforcement** (finding 7) + **input bounds** (finding 8) — close the doc-vs-code gaps.
5. **CI + pins + container hardening** (findings 11, 12) — keep it all from regressing.

Nothing in this document requires touching the compliance templates' claims — the fixes make the code tell the truth the docs already tell carefully.

---

# Part 2 — Functional bugs (second pass)

Not security — things that produce wrong answers or wrong actions today.

| # | Bug | Where | Impact | Fix |
|---|-----|-------|--------|-----|
| F1 | Teacher lookup is a fuzzy `ILIKE '%name%'` with `LIMIT 1` — and the user's `%`/`_` are unescaped wildcards | [main.py:1349](main.py#L1349) | "John" can match "Johnny Cash"; "Ann" matches "Joanne"; a name containing `%` matches everyone. The wrong teacher's leave request gets staged. Non-deterministic which row wins | Exact match on a normalised name, or `staff_id` in the request; escape `LIKE` wildcards (`name.replace('%','\\%')`) if fuzzy search is kept; return 409 on multiple matches instead of `LIMIT 1` |
| F2 | Subject match is exact array equality (`%s = ANY(subjects)`) | [main.py:1366](main.py#L1366) | "Mathematics" ≠ "Maths" ≠ "Math" → zero candidates → "no cover available" when cover exists | Normalise subject vocabulary (alias map in config, same pattern as `pack.config.yml`); fall back to department-level match |
| F3 | Recommended substitute = `candidates[0]` ordered by spare capacity only | [main.py:1389](main.py#L1389) | Ignores subject strength, department, and **cost** — the system will "recommend" an expensive external when a free-period internal exists | Cost-aware ranking (internal first, agency last) — this *is* service **S2** below; the fix and the revenue feature are the same change |
| F4 | Students with **zero** attendance rows are COALESCE'd to 100% attendance | [main.py:500](main.py#L500) | A newly enrolled or never-synced student is invisible to chronic-attendance detection — exactly the child a safeguarding audit should surface | Distinguish "no data" from "perfect": flag `attendance_records = 0` as its own risk (`DATA_GAP`), never as clean |
| F5 | `datetime.utcnow()` — naive and deprecated in 3.12 | [main.py:1218](main.py#L1218) | Mixed naive/aware timestamps across the API (`audit_log` and `roi_events` use aware UTC); format string drift | `datetime.now(timezone.utc).isoformat()` everywhere |
| F6 | Retention cutoff for `date` columns uses local `date.today()`, timestamptz cutoff uses UTC now | [retention_sweep.py:66-67](retention_sweep.py#L66-L67) | On an Asia/Dubai host (UTC+4) rows near the horizon boundary can be swept against a different "today" than the audit timestamp column — small, but this is a *deletion* tool; its clock should be unambiguous | One clock: compute both cutoffs from `datetime.now(timezone.utc)`, derive the date from that |

## Session progress — 2026-10-24 (F1–F6 functional-fix pass, IN PROGRESS)

Status: **5½ of 6 fixes green.** FX.1, FX.2, FX.3, FX.5, FX.6 DONE with
pre-fix-failure evidence. FX.4 behaviour is fixed and green; **2 of its 30
assertions still fail on a test artefact** (see below). Remaining work is
small and fully specified in `IMPLEMENTATION-LEDGER.md` rows **FX.4** and
**QS.12** — read those first when picking this up.

**State of the tree**
- `test_fixes.py` (new, 30 asserts) — SYNTHETIC fixtures in `fx_fixes_test`
  school, self-cleaning. `fixtures/fixes/pre_fix_compliance_default.json` —
  pre-fix compliance snapshot. `evidence/FX_pre_fix_test_fixes.log`
  (**SUMMARY: 14/30** — the failing pre-fix run proving the tests are real)
  and `evidence/FX_post_fix_test_fixes_28of30.log` (**SUMMARY: 28/30**).
- Fixed in `main.py`: normalised exact-first teacher match (404/409,
  no LIMIT 1), `SUBJECT_ALIASES`/`SUBJECT_GROUPS` + per-candidate
  `subject_match_rule`, cost-aware ranking ladder + `cover_class`/`day_rate`/
  `recommendation_reason` (no money language), DATA_GAP risk type
  (attendance_rate float|None, ordered after real risks, never notified),
  `datetime.now(timezone.utc).isoformat()`. Fixed in `schema.sql` + runtime
  startup ensure: additive `staff.is_agency`, `staff.day_rate`. Fixed in
  `retention_sweep.py`: one UTC `now` derives both cutoffs (`cutoff_for`
  takes `now=`).
- DB baseline (start of session): students **543**, staff **61**,
  attendance_logs **5286**. Staff gained ONLY the two new columns. Student
  count held at 543 across both test runs.

**The one open defect (FX.4's 2 failing asserts) — already diagnosed:**
The seeded-cohort finding SETS are identical pre/post (verified ad-hoc:
`sets equal: True`, 74/74 findings, same 50 students, 0 gap rows in the
default-school window). The failures are a **positional `zip` mis-pairing in
the test**: `pg_risk_students` ties on `(grade_level, last_name)` (many
"Al Ameri" grade-1 students) and tie order is arbitrary, so index-wise
comparison mismatches (the `85: vaccination_status 'expired' -> 'up_to_date'`
line is zip noise). Fix: (a) add `s.student_id` as final ORDER BY tie-break
in `pg_risk_students`; (b) in `test_fixes.py`, compare snapshot findings as
a multiset keyed on `(student_id, risk_type)` with per-key `risk_detail`
compare — not positional zip. Re-run → expect 30/30.

**Then finish QS.12's checklist** (ledger row has exact commands): full
regression at exact counts (policy_pack 44/44, oneroster 63/63,
graph_education 43/43, isams 63/63, powerschool 43/43, csv_drop 48/48,
test_api 9/9 — `test_api.py` untouched), psql final `students=543` + staff
column list, one live curl FX.1 proof (ambiguous name → 409 with candidate
names), then finish the QS.12/coverage rows and the handoff bullet.

---

# Part 3 — New services that save substantial money

## How to read the numbers

Same discipline as [`roi_model.py`](roi_model.py): every figure is **re-doable on paper**, assumptions are explicit, and the principal can argue with them. Three claim classes, never mixed:

- **💵 Cash** — money that stops leaving the school (agency invoices not raised). Evidenced by the school's own procurement ledger. The strongest claim class.
- **⏱ Time** — coordination/drafting hours returned, valued at the engagement's own editable rate card ([roi_model.py:123-130](roi_model.py#L123-L130): admin 100 / teacher 120 / compliance 150 / coordinator 180 / leadership 250 AED/hr). This is the existing ROI model's honesty rule: hours first, value is a stated multiplication.
- **📈 Revenue (challengeable)** — protected or won fee income. Always shown as *students × the school's own average fee*, with the retention/conversion assumption visible and overridable. Never a guarantee.

**Reference school for the math:** 2,000 students · avg fee **AED 35,000**/yr → ~**AED 70M** fee income · ~150 teachers + 60 admin/support · UAE private (KHDA/ADEK). Every line below scales linearly with roll and fee — the Discovery "Money Map" (Part 4) recomputes it on the client's real numbers instead.

## The portfolio, ranked by money

| # | Service | Claim class | Est. annual value | Data needed |
|---|---------|-------------|-------------------|-------------|
| S1 | **Retention early-warning & intervention ladder** | 📈 Revenue | **AED 175k–420k** | Today's data ✅ |
| S2 | **Cover-cost control** (cost-aware substitute dispatch) | 💵 Cash | **AED 45k–135k** | Today's data ✅ |
| S3 | **Report-card comment drafting** (grounded in competencies) | ⏱ Time | **AED 96k–240k** | + assessments table |
| S4 | **Admissions funnel responder** | 📈 Revenue | **AED 350k–700k** | + inquiries table |
| S5 | **Statutory reporting & inspection evidence packs** | ⏱ Time | **AED 45k–100k** | Today's data ✅ |
| S6 | **Fee follow-up acceleration** (staged, tone-checked dunning) | ⏱ Time + KPI | **AED 15k–30k** + days-to-payment | Today's data ✅ |
| S7 | **First-day absence chase** (safeguarding loop) | ⏱ Time | **AED 25k–50k** | Today's data ✅ |
| S8 | **Daily grounding digest** (the retainer-keeper) | ⏱ Time + stickiness | unpriced; see below | Today's data ✅ |
| S9 | Timetable/workload balance audit | ⏱/💵 | medium | + timetable |
| S10 | CPD & safeguarding-training expiry autopilot | risk | low cash, high inspection value | + training log |

**A 2,000-student school's total addressable case: AED 750k–1.7M/yr** against an engagement priced at Discovery → Pilot → Retainer well under half of that. That ratio *is* the no-brainer — provided the numbers stay honest.

---

### S1. Retention early-warning & intervention ladder — the biggest lever

**Why it's the one to lead with:** in UAE private schools, tuition *is* the business. One preventable leaver costs a full year of fee. The knowledge graph already holds the leading signals — attendance decay, fee friction, health-data gaps, risk flags — and nobody at the school is looking at the intersection.

**What it does:** nightly pass over the graph/PG → students crossing composite risk thresholds (attendance trajectory, not just level; fee days-overdue *and* trend; sibling/parent friction signals if present) → a **staged, human-approved intervention ladder** (tutor call → head of year → counselor → SLT meeting), each stage drafted, tracked, and audited exactly like the existing parent-notification flow. Reports the leaver-rate delta vs baseline.

**The math (challengeable revenue):** of ~2,000 students, assume **3% leave preventably** (60 — fee-sensitive, dissatisfied, struggling; the rest are expat relocations nobody can prevent). An intervention ladder retains **8–15% of flagged cases** (5–9 students). Value = 5–9 × AED 35,000 = **AED 175k–315k**; a stronger flag set and wider ladder reaches **AED 420k**. Method printed on every report: *retained students (enrolled in term 2 who would have appeared on the leaver list) × school's average fee*. The principal can halve the retention assumption and the case still stands.

**Honesty:** never claim "we saved a student who would have left" — claim *cases flagged, interventions completed, leaver-rate delta measured against the prior year*. The revenue line stays labelled 📈.

**Build on what exists:** `pg_risk_students` ([main.py:479](main.py#L479)) is already the query; `approval_store` already stages irreversible actions; `roi_model` already counts units. New units: `retention_case_flagged`, `intervention_staged`.

### S2. Cover-cost control — the hard-cash claim

**Why:** agency supply staff are the one line where the *school's own invoices* prove the saving. Today's dispatch ([main.py:1338](main.py#L1338)) picks candidates by spare capacity ([F3](#part-2--functional-bugs-second-pass)) and never mentions cost.

**What it does:** (1) cost-aware cover ladder — free periods first, then same-department internal, then cross-subject internal, external agency **last**, each step priced at the school's own rates; (2) leave-pattern forecasting (exam seasons, winter bugs, Sunday–Thursday patterns in UAE) → pre-arranged cover pools; (3) monthly agency-spend report to the COO.

**The math (💵 cash):** 200 staff × ~15 absence days ≈ **3,000 cover days/yr**. Assume ~40% currently go external at AED 250–450/day → **AED 300k–540k** agency spend. Shifting **15–25%** of those days to internal cover = **AED 45k–135k/yr** of invoices never raised. Claim form: *agency days avoided × the school's own agency day-rate* (their procurement ledger supplies both). The school still pays its own staff — no "wage avoided" claim, exactly per the `roi_model` honesty rule.

**Also fixes F3.** New ROI unit: `agency_day_avoided` (💵, days × client rate — the first cash-class unit; keep it in a separate block on the report so time and cash are never summed into one "savings" number).

### S3. Report-card comment drafting — the biggest hours pile in the building

**Why:** report season is the largest recurring unpaid overtime event in a school. 50,000+ comments/year at a 2,000-student secondary. The curriculum graph (MoE AI competencies, subject strands — Phase 5's packs) makes the drafts *grounded*: each comment cites the competency the child is working toward.

**What it does:** assessment ingest (new `assessments` table + CSV-drop extension — the Tier 3 connector pattern already exists) → per-student, per-subject comment drafts constrained to: attainment band, competency citation from the school's own pack, effort/behaviour note from the teacher's two clicks → teacher approves/edits → export to the school's report system. Teacher remains the author of record; this is drafting time removed, which is precisely the ROI model's rule.

**The math (⏱ time):** 2,000 students × 8–10 subject comments × 3 cycles ≈ **50–60k comments/yr**. Manual 2–4 min; automation returns **1–2 min each** = **800–2,000 hrs/yr** → at teacher rate (AED 120) **AED 96k–240k** estimated value. Unit: `report_comment_drafted` (2.0 min, role `teacher`).

**Sells itself twice:** parents receive comments that reference the actual curriculum framework — visible quality, not just back-office speed.

### S4. Admissions funnel responder — revenue lever #2

**Why:** speed-to-lead is the single biggest controllable driver of inquiry→enrollment conversion, and school admissions offices are drowning in day-one work. 1,000 inquiries compete for ~250 seats.

**What it does:** inquiries ingest (CSV/API — most school CRMs export) → instant acknowledgement (human-approved templates), visit-booking chase ladder (day 0/2/7), no-show recovery, offer-stage nudges, and a conversion dashboard (inquiry→visit→offer→enrol) the principal has never had.

**The math (📈 revenue):** +5–10pp visit-booking on ~1,000 inquiries = **50–100 more visits**; at a typical 20% visit→enrol rate = **10–20 more students** = **AED 350k–700k** of first-year fee. Method on the report: *conversion deltas measured in their CRM before/after*, never a promised uplift.

**Commercial note:** this is often the **best first demo** — principals feel admissions pain daily, and the proof lands in weeks (one term of funnel data). It also needs a new `inquiries` entity — a 2-week build, not a 9-month one.

### S5. Statutory reporting & inspection evidence packs

**Why:** KHDA/ADEK inspection prep and statutory data calls consume **250–500 leadership/admin hours a year**, and the thing inspectors ask for — *evidence that a flagged child was seen, by whom, under which policy* — is literally what the 4-hop graph path already produces (`Student → RiskFlag → Standard → Policy`, with reviewer and timestamp in `audit_log`).

**What it does:** one-click evidence packs — safeguarding case files, attendance summaries, intervention logs, policy-citation binders, "show me every action taken for this child this term" — exported as inspection-ready documents with the verified citation chain.

**The math (⏱ time):** 250–500 hrs/yr at compliance/leadership rates = **AED 45k–100k**. Plus the narrative that matters most in a sales meeting: *an inspection-ready school is a school that keeps its rating, and rating drives enrolment*. (Narrative only — no revenue number attached.)

### S6. Fee follow-up acceleration

**What it does:** staged, tone-aware dunning — gentle reminder at day 3/7, payment-plan offer at 14, bursar worklist at 30+ prioritised by amount × days × trend; every outbound message human-approved (the existing guardrail); escalation paths per the school's own fee policy (in the policy pack!).

**The math:** 150–300 follow-up hours/yr (bursar + class teachers + admin) = **AED 15k–30k** time. Plus the KPI a COO actually wants on the dashboard: **median days-to-payment**, measured in their own `fee_ledgers`. Per the honesty rule: **no write-off-avoided claims** — show the write-off *trend* chart and say nothing more.

### S7. First-day absence chase (safeguarding loop)

**What it does:** absence register at 09:30 → contact scripts to parents of unexplained absences (human-approved batch) → anything still unexplained at 14:00 escalates to the Designated Safeguarding Lead with a written trail. This is a *safeguarding* obligation in every school's policy — currently done by hand, inconsistently.

**The math (⏱ time):** attendance officer 250–500 hrs/yr = **AED 25k–50k**. The inspection story (unexplained-absence follow-up, evidenced and timestamped) is the real pitch.

### S8. Daily grounding digest — the retainer-keeper

Not a money-saver on paper; the reason the retainer **renews**. Back-office automation churns at renewal; a principal's 07:30 phone notification does not:

> **MSOE digest — Tue 23 Oct:** 3 new chronic-attendance cases (2 in Grade 8) · 2 fee accounts crossed 60 days (AED 18,400) · 1 cover day next week still unbooked (Science, Wed) · 5 parent letters awaiting your approval.

Grounded in the graph, every line a tap away to its evidence path. Today the platform has **no scheduler at all** (no cron/background jobs — verified) — a lightweight APScheduler loop in the API container, or a cron sidecar, delivering email/WhatsApp via the existing staged-draft flow. This also creates the natural `MSOE_LLM_EXTERNAL` story for clients: digest text is deterministic by default.

### S9–S10. Secondary

- **S9 Timetable/workload balance audit:** `staff.assigned`/`max_periods` already exist ([F2](#part-2--functional-bugs-second-pass) adjacency); full value needs a timetable ingest. Detect overload/imbalance → prevent the avoidable cover hire and the burnout departure (a leaver costs recruitment fees *and* is often a retention case for their students).
- **S10 CPD & safeguarding-training expiry autopilot:** low cash, high inspection value — training-log ingest → expiry ladder with drafted reminders. Also a compliance-doc companion to Phase 6's DPIA (the "DPO/DPIA" buyer cares).

## New schema the portfolio needs (all additive, per the project's migration rule)

| Entity | Unlocks | Size |
|--------|---------|------|
| `assessments` (student × subject × period × attainment) | S3 | small |
| `inquiries` (source, stage, timestamps, owner) | S4 | small |
| `interventions` (case, stage, actor, outcome) | S1, S5 | small |
| `timetable` (staff × period × class × subject) | S2 (better), S9 | medium |
| `training_log` (staff × course × expiry) | S10 | small |

Four of the six money services (S1, S2, S5, S6, S7, S8) run on **today's schema** — the data model is already ahead of the product.

---

# Part 4 — Making it a no-brainer: packaging & sales-system moves

The services above are the *what*. These are the moves that make a school say yes without a procurement fight.

1. **Discovery "Money Map" (2-week paid discovery — the productised first sale).** Point the already-built connectors at the client's SIS, pull 12 months of real attendance/fees/leave, run the risk model read-only, and deliver their **own** leak list ranked in AED with method — retention exposure, agency spend, hours burned in reports/dunning/inspection prep. Deliverable doubles as the sales document for the Pilot. *Almost all the machinery exists* (`executive.query`, `roi_model`, the connectors); what's new is packaging it as a fixed-price SKU.

2. **Baseline mode before automation.** `roi_events` currently records automated runs only. Add a measure-only mode (staff time-survey CSV import, or shadow-run logging of "what this used to take") for 2–4 weeks **before** switching anything on. The pilot's before/after report then writes itself — and "we measured first" is what makes the numbers un-arguable in a board meeting.

3. **The proof book, on *their* data.** DEMO.1/DEMO.2 built the walkthrough with synthetic data. The sales motion that closes is the same page re-rendered with the prospect's own Grade 8 cohort, their own policy pack, their own AED figures. `school_id` scoping (Phase 1) and per-school packs (Phase 5) were built for exactly this.

4. **Publish the unit menu.** `TASK_UNITS` ([roi_model.py:64](roi_model.py#L64)) is already a price list in disguise — every unit has minutes, role and basis. Publish it: *"we return 2 min per risk review, 12 min per cover coordination, 4 min per parent letter — at your rates. The retainer is priced against the hours, and the report proves the hours monthly."* Pricing that the client can audit is a differentiator against every "AI transformation" pitch they've heard.

5. **Speed-to-value as the moat.** OneRoster, Graph/SDS, iSAMS, PowerSchool, Gibbon, CSV-drop are **built and tested** (304 tests green). The pitch: *"live on your data in two weeks, not a nine-month SI engagement."* Every competitor quotes months. The connector registry is the reason that sentence is true.

6. **The Phase 6 compliance pack as the procurement unblocker.** DPA, DPIA template, subprocessor register and the machine-checkable residency proof (`/health` `data_region`) handed over in meeting one — while other vendors are "checking with legal". Note the improvement that makes this bullet defensible: finish **finding 7** (enforce residency) so the claim is enforcement, not a dashboard light.

7. **Pilot that pays for itself or doesn't extend.** Fixed-price Pilot against an agreed hours threshold measured by `roi_events`; if the report misses it, the retainer doesn't start. The ROI instrumentation already persists across restarts precisely because "it is the evidence trail you invoice against" — sell the Pilot on that sentence.

8. **Sticky by 07:30 daily** — ship **S8** early and cheaply. The digest is the difference between a tool that gets used and a tool that gets renewed.

**The pitch in one line:** *Alchemist finds the money your school is already losing — on your data, measured in your currency, proved monthly — and the first map costs two weeks, not a transformation programme.*
