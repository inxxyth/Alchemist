# MSOE × Gibbon — Runbook

How to bring the stack up, move Gibbon data into the four institutional
services, and exercise them. Read this before touching anything.

**Companion docs**
- `handoff.md` — session state, stack inventory, landmines
- `IMPLEMENTATION-LEDGER.md` — task coverage + evidence
- `GIBBON-LOGIN.md` — local Gibbon admin login + org settings

---

## 0. The mental model

The four services **never read Gibbon directly.** They read MSOE's own store.
Gibbon is a *source system* pushed in through a bridge.

```
Gibbon SIS (PHP)          MSOE agentic backend (FastAPI)
  :8080 UI                 :8000 /docs
  MySQL :3306  ──bridge──▶  Postgres :5432  ──▶  4 services
       │                         │
       │                         └──▶ Neo4j :7687  (graph grounding)
       └── MSOE Dashboard module (iframes back into :8000)
```

| Store | Holds | Consumed by |
|-------|-------|-------------|
| `students` | identity, grade, parent contacts, vaccination | compliance, executive |
| `attendance_logs` | per-student daily status | compliance, executive |
| `fee_ledgers` | invoices, overdue days | compliance, executive |
| `staff` | teachers, availability, periods | substitute-dispatch |
| Neo4j | `Student` / `RiskFlag` nodes + paths | executive query (grounding) |

If a service returns empty results, the pipe is dry — run the bridge first
(§2), don't debug the service.

---

## 1. Bring the stack up

```powershell
cd C:\Users\notti\OneDrive\Documents\Alchemist

docker compose up -d
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Expect five containers:

| Container | Port | Role |
|-----------|------|------|
| `msoe_postgres` | 5432 | MSOE relational store |
| `msoe_neo4j` | 7474 / 7687 | Knowledge graph |
| `msoe_api` | 8000 | FastAPI — the four services + SIS + approvals |
| `gibbon_db` | 127.0.0.1:3306 | Gibbon MySQL |
| `gibbon_app` | 8080 | Gibbon SIS v31 (PHP) |

All five should read `Up` / `healthy`.

**Smoke check (always from the Docker network):**

```powershell
docker run --rm --network msoe_default `
  -v C:\Users\notti\OneDrive\Documents\Alchemist\test_api.py:/tmp/test_api.py `
  -e BASE=http://msoe_api:8000 `
  python:3.12-slim python -u /tmp/test_api.py
```

Expect `SUMMARY: 9/9 passed`. If that's green, the API is healthy end to end.

---

## 2. Bridge Gibbon → MSOE (this is the connection)

Without this, the services have nothing to reason over.

```powershell
docker exec msoe_api python gibbon_bridge.py
```

Healthy output:

```json
{
  "source": "gibbon",
  "syncs": {
    "students":   {"upserted": 391, "table": "students"},
    "staff":      {"upserted": 47,  "table": "staff"},
    "attendance": {"upserted": 420, "table": "attendance_logs"},
    "fees":       {"upserted": 37,  "table": "fee_ledgers"},
    "graph":      {"students_upserted": 391, "risk_edges": 0}
  }
}
```

Variants:

```powershell
docker exec msoe_api python gibbon_bridge.py --students-only   # students + graph only
docker exec msoe_api python gibbon_bridge.py --full            # same as default
```

**There is no automatic sync loop.** Re-run the bridge whenever Gibbon data
changes (new students, attendance taken, invoices issued).

### Optional: seed demo attendance + finance

Gibbon ships with empty attendance/finance tables. To give the services
something to pull:

```powershell
docker exec msoe_api python gibbon_seed_demo_ops.py            # additive, skips if rows exist
docker exec msoe_api python gibbon_seed_demo_ops.py --force    # re-seed anyway
```

---

## 3. The four institutional services

Base URL in Docker: `http://msoe_api:8000`
Browser: **http://127.0.0.1:8000/docs** (interactive — easiest way to explore)

| # | Service | Endpoint | Reads | Output |
|---|---------|----------|-------|--------|
| 1 | Compliance audit | `POST /api/v1/audit/compliance` | students + attendance + fees | risk students → policy clauses → parent-notification **drafts** |
| 2 | Curriculum harmonize | `POST /api/v1/curriculum/harmonize` | request body | lesson plan vs MoE AI Competencies |
| 3 | Substitute dispatch | `POST /api/v1/operations/substitute-dispatch` | `staff` | leave → substitute candidates, staged `PENDING_HUMAN_APPROVAL` |
| 4 | Executive query | `POST /api/v1/executive/query` | Neo4j paths | grounded answer + citations, or `REFUSED_INSUFFICIENT_DATA` |

### Call one directly

```powershell
docker run --rm --network msoe_default python:3.12-slim python -c "
import urllib.request, json
body = json.dumps({}).encode()
r = urllib.request.urlopen('http://msoe_api:8000/api/v1/audit/compliance',
    data=body, headers={'Content-Type': 'application/json'})
print(r.read().decode()[:500])
"
```

### Human-in-the-loop

Services 1 and 3 **stage** side-effects; they do not send anything.

```
GET  /api/v1/approvals/pending                  # what's waiting
POST /api/v1/approvals/{id}/execute  {"decision": "approve" | "reject"}
```

Nothing leaves the system until a human executes an approval.

---

## 4. Gibbon SIS UI

