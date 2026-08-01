#!/usr/bin/env python3
"""manage_users.py — account recovery from the host.

    python3 manage_users.py list
    python3 manage_users.py passwd <username>        # prompts, never echoes
    python3 manage_users.py mfa-reset <username>     # re-issue an authenticator
    python3 manage_users.py create <username> [--admin]
    python3 manage_users.py delete <username>
    python3 manage_users.py role <username> admin|user

v58.75. "Forgot password" in a self-hosted app with no mail server has
exactly two honest answers: another admin resets it, or someone with
access to the machine does. There is no third option that is not
security theatre — an emailed reset link needs mail the app does not
have, and a security-question flow is a weaker password.

So the recovery boundary is FILESYSTEM ACCESS. This script reads and
writes the same store the app uses (`~/.ltp-monitor/users.json`, mode
0600), which means running it already proves the authority to change
credentials. It works whether or not the app is running, and whether or
not anyone can still log in — which is the case that matters, because a
locked-out sole admin cannot be helped by anything served over HTTP.

Passwords are read with getpass (never echoed, never in shell history,
never an argv the process table can show).
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth
import store


def _prompt_password(who):
    a = getpass.getpass(f"new password for {who}: ")
    b = getpass.getpass("confirm: ")
    if a != b:
        sys.exit("passwords do not match")
    if len(a) < 8:
        sys.exit("password must be at least 8 characters")
    return a


def cmd_list(_):
    users = auth.list_users()
    if not users:
        print("no accounts yet — create one at /setup, or:")
        print("  python3 manage_users.py create <username> --admin")
        return
    print(f"  store: {store.path('users.json')}\n")
    print(f"  {'username':16} {'role':6} {'MFA':4} {'locked':7} last login")
    for u in users:
        print(f"  {u['username']:16} {u['role']:6} "
              f"{'yes' if u['mfa'] else 'NO':4} "
              f"{'YES' if u['locked'] else '-':7} {u.get('last_login') or 'never'}")


def cmd_passwd(a):
    pw = _prompt_password(a.username)
    auth.set_password(a.username, pw)
    print(f"password updated for {a.username}")
    print("any existing sessions for this account remain valid until they "
          "expire — restart the app to revoke them immediately")


def cmd_mfa_reset(a):
    """Clear MFA and issue a fresh enrollment — the lost-phone case."""
    auth.disable_mfa(a.username)
    secret, uri = auth.begin_mfa_enrollment(a.username)
    print(f"MFA reset for {a.username}. Add this key to the authenticator:\n")
    print(f"    {secret}\n")
    print(f"  otpauth URI: {uri}\n")
    print("Then confirm it (the app must be running):")
    print("  curl -s -X POST localhost:8000/api/auth/mfa/confirm \\")
    print(f"       -H 'Content-Type: application/json' \\")
    print(f"       -d '{{\"username\":\"{a.username}\",\"code\":\"<6 digits>\"}}'")
    print("\nOr just sign in — the login page offers pairing when MFA is "
          "not yet active.")


def cmd_create(a):
    pw = _prompt_password(a.username)
    auth.create_user(a.username, pw, "admin" if a.admin else "user")
    print(f"created {a.username} ({'admin' if a.admin else 'user'})")
    print("now pair an authenticator:  python3 manage_users.py mfa-reset "
          + a.username)


def cmd_delete(a):
    auth.delete_user(a.username)
    print(f"deleted {a.username}")


def cmd_role(a):
    users = auth._load()
    if a.username not in users:
        sys.exit("no such user")
    if a.role not in ("admin", "user"):
        sys.exit("role must be admin or user")
    users[a.username]["role"] = a.role
    auth._save(users)
    auth.audit("user.role", a.username, {"role": a.role})
    print(f"{a.username} is now {a.role}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    for name, fn in (("passwd", cmd_passwd), ("mfa-reset", cmd_mfa_reset),
                     ("delete", cmd_delete)):
        p = sub.add_parser(name); p.add_argument("username"); p.set_defaults(fn=fn)
    p = sub.add_parser("create"); p.add_argument("username")
    p.add_argument("--admin", action="store_true"); p.set_defaults(fn=cmd_create)
    p = sub.add_parser("role"); p.add_argument("username"); p.add_argument("role")
    p.set_defaults(fn=cmd_role)
    a = ap.parse_args()
    try:
        a.fn(a)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
