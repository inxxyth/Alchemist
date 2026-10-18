"""One-off probe: log into Gibbon and confirm the MSOE Dashboard module renders.

Run from the Docker network (host Python hits WinError 10048):
  docker run --rm --network msoe_default \
    -v <this file>:/tmp/probe_dashboard.py \
    python:3.12-slim python -u /tmp/probe_dashboard.py
"""
import re
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://gibbon_app"
USER = "msoe_admin"
PASS = "msoe_admin_2026"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    return m.group(1).strip() if m else "(none)"


# 1) login page
r = opener.open(BASE + "/login.php", timeout=30)
html = r.read().decode("utf-8", "replace")
print("LOGIN PAGE HTTP", r.status, "title:", title_of(html))

# collect hidden inputs (name -> value)
hidden = {}
for tag in re.findall(r"<input\b[^>]*>", html, re.I):
    if not re.search(r'type=["\']hidden["\']', tag, re.I):
        continue
    nm = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
    if not nm:
        continue
    val = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
    hidden[nm.group(1)] = val.group(1) if val else ""

print("HIDDEN FIELDS:", sorted(hidden))

data = {
    "username": USER,
    "password": PASS,
    "loginFormSubmitted": "Y",
    **hidden,
}
body = urllib.parse.urlencode(data).encode()
req = urllib.request.Request(
    BASE + "/login.php",
    data=body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
r2 = opener.open(req, timeout=30)
html2 = r2.read().decode("utf-8", "replace")
print("AFTER LOGIN HTTP", r2.status, "URL", r2.geturl())
print("LOGIN FAILED?", "loginReturn=fail" in (r2.geturl() + html2))
print("HOME TITLE:", title_of(html2))
print("HOME has MSOE Dashboard menu?", "MSOE Dashboard" in html2)

# 2) MSOE Dashboard module page
q = urllib.parse.quote("/modules/MSOE Dashboard/msoeDashboard.php", safe="")
r3 = opener.open(BASE + "/index.php?q=" + q, timeout=30)
html3 = r3.read().decode("utf-8", "replace")
print()
print("DASHBOARD HTTP", r3.status, "title:", title_of(html3))
print("  breadcrumb 'MSOE Dashboard':", "MSOE Dashboard" in html3)
print("  iframe count:", len(re.findall(r"<iframe\b", html3, re.I)))
print("  links to /docs:", "/docs" in html3)
print("  links to /health:", "/health" in html3)
print("  links to /api/v1/sis/status:", "/api/v1/sis/status" in html3)
print("  links to approvals:", "/api/v1/approvals/pending" in html3)
print("  access denied?", "do not have access" in html3.lower())
print("  'Human-in-the-loop' section:", "human-in-the-loop" in html3.lower())

# show a short excerpt around the first iframe for visual confirmation
idx = html3.lower().find("<iframe")
if idx >= 0:
    print()
    print("--- excerpt around first <iframe> ---")
    print(html3[max(0, idx - 120) : idx + 320].replace("\n", " "))
