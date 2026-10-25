"""CSV drop connector tests (Phase 4, N.4) — SYNTHETIC data, zero sockets.

Fixtures in ``fixtures/csv_drop/`` use TEST-/SYNTHETIC- names and
``@synthetic.example`` addresses.  The drop directories are local temp dirs
copied from the fixtures — no network, no database.

Covers
------
* the documented column contract (field_maps.COLUMN_CONTRACT): required
  columns per file, optional columns accepted, unknown columns logged
* rejection rules: unknown file name ignored+logged; missing required column
  rejects the FILE (file + column); missing required value rejects the ROW
  (file + line + column) — nothing malformed is partially ingested
* ``school_id`` stamping: a ``school_id`` column in a drop file is ignored +
  logged — a drop cannot address another school's rows
* idempotent re-import: unchanged file (sha256) is skipped — a re-drop
  transfers 0 new rows; an edited file re-imports
* webhook push shape ({"file": …, "rows": […]}) + registry/health contract

Run (from the container network)::

    python -u test_csv_drop.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connectors import create, get_connector_class, registered_dialects  # noqa: E402
from connectors.state import MemoryStateStore  # noqa: E402
from connectors.csv_drop.connector import (  # noqa: E402
    CsvDropConnector,
    map_csv_files,
    validate_rows,
)
from connectors.csv_drop.field_maps import COLUMN_CONTRACT, FILE_TO_ENTITY  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "csv_drop")

RESULTS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name:<62} {detail}")
    return bool(condition)


def _fixture_dir(kind: str) -> str:
    return os.path.join(FIXTURE_DIR, kind)


def _make_connector(directory: str, store=None, **config) -> CsvDropConnector:
    return CsvDropConnector(school_id="default",
                            directory=directory,
                            state_store=store or MemoryStateStore(),
                            **config)


def _temp_copy(kind: str) -> str:
    dest = tempfile.mkdtemp(prefix="csv_drop_test_")
    src = _fixture_dir(kind)
    for name in os.listdir(src):
        shutil.copy(os.path.join(src, name), os.path.join(dest, name))
    return dest


# ===========================================================================
# Part 1 — the column contract: valid drops map + stamp
# ===========================================================================

def part1() -> None:
    print("-" * 72)
    print("PART 1 — column contract: valid files map to the canonical five")
    print("-" * 72)

    ok("contract documents all five entity files",
       set(FILE_TO_ENTITY) == {"students.csv", "attendance.csv", "fees.csv",
                               "staff.csv", "leave_requests.csv"},
       f"files={sorted(FILE_TO_ENTITY)}")
    ok("contract: students require source_student_id/first_name/last_name",
       COLUMN_CONTRACT["students"][0] == ("source_student_id", "first_name",
                                          "last_name"),
       f"required={COLUMN_CONTRACT['students'][0]}")

    conn = _make_connector(_fixture_dir("good"))
    batch = conn.pull()

    ok("drop maps 3 students", len(batch.students) == 3,
       f"students={len(batch.students)}")
    ok("drop maps 4 attendance rows", len(batch.attendance) == 4,
       f"attendance={len(batch.attendance)}")
    ok("drop maps 2 fees (duplicate invoice row is idempotent)",
       len(batch.fees) == 2, f"fees={len(batch.fees)}")
    ok("drop maps 2 staff", len(batch.staff) == 2,
       f"staff={len(batch.staff)}")
    ok("drop maps 1 leave request", len(batch.leave_requests) == 1,
       f"leave={len(batch.leave_requests)}")

    by_id = {s["source_student_id"]: s for s in batch.students}
    ok("student: is_active parses true/false/TRUE",
       by_id["CSV-001"]["is_active"] is True
       and by_id["CSV-002"]["is_active"] is False
       and by_id["CSV-003"]["is_active"] is True,
       f"{by_id['CSV-001']['is_active']}/{by_id['CSV-002']['is_active']}"
       f"/{by_id['CSV-003']['is_active']}")
    ok("student: gender M/F/X → M/F/U (never guessed)",
       (by_id["CSV-001"]["gender"], by_id["CSV-002"]["gender"],
        by_id["CSV-003"]["gender"]) == ("F", "M", "U"),
       f"{by_id['CSV-001']['gender']}/{by_id['CSV-002']['gender']}"
       f"/{by_id['CSV-003']['gender']}")
    ok("student: grade_level coerced to int",
       by_id["CSV-001"]["grade_level"] == 7,
       f"grade={by_id['CSV-001']['grade_level']}")
    ok("student: emirates_id left empty when the drop carries none (F.4)",
       all(s.get("emirates_id") in (None, "") for s in batch.students),
       "no fabricated NationalId")

    fee_by = {f["invoice_number"]: f for f in batch.fees}
    ok("fee: unpaid + days_overdue → overdue",
       fee_by["CSV-INV-2"]["status"] == "overdue"
       and fee_by["CSV-INV-2"]["days_overdue"] == 10,
       f"status={fee_by['CSV-INV-2']['status']}")
    ok("fee: term label kept verbatim",
       fee_by["CSV-INV-1"]["term_label"] == "Term 2 AY2025-26",
       f"term={fee_by['CSV-INV-1']['term_label']}")

    staff_by = {s["source_staff_id"]: s for s in batch.staff}
    ok("staff: subjects CSV string → list; is_available parses",
       staff_by["CSV-S01"]["subjects"] == ["Maths", "Science"]
       and staff_by["CSV-S02"]["is_available"] is False,
       f"S01={staff_by['CSV-S01']['subjects']} S02={staff_by['CSV-S02']['is_available']}")

    ok("every row carries the connector's school_id",
       all(r["school_id"] == "default"
           for r in (batch.students + batch.attendance + batch.fees
                     + batch.staff + batch.leave_requests)),
       "stamped on every row")
    ok("unknown file name is ignored and logged (never guessed)",
       any("ignore_me.csv" in e and "unknown file name" in e
           for e in batch.errors),
       f"errors={batch.errors}")
    ok("known files are the only ones imported",
       set(batch.meta["files"]) == {"students.csv", "attendance.csv",
                                    "fees.csv", "staff.csv",
                                    "leave_requests.csv"},
       f"files={sorted(batch.meta['files'])}")


# ===========================================================================
# Part 2 — rejection rules: malformed is rejected and logged, never ingested
# ===========================================================================

def part2() -> None:
    print("-" * 72)
    print("PART 2 — rejections: file + row + column are named in the log")
    print("-" * 72)

    conn = _make_connector(_fixture_dir("malformed"))
    batch = conn.pull()
    ok("missing required column rejects the whole file",
       len(batch.students) == 0 and batch.meta["files"]["students.csv"]["status"] == "rejected",
       f"files={batch.meta['files'].get('students.csv')}")
    ok("file rejection names the file and the missing column",
       any("students.csv" in e and "first_name" in e and "missing required column" in e
           for e in batch.errors),
       f"errors={[e for e in batch.errors if 'students.csv' in e]}")
    ok("bad row rejects alone; good rows still land",
       len(batch.attendance) == 1,
       f"attendance={len(batch.attendance)}")
    ok("row rejection names file + line + column",
       any("attendance.csv" in e and "line 3" in e and "attendance_date" in e
           for e in batch.errors),
       f"errors={[e for e in batch.errors if 'attendance.csv' in e]}")

    conn2 = _make_connector(_fixture_dir("mixed"))
    batch2 = conn2.pull()
    ok("mixed file: 2 good rows land, 1 bad row is rejected",
       len(batch2.students) == 2
       and {s["source_student_id"] for s in batch2.students} == {"CSV-301", "CSV-302"},
       f"students={sorted(s['source_student_id'] for s in batch2.students)}")
    ok("bad row rejection names file + line + required column",
       any("students.csv" in e and "line 4" in e and "first_name" in e
           for e in batch2.errors),
       f"errors={batch2.errors}")
    ok("unknown column is ignored and logged (not guessed, not fatal)",
       any("column 'notes' ignored" in e for e in batch2.errors),
       f"errors={[e for e in batch2.errors if 'notes' in e]}")

    # The DO-NOT: a drop must never bypass school_id stamping.
    ok("school_id column in the file is ignored — stamping cannot be bypassed",
       all(s["school_id"] == "default" for s in batch2.students)
       and any("school_id' ignored" in e for e in batch2.errors),
       f"stamped={batch2.students[0]['school_id']} "
       f"log={[e for e in batch2.errors if 'school_id' in e][:1]}")

    # Pure helpers behave the same outside pull().
    accepted, errs = validate_rows(
        "students",
        [{"source_student_id": "X1", "first_name": "A", "last_name": "B"},
         {"source_student_id": "X2", "first_name": "", "last_name": "D"}],
        filename="students.csv")
    ok("validate_rows: empty required value rejected with line + column",
       len(accepted) == 1 and any("line 3" in e and "first_name" in e for e in errs),
       f"errors={errs}")
    ok("map_csv_files: unparseable attendance date is logged, row dropped",
       len(map_csv_files({"attendance": [
           {"source_student_id": "X1", "attendance_date": "not-a-date",
            "status": "present"}]}, school_id="default").attendance) == 0,
       "date coercion refuses to guess")


# ===========================================================================
# Part 3 — idempotent re-import: unchanged files transfer 0 new rows
# ===========================================================================

def part3() -> None:
    print("-" * 72)
    print("PART 3 — idempotent re-import (sha256 per file)")
    print("-" * 72)

    drop = _temp_copy("good")
    store = MemoryStateStore()
    try:
        conn = _make_connector(drop, store=store)
        first = conn.pull()
        ok("first scan imports the five contract files",
           len(first.students) == 3 and len(first.fees) == 2,
           f"students={len(first.students)} fees={len(first.fees)}")
        ok("first scan records a sha256 per file (durable bookkeeping)",
           all(store.get("csv_drop", "default", f"sha256.{n}")
               for n in FILE_TO_ENTITY),
           "sha256.<file> recorded")

        conn2 = _make_connector(drop, store=store)
        second = conn2.pull()
        statuses = {n: m.get("status") for n, m in second.meta["files"].items()}
        ok("re-drop of unchanged files: everything skipped",
           set(statuses.values()) == {"skipped"},
           f"statuses={statuses}")
        ok("second scan transfers 0 new rows (idempotent)",
           second.counts()["students"] == 0
           and second.counts()["fees"] == 0
           and second.counts()["attendance"] == 0,
           f"counts={second.counts()}")
        ok("skip reason is recorded for the operator",
           all(m.get("reason") for m in second.meta["files"].values()),
           "unchanged since last import")

        # Edit one file → only that file re-imports.
        with open(os.path.join(drop, "students.csv"), "a", encoding="utf-8") as fh:
            fh.write("CSV-004,Testfour,Droppy,F,2012-01-01,5,Parent Four,"
                     "parent4@synthetic.example,true\n")
        conn3 = _make_connector(drop, store=store)
        third = conn3.pull()
        ok("edited file re-imports (hash changed)",
           third.meta["files"]["students.csv"]["status"] == "imported"
           and len(third.students) == 4,
           f"students={len(third.students)}")
        ok("untouched files stay skipped on the same scan",
           third.meta["files"]["fees.csv"]["status"] == "skipped",
           f"fees={third.meta['files']['fees.csv']['status']}")
    finally:
        shutil.rmtree(drop, ignore_errors=True)


# ===========================================================================
# Part 4 — registry + detection + health contract
# ===========================================================================

def part4() -> None:
    print("-" * 72)
    print("PART 4 — registry + detection + health contract")
    print("-" * 72)

    dialects = registered_dialects()
    ok("registry lists csv_drop", "csv_drop" in dialects,
       f"registered={dialects}")
    ok("registry resolves the CsvDropConnector class",
       get_connector_class("csv_drop") is CsvDropConnector,
       "connectors.csv_drop.connector.CsvDropConnector")
    ok("create('csv_drop') builds a connector via the registry",
       isinstance(create("csv_drop", school_id="default",
                         directory=_fixture_dir("good"),
                         state_store=MemoryStateStore()),
                  CsvDropConnector),
       "registry factory")

    ok("detect: labelled payload claims dialect",
       CsvDropConnector.detect({"dialect": "csv_drop"}) is True, "dialect key")
    ok("detect: push shape {'file': 'students.csv', 'rows': []} is claimed",
       CsvDropConnector.detect({"file": "students.csv", "rows": []}) is True,
       "push shape")
    ok("detect: contract-column payload is claimed",
       CsvDropConnector.detect({"source_student_id": "1",
                                "first_name": "A"}) is True,
       "contract columns")
    ok("detect: generic Id payload is NOT claimed",
       CsvDropConnector.detect({"Id": "1", "Email": "x@synthetic.example"}) is False,
       "no false claim")
    ok("detect: iSAMS-shaped payload is NOT claimed by csv_drop",
       CsvDropConnector.detect({"students": [{"StudentId": "1"}]}) is False,
       "no cross-dialect steal")

    # Webhook push shape.
    wh = _make_connector(_fixture_dir("good"))
    wb = wh.webhook({
        "file": "students.csv",
        "rows": [{"source_student_id": "WH-1", "first_name": "Hook",
                  "last_name": "Test"}],
    })
    ok("webhook: push shape maps and stamps",
       len(wb.students) == 1 and wb.students[0]["school_id"] == "default",
       f"students={len(wb.students)}")

    saved = os.environ.pop("CSV_DROP_DIR", None)
    try:
        bare = CsvDropConnector(school_id="default", state_store=MemoryStateStore())
        h = bare.health()
        ok("health() without a directory → NOT_CONFIGURED (never lies 'OK')",
           h["status"] == "NOT_CONFIGURED" and h.get("missing_env") == ["CSV_DROP_DIR"],
           f"status={h['status']} missing_env={h.get('missing_env')}")
        missing = CsvDropConnector(school_id="default",
                                   directory="/nonexistent/csv_drop",
                                   state_store=MemoryStateStore())
        hm = missing.health()
        ok("health() with a vanished drop dir → UNHEALTHY (names the path)",
           hm["status"] == "UNHEALTHY" and "not found" in (hm.get("last_error") or ""),
           f"status={hm['status']}")
        good = CsvDropConnector(school_id="default",
                                directory=_fixture_dir("good"),
                                state_store=MemoryStateStore())
        hg = good.health()
        ok("health() with a live drop dir → OK + mode 'watch'",
           hg["status"] == "OK" and hg["mode"] == "watch"
           and hg["directory"] == _fixture_dir("good"),
           f"status={hg['status']} mode={hg['mode']}")
        ok("health() never raises and stays JSON-serialisable",
           bool(json.dumps(h) and json.dumps(hm) and json.dumps(hg)), "json ok")
    finally:
        if saved is not None:
            os.environ["CSV_DROP_DIR"] = saved

    # Pull errors when the directory is missing — surfaced, not swallowed.
    bad = CsvDropConnector(school_id="default",
                           directory="/nonexistent/csv_drop",
                           state_store=MemoryStateStore())
    bb = bad.pull()
    ok("pull() with a missing directory degrades with a named error",
       any("drop directory not found" in e for e in bb.errors),
       f"errors={bb.errors}")


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
