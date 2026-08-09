#!/usr/bin/env python3
"""test_users_panel.py — v59.77, the Users & Access card in Settings.

The UI is a thin layer over admin endpoints that already existed; what
can break silently is (a) the field names the JS renders drifting from
what auth.list_users() actually returns — asserted by EXECUTING the
producer, per the scrape-the-producer rule — and (b) the card calling
routes the app does not register, or unauthenticated access slipping
through. The visual layer itself needs a browser (playwright suite).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_users_panel")

import auth

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- the fields the JS renders come from the real producer --------------
auth.create_user("paneltest", "longenough1", "user")
users = auth.list_users()
u = next((x for x in users if x["username"] == "paneltest"), None)
check("created user appears in list_users", u is not None)
NEEDED = ("username", "role", "mfa", "locked", "last_login")
check("list_users carries every field the panel renders",
      u is not None and all(k in u for k in NEEDED),
      f"missing: {[k for k in NEEDED if u and k not in u]}")
check("a fresh account has no MFA and is not locked",
      u is not None and not u["mfa"] and not u["locked"])
auth.delete_user("paneltest")
check("delete removes the account",
      all(x["username"] != "paneltest" for x in auth.list_users()))

# --- UI ↔ endpoint parity ----------------------------------------------
here = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(here, "static", "dashboard.html")).read()
app_src = open(os.path.join(here, "app.py")).read()
for route, verb_hint in (("/api/auth/users", "auth_create_user"),
                         ("/mfa-reset", "auth_reset_mfa"),
                         ("/password", "auth_reset_password"),
                         ("/api/auth/status", "auth_status")):
    check(f"panel route {route} exists in both UI and app",
          route in html and verb_hint in app_src)
for fn in ("loadUsersPanel", "userAdd", "userDelete",
           "userResetPw", "userResetMfa"):
    check(f"panel function {fn} defined once",
          html.count(f"function {fn}(") == 1)
check("loadSettings loads the panel",
      "loadUsersPanel();" in html.split("async function loadSettings(){")[1][:80])
check("the card is hidden by default and admin-gated in JS",
      'id="usersCard" style="display:none"' in html
      and "st.role!=='admin'" in html)

# --- the endpoints refuse anonymous callers -----------------------------
import app as app_mod
try:
    from fastapi.testclient import TestClient
    import config as _cfg
    _orig = _cfg.load
    _cfg.load = lambda: {**_orig(), "auth_enabled": True}
    try:
        c = TestClient(app_mod.app, raise_server_exceptions=False)
        r1 = c.get("/api/auth/users")
        r2 = c.post("/api/auth/users",
                    json={"username": "x", "password": "y", "role": "admin"})
        check("anonymous list/create are refused",
              r1.status_code in (401, 403) and r2.status_code in (401, 403),
              f"{r1.status_code}/{r2.status_code}")
    finally:
        _cfg.load = _orig
except ImportError:
    check("TestClient available for auth check", False, "starlette missing")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all users-panel checks passed")
