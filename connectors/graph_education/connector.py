"""Microsoft Graph Education / SDS connector (Phase 3, G.1–G.4).

The route that works when IT will only grant a Graph application permission
and will not open the SIS at all.  Where the school already runs **School
Data Sync**, the roster is already in Entra ID and this connector never
touches the SIS — see INTEGRATION-PLAN.md §3.1 for the principal-facing note.

Endpoints consumed (Microsoft Graph v1.0, read-only):
    /education/schools
    /education/users                       (primaryRole: student | teacher)
    /education/classes
    /education/classes/{id}/members
    /education/classes/{id}/assignments    (optional — NOT stored, see G.4)

Auth: OAuth2 client-credentials (app-only) against
``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`` with scope
``https://graph.microsoft.com/.default``.

**Least privilege (G.2):** this module only ever GETs from
``graph.microsoft.com``.  There is no write path — the engine reads rosters
and stages actions for human approval; it never writes back to the school's
directory.  ``test_graph_education.py`` asserts this on every recorded
request.

Env var NAMES (never values):
    MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET,
    MS_GRAPH_SCHOOL_ID_MAP   (JSON: {"graph-school-id": "our-school-id"})

Delta (G.3): ``@odata.deltaLink`` is recorded per school_id in the
:class:`connectors.state.StateStore` so a second pull is incremental; where
the provider offers no delta we page with ``$top`` / ``$skiptoken``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from connectors.base import CanonicalBatch, SourceConnector, as_optional_date, normalize_grade
from connectors.graph_education.field_maps import (
    CLASS_FIELD_MAP,
    DETECT_KEYS,
    ORG_FIELD_MAP,
    STAFF_FIELD_MAP,
    STUDENT_FIELD_MAP,
    STUDENT_PROFILE_FIELD_MAP,
    STUDENT_ROLES,
    STAFF_ROLES,
    _GENDER_MAP,
)
from connectors.state import MemoryStateStore, StateStore, default_store

# Graph API root.  Every call to this host is a GET (G.2 — least privilege).
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# OAuth2 scope for app-only roster reads.  NOT a call — the token endpoint is
# login.microsoftonline.com (see _access_token).
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RETRIES = 3

# (path, key) pairs walked by pull(); assignments intentionally absent (G.4).
RESOURCES = (
    ("education/schools", "schools"),
    ("education/users", "users"),
    ("education/classes", "classes"),
)

DELTA_STATE_KEY = "delta_link"

Transport = Callable[[str, str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


class GraphEducationError(RuntimeError):
    """Graph transport failed after retries, or the envelope broke the contract."""


# ---------------------------------------------------------------------------
# Mapping: Graph objects → canonical batch
# ---------------------------------------------------------------------------

def _apply(field_map: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dest in field_map.items():
        if src in row and row[src] not in (None, ""):
            out[dest] = row[src]
    return out


def _normalize_gender(raw: Any) -> str:
    return _GENDER_MAP.get(str(raw or "").strip().lower(), "U")


def map_graph_objects(
    schools: list[dict[str, Any]],
    users: list[dict[str, Any]],
    classes: list[dict[str, Any]],
    members_by_class: Optional[dict[str, list[dict[str, Any]]]] = None,
    school_id: str = "default",
    school_id_map: Optional[dict[str, str]] = None,
    source: str = "graph_education",
) -> CanonicalBatch:
    """Map Graph education roster objects onto :class:`CanonicalBatch`.

    * ``educationUser`` (primaryRole=student) → Student (+ student{} profile)
    * ``educationUser`` (primaryRole=teacher) → Staff
    * ``educationClass`` / class membership   → grade context only
    * assignments/submissions                 → never stored (G.4)
    """
    school_id_map = school_id_map or {}
    members_by_class = members_by_class or {}
    batch = CanonicalBatch(source=source, school_id=school_id)
    errors = batch.errors

    orgs = {}
    for raw in schools:
        mapped = _apply(ORG_FIELD_MAP, raw)
        orgs[str(mapped.get("source_org_id") or raw.get("id"))] = mapped

    class_grade: dict[str, int] = {}
    class_rows: dict[str, dict[str, Any]] = {}
    for raw in classes:
        mapped = _apply(CLASS_FIELD_MAP, raw)
        cid = str(mapped.get("source_class_id") or raw.get("id") or "")
        if not cid:
            errors.append("classes: object missing id")
            continue
        class_rows[cid] = mapped
        if mapped.get("grade_level"):
            class_grade[cid] = normalize_grade(mapped["grade_level"], default=0)

    # user id → grade + school hints from class membership
    # (educationUser carries no direct school/grade — enrolment does).
    grade_by_user: dict[str, int] = {}
    school_hint_by_user: dict[str, str] = {}
    for cid, members in members_by_class.items():
        grade = class_grade.get(cid, 0)
        school_hint = str((class_rows.get(cid) or {}).get("school_id_hint") or "")
        for member in members:
            uid = str(member.get("id") or "")
            if not uid:
                continue
            if grade:
                grade_by_user[uid] = grade
            if school_hint:
                school_hint_by_user[uid] = school_hint

    # Graph school → our school_id (map lookup; unmapped hints keep the
    # connector's school_id — same semantics as the OneRoster connector).
    def resolve_school(obj: dict[str, Any], uid: str = "") -> str:
        hint = obj.get("schoolId") or (obj.get("school") or {}).get("id") \
            or school_hint_by_user.get(uid)
        if hint is not None and str(hint) in school_id_map:
            return school_id_map[str(hint)]
        return school_id

    for raw in users:
        role = str(raw.get("primaryRole") or "").strip().lower()
        uid = str(raw.get("id") or "")
        if not uid:
            errors.append("users: object missing id")
            continue
        profile = raw.get("student") if isinstance(raw.get("student"), dict) else {}
        pmap = _apply(STUDENT_PROFILE_FIELD_MAP, profile)
        if role in STUDENT_ROLES:
            mapped = _apply(STUDENT_FIELD_MAP, raw)
            grade = normalize_grade(
                mapped.get("grade_level") or pmap.get("grade_level"),
                default=grade_by_user.get(uid, 1),
            )
            org = next(iter(orgs.values()), {})
            canonical = {
                "school_id": resolve_school(raw, uid),
                "source_student_id": uid,
                # No national-ID column in Graph roster — leave NULL (F.4 rule:
                # never fabricate an id that occupies a unique key).
                "emirates_id": None,
                "first_name": mapped.get("first_name") or "Unknown",
                "last_name": mapped.get("last_name") or "Student",
                "gender": _normalize_gender(pmap.get("gender") or raw.get("gender")),
                "date_of_birth": as_optional_date(pmap.get("date_of_birth")),
                "grade_level": grade,
                "curriculum_track": raw.get("curriculumTrack") or "British",
                "school_name": org.get("school_name") or "Microsoft Graph School",
                "emirate": raw.get("emirate") or "Dubai",
                "nationality": raw.get("nationality") or "UAE",
                "parent_full_name": raw.get("parentName") or "Parent/Guardian",
                "parent_email": mapped.get("parent_email") or "",
                "parent_phone": raw.get("phone") or "",
                "vaccination_status": raw.get("vaccination_status") or "up_to_date",
                "vaccination_expiry": as_optional_date(raw.get("vaccination_expiry")),
                "enrollment_date": as_optional_date(raw.get("enrollment_date")),
                "is_active": bool(raw.get("accountEnabled", True))
                             and str(raw.get("status") or "active").lower() != "tobedeleted",
            }
            batch.students.append(canonical)
        elif role in STAFF_ROLES:
            mapped = _apply(STAFF_FIELD_MAP, raw)
            batch.staff.append({
                "school_id": resolve_school(raw, uid),
                "source_staff_id": uid,
                "full_name": mapped.get("display_name")
                             or f"{mapped.get('first_name') or ''} "
                                f"{mapped.get('last_name') or ''}".strip()
                             or "Teacher",
                "email": mapped.get("email") or "",
                "role": "teacher" if role == "teacher" else role,
                "subjects": [],
                "is_available": bool(raw.get("accountEnabled", True)),
                "max_periods": 6,
                "assigned": 0,
            })
        else:
            errors.append(f"users: object {uid!r} has unknown primaryRole "
                          f"{raw.get('primaryRole')!r}")

    batch.meta["class_count"] = len(class_rows)
    batch.meta["school_count"] = len(orgs)
    return batch


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class GraphEducationConnector(SourceConnector):
    """Microsoft Graph Education roster consumer (read-only).

    Config keys:
      ``transport``      — injectable transport (tests)
      ``state_store``    — delta-token store (default: Postgres, else memory)
      ``school_id_map``  — {"graph-school-id": "our-school-id"}
      ``page_size``      — $top page size (default 100)
      ``include_assignments`` — default False; assignments are never stored
    """

    dialect = "graph_education"

    def __init__(self, school_id: str = "default",
                 credential_env_names: Optional[list[str]] = None,
                 **config: Any) -> None:
        super().__init__(school_id, credential_env_names, **config)
        self.transport: Transport = config.get("transport") or _urllib_transport
        self.state_store: StateStore = config.get("state_store") or MemoryStateStore()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.rows_in_last_sync: dict[str, int] = {}
        self.delta_link: Optional[str] = None
        self._store_kind = type(self.state_store).__name__
        if "state_store" not in config and "transport" not in config:
            # Production default: durable store (reported in health()).
            self.state_store = default_store()
            self._store_kind = type(self.state_store).__name__

    # -- credentials (env NAMES only) ---------------------------------------

    def _school_id_map(self) -> dict[str, str]:
        raw = os.environ.get("MS_GRAPH_SCHOOL_ID_MAP") or ""
        env_map: dict[str, str] = {}
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    env_map = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                pass  # health() reports it; never crash a pull over config
        config_map = self.config.get("school_id_map") or {}
        return {**env_map, **{str(k): str(v) for k, v in config_map.items()}}

    def _missing_env(self) -> list[str]:
        names = ["MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET"]
        return [n for n in names if not os.environ.get(n)]

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """OAuth2 client-credentials (app-only).  POST to the login host —
        this is the only non-GET call in the module, and it never targets
        graph.microsoft.com."""
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        missing = self._missing_env()
        if missing:
            raise GraphEducationError(
                "Graph app credentials not configured (need env vars: "
                + ", ".join(missing) + ")")
        tenant = os.environ["MS_GRAPH_TENANT_ID"]
        token_url = (
            f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}"
            f"/oauth2/v2.0/token"
        )
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": os.environ["MS_GRAPH_CLIENT_ID"],
            "client_secret": os.environ["MS_GRAPH_CLIENT_SECRET"],
            "scope": GRAPH_SCOPE,
        }).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _resp_headers, raw = self.transport("POST", token_url, headers,
                                                    self._timeout())
        if status != 200:
            raise GraphEducationError(f"token request failed: HTTP {status}")
        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError as exc:
            raise GraphEducationError("token response is not JSON") from exc
        token = payload.get("access_token")
        if not token:
            raise GraphEducationError("token response has no access_token")
        self._token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in") or 600)
        return self._token

    # -- transport ----------------------------------------------------------

    def _timeout(self) -> float:
        return float(self.config.get("timeout") or DEFAULT_TIMEOUT_S)

    def _graph_get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        """The ONE call site that talks to Graph.  GET only (G.2)."""
        if not url.startswith(GRAPH_BASE):
            raise GraphEducationError(f"refusing non-Graph URL: {url[:80]}")
        token = self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        retries = int(self.config.get("retries") or DEFAULT_RETRIES)
        last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                status, resp_headers, body = self.transport(
                    "GET", url, headers, self._timeout())
                if status < 500:
                    return status, resp_headers, body
                last_exc = GraphEducationError(f"HTTP {status} from {url}")
            except Exception as exc:
                last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        raise GraphEducationError(f"GET {url} failed after {retries} attempts: {last_exc}")

    def _get_json(self, url: str) -> dict[str, Any]:
        status, _resp_headers, body = self._graph_get(url)
        if status in (401, 403):
            raise GraphEducationError(f"HTTP {status} authorising {url}")
        if status == 404:
            return {"value": []}
        if status >= 400:
            raise GraphEducationError(f"HTTP {status} from {url}: {body[:200]!r}")
        try:
            payload = json.loads(body.decode() or "{}")
        except ValueError as exc:
            raise GraphEducationError(f"{url}: response is not JSON") from exc
        if not isinstance(payload, dict):
            raise GraphEducationError(
                f"{url}: envelope must be an object, got {type(payload).__name__}")
        return payload

    def _fetch_collection(self, url: str) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Walk one collection following ``@odata.nextLink``.

        Returns (rows, delta_link).  ``@odata.deltaLink`` is captured from the
        final page when the provider offers delta (G.3).
        """
        rows: list[dict[str, Any]] = []
        delta_link: Optional[str] = None
        seen: set[str] = set()
        while url:
            if url in seen:
                break  # a broken Link loop must not spin forever
            seen.add(url)
            payload = self._get_json(url)
            chunk = payload.get("value")
            if not isinstance(chunk, list):
                raise GraphEducationError(
                    f"{url}: envelope has no 'value' array "
                    f"(keys: {sorted(payload.keys())[:8]})")
            rows.extend(r for r in chunk if isinstance(r, dict))
            delta_link = payload.get("@odata.deltaLink") or delta_link
            url = payload.get("@odata.nextLink")
        return rows, delta_link

    # -- SourceConnector ----------------------------------------------------

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        if payload.get("dialect") == "graph_education":
            return True
        if payload.get("source") == "graph_education":
            return True
        keys = set(payload.keys())
        return bool(DETECT_KEYS & keys) and "primaryRole" in keys

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        """Full roster pull, or incremental when a delta link is recorded.

        ``since`` is used only as the initial delta anchor when the provider
        offers ``@odata.deltaLink``; the link itself is what makes the second
        pull incremental.
        """
        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        try:
            stored = self.state_store.get(self.dialect, self.school_id, DELTA_STATE_KEY)
            self.delta_link = stored

            files: dict[str, list[dict[str, Any]]] = {"schools": [], "users": [], "classes": []}
            new_delta: Optional[str] = None

            if stored:
                # Incremental: the delta link carries $skiptoken state.
                rows, new_delta = self._fetch_collection(stored)
                files["users"] = rows
                self.rows_in_last_sync["users_delta"] = len(rows)
            else:
                page = int(self.config.get("page_size") or DEFAULT_PAGE_SIZE)
                for path, key in RESOURCES:
                    url = f"{GRAPH_BASE}/{path}?$top={page}"
                    if since:
                        url += f"&$filter=lastModifiedDateTime ge {urllib.parse.quote(since)}"
                    try:
                        rows, link = self._fetch_collection(url)
                    except GraphEducationError as exc:
                        if key == "users":
                            raise
                        batch.errors.append(f"{path}: {exc}")
                        rows, link = [], None
                    files[key] = rows
                    self.rows_in_last_sync[key] = len(rows)
                    new_delta = link or new_delta

                # Class membership → grade context (educationEnrollment shape)
                members_by_class: dict[str, list[dict[str, Any]]] = {}
                for cls in files["classes"]:
                    cid = str(cls.get("id") or "")
                    if not cid:
                        continue
                    try:
                        members, _ = self._fetch_collection(
                            f"{GRAPH_BASE}/education/classes/{cid}/members?$top={page}")
                        members_by_class[cid] = members
                        self.rows_in_last_sync[f"members:{cid}"] = len(members)
                    except GraphEducationError as exc:
                        batch.errors.append(f"classes/{cid}/members: {exc}")

                # Assignments are optional and NEVER stored (G.4) — only
                # fetched when a future curriculum consumer asks for them.
                if self.config.get("include_assignments"):
                    batch.meta["assignments_fetched"] = True
                    batch.meta["assignments_note"] = (
                        "assignment/submission objects are not part of the "
                        "canonical five — not persisted (G.4)")

                mapped = map_graph_objects(
                    files["schools"], files["users"], files["classes"],
                    members_by_class=members_by_class,
                    school_id=self.school_id,
                    school_id_map=self._school_id_map(),
                    source=self.dialect,
                )
                mapped.errors.extend(batch.errors)
                mapped.meta.update({k: v for k, v in self.rows_in_last_sync.items()})
                if new_delta:
                    # G.3: record the delta token per school_id.
                    self.state_store.put(self.dialect, self.school_id,
                                         DELTA_STATE_KEY, new_delta)
                    self.delta_link = new_delta
                    mapped.meta["delta_link_recorded"] = True
                self._mark_success()
                return mapped

            # Delta pull path
            mapped = map_graph_objects(
                [], files["users"], [], None,
                school_id=self.school_id,
                school_id_map=self._school_id_map(),
                source=self.dialect,
            )
            mapped.errors.extend(batch.errors)
            if new_delta:
                self.state_store.put(self.dialect, self.school_id,
                                     DELTA_STATE_KEY, new_delta)
                self.delta_link = new_delta
                mapped.meta["delta_link_recorded"] = True
            mapped.meta["delta_pull"] = True
            self._mark_success()
            return mapped
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        """Graph change-notification envelopes and inline roster objects.

        A Microsoft Graph change notification is a validation handshake or a
        resource-changed event — it carries no roster body.  We record the
        hint and return an empty batch (the next pull is the incremental
        read).  Direct object payloads (tests / batch sync) map normally.
        """
        try:
            data = payload.get("value") or payload.get("data") or payload
            if isinstance(data, list):
                users = [r for r in data if isinstance(r, dict)]
                batch = map_graph_objects([], users, [], None,
                                          school_id=self.school_id,
                                          school_id_map=self._school_id_map(),
                                          source=self.dialect)
                self._mark_success()
                return batch
            if isinstance(data, dict):
                if any(k in data for k in ("subscriptionId", "validationTokens",
                                           "clientState")):
                    # Change notification only — roster read happens on pull().
                    batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
                    batch.meta["change_notification"] = True
                    self._mark_success()
                    return batch
                # Accept both the Graph envelope ("users") and the HTTP
                # contract's canonical list keys ("students"/"staff") — the
                # latter hold Graph-shaped objects when dialect=graph_education.
                users = (data.get("users")
                         or data.get("students")
                         or data.get("staff")
                         or ([data] if "primaryRole" in data or "id" in data else []))
                schools = data.get("schools") or []
                classes = data.get("classes") or []
                batch = map_graph_objects(schools, users, classes,
                                          school_id=self.school_id,
                                          school_id_map=self._school_id_map(),
                                          source=self.dialect)
                self._mark_success()
                return batch
            raise GraphEducationError("webhook payload must be an object or list")
        except Exception as exc:
            self._mark_error(exc)
            batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
            return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["mode"] = "pull"
        base["rows_in_last_sync"] = dict(self.rows_in_last_sync)
        base["delta_link_recorded"] = bool(self.delta_link)
        base["state_store"] = self._store_kind
        missing = self._missing_env()
        if missing:
            base["status"] = "NOT_CONFIGURED"
            base["missing_env"] = missing
            base["least_privilege"] = "EducationRoster.Read.All (application)"
        elif self.last_error:
            base["status"] = "UNHEALTHY"
        else:
            base["status"] = "OK"
        base["least_privilege"] = "EducationRoster.Read.All (application)"
        return base
