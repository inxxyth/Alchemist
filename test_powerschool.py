"""PowerSchool connector tests (Phase 4, N.2) — SYNTHETIC data, zero sockets.

Fixtures in ``fixtures/powerschool/`` use TEST-/SYNTHETIC- names and
``@synthetic.example`` addresses.  No real tenant, roster or credential is
present or contacted — the transport is injected, zero sockets in Parts 1–3.

Covers
------
* thin OneRoster profile: roster URLs are ``/ims/oneroster/v1p2/…`` and the
  mapping is the OneRoster binding's — **not** a fork
* bespoke extras only where OneRoster is silent: ``/ws/v1/attendance`` and
  ``/ws/v1/fees``
* attendance **period → day collapse** (severity absent > excused > late >
  present) — same contract as the iSAMS connector
* fees with alias keys (amount/paid/overdueDays), idempotent invoice dedupe,
  overdue inference, term labels kept verbatim
* school_id stamping (direct + POWERSCHOOL_SCHOOL_ID_MAP-style mapping)
* delta (since= watermark), 401 → UNHEALTHY, optional-resource degrade
* health() NOT_CONFIGURED lists the missing env NAMES (names, never values)

Run (from the container network)::

    python -u test_powerschool.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connectors import create, get_connector_class, registered_dialects  # noqa: E402
from connectors.state import MemoryStateStore  # noqa: E402
from connectors.oneroster.rest_binding import OneRosterRestConnector  # noqa: E402
from connectors.powerschool.connector import (  # noqa: E402
    PowerSchoolConnector,
    PowerSchoolError,
    map_powerschool_extras,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "powerschool")
BASE = os.environ.get("BASE", "http://127.0.0.1:8000")

# Synthetic PowerSchool root for the injected transport — never dialed.
PS_ROOT = "https://ps.synthetic.example"
ROSTER = f"{PS_ROOT}/ims/oneroster/v1p2"
EXTRAS = f"{PS_ROOT}/ws/v1"

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
    """Scripted GET transport (OneRoster REST's protocol).  Zero sockets."""

    def __init__(self) -> None:
        self.pages: dict[str, list[tuple[int, dict, bytes]]] = {}
        self.calls: list[str] = []

    def script(self, url_prefix: str, *responses: tuple[int, dict, bytes]) -> None:
        self.pages[url_prefix] = list(responses)

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        for prefix, responses in self.pages.items():
            if url.startswith(prefix):
                if not responses:
                    return 404, {}, b'{"error": "no scripted response"}'
                return responses.pop(0)
        return 404, {}, b'{"error": "unscripted url"}'


def _json(obj) -> bytes:
    return json.dumps(obj).encode()


def _env() -> dict[str, str]:
    return {
        "POWERSCHOOL_BASE_URL": PS_ROOT,
        "POWERSCHOOL_CLIENT_ID": "synthetic-client-id",
        "POWERSCHOOL_CLIENT_SECRET": "synthetic-client-secret",
    }


def _set_env() -> dict[str, str | None]:
    prior = {k: os.environ.get(k) for k in list(_env()) + ["POWERSCHOOL_SCHOOL_ID_MAP"]}
    for k, v in _env().items():
        os.environ[k] = v
    os.environ.pop("POWERSCHOOL_SCHOOL_ID_MAP", None)
    return prior


def _restore_env(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _script_all(transport: FakeTransport) -> None:
    """Script the standard pull sequence: OneRoster roster + /ws/v1 extras."""
    transport.script(f"{ROSTER}/users",
                     (200, {}, _json(load_fixture("users.json"))))
    transport.script(f"{ROSTER}/orgs",
                     (200, {}, _json(load_fixture("orgs.json"))))
    for resource in ("classes", "courses", "enrollments", "demographics"):
        transport.script(f"{ROSTER}/{resource}", (200, {}, _json({resource: []})))
    transport.script(f"{EXTRAS}/attendance",
                     (200, {}, _json(load_fixture("attendance.json"))))
    transport.script(f"{EXTRAS}/fees",
                     (200, {}, _json(load_fixture("fees.json"))))


def _make_connector(store=None, **config) -> PowerSchoolConnector:
    return PowerSchoolConnector(school_id="default",
                                state_store=store or MemoryStateStore(),
                                **config)


def _seed_token(conn: PowerSchoolConnector) -> None:
    """Seed the shared token — the token endpoint has its own tests upstream
    (test_oneroster.py) and is never dialed from the fixture suite."""
    conn._roster._token = "test-token"
    conn._roster._token_expires_at = 10**12


# ===========================================================================
# Part 1 — bespoke mapping: attendance + fees (the OneRoster gap)
# ===========================================================================

def part1() -> None:
    print("-" * 72)
    print("PART 1 — extras mapping (attendance collapse + fees)")
    print("-" * 72)

    attendance = load_fixture("attendance.json")["attendance"]
    fees = load_fixture("fees.json")["fees"]
    batch = map_powerschool_extras(attendance, fees, school_id="default")

    ok("attendance: 6 period rows collapse to 4 student-days",
       len(batch.attendance) == 4, f"day_rows={len(batch.attendance)}")
    ok("attendance: 1 malformed row rejected with an error, not guessed",
       len(batch.errors) == 1 and "date" in batch.errors[0],
       f"errors={batch.errors}")

    att = {(a["source_student_id"], str(a["attendance_date"])): a["status"]
           for a in batch.attendance}
    ok("collapse: Present + Tardy → day late",
       att.get(("PS-501", "2026-10-05")) == "late",
       f"got={att.get(('PS-501', '2026-10-05'))}")
    ok("collapse: code A → absent",
       att.get(("PS-501", "2026-10-06")) == "absent",
       f"got={att.get(('PS-501', '2026-10-06'))}")
    ok("collapse: Excused + P → day excused (excused > present)",
       att.get(("PS-502", "2026-10-05")) == "excused",
       f"got={att.get(('PS-502', '2026-10-05'))}")
    ok("collapse: ADA code → present (documented PS code set)",
       att.get(("PS-502", "2026-10-06")) == "present",
       f"got={att.get(('PS-502', '2026-10-06'))}")
    ok("attendance rows carry school_id",
       all(a["school_id"] == "default" for a in batch.attendance),
       "stamped on every row")

    ok("fees: 4 rows → 3 (duplicate feeId is idempotent, not doubled)",
       len(batch.fees) == 3, f"fees={len(batch.fees)}")
    fee_by = {f["invoice_number"]: f for f in batch.fees}
    ok("fees: explicit Paid status kept",
       fee_by["FEE-2026-01"]["status"] == "paid",
       f"status={fee_by['FEE-2026-01']['status']}")
    ok("fees: alias keys amount/paid/overdueDays map to canonical",
       fee_by["FEE-2026-02"]["amount_due"] == 2000.0
       and fee_by["FEE-2026-02"]["amount_paid"] == 250.0
       and fee_by["FEE-2026-02"]["days_overdue"] == 5,
       f"{fee_by['FEE-2026-02']['amount_due']}/{fee_by['FEE-2026-02']['amount_paid']}"
       f" days={fee_by['FEE-2026-02']['days_overdue']}")
    ok("fees: partial + overdueDays → overdue (not 'partial')",
       fee_by["FEE-2026-02"]["status"] == "overdue",
       f"status={fee_by['FEE-2026-02']['status']}")
    ok("fees: unpaid with no overdue days → current",
       fee_by["FEE-2026-03"]["status"] == "current",
       f"status={fee_by['FEE-2026-03']['status']}")
    ok("fees: term label kept verbatim (no US-season rewrite)",
       fee_by["FEE-2026-03"]["term_label"] == "Trimester 1",
       f"term={fee_by['FEE-2026-03']['term_label']}")

    mapped = map_powerschool_extras(
        [], [{"studentId": "PS-700", "feeId": "F-1", "amount": 10.0,
              "schoolId": "PS-ORG-9"}],
        school_id="default",
        school_id_map={"PS-ORG-9": "school-dubai"},
    )
    ok("school_id_map resolves the provider org id",
       mapped.fees[0]["school_id"] == "school-dubai",
       f"PS-ORG-9 → {mapped.fees[0]['school_id']}")


# ===========================================================================
# Part 2 — thin profile: roster delegated, extras bespoke
# ===========================================================================

def part2() -> None:
    print("-" * 72)
    print("PART 2 — OneRoster profile + /ws/v1 extras (injected transport)")
    print("-" * 72)

    prior = _set_env()
    try:
        store = MemoryStateStore()
        transport = FakeTransport()
        _script_all(transport)
        conn = _make_connector(store, transport=transport)
        _seed_token(conn)

        # Profile wiring — the point of N.2: a config profile, not a fork.
        ok("profile: roster root is {PS}/ims/oneroster/v1p2",
           conn._roster._base_url() == ROSTER,
           f"base={conn._roster._base_url()}")
        ok("profile: token endpoint is {PS}/oauth/access_token (one token)",
           conn._roster._token_url() == f"{PS_ROOT}/oauth/access_token",
           f"token={conn._roster._token_url()}")
        ok("profile: client env names are POWERSCHOOL_* (names, not values)",
           conn._roster._client_id() == "synthetic-client-id"
           and conn._roster.config.get("client_id_env") == "POWERSCHOOL_CLIENT_ID"
           and conn._roster.config.get("client_secret_env") == "POWERSCHOOL_CLIENT_SECRET",
           "POWERSCHOOL_CLIENT_ID / POWERSCHOOL_CLIENT_SECRET")
        ok("profile: rostering class is the OneRoster REST binding",
           isinstance(conn._roster, OneRosterRestConnector)
           and conn._roster.dialect == "oneroster-rest",
           "delegated, not reimplemented")

        batch = conn.pull()
        ok("pull: roster mapped through OneRoster (3 students + 1 staff)",
           len(batch.students) == 3 and len(batch.staff) == 1,
           f"students={len(batch.students)} staff={len(batch.staff)}")
        ok("pull: OneRoster mapping is live (givenName/familyName canonical)",
           any(s["first_name"] == "Synthetic" and s["last_name"] == "Alfa"
               for s in batch.students),
           "users → Student via map_bulk")
        ok("pull: extras mapped (4 attendance days + 3 fees)",
           len(batch.attendance) == 4 and len(batch.fees) == 3,
           f"attendance={len(batch.attendance)} fees={len(batch.fees)}")
        ok("pull: exactly the fixture's malformed row is rejected (nothing else)",
           len(batch.errors) == 1 and "PS-999" in batch.errors[0],
           f"errors={batch.errors}")

        roster_urls = [u for u in transport.calls if u.startswith(ROSTER)]
        extra_urls = [u for u in transport.calls if u.startswith(EXTRAS)]
        ok("wire proof: roster traffic hits /ims/oneroster/v1p2 only",
           roster_urls and all("/ims/oneroster/v1p2/" in u for u in roster_urls),
           f"roster_urls={len(roster_urls)}")
        ok("wire proof: extras traffic hits /ws/v1 only",
           any("/ws/v1/attendance" in u for u in extra_urls)
           and any("/ws/v1/fees" in u for u in extra_urls),
           f"extra_urls={extra_urls}")

        ok("pull reports rows_in_last_sync per entity (N.5)",
           conn.rows_in_last_sync.get("students") == 3
           and conn.rows_in_last_sync.get("attendance") == 4
           and conn.rows_in_last_sync.get("fees") == 3
           and conn.rows_in_last_sync.get("tombstones") == 0,
           f"rows={conn.rows_in_last_sync}")
        ok("pull records a watermark for the next delta",
           bool(conn.watermark) and conn.watermark == store.get(
               "powerschool", "default", "delta.since"),
           f"watermark={conn.watermark}")

        # Second pull is incremental.
        t2 = FakeTransport()
        _script_all(t2)
        conn2 = _make_connector(store, transport=t2)
        _seed_token(conn2)
        conn2.pull()
        ok("second pull passes since=<watermark> on the extras",
           any("since=" in u for u in t2.calls if u.startswith(EXTRAS)),
           f"delta_since={conn2.delta_since}")

        # Optional resource (fees) degrades; the rest of the pull stands.
        t3 = FakeTransport()
        t3.script(f"{ROSTER}/users", (200, {}, _json(load_fixture("users.json"))))
        t3.script(f"{ROSTER}/orgs", (200, {}, _json(load_fixture("orgs.json"))))
        for resource in ("classes", "courses", "enrollments", "demographics"):
            t3.script(f"{ROSTER}/{resource}", (200, {}, _json({resource: []})))
        t3.script(f"{EXTRAS}/attendance",
                  (200, {}, _json(load_fixture("attendance.json"))))
        t3.script(f"{EXTRAS}/fees", (404, {}, b'{}'))
        conn4 = _make_connector(MemoryStateStore(), transport=t3)
        _seed_token(conn4)
        batch4 = conn4.pull()
        ok("fees 404 degrades: roster + attendance still land, error is visible",
           len(batch4.students) == 3 and len(batch4.attendance) == 4
           and any("fees" in e for e in batch4.errors),
           f"errors={batch4.errors}")

        # 401 on attendance (core/flagship): connector UNHEALTHY.
        t5 = FakeTransport()
        t5.script(f"{ROSTER}/users", (200, {}, _json(load_fixture("users.json"))))
        t5.script(f"{ROSTER}/orgs", (200, {}, _json(load_fixture("orgs.json"))))
        for resource in ("classes", "courses", "enrollments", "demographics"):
            t5.script(f"{ROSTER}/{resource}", (200, {}, _json({resource: []})))
        t5.script(f"{EXTRAS}/attendance", (401, {}, b'{"error": "expired"}'))
        t5.script(f"{EXTRAS}/fees", (200, {}, _json({"fees": []})))
        conn5 = _make_connector(MemoryStateStore(), transport=t5)
        _seed_token(conn5)
        batch5 = conn5.pull()
        ok("401 on attendance: pull degrades to UNHEALTHY, never raises",
           conn5.health()["status"] == "UNHEALTHY" and batch5.errors,
           f"status={conn5.health()['status']}")

        # Webhook: extras-shaped body.
        wh = _make_connector(MemoryStateStore(), transport=FakeTransport())
        _seed_token(wh)
        wb = wh.webhook({
            "attendance": load_fixture("attendance.json")["attendance"],
            "fees": load_fixture("fees.json")["fees"],
        })
        ok("webhook: extras body maps without a pull",
           len(wb.attendance) == 4 and len(wb.fees) == 3,
           f"attendance={len(wb.attendance)} fees={len(wb.fees)}")

        hfields = {"last_success_at", "last_error", "rows_in_last_sync",
                   "watermark", "mode"}
        ok("health() carries the N.5 run fields",
           hfields.issubset(set(conn.health())),
           f"fields={sorted(hfields)}")
        ok("health() names the roster profile (honesty about the reuse)",
           "oneroster" in str(conn.health().get("roster_profile", "")),
           f"profile={conn.health().get('roster_profile')}")
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
    ok("registry lists powerschool", "powerschool" in dialects,
       f"registered={dialects}")
    ok("registry resolves the PowerSchoolConnector class",
       get_connector_class("powerschool") is PowerSchoolConnector,
       "connectors.powerschool.connector.PowerSchoolConnector")
    ok("create('powerschool') builds a connector via the registry",
       isinstance(create("powerschool", school_id="default",
                         state_store=MemoryStateStore()),
                  PowerSchoolConnector),
       "registry factory")

    ok("detect: labelled payload claims dialect",
       PowerSchoolConnector.detect({"dialect": "powerschool"}) is True,
       "dialect key")
    ok("detect: attendanceCode + studentId payload is claimed",
       PowerSchoolConnector.detect({"studentId": "1", "attendanceCode": "A"}) is True,
       "PS-shaped row")
    ok("detect: generic Id/Email payload is NOT claimed",
       PowerSchoolConnector.detect({"Id": "1", "Email": "x@synthetic.example"}) is False,
       "no false claim")
    ok("detect: OneRoster user payload is NOT claimed by powerschool",
       PowerSchoolConnector.detect({"users": [{"sourcedId": "1",
                                               "givenName": "A"}]}) is False,
       "roster payloads stay oneroster-rest's")

    prior = _set_env()
    try:
        for k in _env():
            os.environ.pop(k, None)
        bare = PowerSchoolConnector(school_id="default",
                                    state_store=MemoryStateStore())
        h = bare.health()
        ok("health() without env → NOT_CONFIGURED (never lies 'OK')",
           h["status"] == "NOT_CONFIGURED", f"status={h['status']}")
        ok("health() lists the env NAMES that are missing (names, not values)",
           h.get("missing_env") == ["POWERSCHOOL_BASE_URL",
                                    "POWERSCHOOL_CLIENT_ID",
                                    "POWERSCHOOL_CLIENT_SECRET"],
           f"missing_env={h.get('missing_env')}")
        ok("health() never raises and stays JSON-serialisable",
           bool(json.dumps(h)), "json ok")
    finally:
        _restore_env(prior)


# ===========================================================================
# Part 4 — live PowerSchool gate (honest: observed or not-run, never faked)
# ===========================================================================

def part4() -> None:
    print("-" * 72)
    print("PART 4 — live PowerSchool gate")
    print("-" * 72)

    configured = [k for k in ("POWERSCHOOL_BASE_URL", "POWERSCHOOL_CLIENT_ID",
                              "POWERSCHOOL_CLIENT_SECRET")
                  if os.environ.get(k)]
    if len(configured) == 3:
        try:
            conn = PowerSchoolConnector(school_id="default",
                                        state_store=MemoryStateStore(),
                                        timeout=10.0)
            batch = conn.pull()
            ok("live PowerSchool pull observed",
               not batch.errors,
               f"students={len(batch.students)} errors={batch.errors[:1]}")
        except Exception as exc:  # pragma: no cover — needs partner sandbox
            ok("live PowerSchool pull observed", False, f"{type(exc).__name__}: {exc}")
    else:
        reason = "no PowerSchool sandbox credentials in this environment"
        print(f"[NOT RUN] live PowerSchool check — {reason}")
        print("[NOT RUN] contract verified against fixtures/powerschool/ "
              "(SYNTHETIC); no live result is claimed.")
        prior = _set_env()
        try:
            for k in _env():
                os.environ.pop(k, None)
            h = PowerSchoolConnector(school_id="default",
                                     state_store=MemoryStateStore()).health()
            ok("live check not run: connector reports NOT_CONFIGURED (honest)",
               h["status"] == "NOT_CONFIGURED" and h.get("missing_env"),
               f"{reason}")
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
