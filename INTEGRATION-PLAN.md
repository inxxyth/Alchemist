# Alchemist → K-12 School System Integration Plan

**Status:** approved direction, not yet built.
**Date:** 18 October 2026
**Audience:** Alchemist principals; eventually a client group's IT lead.
**Delivery model:** Iterative & Incremental — one phase per session. Execution prompts live in `handoff.md` § **Road-To-AlchemistV1**.

---

## 1. Purpose

The engine is proven: knowledge-graph-grounded automation with human-in-the-loop guardrails, verified in a live run (50 students → 55 risk cases → 55 governing clauses → **55 staged / 0 executed**, 14.63h returned). The barrier to selling into **GEMS, Taaleem and PACE** is that the engine speaks to exactly one school system — Gibbon — through a bespoke MySQL bridge. That is a demo pipe, not an integration.

This document states what those operators actually run, how Alchemist connects to it, what is broken today that must be fixed first, and a phased roadmap.

### Decisions locked

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Deliverable of the *next* work is **foundations + connectors**, not a SaaS | Agency model: Discovery → Pilot → Retainer |
| 2 | Integration wedge = **standards-first** (OneRoster 1.2 + Microsoft Graph Education); **iSAMS** is the first *native* connector after | One standards connector covers PowerSchool, iSAMS and anything certified |
| 3 | Deployment = **one isolated instance per client group**, UAE-hosted, with `school_id` scoping inside | Groups are multi-school; competitors' data must stay physically apart; PDPL-safe posture |

---

## 2. The target operators

| Operator | Shape | What is publicly known about their stack |
|----------|-------|------------------------------------------|
| **GEMS Education** | ~60+ schools — UAE (Dubai, Abu Dhabi, Al Ain, Sharjah, RAK, Fujairah), Doha, Cairo. Largest private K-12 operator globally. Central IT + procurement. | Site names only the **GEMS Connect** parent app. No SIS vendor published. Heavy British/IB/American estate. |
| **Taaleem** | ~15 UAE schools. British / IB / American curricula. | No platform named publicly. |
| **PACE Group** | Smaller cluster (Dubai / Sharjah / Northern Emirates). Cost-conscious. | No platform named publicly. Skews toward lighter/cheaper systems. |

**Honest caveat, and it goes in any client conversation:** no operator publishes its SIS vendor. A group-wide SIS standard should not be assumed. **Discovery produces the per-school answer.** What *is* knowable is the market they sit in.

### The market these schools actually sit in

UAE international-school stacks cluster around:

- **iSAMS** — cloud MIS, OAuth2 API at `developer.isams.com`, dedicated Middle East page, Dubai English Speaking Schools testimonial. Strongest single signal in this market.
- **ManageBac / Faria Education** — 50+ integrations; names **iSAMS as its only SIS partner**; family = OpenApply (admissions) + SchoolsBuddy (activities/billing/transport).
- **PowerSchool** — OneRoster-capable, common in American-curriculum schools.
- **Gibbon** and lighter systems (Classe365, MyClassCampus, Fedena, spreadsheets) at budget-conscious groups like PACE.

GEMS and Taaleem both run heavy British/IB/American estates — that is exactly the iSAMS + ManageBac belt.

**Both groups are multi-school.** Integration has to work at **group level** (central IT, procurement, DPA) and **school level** (whatever that campus runs).

---

## 3. What schools already expose

