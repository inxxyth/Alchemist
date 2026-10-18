"""Register the "MSOE Dashboard" custom Gibbon module (DB side).

Creates/updates the gibbonModule + gibbonAction + gibbonPermission rows that
make `gibbon/modules/MSOE Dashboard/` show up in the Gibbon menu for
Administrators. Idempotent.

Usage:
  python gibbon_install_msoe_module.py
"""

from __future__ import annotations

import os
import sys

import pymysql

GIBBON_MYSQL = {
    "host": os.environ.get("GIBBON_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("GIBBON_MYSQL_PORT", "3306")),
    "user": os.environ.get("GIBBON_MYSQL_USER", "gibbon"),
    "password": os.environ.get("GIBBON_MYSQL_PASSWORD", "msoe_gibbon_2026"),
    "database": os.environ.get("GIBBON_MYSQL_DB", "gibbon"),
    "connect_timeout": 8,
    "charset": "utf8mb4",
}

MODULE_NAME = "MSOE Dashboard"
ACTION_NAME = "MSOE Dashboard"
ENTRY_URL = "msoeDashboard.php"


def conn():
    return pymysql.connect(**GIBBON_MYSQL)


def main() -> int:
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT gibbonModuleID FROM gibbonModule WHERE name=%s LIMIT 1",
                    (MODULE_NAME,),
                )
                row = cur.fetchone()
                if row:
                    module_id = row[0]
                    cur.execute(
                        "UPDATE gibbonModule SET active='Y', entryURL=%s, "
                        "description=%s, type='Additional', category='Other', "
                        "version='1.0.00', author='MSOE', url='http://127.0.0.1:8000/docs' "
                        "WHERE gibbonModuleID=%s",
                        (ENTRY_URL, "MSOE agentic backend panel (local stack integration).", module_id),
                    )
                    module_action = "updated"
                else:
                    cur.execute(
                        "INSERT INTO gibbonModule (name, description, entryURL, type, active, "
                        "category, version, author, url) VALUES (%s, %s, %s, 'Additional', 'Y', "
                        "'Other', '1.0.00', 'MSOE', 'http://127.0.0.1:8000/docs')",
                        (MODULE_NAME, "MSOE agentic backend panel (local stack integration).", ENTRY_URL),
                    )
                    module_id = cur.lastrowid
                    module_action = "created"

                cur.execute(
                    "SELECT gibbonActionID FROM gibbonAction "
                    "WHERE gibbonModuleID=%s AND name=%s LIMIT 1",
                    (module_id, ACTION_NAME),
                )
                action_row = cur.fetchone()
                if action_row:
                    action_id = action_row[0]
                    cur.execute(
                        "UPDATE gibbonAction SET URLList=%s, entryURL=%s, entrySidebar='Y', "
                        "menuShow='Y', defaultPermissionAdmin='Y', categoryPermissionStaff='Y' "
                        "WHERE gibbonActionID=%s",
                        (ENTRY_URL, ENTRY_URL, action_id),
                    )
                    action_action = "updated"
                else:
                    cur.execute(
                        "INSERT INTO gibbonAction (gibbonModuleID, name, precedence, category, "
                        "description, URLList, entryURL, entrySidebar, menuShow, "
                        "defaultPermissionAdmin, defaultPermissionTeacher, defaultPermissionStudent, "
                        "defaultPermissionParent, defaultPermissionSupport, "
                        "categoryPermissionStaff, categoryPermissionStudent, "
                        "categoryPermissionParent, categoryPermissionOther) "
                        "VALUES (%s, %s, 0, 'Other', %s, %s, %s, 'Y', 'Y', "
                        "'Y', 'N', 'N', 'N', 'N', 'Y', 'N', 'N', 'N')",
                        (
                            module_id, ACTION_NAME,
                            "MSOE institutional services panel (docs, health, approvals).",
                            ENTRY_URL, ENTRY_URL,
                        ),
                    )
                    action_id = cur.lastrowid
                    action_action = "created"

                # defaultPermissionAdmin=Y is honoured at install time by Gibbon's
                # installer; here we grant the Administrator role explicitly.
                cur.execute(
                    "SELECT permissionID FROM gibbonPermission "
                    "WHERE gibbonRoleID=001 AND gibbonActionID=%s LIMIT 1",
                    (action_id,),
                )
                if cur.fetchone():
                    perm_action = "exists"
                else:
                    cur.execute(
                        "INSERT INTO gibbonPermission (gibbonRoleID, gibbonActionID) VALUES (001, %s)",
                        (action_id,),
                    )
                    perm_action = "created"

            c.commit()
    except Exception as exc:
        print(f"[msoe-module] FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"[msoe-module] module: {module_action} (id {module_id})")
    print(f"[msoe-module] action: {action_action} (id {action_id})")
    print(f"[msoe-module] permission(Administrator): {perm_action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
