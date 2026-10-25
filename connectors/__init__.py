"""Connector registry — dialect name → SourceConnector class.

Registry entries are **dotted class paths**, imported lazily on first use.
That keeps :mod:`connectors` import-safe from :mod:`sis_adapter` (which loads
the per-dialect field maps from here) without an import cycle: connector
implementations may import ``sis_adapter`` freely, ``sis_adapter`` never has
to import an implementation.

Registry contract (C.1 / C.6)
-----------------------------
* ``registered_dialects()``  — every accepted ``dialect`` value
* ``get_connector_class()``  — resolve + import one connector
* ``create()``               — construct with a school_id / credentials
* ``load_manifest()``        — per-client YAML config (re-exported)
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Optional

from connectors.base import CanonicalBatch, SourceConnector
from connectors.manifest import (
    ClientManifest,
    ManifestError,
    load_manifest,
    load_manifest_dir,
    validate_manifest_dict,
)

__all__ = [
    "CanonicalBatch",
    "SourceConnector",
    "ClientManifest",
    "ManifestError",
    "load_manifest",
    "load_manifest_dir",
    "validate_manifest_dict",
    "register",
    "registered_dialects",
    "get_connector_class",
    "create",
    "detect_dialect",
    "connector_health",
]

# dialect -> dotted path of the SourceConnector subclass
REGISTRY: dict[str, str] = {
    # Tier 2 natives (Gibbon is connector #0 — the one already running)
    "gibbon": "connectors.gibbon.connector.GibbonConnector",
    "isams": "connectors.isams.connector.ISAMSConnector",
    "powerschool": "connectors.powerschool.connector.PowerSchoolConnector",
    # Legacy dialect field maps (source-shaped payloads, still accepted)
    "openeducat": "connectors.openeducat.connector.OpenEduCatConnector",
    "canvas": "connectors.canvas.connector.CanvasConnector",
    # Tier 1 standards — the market wedge
    "oneroster-csv": "connectors.oneroster.csv_binding.OneRosterCsvConnector",
    "oneroster-rest": "connectors.oneroster.rest_binding.OneRosterRestConnector",
    "graph_education": "connectors.graph_education.connector.GraphEducationConnector",
    # Tier 3 universal fallback — "our IT won't do an API"
    "csv_drop": "connectors.csv_drop.connector.CsvDropConnector",
}


def register(dialect: str, class_path: str) -> None:
    """Register a connector under ``dialect`` (used by tests / new modules)."""
    if not dialect:
        raise ValueError("dialect must be non-empty")
    REGISTRY[dialect] = class_path


def registered_dialects() -> list[str]:
    return sorted(REGISTRY.keys())


def get_connector_class(dialect: str) -> type:
    """Import + return the connector class for ``dialect``."""
    class_path = REGISTRY.get(dialect)
    if class_path is None:
        raise KeyError(
            f"no connector registered for dialect '{dialect}' "
            f"(known: {registered_dialects()})"
        )
    module_name, _, class_name = class_path.rpartition(".")
    module = import_module(module_name)
    return getattr(module, class_name)


def create(dialect: str, school_id: str = "default",
           credential_env_names: Optional[list[str]] = None,
           **config: Any) -> SourceConnector:
    """Instantiate a registered connector.

    Credentials are never passed as values here — the connector resolves them
    from the env var NAMES in ``credential_env_names`` (see
    :meth:`ClientManifest.credentials`).
    """
    cls = get_connector_class(dialect)
    return cls(school_id=school_id,
               credential_env_names=credential_env_names or [],
               **config)


def detect_dialect(payload: dict[str, Any]) -> Optional[str]:
    """Ask every registered connector whether ``payload`` is its dialect.

    First match wins.  Returns None when nothing claims the payload.
    """
    for dialect in REGISTRY:
        try:
            cls = get_connector_class(dialect)
        except Exception:
            continue
        try:
            if cls.detect(payload):
                return dialect
        except Exception:
            continue
    return None


def connector_health(school_ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
    """Health snapshot for every registered connector (never raises).

    Connectors without live config report ``NOT_CONFIGURED`` — present in
    the status list either way so a client's IT lead can see what exists.

    N.5: every connector carries a per-``school_id`` block —
    ``last_success_at``, ``last_error``, ``rows_in_last_sync``, ``watermark``,
    ``mode`` — assembled from the durable run record (survives restarts) with
    the probe's in-process view merged on top.
    """
    from connectors.state import default_store, read_sync_run

    store = default_store()
    extra_ids = [str(s) for s in (school_ids or []) if s]
    out: dict[str, dict[str, Any]] = {}
    for dialect in REGISTRY:
        try:
            cls = get_connector_class(dialect)
            probe = cls(school_id="default", state_store=store)
            health = probe.health()
        except Exception as exc:  # a broken connector must not break /status
            out[dialect] = {
                "dialect": dialect,
                "status": "UNHEALTHY",
                "last_error": f"{type(exc).__name__}: {exc}"[:300],
                "schools": {},
            }
            continue

        # Which schools does this connector know about?  The caller's list
        # (live schools) ∪ whatever the state store has seen ∪ 'default'.
        try:
            known = set(store.list_school_ids(dialect))
        except Exception:
            known = set()
        known.update(extra_ids)
        known.add("default")
        known.add(str(health.get("school_id") or "default"))

        schools: dict[str, dict[str, Any]] = {}
        for sid in sorted(known):
            record = read_sync_run(store, dialect, sid)
            # In-process view wins when fresher (the probe is 'default').
            if sid == str(health.get("school_id") or "default"):
                for key in ("last_success_at", "last_error", "rows_in_last_sync",
                            "watermark", "mode"):
                    if health.get(key) not in (None, "", {}):
                        record[key] = health.get(key)
            record.setdefault("mode", health.get("mode", "pull"))
            record["mode"] = record.get("mode") or health.get("mode", "pull")
            schools[sid] = record
        health["schools"] = schools
        out[dialect] = health
    return out
