# Data Residency — Position Paper

**Status:** TEMPLATE / POSITION PAPER for review by qualified legal counsel. **This is not legal advice and not a certification.** Nothing in this document claims ISO, SOC, or "PDPL-certified" status — none of those certifications has been obtained.

**Audience:** a school principal or group executive who needs the answer before the lawyers arrive.

**Last reviewed:** 2026-10-23 · **Review trigger:** publication of the PDPL Executive Regulations (see §5).

---

## 1. The one-line answer

**Student data stays in the United Arab Emirates.** The system that holds it is deployed in the UAE, and the contract we sign says so in plain language.

---

## 2. Where the data lives

| Question | Answer |
|---|---|
| Which country? | **United Arab Emirates** |
| Which data centres? | **Azure UAE North** (Dubai) or **Azure UAE Central** (Abu Dhabi) — the two UAE regions of Microsoft Azure |
| Who runs the data centres? | Microsoft Azure (the infrastructure provider) |
| Can we prove which region we are configured for? | Yes. The system exposes its configured region at `GET /health` (`data_region`), and a verification script (`residency_check.py`) fails loudly if the region is missing or is not a UAE region. |

Azure lists UAE North and UAE Central among its global geographies:
<https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/>
(source: Azure global infrastructure — geographies page).

**Honest caveat:** `residency_check.py` asserts a *configuration* claim — the deployment is set up for a UAE region. It is not an infrastructure attestation or an audit of physical storage location. The configuration check, this document, and the contract together are the evidence package; a client that wants physical attestation should make it a contract term with the infrastructure provider.

---

## 3. One isolated instance per client group

Each client group gets **its own separate installation** — its own database, its own knowledge graph, its own credentials, its own deployment.

- Two school groups **never share an installation**. A competitor group's data cannot appear in your system, because it is not in your system.
- Inside one group's installation, multiple campuses are kept separate by a `school_id` tag on every record — campus A's data is filtered out of campus B's views by the platform, not by convention.
- Nothing in the product is a shared multi-tenant pool. The shared-SaaS model is deliberately **not** the deployment model here.

This is written into the architecture (see `INTEGRATION-PLAN.md` §5.1) and into the Data Processing Agreement (`DPA-TEMPLATE.md`).

---

## 4. Cross-border transfer posture

**Law:** UAE Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data (the **PDPL**). The PDPL restricts transferring personal data outside the UAE; a transfer generally requires an adequacy-style approval from the UAE Data Office, appropriate safeguards, or another condition set by the law and its Executive Regulations. As of the latest published summary of the law, **the Data Office has not yet published the adequacy list**, so the safe posture is not to depend on one.

