"""OneRoster 1.2 REST binding (C.5) — OAuth2 client-credentials consumer.

Same canonical mapping as the CSV binding; this is the transport layer for
providers that expose the OneRoster REST API (PowerSchool, iSAMS and anything
certified — see INTEGRATION-PLAN.md §3).

Contract
--------
* Auth: OAuth2 client-credentials against the provider's token endpoint,
  scope ``https://purl.imsglobal.org/spec/oneroster/v1p2/scope/roster.readonly``
  (overridable).  Env var **names** come from the client manifest; values come
  from the process environment at call time.
* Endpoints: ``/orgs /users /classes /courses /enrollments /demographics``
* Paging: ``limit``/``offset`` and/or RFC 5988 ``Link: <...>; rel="next"``.
* Delta: ``since`` query param where the provider offers it (ignored otherwise).
* Timeout + retry with exponential backoff.  On failure the connector
  reports **unhealthy** and the core services stay up — same degrade rule
  as DeterministicLLM.

The transport is injectable (``transport=``) so tests never open a socket.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from connectors.base import CanonicalBatch, SourceConnector
from connectors.oneroster.csv_binding import map_bulk
from connectors.oneroster.field_maps import DETECT_KEYS

DEFAULT_SCOPE = "https://purl.imsglobal.org/spec/oneroster/v1p2/scope/roster.readonly"
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_RETRIES = 3

# OneRoster REST resource → bulk-set key used by map_bulk
RESOURCES = (
    ("orgs", "orgs"),
    ("users", "users"),
    ("classes", "classes"),
    ("courses", "courses"),
    ("enrollments", "enrollments"),
    ("demographics", "demographics"),
)

# JSON envelope: {"users": [...]} / {"classes": [...]} / …
ENVELOPE_KEYS = {
    "orgs": ("orgs", "org", "academicSessions"),
    "users": ("users", "user"),
    "classes": ("classes", "class"),
    "courses": ("courses", "course"),
    "enrollments": ("enrollments", "enrollment"),
    "demographics": ("demographics", "demographic"),
}

Transport = Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def _urllib_transport(url: str, headers: dict[str, str],
                      timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


class OneRosterRestError(RuntimeError):
    """REST transport failed after retries."""


class OneRosterRestConnector(SourceConnector):
    """OneRoster 1.2 REST binding.

    Config keys:
      ``base_url``            — provider OneRoster root
                                (…/ims/oneroster/v1p2)  [env: ONEROSTER_BASE_URL]
      ``token_url``           — OAuth2 token endpoint   [env: ONEROSTER_TOKEN_URL]
      ``client_id_env``       — env var NAME holding the client id
      ``client_secret_env``   — env var NAME holding the client secret
      ``scope``               — OAuth2 scope (default: roster.readonly)
      ``page_size``           — limit/offset page size (default 100)
      ``timeout``             — per-request timeout seconds (default 10)
      ``retries``             — attempts per request (default 3)
      ``transport``           — injectable GET transport (tests)
    """

    dialect = "oneroster-rest"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.rows_in_last_sync: dict[str, int] = {}
        self.watermark: Optional[str] = None
        self.transport: Transport = config.get("transport") or _urllib_transport

    # -- config helpers -----------------------------------------------------

    def _env_name(self, key: str, default: str) -> str:
        return str(self.config.get(key) or default)

    def _base_url(self) -> str:
        # Explicit config wins: the PowerSchool profile passes its own roots
        # and must not be hijacked by a process-wide ONEROSTER_BASE_URL.
        return str(self.config.get("base_url") or "") or os.environ.get("ONEROSTER_BASE_URL", "")

    def _token_url(self) -> str:
        return str(self.config.get("token_url") or "") or os.environ.get("ONEROSTER_TOKEN_URL", "")

    def _client_id(self) -> str:
        name = self._env_name("client_id_env", "ONEROSTER_CLIENT_ID")
        return os.environ.get(name, "")

    def _client_secret(self) -> str:
        name = self._env_name("client_secret_env", "ONEROSTER_CLIENT_SECRET")
        return os.environ.get(name, "")

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """OAuth2 client-credentials grant.  Cached until near expiry."""
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        token_url = self._token_url()
        client_id, secret = self._client_id(), self._client_secret()
        if not token_url or not client_id or not secret:
            raise OneRosterRestError(
                "OAuth2 client-credentials not configured "
                "(need token_url + client id/secret env vars)")
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": str(self.config.get("scope") or DEFAULT_SCOPE),
        }).encode()
        req = urllib.request.Request(
            token_url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(
                    req, timeout=float(self.config.get("timeout") or DEFAULT_TIMEOUT_S)) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            raise OneRosterRestError(f"token request failed: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise OneRosterRestError("token response has no access_token")
        self._token = str(token)
        # 'expires_in' is seconds; default to 5 minutes when absent
        self._token_expires_at = now + float(payload.get("expires_in") or 300)
        return self._token

    # -- transport ----------------------------------------------------------

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        """GET with timeout + retry/backoff.  Retries 5xx and transport errors."""
        token = self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        timeout = float(self.config.get("timeout") or DEFAULT_TIMEOUT_S)
        retries = int(self.config.get("retries") or DEFAULT_RETRIES)
        last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                status, resp_headers, body = self.transport(url, headers, timeout)
                if status < 500:
                    return status, resp_headers, body
                last_exc = OneRosterRestError(f"HTTP {status} from {url}")
            except Exception as exc:  # timeout, DNS, connection reset…
                last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s …
        raise OneRosterRestError(f"GET {url} failed after {retries} attempts: {last_exc}")

    @staticmethod
    def _next_link(headers: dict[str, str]) -> Optional[str]:
        """RFC 5988 Link header with rel="next", if the provider sent one."""
        link = headers.get("Link") or headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part or "rel=next" in part:
                start, end = part.find("<"), part.find(">")
                if 0 <= start < end:
                    return part[start + 1:end]
        return None

    def _fetch_resource(self, resource: str, since: Optional[str]) -> list[dict[str, Any]]:
        """One resource with paging (limit/offset **and** Link rel=next)."""
        base = self._base_url().rstrip("/")
        if not base:
            raise OneRosterRestError("base_url / ONEROSTER_BASE_URL is not configured")
        page_size = int(self.config.get("page_size") or DEFAULT_PAGE_SIZE)
        rows: list[dict[str, Any]] = []
        offset = 0
        url: Optional[str] = f"{base}/{resource}?limit={page_size}&offset={offset}"
        if since:
            # Provider-offered delta filter; ignored where unsupported.
            url += f"&since={urllib.parse.quote(since)}"
        seen_urls: set[str] = set()
        while url:
            if url in seen_urls:
                break  # defensive — a broken Link loop must not spin forever
            seen_urls.add(url)
            status, headers, body = self._get(url)
            if status == 404:
                return rows  # provider does not expose this resource
            if status in (401, 403):
                raise OneRosterRestError(f"HTTP {status} authorising {resource}")
            if status >= 400:
                raise OneRosterRestError(f"HTTP {status} from {url}: {body[:200]!r}")
            payload = json.loads(body.decode() or "{}")
            chunk = self._unwrap_envelope(resource, payload)
            rows.extend(chunk)
            next_link = self._next_link(headers)
            if next_link:
                url = next_link
            elif len(chunk) >= page_size:
                offset += page_size
                url = f"{base}/{resource}?limit={page_size}&offset={offset}"
                if since:
                    url += f"&since={urllib.parse.quote(since)}"
            else:
                url = None
        return rows

    @staticmethod
    def _unwrap_envelope(resource: str, payload: Any) -> list[dict[str, Any]]:
        """Pull the object list out of the OneRoster JSON envelope.

        ``{"users": [ … ]}`` (standard); a bare list is also tolerated.  Any
        other envelope is a contract error — named, not swallowed.
        """
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            raise OneRosterRestError(
                f"{resource}: response envelope must be an object or list, "
                f"got {type(payload).__name__}")
        for key in ENVELOPE_KEYS.get(resource, (resource,)):
            if key in payload and isinstance(payload[key], list):
                return [r for r in payload[key] if isinstance(r, dict)]
        raise OneRosterRestError(
            f"{resource}: response envelope has none of {ENVELOPE_KEYS.get(resource)} "
            f"(keys: {sorted(payload.keys())[:8]})")

    # -- SourceConnector ----------------------------------------------------

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        if payload.get("dialect") == "oneroster-rest":
            return True
        if payload.get("binding") == "rest":
            return bool(DETECT_KEYS & set(payload.keys()))
        return False

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Pull every rostering resource, then map to canonical entities."""
        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        try:
            files: dict[str, list[dict[str, Any]]] = {}
            for resource, key in RESOURCES:
                try:
                    rows = self._fetch_resource(resource, since)
                except OneRosterRestError as exc:
                    # Optional resources degrade; core roster must succeed.
                    if resource in ("orgs", "users"):
                        raise
                    batch.errors.append(f"{resource}: {exc}")
                    rows = []
                files[key] = rows
                self.rows_in_last_sync[resource] = len(rows)
            mapped = map_bulk(files, school_id=self.school_id,
                              school_id_map=self.config.get("school_id_map") or {},
                              source=self.dialect)
            mapped.errors.extend(batch.errors)
            mapped.meta.update(self.rows_in_last_sync)
            self.watermark = since or self.last_success_at
            self._mark_success()
            return mapped
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """REST notification bodies arrive as the same JSON envelope shapes."""
        try:
            files = {}
            for resource, key in RESOURCES:
                try:
                    files[key] = self._unwrap_envelope(resource, payload) \
                        if any(k in payload for k in ENVELOPE_KEYS.get(resource, ())) \
                        or key in payload else []
                except OneRosterRestError:
                    files[key] = []
            if not any(files.values()) and isinstance(payload.get("data"), dict):
                return self.webhook(payload["data"])
            if not any(files.values()):
                raise OneRosterRestError("payload carries no OneRoster resources")
            batch = map_bulk(files, school_id=self.school_id,
                             school_id_map=self.config.get("school_id_map") or {},
                             source=self.dialect)
            self._mark_success()
            return batch
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["status"] = "OK"
        base["mode"] = "pull"
        base["rows_in_last_sync"] = dict(self.rows_in_last_sync)
        base["watermark"] = self.watermark
        missing = []
        if not self._base_url():
            missing.append("ONEROSTER_BASE_URL")
        if not self._token_url():
            missing.append("ONEROSTER_TOKEN_URL")
        if not self._client_id():
            missing.append(self._env_name("client_id_env", "ONEROSTER_CLIENT_ID"))
        if not self._client_secret():
            missing.append(self._env_name("client_secret_env", "ONEROSTER_CLIENT_SECRET"))
        if missing:
            base["status"] = "NOT_CONFIGURED"
            base["missing_env"] = missing
        elif self.last_error:
            base["status"] = "UNHEALTHY"
        return base
