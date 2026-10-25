# Data Protection Impact Assessment (DPIA) — Template

**Status:** TEMPLATE for completion and review by qualified legal counsel. **This is not legal advice.** Bracketed fields are for the deploying organisation to complete. No real student, staff, or school data appears in this template.

**Law anchor:** UAE PDPL (Federal Decree-Law No. 45 of 2021) — **Article 21** requires a DPIA before processing that uses modern technologies posing a high risk to the privacy and confidentiality of data subjects' data, and where large amounts of Sensitive Personal Data will be processed (per DLA Piper's summary of the law: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>). Processing personal data of **minors at school-group scale** is exactly the case this template is written for. **Article 10** (DPO) is referenced below. **Article 4** (legal bases) and **Article 5** (storage limitation) anchor the lawful-basis and retention sections.

**Roles for this template:**

- **Controller:** [CLIENT GROUP / SCHOOL] — decides why and how pupil and staff data is processed.
- **Processor:** [PROCESSOR — the delivery agency operating the MSOE engine] — processes only on the controller's documented instructions (see `DPA-TEMPLATE.md`).

---

## 1. Processing purpose

| Field | Entry |
|---|---|
| Purpose of processing | Operation of the school's [compliance/safeguarding · curriculum · operations · executive reporting] services: identifying pupil risk flags (vaccination status, attendance, fee arrears), matching them to the school's own policy clauses, drafting parent notifications for staff review, coordinating substitute cover, and answering leadership queries grounded in the school's policy knowledge graph |
| Nature of processing | Collection via SIS integration, storage in a UAE-hosted instance, automated matching and drafting, human approval before any external communication |
| Scope of processing | Pupils enrolled at [SCHOOL/CAMPUS LIST], their parent/guardian contacts, relevant staff, attendance and fee records |
| Duration | Life of the engagement; per-category retention in `DATA-RESIDENCY.md` §6 |

## 2. Categories of data subject

**Include every category; minors are the reason this DPIA exists.**

| Category | Notes |
|---|---|
| **Pupils / minors** (primary) | Typically ages 3–18. **Children — heightened risk category.** Consent from a minor is not relied on as a lawful basis (see §5) |
| Parents / guardians | Contact details, named as recipients of notification drafts |
| Staff (teachers, officers) | Leave, availability, contact for operational services |

## 3. Categories of personal data

| Category | Examples (as held by the engine) | Sensitive? |
|---|---|---|
| Identifiers | Internal student ID, SIS-native ID, Emirates ID (where held) | Emirates ID is a national identifier — treat with heightened care |
| Contact data | Parent/guardian name, email, phone | No |
| Education records | Grade level, curriculum track, enrolment date, school/campus | No |
| **Health data** | **Vaccination status / expiry** | **Yes — health data (Sensitive Personal Data under the PDPL definition, per DLA Piper summary)** |
| Attendance | Daily attendance status, attendance rate | No (but welfare-relevant) |
| Financial | Fee invoice status, days overdue | No (financial detail) |
| Staff HR | Leave dates and reason (free text) | Potentially |

**Data minimisation note (architecture-enforced):** the optional external LLM drafting path receives a minimised payload only — pupil name, grade, school name, parent name, risk type, and the governing policy clause (`SUBPROCESSORS.md` §2). National IDs, contact details, dates of birth, and health/fee detail are **not** sent to any external model. A client can disable external inference entirely (`MSOE_LLM_EXTERNAL=off`).

## 4. Data flows (summary)

1. SIS (OneRoster / Microsoft Graph Education / iSAMS / PowerSchool / Gibbon / CSV) → processor's connectors → UAE instance (PostgreSQL + Neo4j), one instance per client group (`DATA-RESIDENCY.md` §3).
2. Risk flags → policy clause matching **inside** the instance's knowledge graph (no external call).
3. Parent notification drafting → optional external LLM (minimised payload) **or** deterministic in-instance drafting (default fallback and client-forced mode).
4. Every draft is staged for **human approval**; nothing is sent to parents until an authorised officer approves, and the approval is recorded in `audit_log`.
5. No onward transfer to any other third party.

## 5. Lawful basis

**Law anchor: PDPL Article 4** (legal bases of processing — consent is the default basis, with exceptions including performance of a contract and compliance with legal obligations imposed by law; per DLA Piper summary of Federal Decree-Law 45/2021).

| Processing activity | Lawful basis relied on | Rationale |
|---|---|---|
| Core school-operations processing of pupil data (roster, attendance, safeguarding flags) | **Contract** (provision of educational services to the pupil, or the services agreement with the controller) **and legal obligation** (school safeguarding and health-record duties under applicable UAE education regulation) | The school is under duties to monitor attendance, immunisation records, and pupil welfare |
| Parent notification drafting and (on approval) sending | **Contract / legal obligation** — same as above; the communication is a school-to-parent communication about the pupil's education and welfare | — |
| Staff operational data (leave, cover) | **Contract** (employment/engagement administration) | — |
| Optional external LLM drafting | Flows from the above bases; minimised payload; see `SUBPROCESSORS.md` | Controller may forbid it entirely (`MSOE_LLM_EXTERNAL=off`) |

