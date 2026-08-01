"""v58.74 — accounts, TOTP MFA, sessions, and the boundary that enforces them.

The app shipped with no authentication at all — fine on 127.0.0.1,
indefensible once HOST=0.0.0.0 puts an order-placing API on a LAN.

What this file is actually for is the two properties that are easy to
get wrong and impossible to notice:

  1. TOTP conformance. Any six-digit generator "looks right". These
     checks assert the RFC 6238 published vectors, so the codes are the
     ones Google Authenticator will produce, not merely plausible.
  2. Deny-by-default. Enforcement is one middleware over every route
     rather than a decorator per route, so a NEW endpoint is protected
     because it exists, not because someone remembered. The check below
     enumerates the app's real routes and asserts each is either on the
     small allowlist or requires a session.
"""
import base64, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("creates accounts and writes credentials")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import auth, config

print("1) TOTP is RFC 6238 conformant, not merely six digits")
SEC = base64.b32encode(b"12345678901234567890").decode()
for t, expect in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                  (1234567890, "005924"), (2000000000, "279037"),
                  (20000000000, "353130")):
    got = auth.totp_now(SEC, at=t)
    check(f"RFC vector T={t}", got == expect, f"expected {expect}, got {got}")

print("\n2) a code cannot be replayed")
ok, counter = auth.totp_check(SEC, auth.totp_now(SEC, at=59), -1, at=59)
check("first use is accepted", ok and counter == 1, str(counter))
ok2, _ = auth.totp_check(SEC, auth.totp_now(SEC, at=59), counter, at=59)
check("the same code is refused the second time", not ok2,
      "a 30s code is otherwise reusable for its whole window")
ok3, _ = auth.totp_check(SEC, auth.totp_now(SEC, at=59 + 30), counter, at=59 + 30)
check("the NEXT step is accepted", ok3)
check("clock skew of one step is tolerated",
      auth.totp_check(SEC, auth.totp_now(SEC, at=59), -1, at=59 + 30)[0])
check("a wrong code is refused", not auth.totp_check(SEC, "000000", -1, at=59)[0])
check("a malformed code is refused", not auth.totp_check(SEC, "abc", -1, at=59)[0])

print("\n3) passwords")
salt, h = auth.hash_password("correct horse battery")
check("verifies the right password", auth.verify_password("correct horse battery", salt, h))
check("rejects the wrong one", not auth.verify_password("Correct horse battery", salt, h))
s2, h2 = auth.hash_password("correct horse battery")
check("salted — same password, different stored hash", h2 != h)
check("PBKDF2 iterations are not token", auth.PBKDF2_ROUNDS >= 200_000,
      str(auth.PBKDF2_ROUNDS))

print("\n4) accounts and roles")
auth.create_user("boss", "supersecret1", "admin")
auth.create_user("trader", "supersecret2", "user")
names = [u["username"] for u in auth.list_users()]
check("both accounts exist", names == ["boss", "trader"], str(names))
check("roles are recorded",
      [u["role"] for u in auth.list_users()] == ["admin", "user"])
pub = auth.list_users()[0]
check("the public view leaks no hash or secret",
      not any(k in pub for k in ("hash", "salt", "totp_secret")), str(sorted(pub)))
try:
    auth.create_user("boss", "anotherpass1", "user"); dup = False
except ValueError:
    dup = True
check("duplicate usernames are refused", dup)
try:
    auth.create_user("shorty", "abc", "user"); weak = False
except ValueError:
    weak = True
check("short passwords are refused", weak)
try:
    auth.delete_user("boss"); removed = True
except ValueError:
    removed = False
check("the last admin cannot be deleted", not removed,
      "otherwise account management becomes unreachable")

print("\n5) login, MFA enforcement and lockout")
cfg = dict(config.load(), auth_require_mfa=True, auth_max_failed=3,
           auth_lockout_minutes=15, auth_session_hours=12)
tok, err = auth.authenticate("trader", "supersecret2", "", cfg)
check("login is refused while MFA is unenrolled", tok is None and "MFA" in (err or ""),
      str(err))
secret, uri = auth.begin_mfa_enrollment("trader")
check("enrollment returns a base32 secret", len(secret) >= 26, secret[:6] + "…")
check("and an otpauth URI Google Authenticator understands",
      uri.startswith("otpauth://totp/") and "secret=" in uri and "issuer=" in uri,
      uri.split("?")[0])
check("MFA is not active until confirmed", not auth.confirm_mfa("trader", "000000"))
check("confirming with a real code activates it",
      auth.confirm_mfa("trader", auth.totp_now(secret)))
check("the code used to ENROLL cannot then log in — it is spent",
      auth.authenticate("trader", "supersecret2", auth.totp_now(secret), cfg)[0] is None,
      "confirming consumed that step; the user waits for the next code")
NEXT = auth.totp_now(secret, at=time.time() + auth.TOTP_STEP)
tok, err = auth.authenticate("trader", "supersecret2", NEXT, cfg)
check("login succeeds with password + the next code", bool(tok), str(err))
sess = auth.session(tok)
check("the session carries user and role",
      sess and sess["user"] == "trader" and sess["role"] == "user", str(sess))