- **URL:** http://127.0.0.1:8080
- **Login:** `GIBBON-LOGIN.md` (local mock — never reuse elsewhere)
- **Org:** MSOE Academy Dubai / MSOE SIS / AED
- **MSOE Dashboard** module in the menu iframes back to `/docs`, `/health`,
  and approvals — that's the officer's panel.

Optional one-time bootstraps (already run; re-runnable, idempotent):

```powershell
docker exec msoe_api python gibbon_bootstrap_settings.py    # org name/currency + admin
docker exec msoe_api python gibbon_install_msoe_module.py   # MSOE Dashboard module rows
```

---

## 5. LLM provider

| State | `/health` shows | Behaviour |
|-------|-----------------|-----------|
| No key (current) | `llm.provider=deterministic` | deterministic fallback, zero-hallucination constraints |
| `OPENROUTER_API_KEY` set | `llm.provider=openrouter` | NVIDIA Nemotron via OpenRouter, post-validated against graph |

To enable OpenRouter (no other change needed — compose already passes it):

```powershell
# set OPENROUTER_API_KEY in the host environment first — do NOT paste it into any file
# (e.g. put it in ./.env — docker compose reads that automatically; .gitignore excludes it)
# Note: the compose *service* is named `api` (container_name is msoe_api).
docker compose up -d --force-recreate api
# re-run smoke, then:
docker exec msoe_api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
```

Never commit, log, or hardcode the key. Env var name is the contract.

---

## 6. Machine landmines (do not regress)

| Issue | Do this instead |
|-------|-----------------|
| Host `urllib`/`http.client` → **WinError 10048** | Run API clients from the Docker network (`--network msoe_default`, `BASE=http://msoe_api:8000`) |
| Host curl to :8000/:8080 hangs on body | Same — use a Docker-network client |
| PG `127.0.0.1:5432` handshake hangs | Use `localhost`, or connect via `msoe_postgres` on the Docker network |
| Docker build context on OneDrive → Access denied | Build from `%TEMP%\msoe_build` |
| `docker cp` from OneDrive → Access denied | Copy to `%TEMP` first, then `docker cp` |
| PowerShell mangles `$` / `<` / quotes | Use script files + `docker exec`; avoid inline `python -c` on the host |
| Debian mirror unreachable during build | Slim PHP image: no apt; busybox unzip for Composer |
| ~31k stuck FinWait2 sockets | Reboot Windows (host-only, clears itself) |

---

## 7. Day-2 operations

| Task | Command |
|------|---------|
| Restart everything | `docker compose restart` |
| Rebuild API image | copy sources to `%TEMP%\msoe_build`, `docker build -t msoe-api:latest`, `docker compose up -d` |
| Re-pull Gibbon data | `docker exec msoe_api python gibbon_bridge.py` |
| Check API health | `GET http://127.0.0.1:8000/health` (from Docker network) |
| Check SIS adapter | `GET http://127.0.0.1:8000/api/v1/sis/status` |
| Pending approvals | `GET http://127.0.0.1:8000/api/v1/approvals/pending` |
| Reset local Gibbon admin | `docker exec msoe_api python gibbon_bootstrap_settings.py --admin-pass <new-local-pass>` |

Real-time SIS hooks (alternative to the batch bridge):

```
POST /api/v1/sis/webhook   {"event": "sis.student.updated", "data": {...}, "dialect": "gibbon"}
POST /api/v1/sis/sync      {"dialect": "gibbon", "students": [...], "attendance": [...], "fees": [...]}
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Services return empty / no risk students | Bridge never run, or Gibbon tables empty | `docker exec msoe_api python gibbon_bridge.py`; check §2 output counts |
| `attendance.upserted: 0` | `gibbonAttendanceLogPerson` is empty | Seed demo rows (§2) or take attendance in Gibbon UI |
| `fees` key missing from bridge output | No invoices in Gibbon | `gibbon_seed_demo_ops.py`, or raise invoices in Finance module |
| Smoke suite fails on connection | Client running on the host | Use the §1 Docker-network command |
| `/health` shows `deterministic` | No `OPENROUTER_API_KEY` | Expected until the key is set (§5) |
| Executive query `REFUSED_INSUFFICIENT_DATA` | No graph paths for that question | Expected guardrail; try a query about risk/attendance/fees, or re-run the bridge |
| Gibbon page blank / white | PHP fatal in module | Check `gibbon_app` logs: `docker logs gibbon_app --tail 50` |
| Gibbon login rejected | Wrong creds or account reset | `GIBBON-LOGIN.md`, re-run `gibbon_bootstrap_settings.py` |
| MSOE Dashboard iframes blank | `MSOE_API_URL` not reachable from the browser | Default is `http://127.0.0.1:8000`; override via `MSOE_API_URL` in compose |

---

## 9. Minimal cheat sheet

```powershell
# 1. stack
docker compose up -d

# 2. pipe Gibbon → MSOE
docker exec msoe_api python gibbon_bridge.py

# 3. the four services
#    http://127.0.0.1:8000/docs

# 4. Gibbon UI + MSOE Dashboard panel
#    http://127.0.0.1:8080
```

Smoke any time:

```powershell
docker run --rm --network msoe_default `
  -v C:\Users\notti\OneDrive\Documents\Alchemist\test_api.py:/tmp/test_api.py `
  -e BASE=http://msoe_api:8000 `
  python:3.12-slim python -u /tmp/test_api.py
```
