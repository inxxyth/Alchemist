"""Policy-pack library — shared pack format, configuration and resolution.

One source of truth for the three maps that used to be hardcoded against the
MSOE pack (gap #8 in INTEGRATION-PLAN.md §6):

  * ``DEPARTMENTS``        — regulatory departments upserted as ``:Department``
  * ``POLICY_DEPT_RULES``  — Policy title keyword → ``[:GOVERNS]`` target
  * ``RISK_POLICY_MAP``    — risk type → governing policy file + clause keyword

Those maps live in ``<pack>/pack.config.yml`` (see ``policy_vault/FORMAT.md``).
``build_knowledge_graph.py`` and ``sis_adapter.sync_students_to_graph`` both
resolve them through this module, so a client pack with different clause
headings links the same way the MSOE reference pack does — driven by config,
never by a hardcoded ``CASE risk WHEN ... THEN 'Campus Health'``.

Pack layout (see FORMAT.md for the markdown rules)::

    <pack_dir>/
        pack.config.yml          # the three maps (this module)
        *.md                     # policy documents (the pack format)
        README.md / FORMAT.md    # pack documentation — never parsed as policy

School → pack resolution order:

  1. in-process registry   ``register_school_pack(school_id, pack_dir)``
  2. env ``MSOE_POLICY_PACK_MAP``  JSON object {"school_id": "/path/to/pack"}
  3. env ``MSOE_POLICY_PACK`` (default ``policy_vault``) for school_id
     ``default`` / empty
  4. otherwise ``None`` — no pack, so no clause link is created.  We never
     silently borrow another school's clauses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Pack layout constants — documented in policy_vault/FORMAT.md
# ---------------------------------------------------------------------------

PACK_CONFIG_FILENAME = "pack.config.yml"

#: Files that live in a pack directory as author documentation but are never
#: parsed as policy documents.  Keeps the flat ``*.md`` glob honest now that
#: FORMAT.md / README.md sit beside the packs.
PACK_DOC_FILENAMES = frozenset({"README.md", "FORMAT.md"})

#: Section body retained on a ``:Standard.text_content`` (characters).
TEXT_CONTENT_LIMIT = 4000

#: Competency body retained on a ``:Competency.description`` (characters).
COMPETENCY_TEXT_LIMIT = 2000

#: ``**Document Reference:**`` is only honoured in the first N lines.
DOC_REFERENCE_WINDOW = 20

#: Patterns the pack format is defined by.  Kept here so the parser in
#: ``build_knowledge_graph.py`` and the validator cannot drift apart.  All are
#: MULTILINE so they work both per-line (validator) and over whole text
#: (parser).  The em-dash in STRAND_H3_RE is U+2014 — see FORMAT.md §2.5.
CLAUSE_H2_RE = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)
STRAND_H3_RE = re.compile(r"^###\s+(Strand\s+[A-F])\s+—\s+(.+)$", re.MULTILINE)
DOC_REFERENCE_RE = re.compile(r"\*\*Document Reference:\*\*\s*(.+)")

#: Module directory — the reference pack ships beside this file, so a relative
#: ``MSOE_POLICY_PACK`` still resolves when the process runs from elsewhere.
_MODULE_DIR = Path(__file__).resolve().parent

_SCHOOL_PACKS: dict[str, Path] = {}
_CONFIG_CACHE: dict[tuple[str, int], "PackConfig"] = {}


def default_pack_dir() -> Path:
    """The pack used for school_id ``default`` (``MSOE_POLICY_PACK``)."""
    raw = Path(os.environ.get("MSOE_POLICY_PACK", "policy_vault"))
    if raw.is_absolute():
        return raw
    if (Path.cwd() / raw).is_dir():
        return Path.cwd() / raw
    return _MODULE_DIR / raw


class PackConfigError(ValueError):
    """pack.config.yml is missing, unreadable or the wrong shape."""


# ---------------------------------------------------------------------------
# Pack configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackConfig:
    """The three maps extracted from a pack's ``pack.config.yml``."""

    pack_dir: Path
    pack_id: str
    departments: list[dict[str, str]] = field(default_factory=list)
    policy_dept_rules: list[dict[str, str]] = field(default_factory=list)
    risk_policy_map: dict[str, dict[str, str]] = field(default_factory=dict)

    # Names kept equal to the constants they replace in
    # build_knowledge_graph.py so the extraction is greppable.
    @property
    def DEPARTMENTS(self) -> list[dict[str, str]]:  # noqa: N802
        return self.departments

    @property
    def POLICY_DEPT_RULES(self) -> list[dict[str, str]]:  # noqa: N802
        return self.policy_dept_rules

    @property
    def RISK_POLICY_MAP(self) -> dict[str, dict[str, str]]:  # noqa: N802
        return self.risk_policy_map


