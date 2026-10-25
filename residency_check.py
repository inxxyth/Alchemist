"""Residency proof (Phase 6 / K.7) — the deployment must run where it claims.

Checks that ``MSOE_DATA_REGION`` is set to an allowed UAE Azure region
string and fails loudly if unset or unknown. Wired into ``GET /health``
(``data_region`` + ``residency`` block) and runnable standalone:

    python residency_check.py        # exit 0 = claim holds, exit 1 = loud fail

Allowed values name the Azure UAE geographies — UAE North (Dubai) and
UAE Central (Abu Dhabi) — see
https://azure.microsoft.com/en-us/explore/global-infrastructure/geographies/

This asserts a *configuration* claim, not an infrastructure attestation.
It says the deployment is configured for a UAE region; it does not
certify where any given byte physically resides. See
``docs/compliance/DATA-RESIDENCY.md``.
"""

from __future__ import annotations

import os
import sys

REGION_ENV = "MSOE_DATA_REGION"
# Normalised forms of "UAE North" / "UAE Central" (Azure geography names).
ALLOWED_REGIONS = ("uae-north", "uae-central")
ALLOWED_LABELS = {
    "uae-north": "Azure UAE North (Dubai)",
    "uae-central": "Azure UAE Central (Abu Dhabi)",
}


def _normalize(raw: str) -> str:
    return raw.strip().lower().replace("_", "-").replace(" ", "-")


def check_region(value: str | None = None) -> dict:
    """Return the residency claim as a non-secret dict for /health."""
    raw = value if value is not None else os.environ.get(REGION_ENV, "")
    raw = (raw or "").strip()
    configured = bool(raw)
    normalized = _normalize(raw) if configured else ""
    allowed = normalized in ALLOWED_REGIONS
    return {
        "env": REGION_ENV,
        "configured": configured,
        "value": raw or None,
        "allowed": allowed,
        "allowed_values": list(ALLOWED_REGIONS),
        "source": "Azure geographies (UAE North / UAE Central)",
    }


def verify() -> int:
    """CLI proof. Prints the result; exit 0 only when the claim holds."""
    result = check_region()
    if not result["configured"]:
        print(
            f"RESIDENCY CHECK FAILED: {REGION_ENV} is not set.\n"
            f"Set it to one of: {', '.join(ALLOWED_REGIONS)} "
            f"(Azure UAE North / UAE Central).",
            file=sys.stderr,
        )
        return 1
    if not result["allowed"]:
        print(
            f"RESIDENCY CHECK FAILED: {REGION_ENV}={result['value']!r} is not an "
            f"allowed UAE region.\nAllowed: {', '.join(ALLOWED_REGIONS)} "
            f"(Azure UAE North / UAE Central).",
            file=sys.stderr,
        )
        return 1
    label = ALLOWED_LABELS[_normalize(result["value"])]
    print(f"RESIDENCY OK: {REGION_ENV}={result['value']} — {label}")
    return 0


if __name__ == "__main__":
    sys.exit(verify())