*(Source: PDPL Federal Decree-Law 45/2021 as summarised in DLA Piper's Data Protection Laws of the World — UAE: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>. The INTEGRATION-PLAN §4 posture statement derives from the same source.)*

**Our posture, stated operationally:**

1. **Client personal data is stored and processed in the UAE.** That is the default and the contract term.
2. **No cross-border transfer of client personal data is required for the system to work.** Every core service runs inside the UAE installation.
3. **One documented exception — optional LLM drafting.** If a client enables AI-assisted drafting of parent notifications, a small, minimised payload (student name, grade, school name, parent name, risk type, governing policy clause — see `SUBPROCESSORS.md`) is sent to an external model provider. **This is off by default per client contract, and any client can forbid it outright:** the environment switch `MSOE_LLM_EXTERNAL=off` forces the deterministic in-instance drafting path, with **zero external inference**, provable at `GET /health` (`llm.external_allowed=false`). A client that requires "no personal data leaves the UAE ever" runs with this switch off and the answer is absolute.
4. Anything a transfer would touch is covered by the DPA (`DPA-TEMPLATE.md`) and the sub-processor register (`SUBPROCESSORS.md`) before it happens.

---

## 5. What happens if the PDPL Executive Regulations change the answer

The PDPL's **Executive Regulations** — the detailed rules that fill in transfer mechanisms, breach timelines, and registration duties — **were still unpublished** at the time of writing (the law was issued 26 September 2021 and the regulations were expected within six months; see the DLA Piper summary linked above and `INTEGRATION-PLAN.md` §9.1).

**Our commitment when they land:**

1. **Review within 30 days** of publication of the Executive Regulations (or of any UAE Data Office guidance on cross-border transfers).
2. **The conservative posture stays until the review says otherwise.** We do not loosen residency on speculation; we only change it when a written legal review says the new regulations support the change.
3. **Affected artefacts are updated together:** this document, `DPIA-TEMPLATE.md`, `DPA-TEMPLATE.md`, `SUBPROCESSORS.md`, and any live client contract — so they cannot contradict each other.
4. **Clients are notified** of any change that affects their data's location or their sub-processor list, under the change-notice terms of the DPA.
5. If the regulations impose new duties (registration, breach timelines, transfer tools), those duties are **contract commitments**, not marketing lines — and the honest gap between "what the law now requires" and "what we have built" is written down before it is claimed closed.

---

## 6. Retention policy per data category

**Law anchor:** PDPL **Article 5** (storage limitation) — personal data "may not be kept after fulfilling the purpose of Processing" unless anonymised (per the DLA Piper summary of Federal Decree-Law 45/2021). Retention below is our operational answer to that rule.

| Data category | Where it lives | Retention rule | How it is enforced |
|---|---|---|---|
| **Student master records** (identity, enrolment, parent contact) | `students` table | Kept while the student is enrolled, plus the period the client group sets in writing (the school is the controller and the record-holder for statutory purposes) | **Never deleted by any automated sweep.** Deletion only on documented written instruction from the client |
| **Attendance logs** | `attendance_logs` table | **`MSOE_RETENTION_DAYS`** (default **365 days**) | `retention_sweep.py` deletes rows older than the horizon (dry-run by default; `--apply` required) |
| **Fee records** | `fee_ledgers` table | Client finance policy; accounting/invoicing law may require multi-year keeping — the controller decides and tells us | Not auto-swept (statutory finance retention is the controller's call) |
| **Staff records** | `staff` table | Duration of the staff member's engagement + client HR policy | Not auto-swept; deletion on client instruction |
| **Leave requests** | `leave_requests` table | Client HR policy | Not auto-swept |
| **Approval decisions** (who approved which parent letter) | `approval_actions` table | Kept for the life of the engagement — it is the governance evidence trail | Not auto-swept |
| **Audit log** (every approval, side effect, sync run, auth rejection) | `audit_log` table | **`MSOE_RETENTION_DAYS`** (default **365 days**) | `retention_sweep.py`, same as attendance |
| **ROI measurement events** | `roi_events` table | Life of the engagement | Not auto-swept |
| **Connector sync state** (delta tokens/watermarks) | `connector_state` table | While the connector is active | Replaced on each sync; not a personal-data store |

**Safety rule, enforced in code:** `retention_sweep.py` has a hard-coded allowlist of exactly two tables (`attendance_logs`, `audit_log`). **Students are never deleted** by any retention rule — the sweep prints the student count before and after every run and fails if it moved.

---

## 7. Sources (no unsourced legal claims in this document)

1. **UAE PDPL — Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data** (the law itself), as summarised by DLA Piper Data Protection Laws of the World — UAE: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>. Provisions relied on here: **Article 5** (storage limitation), the **cross-border transfer provisions** (transfer restrictions; adequacy list expected from the Data Office), and the **unpublished status of the Executive Regulations**.
2. **Azure global infrastructure — geographies** (UAE North / UAE Central region names): <https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/>
3. **`INTEGRATION-PLAN.md` §4 and §9** in this repository (posture rationale and open risks).

Related documents: `DPIA-TEMPLATE.md` · `DPA-TEMPLATE.md` · `SUBPROCESSORS.md`.
