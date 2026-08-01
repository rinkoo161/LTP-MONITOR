"""auth.py — accounts, TOTP MFA, sessions and an audit trail.

2026-08-01. Until now the app had NO authentication: the README says so
outright ("Anyone who can reach that port can place trades with your
keys — there is no login screen"), which is defensible on 127.0.0.1 and
indefensible the moment HOST=0.0.0.0 is used to reach it from a phone.

Design decisions worth stating, because each closes a specific failure:

  * OFF BY DEFAULT (`auth_enabled`). Turning authentication on in a
    process that is holding live positions, before an admin account
    exists, locks the operator out of their own running system. So it
    ships inert: create the account, scan the QR, THEN flip the switch.

  * BOTH ROLES HAVE THE SAME OPERATIONAL ACCESS, per explicit choice —
    the split exists for separate credentials and attribution, not to
    restrict trading. `admin` additionally manages accounts, because a
    role that grants nothing is not a role.

  * DEPENDENCY-FREE. TOTP is ~40 lines of hmac/struct against RFC 6238,
    and the test asserts the RFC's own published vectors rather than
    "it produced six digits". pyotp would be a dependency for code we
    can verify exactly.

  * REPLAY IS REJECTED. A TOTP code stays valid for its whole 30s step
    (±1 step here for clock skew), so a code shouted over a shoulder or
    captured in a log is reusable for up to 90 seconds. The last
    accepted counter is stored per user and a code at or below it is
    refused — this is the difference between MFA and a second password
    that changes.

Secrets are never logged or returned once enrolled; the provisioning
URI is shown exactly once, at enrollment.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
import urllib.parse

import store

_lock = threading.RLock()

USERS = "users.json"
AUDIT = "auth_audit.jsonl"

PBKDF2_ROUNDS = 600_000          # OWASP 2023 floor for SHA-256
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_SKEW = 1                    # accept ±1 step for clock drift

_sessions = {}                   # token -> {"user", "role", "expires"}


# ------------------------------------------------------------------ store

def _path():
    return store.path(USERS)


def _load():
    try:
        with open(_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users):
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, _path())
    try:
        os.chmod(_path(), 0o600)     # password hashes and TOTP secrets
    except Exception:
        pass


def audit(event, user=None, detail=None, ok=True):
    """Append-only record of who did what. Never contains a secret."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
            "user": user, "ok": bool(ok), "detail": detail}
    try:
        with open(store.path(AUDIT), "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass
    return line


# --------------------------------------------------------------- passwords

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return base64.b64encode(salt).decode(), base64.b64encode(dk).decode()


def verify_password(password, salt_b64, hash_b64):
    try:
        salt = base64.b64decode(salt_b64)
    except Exception:
        return False
    _, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, hash_b64)


# -------------------------------------------------------------------- TOTP

