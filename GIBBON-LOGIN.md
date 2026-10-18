# Gibbon SIS — Local Login

**Local mock only.** These credentials are for the Docker stack on this machine.
Never reuse them in any shared, staging, or production environment.

## UI

| Item | Value |
|------|-------|
| URL | http://127.0.0.1:8080 |
| Username | `msoe_admin` |
| Password | `msoe_admin_2026` |
| Role | Administrator (`001`) |
| Person ID | `0000002774` |

Created by `gibbon_bootstrap_settings.py` (Gibbon's own scheme:
`hash('sha256', salt + password)` stored in `gibbonPerson.passwordStrong`).

## Organisation settings (applied by the same script)

| Setting | Value |
|---------|-------|
| organisationName | MSOE Academy Dubai |
| organisationNameShort | MSOE |
| systemName | MSOE SIS |
| currency | `AED د.إ` |
| organisationEmail | admin@msoe.ae |

UI title now renders `MSOE - MSOE SIS` (was an empty ` - Gibbon` before).

## Database (unchanged, local mock)

| Service | Host/Port | User | Password |
|---------|-----------|------|----------|
| Gibbon MySQL | 127.0.0.1:3306 | `gibbon` / `root` | see `handoff.md` §1 |

## Re-bootstrap / rotate

```bash
# From the msoe_api container (has GIBBON_MYSQL_* env already):
docker exec msoe_api python gibbon_bootstrap_settings.py
# Or rotate the admin password:
docker exec msoe_api python gibbon_bootstrap_settings.py --admin-pass <new-local-pass>
```

Script is idempotent: settings are upserted, the admin row is created or its
password reset. It never touches student/staff/finance/attendance data.

## Demo users

Gibbon demo data uses numeric usernames (`119`, `120`, …) tied to
`gibbonPersonID`. There is no password on file for those accounts in this
stack — use `msoe_admin` for UI access. The classic installer account
(`gibbonPersonID=0000000001`) was never created because the schema was loaded
from demo SQL rather than the web installer.
