import http.client
import socket
import time
import urllib.request


def raw_connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", 8000))
        return "raw.connect OK"
    except Exception as exc:
        return f"raw.connect FAIL {exc!r}"
    finally:
        s.close()


def create_conn():
    try:
        s = socket.create_connection(("127.0.0.1", 8000), timeout=5)
        s.close()
        return "create_connection OK"
    except Exception as exc:
        return f"create_connection FAIL {exc!r}"


def create_conn_localhost():
    try:
        s = socket.create_connection(("localhost", 8000), timeout=5)
        s.close()
        return "create_connection(localhost) OK"
    except Exception as exc:
        return f"create_connection(localhost) FAIL {exc!r}"


def http_client():
    try:
        c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
        c.request("GET", "/health", headers={"Connection": "close"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return f"http.client OK {r.status} {body[:100]!r}"
    except Exception as exc:
        return f"http.client FAIL {exc!r}"


def urllib_call():
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/health", headers={"Connection": "close"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return f"urllib OK {resp.status} {resp.read()[:100]!r}"
    except Exception as exc:
        return f"urllib FAIL {exc!r}"


for fn in (raw_connect, create_conn, create_conn_localhost, http_client, urllib_call):
    t0 = time.time()
    print(f"{fn():<50} {time.time()-t0:.3f}s")
