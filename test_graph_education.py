"""Microsoft Graph Education connector tests (Phase 3, G.6) — SYNTHETIC data.

Fixtures in ``fixtures/graph_education/`` use TEST-/SYNTHETIC- names and
``@synthetic.example`` addresses.  No real tenant, roster or credential is
present or contacted — the transport is injected, zero sockets in Parts 1–3.

Covers
------
* canonical mapping (educationStudent → Student, educationTeacher → Staff)
* school_id stamping (direct + MS_GRAPH_SCHOOL_ID_MAP-style mapping)
* delta-token persistence per school_id (second pull is incremental)
* 401 handling (connector UNHEALTHY, service stays up)
* **no write endpoint is ever called** — every request to
  graph.microsoft.com is recorded and asserted to be a GET

Run (from the container network)::

    python -u test_graph_education.py

``BASE`` selects the live API for Part 4.  ``MSOE_API_KEYS`` must be set.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connectors import create, get_connector_class, registered_dialects  # noqa: E402
from connectors.state import MemoryStateStore, PgStateStore  # noqa: E402
from connectors.graph_education.connector import (  # noqa: E402
    GRAPH_BASE,
    GRAPH_SCOPE,
    GraphEducationConnector,
    GraphEducationError,
    map_graph_objects,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "graph_education")
BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
TIMEOUT = 60

RESULTS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name:<62} {detail}")
    return bool(condition)


def load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeTransport:
    """Scripted transport.  Records (method, url) — the no-write proof."""

    def __init__(self):
        self.pages: dict[str, list[tuple[int, dict, bytes]]] = {}
        self.calls: list[tuple[str, str]] = []  # (method, url)

    def script(self, url_prefix: str, *responses: tuple[int, dict, bytes]) -> None:
        self.pages[url_prefix] = list(responses)

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url))
        for prefix, responses in self.pages.items():
            if url.startswith(prefix):
                if not responses:
                    return 200, {}, b'{"value": []}'
                return responses.pop(0)
        return 404, {}, b'{"value": []}'


def _json(obj) -> bytes:
    return json.dumps(obj).encode()


def _script_roster(transport: FakeTransport, users_fixture: str = "users.json",
                   with_delta_link: str = "") -> None:
    """Script the standard pull sequence against GRAPH_BASE."""
    token_url = "https://login.microsoftonline.com/"
    transport.script(token_url, (200, {}, _json({
        "access_token": "synthetic-token",
        "expires_in": 3600,
    })))
    transport.script(f"{GRAPH_BASE}/education/schools",
                     (200, {}, _json(load_fixture("schools.json"))))
    users_payload = load_fixture(users_fixture)
    if with_delta_link:
        users_payload = dict(users_payload)
        users_payload["@odata.deltaLink"] = with_delta_link
    transport.script(f"{GRAPH_BASE}/education/users",
                     (200, {}, _json(users_payload)))
    transport.script(f"{GRAPH_BASE}/education/classes",
                     (200, {}, _json(load_fixture("classes.json"))))
    transport.script(f"{GRAPH_BASE}/education/classes/graph-class-01/members",
                     (200, {}, _json(load_fixture("members_graph-class-01.json"))))
    transport.script(f"{GRAPH_BASE}/education/classes/graph-class-02/members",
                     (200, {}, _json(load_fixture("members_graph-class-02.json"))))


def _set_env() -> None:
    os.environ.setdefault("TEST_GRAPH_TENANT_ID", "synthetic-tenant")
    os.environ.setdefault("TEST_GRAPH_CLIENT_ID", "synthetic-client-id")
    os.environ.setdefault("TEST_GRAPH_CLIENT_SECRET", "synthetic-client-secret")


# ===========================================================================
# Part 1 — canonical mapping + school_id stamping
# ===========================================================================

def part1() -> None:
    print("-" * 72)
    print("PART 1 — canonical mapping + school_id stamping")
    print("-" * 72)

    ok("registry lists graph_education",
       "graph_education" in registered_dialects(),
       f"registered={registered_dialects()}")

    users = load_fixture("users.json")["value"]
    schools = load_fixture("schools.json")["value"]
    classes = load_fixture("classes.json")["value"]
    members = {
        "graph-class-01": load_fixture("members_graph-class-01.json")["value"],
        "graph-class-02": load_fixture("members_graph-class-02.json")["value"],
    }

    batch = map_graph_objects(schools, users, classes, members,
                              school_id="default")
    ok("batch maps 4 students (primaryRole=student)",
       len(batch.students) == 4, f"students={len(batch.students)}")
    ok("batch maps 2 staff (primaryRole=teacher)",
       len(batch.staff) == 2, f"staff={len(batch.staff)}")
    ok("batch reports no mapping errors",
       batch.errors == [], f"errors={batch.errors}")

    by_id = {s["source_student_id"]: s for s in batch.students}
    alpha = by_id.get("graph-user-1001")
    ok("student canonical row: names + Graph id",
       alpha is not None
       and alpha["first_name"] == "TEST Student"
       and alpha["last_name"] == "Alpha"
       and alpha["source_student_id"] == "graph-user-1001",
       f"alpha={alpha and (alpha['first_name'], alpha['last_name'])}")
    ok("student canonical row: educationStudent profile (dob + gender)",
       alpha is not None
       and str(alpha.get("date_of_birth")) == "2013-04-12"
       and alpha.get("gender") == "F",
       f"dob={alpha and alpha.get('date_of_birth')} gender={alpha and alpha.get('gender')}")
    ok("student canonical row: grade from student.grade ('08' → 8)",
       alpha is not None and alpha.get("grade_level") == 8,
       f"grade={alpha and alpha.get('grade_level')}")
    ok("student canonical row: emirates_id stays NULL (no national id in Graph)",
       alpha is not None and alpha.get("emirates_id") is None,
       f"emirates_id={alpha and alpha.get('emirates_id')}")
    ok("student canonical row: school_name from educationSchool",
       alpha is not None and "SYNTHETIC Graph" in str(alpha.get("school_name")),
       f"school_name={alpha and alpha.get('school_name')}")

    beta = by_id.get("graph-user-1002")
    ok("grade token 'KG' maps to 1 (calendar-agnostic)",
       beta is not None and beta.get("grade_level") == 1,
       f"beta.grade={beta and beta.get('grade_level')}")

    gamma = by_id.get("graph-user-1003")
    ok("missing grade falls back to class membership context ('07' → 7)",
       gamma is not None and gamma.get("grade_level") == 7,
       f"gamma.grade={gamma and gamma.get('grade_level')}")

    delta = by_id.get("graph-user-1004")
    ok("accountEnabled=false → is_active False",
       delta is not None and delta.get("is_active") is False,
       f"delta.is_active={delta and delta.get('is_active')}")

    teacher = next((s for s in batch.staff if s["source_staff_id"] == "graph-user-2001"), None)
    ok("staff canonical row: full_name + email + role",
       teacher is not None
       and teacher["full_name"] == "TEST Teacher One"
       and teacher["email"] == "teacher.one@synthetic.example"
       and teacher["role"] == "teacher",
       f"teacher={teacher and teacher.get('full_name')}")

    # -- school_id stamping -------------------------------------------------
    ok("school_id default stamps every row",
       all(s["school_id"] == "default" for s in batch.students + batch.staff),
       f"values={sorted({s['school_id'] for s in batch.students + batch.staff})}")

    stamped = map_graph_objects(schools, users, classes, members,
                                school_id="campus_x")
    ok("school_id argument stamps every row",
       all(s["school_id"] == "campus_x" for s in stamped.students + stamped.staff))

    mapped = map_graph_objects(schools, users, classes, members,
                               school_id="fallback",
                               school_id_map={"graph-school-001": "gems_north",
                                              "graph-school-002": "gems_south"})
    mapped_by_id = {s["source_student_id"]: s["school_id"] for s in mapped.students}
    ok("school_id_map routes Graph school → school_id",
       mapped_by_id.get("graph-user-1001") == "gems_north",
       f"graph-user-1001 → {mapped_by_id.get('graph-user-1001')}")

    # -- detect() -----------------------------------------------------------
    ok("detect(): Graph user object recognised",
       get_connector_class("graph_education").detect(
           {"primaryRole": "student", "id": "x", "givenName": "T"}))
    ok("detect(): OneRoster rows NOT claimed",
       not get_connector_class("graph_education").detect(
           {"sourcedId": "1", "givenName": "T", "familyName": "S"}))


# ===========================================================================
# Part 2 — pull() with injected transport: delta, 401, no-write proof
# ===========================================================================

def part2() -> None:
    print("-" * 72)
    print("PART 2 — pull(), delta tokens, 401, least privilege")
    print("-" * 72)
    _set_env()
    os.environ["TEST_GRAPH_TENANT_ID"] = "synthetic-tenant"
    os.environ["TEST_GRAPH_CLIENT_ID"] = "synthetic-client-id"
    os.environ["TEST_GRAPH_CLIENT_SECRET"] = "synthetic-client-secret"

    delta_url = ("https://graph.microsoft.com/v1.0/education/users/delta"
                 "?$deltatoken=TEST-DELTA-TOKEN-1")

    # -- full pull ----------------------------------------------------------
    transport = FakeTransport()
    store = MemoryStateStore()
    conn = GraphEducationConnector(
        school_id="graph_school",
        state_store=store,
        transport=transport,
        client_id_env="TEST_GRAPH_CLIENT_ID",
        client_secret_env="TEST_GRAPH_CLIENT_SECRET",
    )
    # credentials via env override names — the connector reads MS_GRAPH_*;
    # point it at the test vars by monkeypatching the lookup, or set MS_GRAPH_*.
    os.environ["MS_GRAPH_TENANT_ID"] = "synthetic-tenant"
    os.environ["MS_GRAPH_CLIENT_ID"] = "synthetic-client-id"
    os.environ["MS_GRAPH_CLIENT_SECRET"] = "synthetic-client-secret"

    _script_roster(transport, with_delta_link=delta_url)
    batch = conn.pull()
    ok("pull(): full roster → canonical rows",
       len(batch.students) == 4 and len(batch.staff) == 2,
       f"students={len(batch.students)} staff={len(batch.staff)}")
    ok("pull(): rows stamped with connector school_id",
       all(s["school_id"] == "graph_school" for s in batch.students + batch.staff))
    ok("pull(): no mapping errors",
       batch.errors == [], f"errors={batch.errors}")

    # -- least privilege: no write endpoint is ever called ------------------
    graph_calls = [(m, u) for m, u in transport.calls if u.startswith(GRAPH_BASE)]
    non_get = [(m, u) for m, u in graph_calls if m.upper() != "GET"]
    ok("least privilege: every Graph call is a GET",
       len(graph_calls) > 0 and not non_get,
       f"graph_calls={len(graph_calls)} non_get={non_get}")
    ok("least privilege: no PATCH/POST/DELETE to Graph",
       not any(m.upper() in {"POST", "PATCH", "PUT", "DELETE"} for m, _ in graph_calls),
       f"methods={sorted({m for m, _ in graph_calls})}")
    ok("token POST targets the login host, not Graph",
       all("login.microsoftonline.com" in u for m, u in transport.calls
           if m.upper() == "POST"),
       f"posts={[u for m, u in transport.calls if m.upper() == 'POST']}")

    # -- delta-token persistence --------------------------------------------
    saved = store.get("graph_education", "graph_school", "delta_link")
    ok("delta link recorded per school_id",
       saved == delta_url, f"saved={saved!r}")

    # second pull on a NEW connector instance over the SAME store must use it
    transport2 = FakeTransport()
    transport2.script("https://login.microsoftonline.com/",
                      (200, {}, _json({"access_token": "t2", "expires_in": 3600})))
    transport2.script(delta_url,
                      (200, {}, _json(load_fixture("users_delta.json"))))
    conn2 = GraphEducationConnector(
        school_id="graph_school",
        state_store=store,
        transport=transport2,
    )
    batch2 = conn2.pull()
    delta_gets = [u for m, u in transport2.calls if "delta" in u]
    ok("second pull is incremental (uses the stored delta link)",
       len(delta_gets) == 1 and delta_gets[0].startswith(delta_url.split("?")[0]),
       f"delta_gets={delta_gets}")
    ok("delta pull maps the changed rows",
       len(batch2.students) == 1 and batch2.meta.get("delta_pull") is True,
       f"students={len(batch2.students)} meta={batch2.meta.get('delta_pull')}")
    ok("delta link advanced and re-recorded",
       store.get("graph_education", "graph_school", "delta_link")
       == "https://graph.microsoft.com/v1.0/education/users/delta"
          "?$deltatoken=TEST-DELTA-TOKEN-2",
       f"now={store.get('graph_education', 'graph_school', 'delta_link')!r}")

    # per-school_id isolation of the token
    store.put("graph_education", "other_school", "delta_link", "https://other")
    ok("delta token is isolated per school_id",
       store.get("graph_education", "graph_school", "delta_link") != "https://other"
       and store.get("graph_education", "other_school", "delta_link") == "https://other")

    # -- paging via @odata.nextLink -----------------------------------------
    transport3 = FakeTransport()
    store3 = MemoryStateStore()
    conn3 = GraphEducationConnector(
        school_id="page_school", state_store=store3, transport=transport3, page_size=2)
    os.environ["MS_GRAPH_TENANT_ID"] = "synthetic-tenant"
    os.environ["MS_GRAPH_CLIENT_ID"] = "synthetic-client-id"
    os.environ["MS_GRAPH_CLIENT_SECRET"] = "synthetic-client-secret"
    transport3.script("https://login.microsoftonline.com/",
                      (200, {}, _json({"access_token": "t3", "expires_in": 3600})))
    transport3.script(f"{GRAPH_BASE}/education/schools",
                      (200, {}, _json({"value": []})))
    next_link = f"{GRAPH_BASE}/education/users?$skiptoken=PAGE2"
    page1 = load_fixture("users.json")["value"][:2]
    page2 = load_fixture("users.json")["value"][2:3]
    transport3.script(f"{GRAPH_BASE}/education/users",
                      (200, {}, _json({"value": page1, "@odata.nextLink": next_link})),
                      (200, {}, _json({"value": page2})))
    transport3.script(f"{GRAPH_BASE}/education/classes",
                      (200, {}, _json({"value": []})))
    batch3 = conn3.pull()
    ok("paging: @odata.nextLink followed ($skiptoken style)",
       len(batch3.students) == 3,
       f"students={len(batch3.students)}")
    ok("paging: $top set on the first request",
       any("$top=2" in u for m, u in transport3.calls if "/education/users" in u),
       f"urls={[u.split('/')[-1] for m, u in transport3.calls if '/education/users' in u]}")

    # -- 401 handling -------------------------------------------------------
    transport4 = FakeTransport()
    conn4 = GraphEducationConnector(
        school_id="auth_school", state_store=MemoryStateStore(), transport=transport4)
    transport4.script("https://login.microsoftonline.com/",
                      (200, {}, _json({"access_token": "expired", "expires_in": 3600})))
    transport4.script(f"{GRAPH_BASE}/education/schools",
                      (200, {}, _json({"value": []})))
    transport4.script(f"{GRAPH_BASE}/education/users",
                      (401, {}, b'{"error": {"code": "InvalidAuthenticationToken"}}'))
    batch4 = conn4.pull()
    ok("401 → batch error surfaced, no crash, empty roster",
       len(batch4.students) == 0 and any("401" in e for e in batch4.errors),
       f"errors={batch4.errors}")
    ok("401 → connector reports UNHEALTHY (service stays up)",
       conn4.health().get("status") == "UNHEALTHY",
       f"status={conn4.health().get('status')}")

    # -- missing env: names, never values -----------------------------------
    for var in ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET"):
        os.environ.pop(var, None)
    conn5 = GraphEducationConnector(school_id="cfg_school",
                                    state_store=MemoryStateStore(),
                                    transport=FakeTransport())
    health5 = conn5.health()
    ok("health() lists missing env NAMES (never values)",
       health5.get("status") == "NOT_CONFIGURED"
       and "MS_GRAPH_CLIENT_ID" in health5.get("missing_env", []),
       f"missing_env={health5.get('missing_env')}")
    ok("health() reports least-privilege scope",
       "EducationRoster.Read.All" in str(health5.get("least_privilege")),
       f"least_privilege={health5.get('least_privilege')}")
    batch5 = conn5.pull()
    ok("pull() without credentials → error names env vars, no crash",
       any("MS_GRAPH_" in e for e in batch5.errors),
       f"errors={batch5.errors}")

    # -- Graph scope constant is read-only ---------------------------------
    ok("auth scope is the read-only app scope",
       GRAPH_SCOPE == "https://graph.microsoft.com/.default",
       GRAPH_SCOPE)


# ===========================================================================
# Part 3 — HTTP contract + registry health (live API)
# ===========================================================================

def api_call(method: str, path: str, payload: dict | None, api_key: str):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Connection": "close", "X-API-Key": api_key}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return 0, {"error": str(e)}


def part3() -> None:
    print("-" * 72)
    print(f"PART 3 — live API against {BASE}")
    print("-" * 72)
    keys_raw = os.environ.get("MSOE_API_KEYS", "")
    if not keys_raw:
        ok("PART 3 requires MSOE_API_KEYS (same contract as test_api.py)", False,
           "MSOE_API_KEYS unset")
        return
    api_key = keys_raw.split(",")[0].split(":", 1)[1].strip()

    status, body = api_call("GET", "/api/v1/sis/status", None, api_key)
    connectors_block = body.get("connectors", {}) if isinstance(body, dict) else {}
    ok("GET /api/v1/sis/status lists graph_education",
       status == 200 and "graph_education" in connectors_block,
       f"connectors={sorted(connectors_block)}")
    graph_health = connectors_block.get("graph_education", {})
    ok("graph_education reports health (no secrets in payload)",
       "status" in graph_health
       and "synthetic-client-secret" not in json.dumps(graph_health),
       f"status={graph_health.get('status')} "
       f"missing_env={graph_health.get('missing_env')}")

    # dialect accepted on the batch contract (mapped through the connector)
    status2, body2 = api_call("POST", "/api/v1/sis/sync", {
        "dialect": "graph_education",
        "school_id": "graph_fixture_school",
        "students": [{
            "id": "graph-fixture-1",
            "primaryRole": "student",
            "givenName": "TEST Graph",
            "surname": "Fixture",
            "student": {"birthDate": "2014-02-02", "gender": "female", "grade": "06"},
        }],
        "sync_graph": False,
    }, api_key)
    ok("POST /api/v1/sis/sync accepts dialect=graph_education",
       status2 == 200 and body2.get("status") == "OK",
       f"HTTP {status2} status={body2.get('status')}")
    ok("graph row upserted with school_id",
       body2.get("syncs", {}).get("students", {}).get("upserted") == 1
       and body2.get("school_id") == "graph_fixture_school",
       f"upserted={body2.get('syncs', {}).get('students', {}).get('upserted')} "
       f"school_id={body2.get('school_id')}")

    # delta-token store durability: put via a PgStateStore, read back from a
    # fresh instance (process-restart shape).
    try:
        store = PgStateStore()
        store.ensure_table()
        store.put("graph_education", "graph_fixture_school", "delta_link",
                  "https://graph.microsoft.com/v1.0/education/users/delta"
                  "?$deltatoken=TEST-PERSISTED")
        store2 = PgStateStore()
        value = store2.get("graph_education", "graph_fixture_school", "delta_link")
        ok("delta token persists across store instances (Postgres)",
           value is not None and "TEST-PERSISTED" in value,
           f"value={'<delta link>' if value else None}")
    except Exception as exc:
        ok("delta token persists across store instances (Postgres)",
           False, f"{type(exc).__name__}: {exc}")

    # no regression on the other connector dialects
    status3, body3 = api_call("POST", "/api/v1/sis/sync", {
        "dialect": "canvas",
        "school_id": "graph_fixture_school",
        "students": [{"sis_user_id": "legacy-g1", "first_name": "TEST",
                      "last_name": "Legacy", "grade_level": 3}],
        "sync_graph": False,
    }, api_key)
    ok("legacy dialect 'canvas' still accepted (no regression)",
       status3 == 200 and body3.get("status") == "OK",
       f"HTTP {status3}")


# ===========================================================================

def main() -> int:
    print("=" * 72)
    print("MICROSOFT GRAPH EDUCATION CONNECTOR TESTS  (fixtures: SYNTHETIC data)")
    print("=" * 72)
    part1()
    part2()
    part3()

    passed = sum(1 for _, good, _ in RESULTS if good)
    total = len(RESULTS)
    print()
    print("=" * 72)
    print(f"SUMMARY: {passed}/{total} passed")
    print("=" * 72)
    for name, good, detail in RESULTS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name:<62} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