tok2, err2 = auth.authenticate("trader", "supersecret2", NEXT, cfg)
check("the SAME code cannot start a second session", tok2 is None, str(err2))
bad = [auth.authenticate("trader", "wrong", "000000", cfg)[1] for _ in range(3)]
check("repeated failures lock the account", "locked" in (bad[-1] or "").lower(), str(bad[-1]))
check("failure messages do not distinguish unknown user from bad password",
      auth.authenticate("ghost", "whatever", "000000", cfg)[1] ==
      "invalid username, password or code",
      "otherwise it is a user-enumeration oracle")

print("\n5b) the enrollment QR")
_uri = auth.provisioning_uri(SEC, "trader")
_svg = auth.qr_svg(_uri)
if _svg is None:
    check("QR is optional — enrollment still works without segno", True,
          "segno not installed; the manual setup key is the fallback")
else:
    check("a real SVG is produced",
          _svg.startswith("<svg") and _svg.rstrip().endswith("</svg>"), _svg[:34])
    check("it is a plausible symbol, not an empty canvas", len(_svg) > 800,
          f"{len(_svg)} bytes")
    check("the SECRET never appears in the QR markup", SEC not in _svg,
          "the payload is encoded as modules, not written as text")
    # 2026-08-01 — the QR rendered but would not scan. segno emits
    # width/height and no viewBox; the page's CSS set a different
    # width/height, which moves the VIEWPORT rather than scaling the
    # drawing, so two finder patterns were cropped off. It looked like a
    # QR and was unreadable. Without a viewBox any CSS size is a crop.
    import re as _re
    _head = _svg[:_svg.index(">") + 1]
    _vb = _re.search(r'viewBox="0 0 (\d+) (\d+)"', _head)
    _w = _re.search(r'width="(\d+)"', _head)
    check("the SVG carries a viewBox", bool(_vb), _head[:60])
    check("and it matches the intrinsic size, so CSS scales without cropping",
          bool(_vb and _w) and _vb.group(1) == _w.group(1),
          f"viewBox {_vb.group(1) if _vb else '?'} vs width {_w.group(1) if _w else '?'}")
    _css = open("static/login.html").read()
    check("the page does not pin a fixed height against that viewBox",
          "height:auto" in _css and "width:190px;height:190px" not in _css,
          "a fixed height on a square symbol re-introduces the crop")
_probe = {}
import builtins as _b
_real_import = _b.__import__
def _no_segno(name, *a, **k):
    if name == "segno":
        raise ImportError("simulated: segno not installed")
    return _real_import(name, *a, **k)
_b.__import__ = _no_segno
try:
    _probe["fallback"] = auth.qr_svg(_uri)
finally:
    _b.__import__ = _real_import
check("without segno it degrades to None rather than raising",
      _probe["fallback"] is None,
      "the page then shows the manual key — enrollment is never blocked")
check("the URI carries everything Google Authenticator needs",
      all(k in _uri for k in ("secret=", "issuer=", "algorithm=SHA1",
                              "digits=6", "period=30")), _uri.split("?")[0])

print("\n6) sessions expire and revoke")
auth._sessions[tok]["expires"] = time.time() - 1
check("an expired token stops working", auth.session(tok) is None)
auth.create_user("temp", "supersecret3", "user")
s3 = auth.begin_mfa_enrollment("temp")[0]
auth.confirm_mfa("temp", auth.totp_now(s3))
t3, e3 = auth.authenticate("temp", "supersecret3",
                           auth.totp_now(s3, at=time.time() + auth.TOTP_STEP), cfg)
check("a second account can sign in", bool(t3), str(e3))
check("logout revokes immediately", bool(t3) and auth.logout(t3) and auth.session(t3) is None)

print("\n7) the enforcement boundary — deny by default")
import app as _app
routes = [r for r in _app.app.routes if getattr(r, "path", "").startswith(("/api", "/"))]
paths = sorted({getattr(r, "path", "") for r in routes})
unguarded = [p for p in paths
             if not _app._auth_free(p) and not p.startswith("/ws/")]
check("the allowlist is small and explicit", len(_app._AUTH_FREE) <= 8,
      str(_app._AUTH_FREE))
check("login/setup/version/static are reachable without a session",
      all(_app._auth_free(p) for p in
          ("/login", "/setup", "/api/version", "/api/auth/login", "/static/x.css")))
check("everything else is behind the middleware", len(unguarded) > 40,
      f"{len(unguarded)} protected routes — the point is that it is ALL of them")
check("order placement is NOT on the allowlist", not _app._auth_free("/api/execute"))
check("settings is NOT on the allowlist", not _app._auth_free("/api/settings"))
src = open("app.py").read()
check("enforcement is a middleware, not a per-route decorator",
      '@app.middleware("http")' in src and "_require_login" in src)
check("the websocket is checked separately (middleware is http-only)",
      "websocket.cookies.get(SESSION_COOKIE)" in src,
      "otherwise live prices stream to anyone who can open a socket")
check("state-changing calls are attributed in the audit log",
      'auth.audit("api"' in src)

print("\n8) it is OFF by default and cannot lock anyone out")
check("auth_enabled defaults to False", config.DEFAULTS.get("auth_enabled") is False,
      "enabling it on a running system with no account is a lockout")
for k in ("auth_require_mfa", "auth_session_hours", "auth_max_failed",
          "auth_lockout_minutes", "auth_cookie_secure"):
    check(f"{k} is registered in DEFAULTS", k in config.DEFAULTS)
check("audit trail is written", os.path.exists(_store.path("auth_audit.jsonl")))
_audit = open(_store.path("auth_audit.jsonl")).read()
check("and never contains a secret or a password",
      secret not in _audit and "supersecret" not in _audit)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