def _require_list_of_dicts(value: Any, key: str, required: tuple[str, ...]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PackConfigError(f"{PACK_CONFIG_FILENAME}: '{key}' must be a list")
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            raise PackConfigError(f"{PACK_CONFIG_FILENAME}: '{key}[{i}]' must be a mapping")
        for field_name in required:
            if not str(row.get(field_name, "")).strip():
                raise PackConfigError(
                    f"{PACK_CONFIG_FILENAME}: '{key}[{i}]' is missing '{field_name}'")
    return [{str(k): str(v) for k, v in row.items()} for row in value]


def load_pack_config(pack_dir: str | os.PathLike[str]) -> PackConfig:
    """Load and validate ``<pack_dir>/pack.config.yml``.

    Raises :class:`PackConfigError` when the file is missing or malformed —
    a pack without config cannot link risks to clauses, and guessing would
    reintroduce the silent fallback this module exists to remove.
    """
    pack_path = Path(pack_dir)
    cfg_file = pack_path / PACK_CONFIG_FILENAME
    if not cfg_file.is_file():
        raise PackConfigError(f"{cfg_file}: not found — a pack must ship {PACK_CONFIG_FILENAME}")

    try:
        stat_key = (str(cfg_file.resolve()), cfg_file.stat().st_mtime_ns)
    except OSError as exc:  # pragma: no cover - unreadable file
        raise PackConfigError(f"{cfg_file}: cannot stat ({exc})") from exc
    cached = _CONFIG_CACHE.get(stat_key)
    if cached is not None:
        return cached

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
        raise PackConfigError("PyYAML is required to read pack.config.yml") from exc

    try:
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise PackConfigError(f"{cfg_file}: invalid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise PackConfigError(f"{PACK_CONFIG_FILENAME}: top level must be a mapping")

    departments = _require_list_of_dicts(
        raw.get("DEPARTMENTS", []), "DEPARTMENTS", ("name", "full_name", "jurisdiction"))
    dept_names = {d["name"] for d in departments}

    rules = _require_list_of_dicts(
        raw.get("POLICY_DEPT_RULES", []), "POLICY_DEPT_RULES", ("match", "dept"))
    for i, rule in enumerate(rules):
        if rule["dept"] not in dept_names:
            raise PackConfigError(
                f"{PACK_CONFIG_FILENAME}: 'POLICY_DEPT_RULES[{i}].dept' is "
                f"'{rule['dept']}' but no DEPARTMENTS entry has that name")

    risk_map_raw = raw.get("RISK_POLICY_MAP", {})
    if not isinstance(risk_map_raw, dict):
        raise PackConfigError(f"{PACK_CONFIG_FILENAME}: 'RISK_POLICY_MAP' must be a mapping")
    risk_map: dict[str, dict[str, str]] = {}
    for risk_type, entry in risk_map_raw.items():
        if not isinstance(entry, dict):
            raise PackConfigError(
                f"{PACK_CONFIG_FILENAME}: 'RISK_POLICY_MAP.{risk_type}' must be a mapping")
        missing = [k for k in ("policy_file", "clause_keyword")
                   if not str(entry.get(k, "")).strip()]
        if missing:
            raise PackConfigError(
                f"{PACK_CONFIG_FILENAME}: 'RISK_POLICY_MAP.{risk_type}' is missing "
                f"{', '.join(missing)}")
        risk_map[str(risk_type)] = {
            "policy_file": str(entry["policy_file"]).strip(),
            "clause_keyword": str(entry["clause_keyword"]).strip(),
        }

    config = PackConfig(
        pack_dir=pack_path,
        pack_id=str(raw.get("pack_id") or pack_path.name),
        departments=departments,
        policy_dept_rules=rules,
        risk_policy_map=risk_map,
    )
    _CONFIG_CACHE[stat_key] = config
    return config


# ---------------------------------------------------------------------------
# Pack documents
# ---------------------------------------------------------------------------

def iter_pack_documents(pack_dir: str | os.PathLike[str]) -> list[Path]:
    """Sorted ``<pack_dir>/*.md`` policy documents, minus pack documentation.

    Mirrors the loader's historical ``sorted(POLICY_VAULT.glob('*.md'))``
    (flat, sorted) — README.md and FORMAT.md are excluded because they are
    pack documentation, not policy documents.
    """
    pack_path = Path(pack_dir)
    return sorted(
        p for p in pack_path.glob("*.md")
        if p.is_file() and p.name not in PACK_DOC_FILENAMES
    )


def pack_content_hash(path: str | os.PathLike[str]) -> str:
    """sha256 of the document bytes — the pack revision fingerprint."""
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()


def document_clause_codes(reference: str, title: str, clause_numbers: Iterable[str]) -> list[str]:
    """Standard codes exactly as the loader derives them (see FORMAT.md)."""
    base = (reference or title).strip()
    return [f"{base}§{num}" for num in clause_numbers]


# ---------------------------------------------------------------------------
# School → pack resolution
# ---------------------------------------------------------------------------

def register_school_pack(school_id: str, pack_dir: str | os.PathLike[str]) -> Path:
    """Point a school at a pack for this process (tests / CLI runs)."""
    pack_path = Path(pack_dir)
    if not pack_path.is_dir():
        raise PackConfigError(f"pack directory not found: {pack_path}")
    _SCHOOL_PACKS[str(school_id)] = pack_path
    return pack_path


def clear_school_pack_registry() -> None:
    _SCHOOL_PACKS.clear()


def _env_pack_map() -> dict[str, Path]:
    raw = os.environ.get("MSOE_POLICY_PACK_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise PackConfigError(f"MSOE_POLICY_PACK_MAP: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise PackConfigError("MSOE_POLICY_PACK_MAP: must be a JSON object {school_id: path}")
    return {str(k): Path(str(v)) for k, v in data.items()}


def pack_dir_for_school(school_id: str | None) -> Path | None:
    """Resolve the pack directory governing ``school_id``, or ``None``.

    ``None`` means "this school has no pack" — callers must then leave the
    graph unlinked rather than borrowing another school's clauses.
    """
    sid = str(school_id or "default")
    if sid in _SCHOOL_PACKS:
        return _SCHOOL_PACKS[sid]
    env_map = _env_pack_map()
    if sid in env_map:
        return env_map[sid]
    if sid in ("default", ""):
        return default_pack_dir()
    return None


def risk_policy_entry(school_id: str | None, risk_type: str) -> dict[str, str] | None:
    """``{policy_file, clause_keyword}`` governing ``risk_type`` for a school.

    This is the single resolution point for the ADDRESSED_BY link.  Both
    ``build_knowledge_graph.link_risks_to_standards`` and
    ``sis_adapter.sync_students_to_graph`` call it, so a pack's clause
    headings can differ from MSOE's without the path breaking.
    """
    pack_dir = pack_dir_for_school(school_id)
    if pack_dir is None:
        return None
    try:
        config = load_pack_config(pack_dir)
    except PackConfigError:
        return None
    entry = config.risk_policy_map.get(str(risk_type))
    return dict(entry) if entry else None


def clause_keyword_for_risk(school_id: str | None, risk_type: str) -> str | None:
    entry = risk_policy_entry(school_id, risk_type)
    return entry["clause_keyword"] if entry else None


# ---------------------------------------------------------------------------
# The ADDRESSED_BY link — one query, used by both call sites
# ---------------------------------------------------------------------------
#
# Selection rule (FORMAT.md §3.3): among the Standards of the governing
# document, current revision only, take the lowest-numbered clause whose
# heading contains the clause_keyword; if none matches, fall back to that
# document's first clause.  ``build_knowledge_graph.link_risks_to_standards``
# and ``sis_adapter.sync_students_to_graph`` both call :func:`link_risk_to_clause`,
# so a client pack with different clause headings resolves the same way the
# MSOE reference pack does.

_ADDRESSED_BY_SELECT = """
    MATCH (rf:RiskFlag {type: $risk, school_id: $school_id})
    MATCH (s:Standard {school_id: $school_id})-[:PART_OF]->(p:Policy {school_id: $school_id})
    WHERE coalesce(p.status, 'active') = 'active'
      AND p.source_file = $policy_file + '.md'
@@WHERE_EXTRA@@
    WITH rf, s
    ORDER BY toInteger(s.clause_number) ASC
    LIMIT 1
    MERGE (rf)-[:ADDRESSED_BY]->(s)
    RETURN count(*) AS c
"""

# NOTE: plain replace, not str.format — the Cypher above is full of braces.
ADDRESSED_BY_CYPHER = _ADDRESSED_BY_SELECT.replace(
    "@@WHERE_EXTRA@@",
    "      AND toLower(s.heading) CONTAINS toLower($clause_keyword)")

ADDRESSED_BY_FALLBACK_CYPHER = _ADDRESSED_BY_SELECT.replace("@@WHERE_EXTRA@@", "")


def link_risk_to_clause(session: Any, risk_type: str,
                        school_id: str | None = "default") -> int:
    """MERGE the ``ADDRESSED_BY`` edge for one risk type.  Returns edge count.

    ``session`` is any object with a ``run(cypher, **params)`` returning a
    result with ``single()['c']`` — a Neo4j session.  Returns 0 (creating no
    edge) when the school has no pack or the risk is not in its
    ``RISK_POLICY_MAP``: nothing is invented.
    """
    entry = risk_policy_entry(school_id, risk_type)
    if not entry:
        return 0
    params = {
        "risk": str(risk_type),
        "school_id": str(school_id or "default"),
        "policy_file": entry["policy_file"],
        "clause_keyword": entry["clause_keyword"],
    }
    c = session.run(ADDRESSED_BY_CYPHER, **params).single()["c"]
    if c == 0:
        c = session.run(ADDRESSED_BY_FALLBACK_CYPHER,
                        risk=params["risk"],
                        school_id=params["school_id"],
                        policy_file=params["policy_file"]).single()["c"]
    return int(c)
