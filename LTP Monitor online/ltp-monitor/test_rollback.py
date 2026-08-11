#!/usr/bin/env python3
"""test_rollback.py — the rollback guarantee, re-checked by execution.

Per ltp-monitor-claude-code-brief.md Kickoff item 1, the rollback must be
TESTED, not asserted. ROLLBACK.md records one verified round trip; this
re-runs it on demand so the guarantee survives future history rewrites,
tag deletions, and moved repo roots.

Safe by construction: works on a throwaway branch cut from the baseline
tag, restores the original branch in a finally block, and refuses to run
at all if the tracked tree is dirty (it would otherwise carry a dirty
tree across a checkout and could lose work).
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
PROBE = "LTP Monitor online/ltp-monitor/agents.py"
SCRATCH = "scratch-rollback-verify-test"

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def git(*args, check_rc=True):
    r = subprocess.run(["git", "-C", REPO, *args],
                       capture_output=True, text=True)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def sha(path):
    with open(os.path.join(REPO, path), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --- the baseline must exist and be reachable ---------------------------
tags = git("tag", "-l", "stable-baseline-*").split()
check("a stable-baseline tag exists", bool(tags), str(tags))
if not tags:
    print("\nno baseline tag — nothing to verify")
    sys.exit(1)
BASELINE = sorted(tags)[-1]
check("main-stable branch points somewhere",
      bool(git("rev-parse", "--verify", "-q", "main-stable", check_rc=False)))
check("ROLLBACK.md documents THIS baseline",
      BASELINE in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "ROLLBACK.md")).read(),
      BASELINE)

# --- refuse to run against a dirty tree ---------------------------------
dirty = git("status", "--porcelain", "--untracked-files=no")
if dirty:
    print("\nTracked tree is dirty — refusing to switch branches.\n"
          "Commit or stash first; this test must never risk your work.\n"
          f"{dirty[:400]}")
    sys.exit(1)
check("tracked tree is clean (safe to branch)", True)

# --- the round trip -----------------------------------------------------
original = git("rev-parse", "--abbrev-ref", "HEAD")
restored = False
try:
    git("checkout", "-q", "-b", SCRATCH, BASELINE)
    before = sha(PROBE)
    with open(os.path.join(REPO, PROBE), "a") as f:
        f.write("\n# deliberate corruption for rollback verification\n")
    mutated = sha(PROBE)
    check("the probe file was actually corrupted", before != mutated)
    git("checkout", "-q", "--", PROBE)
    after = sha(PROBE)
    check("rollback restores the file BYTE-IDENTICALLY",
          after == before, f"{before[:12]}… vs {after[:12]}…")
finally:
    git("checkout", "-q", "--", PROBE, check_rc=False)
    git("checkout", "-q", original, check_rc=False)
    git("branch", "-q", "-D", SCRATCH, check_rc=False)
    restored = git("rev-parse", "--abbrev-ref", "HEAD") == original
check("the original branch is restored and the scratch branch removed",
      restored and SCRATCH not in git("branch", "--list", SCRATCH))

# --- secrets must stay out of the tracked tree --------------------------
tracked = git("ls-files")
check("no config.json / .env / key material is tracked",
      not [p for p in tracked.splitlines()
           if p.endswith(("config.json", ".env", ".pem", ".key"))])
ignored = open(os.path.join(REPO, ".gitignore")).read()
for pat in ("config.json", ".env", "*.key", ".ltp-monitor/"):
    check(f".gitignore covers {pat}", pat in ignored)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all rollback checks passed")
