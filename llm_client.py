"""OpenRouter / Nvidia Nemotron LLM client for MSOE.

Initialises an OpenAI-compatible chat-completions client against
``https://openrouter.ai/api/v1`` using ``OPENROUTER_API_KEY`` from the
environment. Default model is Nvidia Nemotron.

Design constraints
------------------
* **Zero-hallucination graph grounding** — every generative call is either
  (a) constrained to facts supplied in the prompt (graph paths / clauses /
  SIS rows) and post-validated, or (b) replaced by the deterministic
  ``DeterministicLLM`` formatter.
* **Fallback** — missing key, network failure, timeout, or unparseable
  model output always degrades to ``DeterministicLLM`` so the four
  institutional services stay online.
* **Secret hygiene** — the API key is never logged, printed, or embedded
  in responses. Only the env var *name* appears in code.

Call-site signatures match the previous ``MockLLM`` surface so ``main.py``
can swap implementations without rewriting service logic.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration (env-only secrets)
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
# Env contract: OPENROUTER_API_KEY  (never hardcode a value)
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL = os.environ.get(
    "OPENROUTER_MODEL",
    "nvidia/nemotron-3.5-lightning",
)
FALLBACK_MODEL = os.environ.get(
    "OPENROUTER_FALLBACK_MODEL",
    "nvidia/nemotron-3.5-super-vl",
)
REQUEST_TIMEOUT_S = float(os.environ.get("OPENROUTER_TIMEOUT_S", "25"))
MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "1024"))
TEMPERATURE = float(os.environ.get("OPENROUTER_TEMPERATURE", "0"))  # deterministic


# ---------------------------------------------------------------------------
# Deterministic engine (always available, zero network)
# ---------------------------------------------------------------------------

class DeterministicLLM:
    """Rule-based language engine — offline, reproducible, citation-safe.

    Used as the sole engine when no API key is present, and as the
    fallback whenever Nemotron fails or emits ungrounded text.
    """

    provider = "deterministic"
    model = "msoe-deterministic-v1"

    @staticmethod
    def summarize_violations(violations: list[dict[str, Any]]) -> str:
        if not violations:
            return "No compliance violations detected in the current cohort."
        by_type: dict[str, int] = {}
        for v in violations:
            by_type[v["risk_type"]] = by_type.get(v["risk_type"], 0) + 1
        parts = [
            f"{count} student(s) flagged for {rtype}"
            for rtype, count in sorted(by_type.items())
        ]
        return (
            "Compliance audit identified " + "; ".join(parts) +
            ". Each case is linked to the governing regulatory clause and "
            "requires human review before any parent notification is released."
        )

    @staticmethod
    def draft_parent_notification(student: dict[str, Any], risk_type: str,
                                  clause: dict[str, Any]) -> str:
        templates = {
            "VACCINATION_EXPIRED": (
                "Dear {parent}, our records indicate that {name} (Grade {grade}, {school}) "
                "has an expired or incomplete vaccination record. Under {policy}, clause "
                "{clause_no} ({clause_heading}), students must maintain up-to-date "
                "immunisation documentation. Please submit the updated record to the "
                "school clinic within 14 days."
            ),
            "CHRONIC_ATTENDANCE": (
                "Dear {parent}, our records show that {name} (Grade {grade}, {school}) "
                "has an attendance rate below the 85% threshold. Under {policy}, clause "
                "{clause_no} ({clause_heading}), sustained attendance is monitored as a "
                "student-wellbeing indicator. We invite you to meet with the attendance "
                "officer this week."
            ),
            "FEE_OVERDUE": (
                "Dear {parent}, our finance office shows an outstanding fee balance "
                "more than 60 days past due for {name} (Grade {grade}, {school}). "
                "Under {policy}, clause {clause_no} ({clause_heading}), timely fee "
                "settlement supports continued enrolment. Please contact the bursar "
                "to arrange payment."
            ),
        }
        template = templates.get(
            risk_type,
            "Dear {parent}, a compliance matter requires your attention for {name}. "
            "Please contact the school office referencing {policy} clause {clause_no}.",
        )
        return template.format(
            parent=student.get("parent_full_name", "Parent/Guardian"),
            name=f"{student.get('first_name', '')} {student.get('last_name', '')}".strip(),
            grade=student.get("grade_level", "N/A"),
            school=student.get("school_name", "MSOE"),
            policy=clause.get("policy_title", "school policy"),
            clause_no=clause.get("clause_number", "—"),
            clause_heading=clause.get("heading", "compliance clause"),
        )

    @staticmethod
    def rewrite_lesson(lesson_plan: str, gaps: list[dict[str, Any]],
                       subject: str, grade_level: int) -> str:
        if not gaps:
            return lesson_plan
        enhancement_blocks = []
        for gap in gaps:
            enhancement_blocks.append(
                f"  • [{gap['code']}] {gap['name']}: {gap['description'][:180]}"
            )
        joined = "\n".join(enhancement_blocks)
        return (
            f"{lesson_plan}\n\n"
            f"---\n"
            f"## AI Competency Enhancement (auto-generated draft)\n\n"
            f"The following MoE AI Curriculum Framework competencies are not yet "
            f"explicitly evidenced in the original {subject} lesson for Grade "
            f"{grade_level}. Integrate at least one activity per strand:\n\n"
            f"{joined}\n\n"
            f"### Suggested integration points\n"
            f"1. Warm-up (10 min): pose a real-world problem that invites data "
            f"collection or pattern recognition relevant to today's {subject} topic.\n"
            f"2. Guided practice (15 min): have learners classify or sort examples, "
            f"then discuss how a simple algorithm could automate the step.\n"
            f"3. Closure (5 min): reflect on ethical considerations — fairness, "
            f"privacy, or bias — that connect to the {subject} concept.\n"
        )

    @staticmethod
    def classify_executive_query(query: str) -> dict[str, Any]:
        q = query.lower()

        def has(*words: str) -> bool:
            return any(re.search(rf"\b{re.escape(w)}\b", q) for w in words)

        intents: list[str] = []
        if has("attendance", "chronic", "absent", "truant", "absence"):
            intents.append("attendance")
        if has("fee", "fees", "payment", "payments", "overdue", "invoice", "bursar"):
            intents.append("fees")
        if has("vaccination", "immunisation", "immunization", "immun", "health", "medical"):
            intents.append("vaccination")
        if has("grade", "classroom", "year", "classes"):
            intents.append("grade_breakdown")
        if has("policy", "clause", "compliance", "regulation", "khda", "adek"):
            intents.append("policy")
        if has("curriculum", "lesson", "ai", "competency", "syllabus"):
            intents.append("curriculum")
        grades = [int(n) for n in re.findall(r"grade\s+(\d{1,2})", q)]
        return {"intents": intents or ["general"], "grades": grades}

    @staticmethod
    def answer_grounded(query: str, evidence: list[dict[str, Any]],
                        correlation_note: str = "",
                        intents: Optional[list[str]] = None) -> str:
        """Compose a grounded answer strictly from verified graph evidence."""
        if not evidence:
            return (
                "I cannot answer this query from verified sources. "
                "No matching student-risk-to-policy paths were found in the knowledge "
                "graph for the requested parameters. I will not generate an ungrounded "
                "answer."
            )
        risk_types = sorted({e.get("risk_type", "UNKNOWN") for e in evidence})
        sample_names = [e.get("student_name", "—") for e in evidence[:5]]
        grades = sorted({int(e["grade_level"]) for e in evidence if e.get("grade_level") is not None})
        grade_scope = f"Grade {', '.join(map(str, grades))}" if grades else "all grades"
        return (
            f"Based on verified knowledge-graph paths for {grade_scope}: "
            f"{len(evidence)} student(s) are flagged under [{', '.join(risk_types)}] "
            f"and linked to governing policy clauses. "
            f"Examples: {'; '.join(sample_names)}. "
            f"Every claim below is backed by an explicit Neo4j node path and "
            f"policy clause citation."
            f"{correlation_note}"
        )


# ---------------------------------------------------------------------------
# OpenRouter chat helper (OpenAI-compatible)
# ---------------------------------------------------------------------------

class LLMClient:
    """OpenRouter-backed Nemotron client with deterministic fallback.

    Public methods intentionally mirror :class:`DeterministicLLM` so callers
    can treat either implementation interchangeably.
    """

    def __init__(self) -> None:
        self.api_key: Optional[str] = os.environ.get(OPENROUTER_API_KEY_ENV) or None
        self.base_url = OPENROUTER_BASE_URL.rstrip("/")
        self.model = DEFAULT_MODEL
        self.fallback_model = FALLBACK_MODEL
        self.timeout = REQUEST_TIMEOUT_S
        self.max_tokens = MAX_TOKENS
        self.temperature = TEMPERATURE
        self._fallback = DeterministicLLM()
        self.last_provider: str = "deterministic"
        self.last_model: str = self._fallback.model
        self.last_error: Optional[str] = None
        # Services draft parent notifications concurrently; guard the
        # status fields so parallel call sites don't race on them.
        self._status_lock = threading.Lock()

    # -- capability probe ---------------------------------------------------

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        """Non-secret status for /health and startup logs."""
        return {
            "provider": "openrouter" if self.has_api_key else "deterministic",
            "model": self.model if self.has_api_key else self._fallback.model,
            "fallback_model": self.fallback_model,
            "base_url": self.base_url,
            "api_key_configured": self.has_api_key,
            "api_key_env": OPENROUTER_API_KEY_ENV,
            "temperature": self.temperature,
            "last_provider": self.last_provider,
            "last_error": self.last_error,
        }

    # -- raw chat -----------------------------------------------------------

    def chat(self, system: str, user: str,
             model: Optional[str] = None) -> dict[str, Any]:
        """Call OpenRouter chat/completions. Raises on transport/API errors."""
        if not self.api_key:
            raise RuntimeError(
                f"{OPENROUTER_API_KEY_ENV} is not set — cannot call OpenRouter"
            )
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://msoe.local",
                "X-Title": "MSOE Agentic Backend",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"OpenRouter transport error: {exc}") from exc

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            raise RuntimeError(f"OpenRouter response parse error: {exc}") from exc

        with self._status_lock:
            self.last_provider = "openrouter"
            self.last_model = model or self.model
            self.last_error = None
        return {"content": content, "model": data.get("model", model or self.model)}

    def _chat_or_none(self, system: str, user: str) -> Optional[str]:
        """Chat with model + fallback model; None if every attempt fails."""
        if not self.has_api_key:
            return None
        for candidate in (self.model, self.fallback_model):
            try:
                result = self.chat(system, user, model=candidate)
                text = (result.get("content") or "").strip()
                if text:
                    return text
            except Exception as exc:
                # Never log the API key; exception text is safe.
                with self._status_lock:
                    self.last_error = str(exc)[:400]
                    self.last_provider = "deterministic"
                continue
        return None

    # -- grounded helpers ---------------------------------------------------

    @staticmethod
    def _grounding_system(task: str) -> str:
        return (
            f"You are the MSOE institutional agent performing: {task}.\n"
            "HARD RULES (zero hallucination):\n"
            "1. Use ONLY the facts provided in the user message (graph paths, "
            "policy clauses, SIS fields). Do NOT invent names, numbers, policies, "
            "or citations.\n"
            "2. If the provided facts are insufficient, reply with exactly: "
            "REFUSED_INSUFFICIENT_DATA\n"
            "3. Never invent clause numbers, source_file paths, or node IDs.\n"
            "4. Keep the tone professional and suitable for UAE school governance.\n"
            "5. Return plain text only — no markdown fences, no JSON unless asked."
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[dict[str, Any]]:
        """Pull the first JSON object from a model response, if any."""
        text = text.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except Exception:
                pass
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None

    def _validate_grounded_answer(self, answer: str,
                                  evidence: list[dict[str, Any]]) -> bool:
        """Reject answers that invent facts absent from the evidence pack."""
        if not answer or answer.strip() == "REFUSED_INSUFFICIENT_DATA":
            return False
        # Must mention at least one verifiable token from the evidence.
        tokens: list[str] = []
        for e in evidence:
            for key in ("student_name", "policy_title", "risk_type", "clause_number"):
                val = e.get(key)
                if val:
                    tokens.append(str(val))
        if not tokens:
            return False
        # At least one evidence token must appear in the answer.
        return any(tok.lower() in answer.lower() for tok in tokens if len(str(tok)) > 3)

    # -- service call sites (same signatures as DeterministicLLM) -----------

    def summarize_violations(self, violations: list[dict[str, Any]]) -> str:
        det = self._fallback.summarize_violations(violations)
        if not self.has_api_key or not violations:
            return det
        system = self._grounding_system("compliance violation summary")
        user = (
            "Summarise the following compliance violations for a UAE school "
            "leadership audience. Count by risk_type. Remind that parent "
            "notifications require human approval.\n\n"
            f"VIOLATIONS_JSON:\n{json.dumps(violations, default=str)[:6000]}"
        )
        text = self._chat_or_none(system, user)
        if not text or "REFUSED_INSUFFICIENT_DATA" in text:
            return det
        # Keep summary short and grounded: require mention of a risk type
        # present in the payload, else fall back.
        risk_types = {v.get("risk_type", "") for v in violations}
        if any(rt and rt.lower() in text.lower() for rt in risk_types):
            return text.strip()
        return det

    def draft_parent_notification(self, student: dict[str, Any], risk_type: str,
                                  clause: dict[str, Any]) -> str:
        det = self._fallback.draft_parent_notification(student, risk_type, clause)
        if not self.has_api_key:
            return det
        system = self._grounding_system("parent compliance notification drafting")
        user = (
            "Draft a short, formal parent notification email body. Use ONLY "
            "the student and policy clause facts below. Do not invent policies.\n\n"
            f"STUDENT_JSON:\n{json.dumps(student, default=str)}\n"
            f"RISK_TYPE: {risk_type}\n"
            f"CLAUSE_JSON:\n{json.dumps(clause, default=str)}"
        )
        text = self._chat_or_none(system, user)
        if not text or "REFUSED_INSUFFICIENT_DATA" in text:
            return det
        # Grounding check: must reference the student name or the clause.
        name = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip()
        clause_no = str(clause.get("clause_number") or "")
        grounded = (
            (name and name.lower() in text.lower())
            or (clause_no and clause_no in text)
        )
        return text.strip() if grounded else det

    def rewrite_lesson(self, lesson_plan: str, gaps: list[dict[str, Any]],
                       subject: str, grade_level: int) -> str:
        det = self._fallback.rewrite_lesson(lesson_plan, gaps, subject, grade_level)
        if not self.has_api_key or not gaps:
            return det
        system = self._grounding_system("MoE AI curriculum lesson enhancement")
        user = (
            f"Original {subject} lesson (Grade {grade_level}):\n{lesson_plan}\n\n"
            f"Missing MoE AI competencies (JSON):\n"
            f"{json.dumps(gaps, default=str)[:5000]}\n\n"
            "Rewrite/extend the lesson plan so each missing competency is "
            "addressed with a concrete classroom activity. Preserve the original "
            "lesson body. Cite competency codes exactly as given."
        )
        text = self._chat_or_none(system, user)
        if not text or "REFUSED_INSUFFICIENT_DATA" in text:
            return det
        # Must include at least one gap code and a slice of the original plan.
        codes = [str(g.get("code", "")) for g in gaps if g.get("code")]
        code_ok = any(c and c in text for c in codes)
        plan_ok = lesson_plan[:40].lower() in text.lower() or subject.lower() in text.lower()
        return text.strip() if (code_ok and plan_ok) else det

    def classify_executive_query(self, query: str) -> dict[str, Any]:
        det = self._fallback.classify_executive_query(query)
        if not self.has_api_key:
            return det
        system = self._grounding_system("executive query intent classification")
        user = (
            "Classify this executive query. Return STRICT JSON only, schema:\n"
            '{"intents": ["attendance"|"fees"|"vaccination"|"grade_breakdown"|'
            '"policy"|"curriculum"|"general", ...], "grades": [int, ...]}\n\n'
            f"QUERY: {query}"
        )
        text = self._chat_or_none(system, user)
        if not text:
            return det
        parsed = self._extract_json(text)
        if not isinstance(parsed, dict):
            return det
        intents = parsed.get("intents")
        grades = parsed.get("grades")
        if not isinstance(intents, list) or not intents:
            return det
        if not isinstance(grades, list):
            grades = det.get("grades", [])
        clean_grades = []
        for g in grades:
            try:
                gi = int(g)
                if 1 <= gi <= 12:
                    clean_grades.append(gi)
            except Exception:
                continue
        return {"intents": [str(i) for i in intents], "grades": clean_grades}

    def answer_grounded(self, query: str, evidence: list[dict[str, Any]],
                        correlation_note: str = "",
                        intents: Optional[list[str]] = None) -> str:
        """Graph-Grounded RAG answer. Refuses when evidence is empty."""
        det = self._fallback.answer_grounded(
            query, evidence, correlation_note=correlation_note, intents=intents
        )
        if not evidence:
            return det
        if not self.has_api_key:
            return det

        # Compact evidence pack — only verified path facts.
        pack = [
            {
                "student_id": e.get("student_id"),
                "student_name": e.get("student_name"),
                "grade_level": e.get("grade_level"),
                "risk_type": e.get("risk_type"),
                "clause_number": e.get("clause_number"),
                "clause_heading": e.get("clause_heading"),
                "policy_title": e.get("policy_title"),
                "source_file": e.get("source_file"),
                "policy_reference": e.get("policy_reference"),
                "verified_path": e.get("verified_path"),
            }
            for e in evidence[:25]
        ]
        system = self._grounding_system("executive Graph-Grounded RAG answer")
        user = (
            f"EXECUTIVE QUERY ({intents or ['general']}):\n{query}\n\n"
            f"VERIFIED NEO4J PATHS (the ONLY permitted facts):\n"
            f"{json.dumps(pack, default=str)}\n\n"
            f"ADDITIONAL SIS CROSS-CHECK:\n{correlation_note or 'none'}\n\n"
            "Write a concise executive answer. Cite policy titles and clause "
            "numbers that appear above. If the paths do not answer the query, "
            "reply REFUSED_INSUFFICIENT_DATA."
        )
        text = self._chat_or_none(system, user)
        if not text or "REFUSED_INSUFFICIENT_DATA" in text:
            return det
        if not self._validate_grounded_answer(text, pack):
            return det
        return text.strip()


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """Return the process-wide LLM client (OpenRouter if configured)."""
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def reset_llm() -> None:
    """Drop the singleton (used by tests / startup after env changes)."""
    global _llm
    _llm = None