def new_totp_secret():
    """20 random bytes, base32 — what Google Authenticator expects."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret_b32, counter):
    pad = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(pad, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_now(secret_b32, at=None):
    return _totp_at(secret_b32, int((at or time.time()) // TOTP_STEP))


def totp_check(secret_b32, code, last_counter=-1, at=None):
    """(ok, counter). Rejects a counter already used — see module docstring."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False, last_counter
    now = int((at or time.time()) // TOTP_STEP)
    for drift in range(-TOTP_SKEW, TOTP_SKEW + 1):
        c = now + drift
        if c <= last_counter:
            continue                       # replay of an accepted step
        if hmac.compare_digest(_totp_at(secret_b32, c), code):
            return True, c
    return False, last_counter


def qr_svg(uri, scale=5):
    """Inline SVG QR for the provisioning URI, or None if segno is absent.

    Generated SERVER-SIDE and returned in the enrollment POST response —
    never as a GET endpoint. A URL carrying the TOTP secret would end up
    in access logs, the browser history and any proxy in between, which
    would defeat the point of the second factor.

    segno rather than a hand-rolled encoder: QR needs Reed-Solomon,
    version selection and mask scoring, and there is no way to verify a
    hand-rolled symbol offline. A QR that does not scan is worse than
    the manual key. Optional, though — the manual setup key is always
    shown, so a missing library degrades the page rather than breaking
    enrollment.
    """
    try:
        import segno
    except ImportError:
        return None
    try:
        import io
        buf = io.BytesIO()
        # dark/light chosen for the login card, not the OS theme: a
        # scanner needs contrast, and an inverted QR fails on some
        # readers. Quiet zone (border) is required by the spec.
        q = segno.make(uri, error="m")
        q.save(buf, kind="svg", scale=scale, border=3, dark="#0B0E14",
               light="#FFFFFF", xmldecl=False, svgns=True)
        svg = buf.getvalue().decode()
        # 2026-08-01 — segno emits width/height and NO viewBox. Any CSS
        # that sets a different width/height then moves the VIEWPORT
        # instead of scaling the drawing, so the page showed the
        # top-left corner of the symbol and cropped two finder patterns
        # off the right and bottom. It rendered as a convincing QR and
        # could not be scanned by anything.
        #
        # A viewBox makes the symbol scale to whatever box the CSS gives
        # it, which is the only way a fixed-size layout can hold a
        # variable-size QR (the version, and so the pixel size, grows
        # with the length of the account name).
        if "viewBox" not in svg:
            w, h = q.symbol_size(scale=scale, border=3)
            svg = svg.replace("<svg ", f'<svg viewBox="0 0 {w} {h}" ', 1)
        return svg
    except Exception:
        return None


def provisioning_uri(secret_b32, username, issuer="LTP Monitor"):
    """otpauth:// URI — paste into a QR generator, or type the secret."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    q = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer,
                                "algorithm": "SHA1", "digits": TOTP_DIGITS,
                                "period": TOTP_STEP})
    return f"otpauth://totp/{label}?{q}"


# ------------------------------------------------------------------- users

def list_users():
    """Public view — never exposes hashes or TOTP secrets."""
    with _lock:
        return [{"username": u, "role": d.get("role", "user"),
                 "mfa": bool(d.get("totp_secret")),
                 "created": d.get("created"), "last_login": d.get("last_login"),
                 "locked": _locked_for(d) > 0}
                for u, d in sorted(_load().items())]


def user_count():
    return len(_load())


def has_admin():
    return any(d.get("role") == "admin" for d in _load().values())


def create_user(username, password, role="user"):
    username = (username or "").strip().lower()
    if not username or not username.isalnum():
        raise ValueError("username must be alphanumeric")
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    if role not in ("admin", "user"):
        raise ValueError("role must be admin or user")
    with _lock:
        users = _load()
        if username in users:
            raise ValueError("that username already exists")
        salt, h = hash_password(password)
        users[username] = {"salt": salt, "hash": h, "role": role,
                           "totp_secret": None, "totp_last": -1,
                           "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "failed": 0, "locked_until": 0}
        _save(users)
    audit("user.create", username, {"role": role})
    return username


def delete_user(username):
    with _lock:
        users = _load()
        if username not in users:
            raise ValueError("no such user")
        if users[username].get("role") == "admin" and \
                sum(1 for d in users.values() if d.get("role") == "admin") <= 1:
            raise ValueError("cannot delete the only admin")
        users.pop(username)
        _save(users)
    for tok, s in list(_sessions.items()):
        if s["user"] == username:
            _sessions.pop(tok, None)
    audit("user.delete", username)


def set_password(username, password):
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    with _lock:
        users = _load()
        if username not in users:
            raise ValueError("no such user")
        users[username]["salt"], users[username]["hash"] = hash_password(password)
        _save(users)
    audit("user.password", username)


def begin_mfa_enrollment(username):
    """Returns (secret, uri) — shown ONCE. Not active until confirmed."""
    with _lock:
        users = _load()
        if username not in users:
            raise ValueError("no such user")
        secret = new_totp_secret()
        users[username]["totp_pending"] = secret
        _save(users)
    audit("mfa.enroll_begin", username)
    return secret, provisioning_uri(secret, username)


def confirm_mfa(username, code):
    """Activate MFA only after the user proves the app is set up."""
    with _lock:
        users = _load()
        d = users.get(username)
        if not d or not d.get("totp_pending"):
            raise ValueError("no enrollment in progress")
        ok, counter = totp_check(d["totp_pending"], code, -1)
        if not ok:
            audit("mfa.enroll_confirm", username, ok=False)
            return False
        d["totp_secret"] = d.pop("totp_pending")
        d["totp_last"] = counter
        _save(users)
    audit("mfa.enroll_confirm", username)
    return True


def restart_enrollment(username, password):
    """Re-issue an enrollment secret for an account whose MFA is NOT yet
    active, authenticated by password.

    2026-08-01 — without this an interrupted enrollment is a dead end,
    and the operator hit it on the first attempt: /setup refuses once an
    account exists, /mfa/enroll requires a session, and login refuses
    because MFA is not enrolled. Three correct rules with no way out
    between them.

    It weakens nothing: whoever knows the password could have completed
    the original enrollment anyway, and it refuses outright once MFA is
    active — at that point re-enrollment requires a signed-in session.
    """
    username = (username or "").strip().lower()
    with _lock:
        users = _load()
        d = users.get(username)
        if not d or not verify_password(password or "", d.get("salt", ""),
                                        d.get("hash", "")):
            audit("mfa.restart", username, {"reason": "bad credentials"}, ok=False)
            raise ValueError("invalid username or password")
        if d.get("totp_secret"):
            raise ValueError("MFA is already active — sign in, then re-enroll")
    return begin_mfa_enrollment(username)


def disable_mfa(username):
    with _lock:
        users = _load()
        if username in users:
            users[username]["totp_secret"] = None
            users[username]["totp_last"] = -1
            users[username].pop("totp_pending", None)
            _save(users)
    audit("mfa.disable", username)


# ---------------------------------------------------------------- lockout

def _locked_for(d):
    return max(0, int(d.get("locked_until", 0) - time.time()))


# --------------------------------------------------------------- sessions

def authenticate(username, password, code, cfg=None):
    """(session_token, error). Deliberately vague on failure — a message
    that distinguishes 'no such user' from 'wrong password' is a user
    enumeration oracle on a box exposed to a LAN."""
    cfg = cfg or {}
    username = (username or "").strip().lower()
    generic = "invalid username, password or code"
    with _lock:
        users = _load()
        d = users.get(username)
        if not d:
            audit("login", username, {"reason": "no such user"}, ok=False)
            return None, generic
        wait = _locked_for(d)
        if wait:
            audit("login", username, {"reason": "locked"}, ok=False)
            return None, f"account locked, try again in {wait}s"

        if not verify_password(password or "", d["salt"], d["hash"]):
            return None, _fail(users, username, d, cfg, "bad password")

        if d.get("totp_secret"):
            ok, counter = totp_check(d["totp_secret"], code, d.get("totp_last", -1))
            if not ok:
                return None, _fail(users, username, d, cfg, "bad or reused code")
            d["totp_last"] = counter
        elif cfg.get("auth_require_mfa", True):
            audit("login", username, {"reason": "mfa not enrolled"}, ok=False)
            return None, "MFA is required — enroll an authenticator first"

        d["failed"] = 0
        d["locked_until"] = 0
        d["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(users)
        token = secrets.token_urlsafe(32)
        hours = float(cfg.get("auth_session_hours", 12) or 12)
        _sessions[token] = {"user": username, "role": d.get("role", "user"),
                            "expires": time.time() + hours * 3600}
    audit("login", username, {"role": d.get("role")})
    return token, None


def _fail(users, username, d, cfg, reason):
    d["failed"] = int(d.get("failed", 0)) + 1
    limit = int(cfg.get("auth_max_failed", 5) or 5)
    msg = "invalid username, password or code"
    if d["failed"] >= limit:
        mins = float(cfg.get("auth_lockout_minutes", 15) or 15)
        d["locked_until"] = time.time() + mins * 60
        d["failed"] = 0
        msg = f"too many attempts — locked for {mins:.0f} minutes"
    _save(users)
    audit("login", username, {"reason": reason}, ok=False)
    return msg


def session(token):
    """Live session for a token, or None. Expired tokens are dropped."""
    if not token:
        return None
    s = _sessions.get(token)
    if not s:
        return None
    if s["expires"] < time.time():
        _sessions.pop(token, None)
        return None
    return s


def logout(token):
    s = _sessions.pop(token, None)
    if s:
        audit("logout", s["user"])
    return bool(s)


def revoke_all():
    _sessions.clear()
