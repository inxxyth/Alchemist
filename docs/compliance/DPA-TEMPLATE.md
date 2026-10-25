# Data Processing Agreement (DPA) — Template

**Status:** TEMPLATE for review and completion by group legal counsel. **This is not legal advice** and is not a signed instrument. Bracketed fields are for the parties to complete. No real secret, student record, or school name appears below.

**Law anchors (UAE PDPL — Federal Decree-Law No. 45 of 2021, per DLA Piper's summary: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>):** **Article 4** (legal bases), **Article 5** (storage limitation / deletion), **Article 9** (breach notification), **Article 10** (DPO), **Article 21** (DPIA), and the cross-border transfer provisions (see `DATA-RESIDENCY.md` §4).

---

## DATA PROCESSING AGREEMENT

**Between:**

**(1) [CLIENT GROUP / SCHOOL]** of [address], United Arab Emirates (the **"Controller"**), and

**(2) [PROCESSOR — the delivery agency operating the MSOE engine]** of [address] (the **"Processor"**).

Each a **"Party"**; together the **"Parties"**.

**Effective date:** [ ]

### 1. Definitions

"Personal Data", "Processing", "Data Subject", "Controller", "Processor", and "Personal Data Breach" have the meanings given in the PDPL (Federal Decree-Law No. 45 of 2021). "Sub-processor" means any third party engaged by the Processor to Process Personal Data on the Controller's behalf. "Services" means the MSOE engine services described in the Statement of Work dated [ ].

### 2. Scope and subject matter

2.1. The Processor shall Process Personal Data only on documented instructions from the Controller, including with regard to transfers, unless required by Union or Member State law — here, unless required by UAE law — in which case the Processor shall inform the Controller before Processing, unless that law prohibits such information.

2.2. Subject matter: the categories of data, data subjects, and purposes set out in the completed `DPIA-TEMPLATE.md` (Appendix 1) and summarised below:

| Item | Summary (complete in Appendix 1) |
|---|---|
| Categories of Data Subject | Pupils (minors), parents/guardians, staff |
| Categories of Personal Data | Identifiers, contact, education records, **health data (vaccination status)**, attendance, fee status |
| Purpose | School compliance/safeguarding, curriculum, operations and executive-reporting services |
| Duration | Life of the engagement + retention per `DATA-RESIDENCY.md` §6 |

2.3. **Residency.** The Processor shall Process and store Personal Data in the United Arab Emirates (Azure UAE North and/or UAE Central). Cross-border transfer is governed by clause 6.

### 3. Roles

3.1. The Controller is the controller of the Personal Data. The school group determines purposes and means; the Processor processes only on instruction.

3.2. Where the Processor (or its delivery agency) determines purposes and means for any processing of its own (e.g. security logging, billing), that processing is outside this DPA's instruction scope and is covered by the parties' main agreement.

### 4. Processor obligations

The Processor shall:

4.1. Process Personal Data only on documented instructions, including as regards transfers;

4.2. ensure persons authorised to Process are bound by confidentiality;

4.3. implement the technical and organisational measures in clause 5;

4.4. respect the conditions in clause 7 for engaging Sub-processors;

4.5. assist the Controller, taking into account the nature of Processing, with data-subject-rights requests (access, correction, erasure, restriction, objection — PDPL Chapter on rights of the data subject; Article 13 territory per the DLA Piper summary);

4.6. assist the Controller with security, breach notification (clause 6), DPIA (PDPL Article 21) and DPO (Article 10) obligations;

4.7. delete or return all Personal Data after the end of the provision of services (clause 8);

4.8. make available to the Controller all information necessary to demonstrate compliance (clause 9).

### 5. Security measures (technical and organisational)

**What the engine does today** (evidence in the platform and repository; marked ✓). Items marked ☐ are contract promises the Processor must confirm before signing.

| Measure | Status |
|---|---|
| One isolated instance per client group — no shared data pool between groups | ✓ (`INTEGRATION-PLAN.md` §5.1) |
| Per-client API keys (`X-API-Key`) on every API route; HMAC-SHA256 signed sync webhooks | ✓ |
| Human-in-the-loop: irreversible actions (parent notifications, schedule writes) staged `PENDING_HUMAN_APPROVAL`; no execution without explicit approval | ✓ |
| Audit logging of every approval decision, side effect, sync run and auth rejection (`audit_log`), queryable by the Controller | ✓ (`GET /api/v1/audit/log`) |
| Least-privilege SIS connectors (e.g. Microsoft Graph read-only roster scope) | ✓ (`INTEGRATION-PLAN.md` §3.1) |
| Data minimisation on any external LLM call; client kill switch (`MSOE_LLM_EXTERNAL=off`) eliminating all external inference | ✓ (`SUBPROCESSORS.md`) |
| Configured-data-region proof exposed at `GET /health` and enforced by `residency_check.py` | ✓ |
| Retention sweep with dry-run default; student records never auto-deleted | ✓ (`retention_sweep.py`) |
| Encryption in transit (TLS) for all client-facing endpoints | ☐ (confirm deployment configuration) |
| Encryption at rest for the database volumes | ☐ (confirm Azure storage configuration; note reliance on platform capability) |
| Access control / least privilege for operators of the instance | ☐ (confirm operational procedure) |
| Logging & monitoring of access to the instance | ☐ (audit_log covers application decisions; infrastructure access logging to confirm) |
| Business continuity / backup posture | ☐ (to be specified per engagement) |

### 6. Personal data breach

6.1. **The Processor shall notify the Controller of a Personal Data Breach immediately upon becoming aware of it.** This is the standard set by the PDPL's breach provision for controllers — **PDPL Article 9** requires the Controller to report a breach and the results of its investigation to the UAE Data Office **"immediately upon becoming aware"** of a breach prejudicing the privacy, confidentiality or security of personal data (wording per the DLA Piper summary of Federal Decree-Law 45/2021; timing and procedure detail await the Executive Regulations). The Processor mirrors that standard in favour of the Controller so the Controller can meet its own Article 9 duty without delay.

6.2. The notification shall describe the nature of the breach, categories and approximate numbers of data subjects and records affected, likely consequences, and measures taken or proposed.

6.3. The Processor shall not notify any supervisory authority or data subject regarding the breach without the Controller's prior written consent, unless required by UAE law.

### 7. Sub-processors

7.1. The Controller authorises the Sub-processors listed in the register at `SUBPROCESSORS.md` (Appendix 2), notably: **Microsoft Azure** (hosting, UAE regions) and **OpenRouter / the model provider** (optional LLM drafting of parent notifications only).

7.2. The Processor shall inform the Controller of any intended addition or replacement of a Sub-processor, giving the Controller the opportunity to object, and shall flow down data-protection terms no less protective than those in this DPA.

7.3. **LLM drafting option.** The Controller may require deterministic-only operation at any time by instructing the Processor to set `MSOE_LLM_EXTERNAL=off`; in that mode no Personal Data is sent to any external model provider (proof: `GET /health` → `llm.external_allowed=false`).

### 8. Deletion / return on termination

8.1. On termination or expiry of the services, the Processor shall, at the Controller's choice, return all Personal Data to the Controller and delete existing copies, unless UAE law requires continued storage (**PDPL Article 5** storage-limitation principle: data may not be kept after fulfilling the purpose of Processing, unless anonymised — per the DLA Piper summary).

8.2. Deletion shall be certified in writing on request. Backups are covered by [insert backup-expiry period]; Personal Data in backups is not restored except for disaster recovery and is deleted on the normal backup cycle.

8.3. The Controller's own retention duties (statutory finance and pupil-record rules) override deletion for the categories concerned; the Controller shall identify those categories in Appendix 1.

### 9. Audit rights

9.1. The Processor shall make available to the Controller all information necessary to demonstrate compliance with this DPA and shall allow and contribute to audits, including inspections, conducted by the Controller or an independent auditor mandated by the Controller, [annually / with 30 days' notice], during business hours, without unreasonably disrupting operations.

9.2. **Evidence on request, without an inspection,** includes: `audit_log` extracts (approvals, side effects, sync runs, auth rejections), the output of `residency_check.py`, `GET /health` residency and LLM-switch state, the sub-processor register, and the retention sweep records.

9.3. The Processor shall inform the Controller immediately if, in its opinion, an instruction infringes the PDPL.

### 10. Liability, indemnity, governing law

[For counsel: insert liability caps, indemnities, and governing law/dispute resolution consistent with the main services agreement. Do not treat this template as drafting those terms.]

### 11. Signatures

| For the Controller | For the Processor |
|---|---|
| Name: [ ] | Name: [ ] |
| Role: [ ] | Role: [ ] |
| Date: [ ] | Date: [ ] |
| Signature: [ ] | Signature: [ ] |

---

## Appendix 1 — completed DPIA summary (attach)

Attach the completed `DPIA-TEMPLATE.md` for this engagement.

## Appendix 2 — sub-processor register (attach)

Attach the current `SUBPROCESSORS.md`. Change-notice mechanics per clause 7.

---

## Sources

1. **UAE PDPL — Federal Decree-Law No. 45 of 2021**, as summarised by DLA Piper Data Protection Laws of the World — UAE: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>. Provisions relied on: **Article 9** (breach notification — "immediately upon becoming aware" standard, quoted in clause 6.1), **Article 5** (storage limitation — clause 8), **Article 4** (legal bases), **Article 10** (DPO), **Article 21** (DPIA), data-subject rights provisions, and the cross-border transfer provisions.
2. **Azure geographies** (UAE North / UAE Central): <https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/>
3. Repository: `DATA-RESIDENCY.md`, `SUBPROCESSORS.md`, `DPIA-TEMPLATE.md`, `INTEGRATION-PLAN.md` §4.
