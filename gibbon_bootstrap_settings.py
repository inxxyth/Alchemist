"""Gibbon settings bootstrap — organisation name/currency + local-mock admin login.

Idempotent. Only writes gibbonSetting rows and ensures one local-mock
Administrator account exists. Never touches student/staff/finance rows.

Usage:
  python gibbon_bootstrap_settings.py
  python gibbon_bootstrap_settings.py --admin-user msoe_admin --admin-pass <local-only>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
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

# Matches gibbon/functions.php getSalt() charset + length (22).
_SALT_ALPHABET = "./aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ0123456789"

# Client-configurable identity literals (F.8 — env with demo defaults).
# See client.manifest.example.yml for the full config surface.
ORG_SETTINGS = {
    "organisationName": os.environ.get("MSOE_SCHOOL_NAME", "MSOE Academy Dubai"),
    "organisationNameShort": os.environ.get("MSOE_SCHOOL_NAME_SHORT", "MSOE"),
    "currency": os.environ.get("MSOE_CURRENCY_LABEL", "AED د.إ"),
    "organisationEmail": os.environ.get("MSOE_ORG_EMAIL", "admin@msoe.ae"),
    "systemName": os.environ.get("MSOE_SYSTEM_NAME", "MSOE SIS"),
}

# gibbonRoleIDAll is a comma-separated list of 3-digit role IDs.
# 001=Administrator, 002=Teacher, 003=Student, 004=Parent, 006=Support Staff
ADMIN_ROLES = "001"


def gibbon_password_hash(password: str, salt: str | None = None) -> tuple[str, str]:
    """Mirror InstallController: hash('sha256', $salt.$password)."""
    salt = salt or "".join(secrets.choice(_SALT_ALPHABET) for _ in range(22))
    digest = hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()
    return digest, salt


def conn():
    return pymysql.connect(**GIBBON_MYSQL)


def ensure_settings(cur) -> dict[str, str]:
    applied = {}
    for name, value in ORG_SETTINGS.items():
        cur.execute(
            "UPDATE gibbonSetting SET value=%s WHERE name=%s",
            (value, name),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO gibbonSetting (scope, name, value, nameDisplay, description) "
                "VALUES ('System', %s, %s, %s, %s)",
                (name, value, name, f"Set by gibbon_bootstrap_settings.py"),
            )
        applied[name] = value
    return applied


def ensure_admin(cur, username: str, password: str, email: str) -> dict[str, str]:
    cur.execute(
        "SELECT gibbonPersonID FROM gibbonPerson WHERE username=%s LIMIT 1",
        (username,),
    )
    row = cur.fetchone()
    pw_hash, salt = gibbon_password_hash(password)

    if row:
        cur.execute(
            "UPDATE gibbonPerson SET passwordStrong=%s, passwordStrongSalt=%s, "
            "passwordForceReset='N', canLogin='Y', status='Full', "
            "gibbonRoleIDPrimary=001, gibbonRoleIDAll=%s WHERE gibbonPersonID=%s",
            (pw_hash, salt, ADMIN_ROLES, row[0]),
        )
        return {"action": "updated", "gibbonPersonID": str(row[0]).zfill(10), "username": username}

    # gibbonPerson has many NOT NULL columns with no DB default; supply empty strings.
    cur.execute(
        "INSERT INTO gibbonPerson ("
        "title, surname, firstName, preferredName, officialName, nameInCharacters, "
        "username, passwordStrong, passwordStrongSalt, passwordForceReset, status, "
        "canLogin, gibbonRoleIDPrimary, gibbonRoleIDAll, email, "
        "address1, address1District, address1Country, address2, address2District, address2Country, "
        "phone1CountryCode, phone1, phone2CountryCode, phone2, phone3CountryCode, phone3, "
        "phone4CountryCode, phone4, website, languageFirst, languageSecond, languageThird, "
        "countryOfBirth, birthCertificateScan, ethnicity, religion, profession, employer, "
        "jobTitle, emergency1Name, emergency1Number1, emergency1Number2, emergency1Relationship, "
        "emergency2Name, emergency2Number1, emergency2Number2, emergency2Relationship, "
        "studentID, lastSchool, nextSchool, departureReason, transport, transportNotes, "
        "calendarFeedPersonal, lockerNumber, vehicleRegistration, personalBackground, "
        "googleAPIRefreshToken, microsoftAPIRefreshToken, genericAPIRefreshToken, fields"
        ") VALUES ("
        "'', 'Admin', 'MSOE', 'MSOE', 'MSOE Admin', '', "
        "%s, %s, %s, 'N', 'Full', 'Y', 001, %s, %s, "
        "'', '', 'UAE', '', '', '', "
        "'', '', '', '', '', '', "
        "'', '', '', '', '', '', "
        "'', '', '', '', '', '', "
        "'', '', '', '', '', "
        "'', '', '', '', "
        "'', '', '', '', '', '', "
        "'', '', '', '', "
        "'', '', '', ''"
        ")",
        (username, pw_hash, salt, ADMIN_ROLES, email),
    )
    person_id = cur.lastrowid
    # Staff row so the account is visible in HR/staff lists (Gibbon's installer does the same).
    cur.execute(
        "INSERT INTO gibbonStaff ("
        "gibbonPersonID, type, jobTitle, countryOfOrigin, qualifications, biography, "
        "biographicalGrouping, biographicalGroupingPriority"
        ") VALUES (%s, 'Support', 'MSOE Administrator', '', '', '', '', 0)",
        (person_id,),
    )
    return {"action": "created", "gibbonPersonID": str(person_id).zfill(10), "username": username}


def main() -> int:
    parser = argparse.ArgumentParser(description="Gibbon org settings + local admin bootstrap")
    parser.add_argument("--admin-user", default="msoe_admin")
    parser.add_argument("--admin-pass", default="msoe_admin_2026")
    parser.add_argument("--admin-email", default="admin@msoe.ae")
    args = parser.parse_args()

    try:
        with conn() as c:
            with c.cursor() as cur:
                settings = ensure_settings(cur)
                admin = ensure_admin(cur, args.admin_user, args.admin_pass, args.admin_email)
            c.commit()
    except Exception as exc:
        print(f"[bootstrap] FAILED: {exc}", file=sys.stderr)
        return 1

    print("[bootstrap] settings applied:")
    for k, v in settings.items():
        print(f"  {k} = {v}")
    print(f"[bootstrap] admin {admin['action']}: {admin['username']} (id {admin['gibbonPersonID']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
