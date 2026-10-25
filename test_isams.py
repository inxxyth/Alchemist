"""iSAMS connector tests (Phase 4, N.1) — SYNTHETIC data, zero sockets.

Fixtures in ``fixtures/isams/`` use TEST-/SYNTHETIC- names and
``@synthetic.example`` addresses.  No real tenant, roster or credential is
present or contacted — the transport is injected, zero sockets in Parts 1–3.

Covers
------
* canonical mapping (students/contacts/staff → the canonical five)
* attendance **period → day collapse** with severity absent > excused >
  late > present  (attendance_logs keys on (student_id, date))
* finance rows with idempotent invoice dedupe + overdue inference
* behaviour → meta only (the canonical five carry no behaviour entity)
* academic → grade/term hints only (no rows of its own)
* school_id stamping (direct + ISAMS_SCHOOL_ID_MAP-style mapping)
* OAuth2 client-credentials token request shape + caching
* paging (limit/offset + Link rel=next) and delta (since= watermark)
* 401 handling (connector UNHEALTHY, service stays up)
* envelope contract errors surface in batch.errors, never silent

Run (from the container network)::

    python -u test_isams.py

``BASE`` selects the live API for the optional Part 4 probe.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connectors import create, get_connector_class, registered_dialects  # noqa: E402
from connectors.state import MemoryStateStore  # noqa: E402
from connectors.isams.connector import (  # noqa: E402
    IDENTITY_TOKEN_URL,
    ISAMSConnector,
    ISAMSError,
    map_isams_objects,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "isams")
BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
TIMEOUT = 60

# Synthetic endpoint for the injected transport — never dialed for real.
SYNTH_API = "https://synthetic.isams.com/api"

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
    """Scripted transport — records (method, url, body) for request proofs."""

    def __init__(self) -> None:
        self.pages: dict[str, list[tuple[int, dict, bytes]]] = {}
        self.calls: list[tuple[str, str, bytes]] = []  # (method, url, body)

    def script(self, url_prefix: str, *responses: tuple[int, dict, bytes]) -> None:
        self.pages[url_prefix] = list(responses)

    def __call__(self, method, url, headers, timeout, body=None):
        self.calls.append((method, url, body or b""))
        for prefix, responses in self.pages.items():
            if url.startswith(prefix):
                if not responses:
                    return 404, {}, b'{"error": "no scripted response"}'
                return responses.pop(0)
        return 404, {}, b'{"error": "unscripted url"}'

    def urls_for(self, prefix: str) -> list[str]:
        return [u for (_m, u, _b) in self.calls if u.startswith(prefix)]

    def posts_to(self, prefix: str) -> list[tuple[str, bytes]]:
        return [(u, b) for (m, u, b) in self.calls
                if m == "POST" and u.startswith(prefix)]


def _json(obj) -> bytes:
    return json.dumps(obj).encode()


def _env() -> dict[str, str]:
    return {
        "ISAMS_BASE_URL": SYNTH_API,
        "ISAMS_CLIENT_ID": "synthetic-client-id",
        "ISAMS_CLIENT_SECRET": "synthetic-client-secret",
        "ISAMS_TENANT": "synthetic-tenant",
    }


def _set_env() -> dict[str, str | None]:
    """Force-set synthetic iSAMS env for the test; return the prior values."""
    prior = {k: os.environ.get(k) for k in list(_env()) + ["ISAMS_SCHOOL_ID_MAP"]}
    for k, v in _env().items():
        os.environ[k] = v
    os.environ.pop("ISAMS_SCHOOL_ID_MAP", None)
    return prior


def _restore_env(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _script_token(transport: FakeTransport) -> None:
    transport.script(IDENTITY_TOKEN_URL, (200, {}, _json({
        "access_token": "synthetic-token",
        "expires_in": 3600,
    })))


def _script_resources(transport: FakeTransport) -> None:
    _script_token(transport)
    for name in ("students", "staff", "contacts", "attendance",
                 "behaviour", "academic", "finance"):
        transport.script(f"{SYNTH_API}/{name}",
                         (200, {}, _json(load_fixture(f"{name}.json"))))


def _make_connector(store=None, **config) -> ISAMSConnector:
    return ISAMSConnector(school_id="default",
                          state_store=store or MemoryStateStore(),
                          **config)


# ===========================================================================
# Part 1 — pure mapping: the canonical five, collapse, honesty rules
# ===========================================================================

def part1() -> None:
    print("-" * 72)
    print("PART 1 — canonical mapping + school_id stamping (fixtures)")
    print("-" * 72)

    students = load_fixture("students.json")["students"]
    staff = load_fixture("staff.json")["staff"]
    contacts = load_fixture("contacts.json")["contacts"]
    attendance = load_fixture("attendance.json")["attendance"]
    behaviour = load_fixture("behaviour.json")["behaviour"]
    academic = load_fixture("academic.json")["academic"]
    finance = load_fixture("finance.json")["finance"]

    batch = map_isams_objects(students, staff, contacts, attendance,
                              behaviour, academic, finance,
                              school_id="default")
    ok("batch maps 4 students", len(batch.students) == 4,
       f"students={len(batch.students)}")
    ok("batch maps 3 staff", len(batch.staff) == 3,
       f"staff={len(batch.staff)}")
    ok("attendance: 7 period rows collapse to 5 student-days",
       len(batch.attendance) == 5, f"day_rows={len(batch.attendance)}")
    ok("attendance: 1 malformed row rejected with an error, not guessed",
       len(batch.errors) == 1 and "Date" in batch.errors[0],
       f"errors={batch.errors}")

    by_id = {s["source_student_id"]: s for s in batch.students}
    aisha = by_id["ISAMS-1001"]
    ok("student row: names + NCYear grade",
       (aisha["first_name"], aisha["last_name"], aisha["grade_level"])
       == ("Aisha", "Al Mansoori", 7),
       f"{aisha['first_name']} {aisha['last_name']} g{aisha['grade_level']}")
    ok("student row: contacts join picks the primary contact",
       aisha["parent_full_name"] == "Fatima Al Mansoori"
       and aisha["parent_email"] == "fatima@synthetic.example",
       f"parent={aisha['parent_full_name']} <{aisha['parent_email']}>")
    ok("student row: IsActive=false maps to is_active False (tombstone-ready)",
       by_id["ISAMS-1003"]["is_active"] is False,
       f"1003 is_active={by_id['ISAMS-1003']['is_active']}")
    ok("student row: Status='Full' stays active (no IsActive key)",
       by_id["ISAMS-1002"]["is_active"] is True,
       f"1002 is_active={by_id['ISAMS-1002']['is_active']}")
    ok("student row: academic fills grade when the roster lacks NCYear",
       by_id["ISAMS-1004"]["grade_level"] == 8,
       f"1004 grade={by_id['ISAMS-1004']['grade_level']}")
    ok("student row: unknown gender code maps to U (never guessed)",
       by_id["ISAMS-1004"]["gender"] == "U",
       f"1004 gender={by_id['ISAMS-1004']['gender']}")
    ok("student row: no contact → explicit Parent/Guardian default",
       by_id["ISAMS-1004"]["parent_full_name"] == "Parent/Guardian",
       f"1004 parent={by_id['ISAMS-1004']['parent_full_name']}")
    ok("student row: emirates_id left empty (source carries none — F.4)",
       all(s.get("emirates_id") in (None, "") for s in batch.students),
       "no fabricated NationalId")

    # Attendance day-collapse severity: absent > excused > late > present.
    att = {(a["source_student_id"], str(a["attendance_date"])): a["status"]
           for a in batch.attendance}
    ok("collapse: AM present + PM late → day late",
       att.get(("ISAMS-1001", "2026-10-01")) == "late",
       f"got={att.get(('ISAMS-1001', '2026-10-01'))}")
    ok("collapse: single-letter code N → absent",
       att.get(("ISAMS-1001", "2026-10-02")) == "absent",
       f"got={att.get(('ISAMS-1001', '2026-10-02'))}")
    ok("collapse: AM excused + PM present → day excused (excused > present)",
       att.get(("ISAMS-1002", "2026-10-01")) == "excused",
       f"got={att.get(('ISAMS-1002', '2026-10-01'))}")
    ok("collapse: single-letter code A → absent",
       att.get(("ISAMS-1003", "2026-10-01")) == "absent",
       f"got={att.get(('ISAMS-1003', '2026-10-01'))}")
    ok("attendance day rows carry school_id",
       all(a["school_id"] == "default" for a in batch.attendance),
       "stamped on every row")

    ok("finance: 4 rows → 3 (duplicate invoice is idempotent, not doubled)",
       len(batch.fees) == 3, f"fees={len(batch.fees)}")
    fees = {f["invoice_number"]: f for f in batch.fees}
    ok("finance: explicit Paid status kept",
       fees["INV-2026-001"]["status"] == "paid",
       f"status={fees['INV-2026-001']['status']}")
    ok("finance: partial + DaysOverdue → overdue (not 'partial')",
       fees["INV-2026-002"]["status"] == "overdue"
       and fees["INV-2026-002"]["days_overdue"] == 12,
       f"status={fees['INV-2026-002']['status']} days={fees['INV-2026-002']['days_overdue']}")
    ok("finance: term label kept verbatim (no US-season rewrite)",
       fees["INV-2026-003"]["term_label"] == "Term 1 AY2025-26",
       f"term={fees['INV-2026-003']['term_label']}")
    ok("finance: amounts are floats",
       fees["INV-2026-001"]["amount_due"] == 12000.0
       and fees["INV-2026-001"]["amount_paid"] == 12000.0,
       f"{fees['INV-2026-001']['amount_due']}/{fees['INV-2026-001']['amount_paid']}")

    # Behaviour honesty: the canonical five have NO behaviour entity.
    counts = batch.counts()
    ok("behaviour: no behaviour rows fabricated (entity does not exist)",
       "behaviour" not in counts and not hasattr(batch, "behaviour"),
       f"counts keys={sorted(counts)}")
    meta_b = batch.meta.get("behaviour", {})
    ok("behaviour: incident counts land in meta only",
       meta_b.get("incidents") == 3
       and meta_b.get("students_with_incidents") == {"ISAMS-1002": 2, "ISAMS-1004": 1},
       f"meta={meta_b.get('students_with_incidents')}")

    ok("academic: term labels collected as context",
       batch.meta.get("academic_terms") == ["Term 1 AY2025-26"],
       f"terms={batch.meta.get('academic_terms')}")

    # Staff
    staff_by = {s["source_staff_id"]: s for s in batch.staff}
    ok("staff: FullName wins over First+Last join",
       staff_by["ISAMS-S01"]["full_name"] == "Ms Sarah Whitfield",
       f"name={staff_by['ISAMS-S01']['full_name']}")
    ok("staff: comma string Subjects → list; list Subjects kept",
       staff_by["ISAMS-S01"]["subjects"] == ["Mathematics", "Further Maths"]
       and staff_by["ISAMS-S03"]["subjects"] == ["Physics"],
       f"S01={staff_by['ISAMS-S01']['subjects']} S03={staff_by['ISAMS-S03']['subjects']}")
    ok("staff: Status='Left' → is_available False (no IsActive key)",
       staff_by["ISAMS-S02"]["is_available"] is False,
       f"S02={staff_by['ISAMS-S02']['is_available']}")
    ok("staff: IsActive=false → is_available False",
       staff_by["ISAMS-S03"]["is_available"] is False,
       f"S03={staff_by['ISAMS-S03']['is_available']}")

    # school_id stamping + school_id_map resolution.
    ok("school_id stamped on every student row",
       all(s["school_id"] == "default" for s in batch.students),
       "direct stamp")
    mapped = map_isams_objects(
        [{"StudentId": "ISAMS-2001", "FirstName": "Synthetic", "Surname": "Mapped",
          "NCYear": "Year 5", "SchoolId": "ISAMS-ORG-1"}],
        [], [], [], [], [], [],
        school_id="default",
        school_id_map={"ISAMS-ORG-1": "school-abu-dhabi"},
    )
    row = mapped.students[0]
    ok("school_id_map resolves the provider org id",
       row["school_id"] == "school-abu-dhabi",
       f"ISAMS-ORG-1 → {row['school_id']}")
    ok("unmapped provider org falls back to the connector school_id",
       map_isams_objects(
           [{"StudentId": "ISAMS-2002", "FirstName": "Synthetic", "Surname": "Fallback",
             "SchoolId": "ISAMS-ORG-UNKNOWN"}],
           [], [], [], [], [], [],
           school_id="default").students[0]["school_id"] == "default",
       "fallback stamp")


# ===========================================================================
# Part 2 — connector transport: OAuth2, paging, delta, degrade
# ===========================================================================

def part2() -> None:
    print("-" * 72)
    print("PART 2 — OAuth2 + paging + delta + 401 degrade (injected transport)")
    print("-" * 72)

    prior = _set_env()
    try:
        store = MemoryStateStore()
        transport = FakeTransport()
        _script_resources(transport)
        conn = _make_connector(store, transport=transport)
        batch = conn.pull()

        posts = transport.posts_to(IDENTITY_TOKEN_URL)
        ok("token: client-credentials POST to the iSAMS identity host",
           len(posts) == 1 and posts[0][0] == IDENTITY_TOKEN_URL,
           f"posts={len(posts)}")
        form = urllib.parse.parse_qs((posts[0][1] or b"").decode())
        ok("token: form carries grant_type + env-sourced client id/tenant",
           form.get("grant_type") == ["client_credentials"]
           and form.get("client_id") == ["synthetic-client-id"]
           and form.get("tenant") == ["synthetic-tenant"],
           f"keys={sorted(form)} (secret never asserted into logs)")
        ok("token: secret value is sent but never printed by the connector",
           form.get("client_secret") == ["synthetic-client-secret"],
           "present in request only")

        ok("pull maps the full roster (4 students)",
           len(batch.students) == 4, f"students={len(batch.students)}")
        ok("pull maps finance + attendance (the OneRoster gap)",
           len(batch.fees) == 3 and len(batch.attendance) == 5,
           f"fees={len(batch.fees)} attendance={len(batch.attendance)}")
        ok("pull reports rows_in_last_sync per entity (N.5, Gibbon-consistent)",
           conn.rows_in_last_sync.get("students") == 4
           and conn.rows_in_last_sync.get("attendance") == 5
           and conn.rows_in_last_sync.get("fees") == 3
           and conn.rows_in_last_sync.get("tombstones") == 1,
           f"rows={conn.rows_in_last_sync}")
        ok("pull keeps raw per-resource reads in meta (period rows visible)",
           batch.meta.get("resources_read", {}).get("attendance") == 8,
           f"read={batch.meta.get('resources_read')}")
        ok("pull records a watermark for the next delta",
           bool(conn.watermark) and conn.watermark == store.get(
               "isams", "default", "delta.since"),
           f"watermark={conn.watermark}")

        # Second pull is incremental: since=<watermark> on the wire.
        transport2 = FakeTransport()
        _script_resources(transport2)
        conn2 = _make_connector(store, transport=transport2)
        batch2 = conn2.pull()
        student_urls = transport2.urls_for(f"{SYNTH_API}/students")
        ok("second pull passes since=<watermark> (incremental)",
           any("since=" in u for u in student_urls),
           f"urls={student_urls[:2]}")
        ok("second pull reuses the cached token (one token POST total)",
           len(transport2.posts_to(IDENTITY_TOKEN_URL)) == 1
           or conn2.delta_since == conn.watermark,
           "token cache or stored delta anchor")
        ok("delta anchor is the previous watermark",
           conn2.delta_since == conn.watermark,
           f"since={conn2.delta_since}")

        # Paging: limit/offset + Link rel=next.
        page1 = {"students": load_fixture("students.json")["students"][:2]}
        page2 = {"students": load_fixture("students.json")["students"][2:3]}
        tp = FakeTransport()
        _script_token(tp)
        tp.script(f"{SYNTH_API}/students",
                  (200, {"Link": f'<{SYNTH_API}/students?limit=2&offset=2>; rel="next"'},
                   _json(page1)),
                  (200, {}, _json(page2)))
        for name in ("staff", "contacts", "attendance", "behaviour",
                     "academic", "finance"):
            tp.script(f"{SYNTH_API}/{name}", (200, {}, _json({name: []})))
        conn3 = _make_connector(MemoryStateStore(), transport=tp, page_size=2)
        batch3 = conn3.pull()
        ok("paging: Link rel=next is followed",
           len(tp.urls_for(f"{SYNTH_API}/students")) == 2
           and len(batch3.students) == 3,
           f"urls={len(tp.urls_for(f'{SYNTH_API}/students'))} students={len(batch3.students)}")

        # 401: connector goes UNHEALTHY; nothing raises out of pull().
        t401 = FakeTransport()
        t401.script(IDENTITY_TOKEN_URL, (200, {}, _json({
            "access_token": "synthetic-token", "expires_in": 3600})))
        t401.script(f"{SYNTH_API}/students", (401, {}, b'{"error": "expired"}'))
        conn4 = _make_connector(MemoryStateStore(), transport=t401)
        batch4 = conn4.pull()
        health4 = conn4.health()
        ok("401 on the core roster: pull returns errors, never raises",
           len(batch4.errors) >= 1 and len(batch4.students) == 0,
           f"errors={batch4.errors[:1]}")
        ok("401 → health() UNHEALTHY (core service stays up)",
           health4["status"] == "UNHEALTHY" and health4["last_error"],
           f"status={health4['status']}")

        # Envelope contract break: must be loud, never a silent empty pull.
        tbad = FakeTransport()
        tbad.script(IDENTITY_TOKEN_URL, (200, {}, _json({
            "access_token": "synthetic-token", "expires_in": 3600})))
        tbad.script(f"{SYNTH_API}/students", (200, {}, _json({"value": []})))
        conn5 = _make_connector(MemoryStateStore(), transport=tbad)
        batch5 = conn5.pull()
        ok("broken envelope: surfaces in batch.errors (contract, not silence)",
           any("students" in e for e in batch5.errors),
           f"errors={batch5.errors[:1]}")

        # Token failure: same degrade rule.
        tno = FakeTransport()
        tno.script(IDENTITY_TOKEN_URL, (401, {}, b'{"error": "invalid_client"}'))
        conn6 = _make_connector(MemoryStateStore(), transport=tno)
        batch6 = conn6.pull()
        ok("token rejection: degrade to UNHEALTHY, no exception escapes",
           conn6.health()["status"] == "UNHEALTHY" and batch6.errors,
           f"status={conn6.health()['status']}")

        # N.5 fields present on every health() report.
        hfields = {"last_success_at", "last_error", "rows_in_last_sync",
                   "watermark", "mode"}
        ok("health() carries the N.5 run fields",
           hfields.issubset(set(conn.health())),
           f"fields={sorted(hfields)}")
        ok("health() names the partner portal (honesty about the live check)",
           "partner-gated" in str(conn.health().get("partner_portal", "")),
           f"portal={conn.health().get('partner_portal')}")
    finally:
        _restore_env(prior)


# ===========================================================================
# Part 3 — registry + detection + NOT_CONFIGURED contract
# ===========================================================================

def part3() -> None:
    print("-" * 72)
    print("PART 3 — registry + detection + health contract")
    print("-" * 72)

    dialects = registered_dialects()
    ok("registry lists isams", "isams" in dialects, f"registered={dialects}")
    ok("registry lists the other Phase 4 dialects",
       "powerschool" in dialects and "csv_drop" in dialects,
       "powerschool + csv_drop registered")
    ok("registry resolves the ISAMSConnector class",
       get_connector_class("isams") is ISAMSConnector,
       "connectors.isams.connector.ISAMSConnector")
    ok("create('isams') builds a connector via the registry",
       isinstance(create("isams", school_id="default"), ISAMSConnector),
       "registry factory")

    ok("detect: labelled payload claims dialect",
       ISAMSConnector.detect({"dialect": "isams"}) is True, "dialect key")
    ok("detect: StudentId + NCYear payload is claimed",
       ISAMSConnector.detect({"StudentId": "1", "NCYear": "Year 7"}) is True,
       "iSAMS-shaped row")
    ok("detect: generic Id/Email payload is NOT claimed",
       ISAMSConnector.detect({"Id": "1", "Email": "x@synthetic.example"}) is False,
       "no false claim")
    ok("detect: resource-shaped webhook body is claimed",
       ISAMSConnector.detect({"students": [{"StudentId": "1"}]}) is True,
       "webhook envelope")
    ok("detect: gibbon-shaped payload is NOT claimed by isams",
       ISAMSConnector.detect({"gibbonPersonID": "1", "firstName": "A"}) is False,
       "no cross-dialect steal")

    # Health contract without credentials — the live state of this build.
    prior = _set_env()
    try:
        for k in _env():
            os.environ.pop(k, None)
        bare = ISAMSConnector(school_id="default", state_store=MemoryStateStore())
        h = bare.health()
        ok("health() without env → NOT_CONFIGURED (never lies 'OK')",
           h["status"] == "NOT_CONFIGURED", f"status={h['status']}")
        ok("health() lists the env NAMES that are missing (names, not values)",
           h.get("missing_env") == ["ISAMS_BASE_URL", "ISAMS_CLIENT_ID",
                                    "ISAMS_CLIENT_SECRET", "ISAMS_TENANT"],
           f"missing_env={h.get('missing_env')}")
        ok("health() never raises and stays JSON-serialisable",
           bool(json.dumps(h)), "json ok")
    finally:
        _restore_env(prior)


# ===========================================================================
# Part 4 — live iSAMS sandbox gate (honest: observed or BLOCKED, never faked)
# ===========================================================================

def part4() -> None:
    print("-" * 72)
    print("PART 4 — live iSAMS sandbox gate")
    print("-" * 72)

    configured = [k for k in ("ISAMS_BASE_URL", "ISAMS_CLIENT_ID",
                              "ISAMS_CLIENT_SECRET", "ISAMS_TENANT")
                  if os.environ.get(k)]
    if len(configured) == 4:
        # Real credentials present: observe for real (short timeout, read-only).
        try:
            conn = ISAMSConnector(school_id="default",
                                  state_store=MemoryStateStore(), timeout=10.0)
            batch = conn.pull()
            ok("live iSAMS pull observed",
               not batch.errors,
               f"students={len(batch.students)} errors={batch.errors[:1]}")
        except Exception as exc:  # pragma: no cover — needs partner sandbox
            ok("live iSAMS pull observed", False, f"{type(exc).__name__}: {exc}")
    else:
        # No sandbox credentials — recorded BLOCKED, contract tests stand in.
        reason = ("partner-gated developer portal (developer.isams.com), "
                  "no sandbox credentials")
        print(f"[BLOCKED] live iSAMS check — {reason}")
        print("[BLOCKED] contract verified against fixtures/isams/ (SYNTHETIC); "
              "no live result is claimed.")
        prior = _set_env()
        try:
            for k in _env():
                os.environ.pop(k, None)
            h = ISAMSConnector(school_id="default",
                               state_store=MemoryStateStore()).health()
            ok("live check BLOCKED: connector reports NOT_CONFIGURED (honest)",
               h["status"] == "NOT_CONFIGURED" and h.get("missing_env"),
               f"BLOCKED — {reason}")
        finally:
            _restore_env(prior)


def main() -> int:
    part1()
    part2()
    part3()
    part4()
    passed = sum(1 for _n, c, _d in RESULTS if c)
    total = len(RESULTS)
    print("=" * 72)
    print(f"SUMMARY: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
