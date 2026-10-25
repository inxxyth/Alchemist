"""Client manifest — per-client YAML config for the connector framework.

Adding a client's SIS = new manifest + a connector module.  Never edits to
core service code (that is what stops the agency becoming a body shop).

**Manifests hold NAMES, never values.**  Credential fields list environment
variable *names*; the connector resolves the actual secret from the process
environment at call time.  The validator rejects value-shaped credential
fields so a real secret cannot quietly land in a committed manifest.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import yaml
except ImportError:  # PyYAML is only needed to *load* manifests
    yaml = None

SYNC_MODES = ("pull", "webhook", "batch", "sftp")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Credential-ish keys must hold env var NAMES (or be empty), never values.
SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|credential|private_key)$",
    re.IGNORECASE,
)


class ManifestError(ValueError):
    """Manifest failed validation.  Message names every violation."""


@dataclass
class ClientManifest:
    """Parsed + validated client manifest."""

    client_id: str
    dialect: str
    raw: dict[str, Any] = field(default_factory=dict)
    school_id_map: dict[str, str] = field(default_factory=dict)
    credential_env_names: list[str] = field(default_factory=list)
    sync_mode: str = "pull"
    sync_schedule: str = ""
    policy_pack_path: str = ""
    engagement_id: str = ""
    currency: str = ""
    org_name: str = ""
    path: str = ""

    def resolve_school_id(self, source_school: Optional[str] = None) -> str:
        """Map a source-native school/organisation id onto our school_id."""
        if not source_school:
            # single-school manifests: the first map value is the school_id
            return next(iter(self.school_id_map.values()), "default")
        return self.school_id_map.get(str(source_school), str(source_school))

    def credentials(self) -> dict[str, str]:
        """Resolve credential env-var NAMES to values from the process env.

        Missing env vars are simply absent from the result — the connector
        reports unhealthy rather than crashing.  No value is ever logged.
        """
        out: dict[str, str] = {}
        for name in self.credential_env_names:
            value = os.environ.get(name)
            if value:
                out[name] = value
        return out

    def missing_credentials(self) -> list[str]:
        return [n for n in self.credential_env_names if not os.environ.get(n)]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_manifest_dict(data: dict[str, Any], source: str = "<manifest>") -> list[str]:
    """Return a list of violations (empty list == valid)."""
    from connectors import registered_dialects  # local import — registry knows the names

    problems: list[str] = []

    def bad(msg: str) -> None:
        problems.append(f"{source}: {msg}")

    if not isinstance(data, dict):
        return [f"{source}: manifest must be a mapping"]

    client_id = data.get("client_id")
    if not client_id or not isinstance(client_id, str):
        bad("client_id is required and must be a non-empty string")

    dialect = data.get("dialect")
    known = registered_dialects()
    if not dialect or not isinstance(dialect, str):
        bad("dialect is required and must be a non-empty string")
    elif dialect not in known:
        bad(f"dialect '{dialect}' is not a registered connector (known: {sorted(known)})")

    school_id_map = data.get("school_id_map")
    if school_id_map is None:
        bad("school_id_map is required (map source school/organisation id -> school_id)")
    elif not isinstance(school_id_map, dict):
        bad("school_id_map must be a mapping")
    else:
        for k, v in school_id_map.items():
            if not isinstance(v, str) or not v:
                bad(f"school_id_map['{k}'] must be a non-empty string")

    creds = data.get("credential_env_names")
    if creds is None:
        bad("credential_env_names is required (list of env var NAMES — may be empty)")
    elif not isinstance(creds, list):
        bad("credential_env_names must be a list")
    else:
        for name in creds:
            if not isinstance(name, str) or not ENV_NAME_RE.match(name):
                bad(f"credential_env_names entry {name!r} must look like an env var name (A-Z0-9_)")

    sync = data.get("sync")
    if sync is None:
        bad("sync is required (mapping with mode + schedule)")
    elif not isinstance(sync, dict):
        bad("sync must be a mapping")
    else:
        mode = sync.get("mode")
        if mode not in SYNC_MODES:
            bad(f"sync.mode '{mode}' must be one of {SYNC_MODES}")
        if "schedule" not in sync:
            bad("sync.schedule is required (cron-style string; '' for on-demand)")

    if "policy_pack" not in data:
        bad("policy_pack is required (mapping with path)")
    else:
        pack = data["policy_pack"]
        if not isinstance(pack, dict) or not isinstance(pack.get("path"), str):
            bad("policy_pack.path must be a string")

    for key in ("engagement_id", "currency", "org_name"):
        if key in data and not isinstance(data[key], str):
            bad(f"{key} must be a string")

    # Manifests hold NAMES, never values — reject secret-shaped literals.
    for key, value in _walk_strings(data):
        if SECRET_KEY_RE.search(key) and value and not ENV_NAME_RE.match(value):
            bad(f"field '{key}' looks like a secret value; manifests must name env vars only")

    return problems


def _walk_strings(node: Any, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_strings(value, path)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, f"{prefix}[{i}]")
    elif isinstance(node, str):
        yield prefix, node


def load_manifest(path: str) -> ClientManifest:
    """Load + validate a client manifest YAML file.

    Raises :class:`ManifestError` naming every violation.
    """
    if yaml is None:
        raise ManifestError("PyYAML is required to load manifests (pip install PyYAML)")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    problems = validate_manifest_dict(data, source=os.path.basename(path))
    if problems:
        raise ManifestError("; ".join(problems))
    sync = data.get("sync") or {}
    pack = data.get("policy_pack") or {}
    return ClientManifest(
        client_id=str(data["client_id"]),
        dialect=str(data["dialect"]),
        raw=data,
        school_id_map=dict(data.get("school_id_map") or {}),
        credential_env_names=list(data.get("credential_env_names") or []),
        sync_mode=str(sync.get("mode") or "pull"),
        sync_schedule=str(sync.get("schedule") or ""),
        policy_pack_path=str(pack.get("path") or ""),
        engagement_id=str(data.get("engagement_id") or ""),
        currency=str(data.get("currency") or ""),
        org_name=str(data.get("org_name") or ""),
        path=path,
    )


def load_manifest_dir(directory: str) -> dict[str, ClientManifest]:
    """Load every ``*.yml`` / ``*.yaml`` manifest in a directory."""
    out: dict[str, ClientManifest] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yml", ".yaml")):
            continue
        manifest = load_manifest(os.path.join(directory, name))
        out[manifest.client_id] = manifest
    return out