> **Explicitly not relied on: consent from minors.** Consent is not a sound basis for processing children's school records — a minor cannot give free, informed consent, and withdrawing it cannot undo attendance or safeguarding obligations. The engine never asks a pupil to consent to this processing. (Where any consent-based processing is ever proposed for other purposes, it must be reviewed by counsel first.)

## 6. Retention

See `DATA-RESIDENCY.md` §6 for the per-category table. Summary: attendance and audit rows are swept after `MSOE_RETENTION_DAYS` (default 365) by `retention_sweep.py` (dry-run by default); student master records are **never** auto-deleted. **Law anchor: PDPL Article 5** (storage limitation).

## 7. Sub-processors

Full register in `SUBPROCESSORS.md`. The one that must be named in any DPIA:

- **OpenRouter / the model provider** — sub-processor for **parent-notification drafting only** (optional; minimised payload; temperature 0.0; post-validated against graph evidence; deterministic fallback; client kill switch `MSOE_LLM_EXTERNAL=off` with zero external inference).
- **Microsoft Azure** — infrastructure hosting (UAE North / UAE Central).

## 8. Risks and mitigations

| # | Risk | Likelihood before controls | Impact | Mitigation (architecture) | Residual |
|---|---|---|---|---|---|
| R1 | Unauthorised access to pupil data | Medium | High | Per-client isolated instance; per-client API keys (`X-API-Key`) on every `/api/v1/*` route; HMAC-signed webhooks; school-level `school_id` scoping inside the instance | Low–Medium |
| R2 | Cross-contamination between client groups / competitors | Low | High | One instance per client group — no shared pool (`DATA-RESIDENCY.md` §3) | Low |
| R3 | Disclosure of pupil-adjacent text to an external LLM provider | Medium (if enabled) | Medium | Minimised payload; `MSOE_LLM_EXTERNAL=off` kill switch; temperature 0.0; output post-validated against graph evidence; deterministic fallback always available | Low (off) / Low–Medium (on) |
| R4 | A parent letter goes out without human review | Low | High | Irreversible actions are staged `PENDING_HUMAN_APPROVAL`; no executor runs without an explicit approve; decision recorded in `audit_log` | Low |
| R5 | Over-retention of pupil data | Medium | Medium | `retention_sweep.py` + `MSOE_RETENTION_DAYS`; retention table per category; students never auto-deleted (the controller decides) | Low–Medium |
| R6 | Data leaves the UAE | Low | High | UAE-only deployment; transfer posture in `DATA-RESIDENCY.md` §4; deterministic-only mode for clients forbidding any egress | Low |
| R7 | Inability to answer "who approved this letter?" | Medium | Medium | Durable `audit_log` (every approval, side effect, sync run, auth rejection); `GET /api/v1/audit/log` | Low |
| R8 | Regulatory change (PDPL Executive Regulations) | High (they are unpublished) | Medium | 30-day review commitment + conservative interim posture (`DATA-RESIDENCY.md` §5) | Medium |

## 9. Consultation

| Party | Consulted? | Input |
|---|---|---|
| Controller's DPO / legal counsel | ☐ (to complete) | |
| Data subjects or their representatives (parents) | ☐ (to complete — controller decision) | |
| Processor's security lead | ☐ (to complete) | |

## 10. Sign-off

| Role | Name | Organisation | Date | Signature |
|---|---|---|---|---|
| Controller — Principal / authorised officer | [ ] | [CLIENT GROUP] | [ ] | [ ] |
| Controller — DPO / legal counsel | [ ] | [ ] | [ ] | [ ] |
| Processor — engagement lead | [ ] | [PROCESSOR] | [ ] | [ ] |

**Review triggers:** publication of the PDPL Executive Regulations; any new sub-processor; any change of hosting region; any new category of personal data; at least annually.

---

## 11. Sources

1. **UAE PDPL — Federal Decree-Law No. 45 of 2021**, as summarised by DLA Piper Data Protection Laws of the World — UAE: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>. Provisions relied on: **Article 21** (DPIA requirement — high-risk / large-scale sensitive processing), **Article 10** (DPO), **Article 4** (legal bases), **Article 5** (storage limitation), the **Sensitive Personal Data** definition (includes health data), and the transfer provisions (see `DATA-RESIDENCY.md` §4).
2. **Azure geographies** (UAE North / UAE Central): <https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/>
3. Repository architecture: `INTEGRATION-PLAN.md` §4–§5; `DATA-RESIDENCY.md`; `SUBPROCESSORS.md`; `DPA-TEMPLATE.md`.