| Layer | What it gives us | Source |
|---|---|---|
| **OneRoster 1.2** (1EdTech) | The roster interchange standard. Three independently implementable services — **Rostering**, **Gradebook**, **Resource**. Both **CSV bulk** and **REST/JSON** bindings. Certification programme + TrustEd Apps Directory. 1EdTech's own framing: *"onboarding new systems is a breeze if they support ingesting OneRoster data."* | [1edtech.org/standards/oneroster](https://www.1edtech.org/standards/oneroster) |
| **Microsoft Graph Education APIs** | `educationSchool`, `educationClass`, `educationUser`, `educationStudent`, `educationTeacher`, `educationEnrollment`, `educationAssignment`. Microsoft's framing: *hundreds of SISs with proprietary formats* is the problem; **School Data Sync + roster APIs** is the answer. | [learn.microsoft.com/graph/education-concept-overview](https://learn.microsoft.com/en-us/graph/education-concept-overview) |
| **School Data Sync (SDS)** | Free. Ingests **OneRoster API** and CSV from any SIS into Entra ID, then exposes roster to third-party apps **via a no-cost API**. The route where we never touch the SIS directly. | [learn.microsoft.com/schooldatasync](https://learn.microsoft.com/en-us/schooldatasync/) |
| **iSAMS native API** | OAuth2-authorised, partner-gated developer portal (`developer.isams.com`). Resources across students/staff/contacts/attendance/behaviour/academic/finance. Integration Marketplace. | [isams.com](https://www.isams.com/) |
| **Faria / ManageBac** | 50+ integrations (Google, O365, Clever, ClassLink, iSAMS, Turnitin…). No public API docs on the marketing site — partner-gated. | [managebac.com](https://www.managebac.com/) |

### Strategic consequence

**We do not build N bespoke SIS connectors.** We build **three tiers** — standards/shared-identity first, then a short list of native connectors for the SIS that dominate this market, then a universal fallback. One OneRoster connector covers PowerSchool, iSAMS and anything else certified.

---

### 3.1 Microsoft Graph Education — permissions (least privilege)

The Graph Education connector is a **read-only consumer**. We read rosters and
stage actions for human approval; we never write back to the school's
directory. That constraint is architectural, not aspirational:
`connectors/graph_education/` has a single Graph call site and it only ever
sends GET requests (`test_graph_education.py` asserts this on every recorded
request).

**Exact Graph application permissions requested:**

| Permission | Type | Used for | Required |
|---|---|---|---|
| `EducationRoster.Read.All` | Application (app-only) | `/education/schools`, `/education/users`, `/education/classes`, `/education/classes/{id}/members` — rosters | **Yes** |
| `EducationAssignments.Read.All` | Application (app-only) | `/education/classes/{id}/assignments` — optional enrichment for the curriculum service only | No — request only if the engagement needs it |

**No write permissions are requested** — not `EducationRoster.ReadWrite.All`,
not `EducationAssignments.ReadWrite.All`, not directory write scopes. A group
IT lead should be able to grant this and see, in the admin consent screen,
exactly two read scopes. If a vendor asks for write access to your directory
to "sync a roster", that is a red flag.

Auth is OAuth2 client-credentials (app-only) against
`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` with scope
`https://graph.microsoft.com/.default`. Secrets live in environment variables
(`MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`,
`MS_GRAPH_SCHOOL_ID_MAP`) — never in a manifest, never in source.

Assignment/submission objects are **out of scope for the compliance engine**
and are not persisted (they would be fetched only if a future curriculum
consumer asks for them).

### 3.2 The SDS / Entra identity note (principal-facing)

**For a non-technical principal or group IT lead:**

> Microsoft School Data Sync (SDS) is a free Microsoft service that already
> keeps your school's class lists up to date inside your Microsoft 365
> environment. If your school already runs SDS, the authoritative list of
> students, teachers and classes is already there — securely, and under your
> IT's control. Alchemist's integration simply *reads* that list with a
> permission that only allows reading. We never change it, and we never ask
> for a login to your student information system. That is why this route
> works even when a school's IT policy says "no new vendor touches our SIS":
> nothing new touches it. The same data your teachers already see in Teams
> class rosters is what feeds the compliance engine — nothing more.

**The engineering version:** where the school already runs SDS, the roster is
already in Entra ID and this connector never touches the SIS. SDS ingests
OneRoster/CSV from any SIS into Entra ID and exposes roster to third-party
apps via a no-cost API — we consume *that*, not the SIS. For a group
considering many campuses with different SIS vendors, this is the
single integration surface that works everywhere the group already has
Microsoft 365.

### 3.3 Future work (noted, not built)

- **Google Classroom API** — the Google-side equivalent of the Graph
  Education route. Same connector shape (roster read-only, canonical five).
  Deliberately **not** built in Phase 3; revisit when a client estate is
  Google-first.

---

## 4. UAE regulatory and residency posture

Source: UAE PDPL (Federal Decree-Law No. 45 of 2021) — see [DLA Piper Data Protection Laws of the World](https://www.dlapiperdataprotection.com/index.html?t=law&c=AE).

- **Lawful basis:** consent is the default; contract-performance and legal-obligation bases exist. For school operations, the contract/legal-obligation bases are the ones to lean on — not consent from minors.
- **Cross-border transfers are restricted.** The Data Office is expected to publish an adequacy list; it has not. **Safe posture: keep student data in the UAE.**
- **Breach notification** to the Data Office *"immediately upon becoming aware"* (Executive Regulations still unpublished).
- **DPO required** where processing is large-scale / involves sensitive data. **DPIA required** for high-risk processing. Student data of minors at group scale is squarely in both categories.
- Free zones (DIFC, ADGM) run separate regimes.
- **Azure UAE North and UAE Central regions exist** ([Azure geographies](https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/)). In-region hosting is available today.

**Commercial consequence:** the **Data Processing Agreement** and the **residency answer** are sales artefacts, not afterthoughts. GEMS/Taaleem/PACE procurement will ask for them in the first meeting. They ship in Phase 6, but the architecture must make them true from Phase 1.

---

## 5. Architecture

### 5.1 Deployment — one isolated instance per client group

```
┌── GEMS engagement ──────────────────────────────────────────┐
│  Azure UAE North                                            │
│  ┌─────────────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐  │
│  │ msoe_api    │──│ Postgres │  │ Neo4j   │  │ connectors│  │
│  └─────────────┘  └──────────┘  └─────────┘  └──────────┘  │
│        school_id scoping: campus A | campus B | campus C    │
└─────────────────────────────────────────────────────────────┘
┌── Taaleem engagement ────────┐   ┌── PACE engagement ───────┐
│  separate instance, separate │   │  separate instance       │
│  credentials, separate data  │   │  separate credentials    │
└──────────────────────────────┘   └──────────────────────────┘
```

- One compose/k8s unit per **client group**, provisioned from a template, hosted in **Azure UAE North/Central**.
- Inside that instance, a **`school_id` / `campus_id` dimension** on every SIS table and every Neo4j node that carries school meaning — so one GEMS instance serves many campuses.
- Different clients = different instances. Competitors' data never commingles. That is what makes the DPA defensible.

### 5.2 Connector model — three tiers

| Tier | What | Why |
|---|---|---|
| **Tier 1 — Standards & shared identity** | **OneRoster 1.2** consumer (CSV + REST), **Microsoft Graph Education** consumer (via SDS), Google Classroom API | One connector covers PowerSchool, iSAMS and anything certified. The Graph route works even when we are never given SIS credentials. **This is the wedge.** |
| **Tier 2 — Native connectors** | **iSAMS** (OAuth2) first, then PowerSchool, ManageBac/OpenApply, Bromcom. **Gibbon already exists** and becomes connector #0. | For schools that won't open OneRoster, or where we need data OneRoster doesn't carry (fees, behaviour, vaccination). |
| **Tier 3 — Universal fallback** | SFTP CSV drop + our existing `POST /api/v1/sis/sync` batch and **signed** `POST /api/v1/sis/webhook` | The answer to "our IT won't do an API". Always available, never blocks a deal. |

### 5.3 Connector manifest

A per-client YAML manifest names: dialect, credential env-var names, `school_id` mapping, sync mode, policy-pack path, engagement_id. Adding a client's SIS = new manifest entry + a connector module, **never** edits to core service code.

This is what stops the agency becoming a body shop.

### 5.4 Canonical model

Keep the five entities already named in `sis_adapter.py` — **Student, Attendance, Fee, Staff, LeaveRequest**. Change only what multi-school forces:

- `school_id` on `students`, `attendance_logs`, `fee_ledgers`, `staff`, `leave_requests`, `roi_events`
- Natural-key collisions fixed (see gap analysis §6 items 3)
- Period-level attendance (real SISs do not collapse to one row per student per day)
- `staff` / `leave_requests` DDL committed to version control

### 5.5 What stays exactly as it is

- The four services and their Graph-Grounded RAG contracts (`GROUNDED` / `REFUSED_INSUFFICIENT_DATA`)
- The human-in-the-loop guardrail — `PENDING_HUMAN_APPROVAL`, zero side effects without a named officer
- `roi_model.py` assumptions/method exposure and its `engagement_id` hook
- The policy pack as markdown (we **write the format spec down** rather than replace it)

---

## 6. Gap analysis — what blocks a real client

Ranked. "Blocks pilot" = no client can run without it. "Blocks group" = needed once a client is more than one school.

| # | Gap | Owner file | Blocks |
|---|-----|-----------|--------|
| 1 | **Zero auth.** CORS `*`, no API key, no OAuth, no webhook signature verification. Anyone reaching :8000 can POST `/sis/sync` and write every school's data. | `main.py` | **pilot** |
| 2 | **No school/tenant scoping anywhere.** No `school_id` column on any table, none in Neo4j, none in request models, none on webhooks. | `seed_sis.py`, `sis_adapter.py`, `main.py` | **pilot** (group at latest) |
| 3 | **Global UNIQUE constraints that will corrupt multi-school data.** `fee_ledgers.invoice_number UNIQUE` (bridge emits `INV-{invoiceID}` — two sources both produce `INV-1`); `gibbon_bridge` maps `gibbonPersonID → student_id` PK so two systems overwrite each other; `staff.email UNIQUE`; `students.emirates_id UNIQUE` with a **hash-synthesised mock fallback** that occupies the key. | `seed_sis.py`, `gibbon_bridge.py`, `sis_adapter.py` | **pilot** |
| 4 | **Neo4j is single-tenant and destructive.** One global `RiskFlag {type}` node per risk type; `Policy.title` / `Standard.code` globally unique; `build_knowledge_graph.py` does `DETACH DELETE` of the whole graph on every run. | `build_knowledge_graph.py`, `sis_adapter.py` | **pilot** |
| 5 | **`ApprovalStore` is in-memory** (`main.py`) — approvals vanish on restart. Docstring promises a Postgres variant that does not exist. Contrast: `roi_events` is durable. | `main.py` | **pilot** |
| 6 | **No DDL in version control for `staff` or `leave_requests`** — both are read/written by the code. | missing | **pilot** |
| 7 | **Org identity hardcoded.** `ORG_SETTINGS` ("MSOE Academy Dubai"), school/emirate/curriculum literals in `gibbon_bridge.py` SQL and `sis_adapter.py` defaults, `@msoe.ae` emails, `CURRENCY_DEFAULT="AED"`. | `gibbon_bootstrap_settings.py`, `gibbon_bridge.py`, `sis_adapter.py`, `roi_model.py` | **pilot** |
| 8 | **Policy-pack format is regex-implied, not specified.** `RISK_POLICY_MAP`, `POLICY_DEPT_RULES`, `DEPARTMENTS` hardcoded to the MSOE pack. A client's pack with different clause headings silently falls back to "first clause of the policy". | `build_knowledge_graph.py` | **group** (Phase 5) |
| 9 | **Single-tenant deployment.** `docker-compose.yml` sets `container_name:` on every service, fixed host ports, literal credentials, global volume names. | `docker-compose.yml` | **group** |
| 10 | **No scheduler / no incremental sync.** `gibbon_bridge.py` is a one-shot manual pull — no watermark, no tombstones. | `gibbon_bridge.py` | **group** |

### Reusable as-is (do not rewrite)

- `sis_adapter.py` — dict-driven dialect maps (`STUDENT_FIELD_MAPS` etc., keyed `gibbon`/`openeducat`/`canvas`) → canonical PG rows → `sync_*` upserts → `handle_webhook` event router. Adding a dialect is genuinely config-shaped.
- `POST /api/v1/sis/sync` (batch) and `POST /api/v1/sis/webhook` (event push) are the right external contracts.
- `roi_model.py` — `engagement_id` is already a first-class indexed column and `GET /api/v1/roi/report` already filters on it. Only the four `record_units(...)` call sites need to stamp it.
- `policy_vault/*.md` → `build_knowledge_graph.py` → Neo4j Policy/Standard/Competency. The cleanest reuse asset in the repo.
- `test_api.py` — instance-agnostic acceptance harness (only `BASE` binds it). Caveat: it is not read-only, and `leave_date: "2026-10-22"` is a calendar landmine.

---

## 7. Roadmap

| Phase | Name | Unblocks commercially |
|-------|------|----------------------|
| **1** | Foundations | *Everything.* Without this, no real client data can be touched. |
| **2** | Connector framework + OneRoster 1.2 | The market wedge — PowerSchool, iSAMS, and anything certified, from one connector. |
| **3** | Microsoft Graph Education / SDS | The route that works **without SIS credentials** — often the only door IT will open. |
| **4** | Native connectors (iSAMS → PowerSchool; Gibbon becomes #0) | Schools that won't open OneRoster, and the data OneRoster doesn't carry (fees, behaviour, vaccination). |
| **5** | Policy-pack library | Engagement #3 costing ~30% of engagement #1. This is what stops the agency becoming a body shop. |
| **6** | Compliance pack (UAE residency, DPIA/DPA, audit) | The artefacts procurement asks for in meeting one. |

Each phase is **one session**. Method, verification and definition-of-done for each live in `handoff.md` → **Road-To-AlchemistV1**.

---

## 8. Commercial packaging

| Engagement stage | What the client gets | Maps to phase |
|------------------|----------------------|---------------|
| **Discovery** | Time-and-motion on their operations, policy pack ingested into the graph, a ranked list of automations each with a defensible hours estimate. We identify which of the three tiers their SIS falls into. Output includes a residency + DPA position paper. | 0 (no code) |
| **Pilot** | One workflow, end to end, on live data with the guardrail on. Staged actions, officer approval, the same ROI ledger the proof book shows. If the numbers don't hold up in their office, we stop. | 1 + 2 (or 3) |
| **Retainer** | Automations in production, policy graph kept current as their documents change, coverage extended, hours-returned reported on a board-ready schedule. | 4 + 5 + 6 |

---

## 9. Risks and open questions

1. **PDPL Executive Regulations remain unpublished.** The residency posture is deliberately conservative; revisit when they land.
2. **Group IT procurement cycles** at GEMS/Taaleem are long. A pilot at a single campus with a sympathetic principal is the realistic first sale, not a group contract.
3. **Will GEMS/Taaleem open OneRoster at all?** Unknown. The Graph/SDS route (Phase 3) is the hedge — it works when IT will only grant a Graph application permission.
4. **LLM data-processing terms.** Parent-notification drafts contain student-adjacent text sent to OpenRouter. We need a DPA position on that, and the option to run deterministic-only where a client forbids external inference.
5. **iSAMS API access is partner-gated.** Phase 4 may need a partner application before it can be built against real endpoints. Plan the sandbox ask early.
6. **Multi-tenant SaaS is shelved, not deleted.** The `school_id` work in Phase 1 is the cheap insurance that makes it possible later.

---

## 10. Sources

- 1EdTech OneRoster — https://www.1edtech.org/standards/oneroster
- Microsoft Graph Education API overview — https://learn.microsoft.com/en-us/graph/education-concept-overview
- Microsoft School Data Sync — https://learn.microsoft.com/en-us/schooldatasync/
- iSAMS — https://www.isams.com/ · https://developer.isams.com/
- Faria Education / ManageBac — https://www.managebac.com/
- GEMS Education — https://www.gemseducation.com/en/our-schools/
- UAE PDPL summary — https://www.dlapiperdataprotection.com/index.html?t=law&c=AE
- Azure global infrastructure (UAE North / UAE Central) — https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/
