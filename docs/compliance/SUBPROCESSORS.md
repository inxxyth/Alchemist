# Sub-processor Register

**Status:** LIVING REGISTER (template format for counsel review — **not legal advice**). Maintained per engagement; the DPA (`DPA-TEMPLATE.md` clause 7) makes this register the authorised list and requires change notice before any addition or replacement. No real secret, student record, or school name appears here.

**Law anchors (UAE PDPL — Federal Decree-Law No. 45 of 2021, per DLA Piper's summary: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>):** the **cross-border transfer provisions** (any send outside the UAE is a restricted transfer — `DATA-RESIDENCY.md` §4), **Article 9** (breach notification — "immediately upon becoming aware"), and **Article 4** (legal bases for the underlying processing).

---

## 1. Register

| # | Sub-processor | Purpose | What is sent | Where | Enabled by default? | Controls |
|---|---|---|---|---|---|---|
| 1 | **Microsoft Azure** | Infrastructure hosting of the client's isolated instance | All client data (at rest and in transit within the instance) | **UAE North** (Dubai) / **UAE Central** (Abu Dhabi) — <https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/> | Yes (it is the deployment) | Residency proof at `GET /health` (`data_region`) + `residency_check.py`; one instance per client group (`DATA-RESIDENCY.md` §3) |
| 2 | **OpenRouter / the model provider** (default model family: Nvidia Nemotron via OpenRouter) | **Optional** AI drafting of parent-notification bodies (and, when the executive/curriculum services run in generative mode, drafting of executive answers and lesson enhancements) | **Minimised, student-adjacent text only** — see §2 | Provider's serving infrastructure (**may be outside the UAE** — this is the one known cross-border path; see §3) | **No — a client contract may disable it entirely; deterministic mode is the safe default when no key is configured** | `MSOE_LLM_EXTERNAL=off` kill switch (§4), temperature 0.0, post-validation against graph evidence, deterministic fallback (§5) |

That is the complete list. No analytics, advertising, email-relay, or other third party receives client personal data. (Parent-notification *delivery* is executed by the approving school's own channel — the engine drafts and stages; a simulated relay exists in the demo build only and sends nothing.)

---

## 2. The LLM question — exactly what is sent to OpenRouter

When (and only when) external LLM calls are enabled, these call sites send text to the provider. All of them run at **temperature 0.0** (`OPENROUTER_TEMPERATURE` default `"0"`).

| Call site | Trigger | Payload sent externally | Student-adjacent? |
|---|---|---|---|
| **Parent-notification drafting** (the register's headline case) | Compliance audit finds a risk-flagged pupil | **risk type + pupil name + grade + school name + parent/guardian name + governing policy clause (title / number / heading)** — student-adjacent text, nothing more | **Yes** |
| Violation summary | Same audit run | Per-case **name, grade, school name, risk type** (national IDs, contact details and health/fee detail are stripped before sending) | Minimised |
| Executive grounded answer | Leadership query | Query text + up to 25 **verified graph paths** (pupil name, risk type, clause citations) + a SIS cross-check note | **Yes** |
| Executive intent classification | Leadership query | Query text only | Only if the query names someone |
| Lesson enhancement | Curriculum service | Teacher's lesson-plan text + competency gap codes | No pupil data |

**Never sent to any external provider:** Emirates ID / national identifiers, dates of birth, parent email or phone, vaccination or fee detail beyond the bare risk type, full SIS rows, API keys or credentials.

**Post-validation against graph evidence (zero-hallucination contract):** every generative answer is checked against the knowledge graph before it is used. Parent drafts must reference the pupil's name or the clause number or they are discarded and replaced by the deterministic template. Executive answers are validated against the verified path facts and degrade to `REFUSED_INSUFFICIENT_DATA` when the graph cannot support them. Provider failures, timeouts (drafts run under a wall-clock budget), or ungrounded output all degrade to the deterministic engine — the four services never return partial results because a provider misbehaved.

---

## 3. Cross-border status of this sub-processor

Sending even minimised pupil-adjacent text to OpenRouter is a **cross-border transfer** for PDPL purposes (the PDPL restricts transfers outside the UAE; the Data Office adequacy list has not been published — `DATA-RESIDENCY.md` §4). The honest position:

1. **Clients who require "no personal data leaves the UAE" run with the switch off** (§4). Then this row of the register is dormant: zero external inference, no transfer at all.
2. Clients who permit drafting assistance accept this transfer under the DPA, with the minimisation in §2 as the contractual data set.
3. When the PDPL Executive Regulations publish transfer mechanisms, this register and the DPA transfer terms are re-reviewed within 30 days (`DATA-RESIDENCY.md` §5).

---

## 4. Deterministic-only mode — `MSOE_LLM_EXTERNAL=off`

Any client can require **no external inference at all**. The processor sets one environment variable on the client's instance:

```yaml
MSOE_LLM_EXTERNAL: off
```

**What the switch does (enforced in `llm_client.py`):**

- Forces the **`DeterministicLLM`** path for every service call site — even if `OPENROUTER_API_KEY` is configured, the key is discarded at client construction and no request leaves the instance.
- Any attempt to call the provider raises instead of dialling out.
- **Provable in `GET /health`:**

```json
"llm": {
  "provider": "deterministic",
  "external_allowed": false,
  "external_env": "MSOE_LLM_EXTERNAL",
  ...
}
```

A client (or an auditor) can verify the promise in one GET. Accepted off-values: `off`, `false`, `0`, `no`, `disabled`, `deterministic`. Anything else (or unset) means external calls are permitted subject to the other controls.

---

## 5. The DeterministicLLM fallback path

Even with external drafting enabled, the fallback is always standing by:

| Condition | Behaviour |
|---|---|
| No `OPENROUTER_API_KEY` configured | Deterministic templates only |
| `MSOE_LLM_EXTERNAL=off` | Deterministic templates only — switch wins over any key |
| Provider error / timeout / refusal / ungrounded output | That draft degrades to the deterministic template; the service still returns every draft (never partial) |

`DeterministicLLM` is offline and reproducible: fixed templates per risk type, no network, no inference. It is the mode used in the recorded demo runs and the mode a cautious client can lock permanently.

---

## 6. Change management

1. Any new sub-processor is added to §1 **before** it processes data.
2. The Controller is notified under DPA clause 7.2 and may object.
3. This document, the DPIA (§7), and the client's DPA are updated together so they cannot drift.

## Sources

1. **UAE PDPL — Federal Decree-Law No. 45 of 2021**, as summarised by DLA Piper Data Protection Laws of the World — UAE: <https://www.dlapiperdataprotection.com/index.html?t=law&c=AE>. Provisions relied on: **cross-border transfer provisions** (§3), **Article 9** (breach notification — "immediately upon becoming aware"), **Article 4** (legal bases).
2. **Azure geographies** (UAE North / UAE Central): <https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/>
3. Repository (what the code actually does): `llm_client.py` (kill switch, temperature default 0, grounding validation, fallback), `main.py` (`draft_parent_notifications` budget + fallback), `GET /health`, `DATA-RESIDENCY.md`, `DPA-TEMPLATE.md`, `INTEGRATION-PLAN.md` §4 & §9.4.
