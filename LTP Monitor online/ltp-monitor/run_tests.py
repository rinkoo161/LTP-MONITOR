#!/usr/bin/env python3
"""run_tests.py — run the suite against a PRIVATE store.

    python3 run_tests.py                 # all tests, temp store
    python3 run_tests.py futures s10     # only tests matching these words
    python3 run_tests.py --timeout 30    # per-test limit (default 90s)
    python3 run_tests.py --home /tmp/x   # keep the store for inspection

Why this exists (2026-07-31): the tests are standalone scripts that
import the real modules, so anything constructing a Bus, deploying a
paper future or persisting a candle wrote into ~/.ltp-monitor. A suite
run that day put 11 fake futures signals into the operator's shadow
journal and a run of S10 observations into activity.log, which were
then read back as real trading activity. LTP_MONITOR_HOME (see
store.py) redirects the whole application; this runner sets it so no
individual test has to remember.

Two further things learned the hard way in the same session:

  * a per-test timeout is mandatory — test_kotak_ws.py blocks forever
    on a live websocket connect when no Kotak credentials exist;
  * results depend on the operator's own config.json (a `fee_per_lot`
    of 30 vs the hardcoded 40 fails test_futures_trading) and on the
    time of day (deploy tests gate on market hours), so a fresh store
    with DEFAULTS is the only reproducible baseline. Tests needing
    credentials will fail here, and that is the honest outcome rather
    than a number that changes per machine.
"""
import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("patterns", nargs="*", help="substrings; empty = all tests")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--home", default=None,
                    help="store directory (default: a fresh temp dir)")
    ap.add_argument("--keep-going", action="store_true", default=True)
    a = ap.parse_args()

    home = a.home or tempfile.mkdtemp(prefix="ltp-test-store-")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, LTP_MONITOR_HOME=home)

    tests = sorted(p.name for p in BASE.glob("test_*.py"))
    if a.patterns:
        tests = [t for t in tests if any(p.lower() in t.lower() for p in a.patterns)]
    if not tests:
        print("no tests matched")
        return 1

    print(f"store: {home}")
    print(f"tests: {len(tests)}   timeout: {a.timeout}s per test\n", flush=True)

    # v59.0 item 40 — exit code 77 means SKIPPED, not failed. Some tests
    # genuinely cannot run without live broker credentials or an
    # interactive TTY, and reporting those as failures leaves the suite
    # permanently red. A permanently-red suite is how a real regression
    # arrives invisible — which is exactly what happened on 2026-08-01,
    # when a live regression hid inside an already-failing file.
    SKIP_CODE = 77
    passed, failed, timed_out, skipped = [], [], [], []
    for t in tests:
        try:
            r = subprocess.run([sys.executable, t], cwd=BASE, env=env,
                               capture_output=True, text=True, timeout=a.timeout)
        except subprocess.TimeoutExpired:
            timed_out.append(t)
            print(f"  TIMEOUT  {t}  (>{a.timeout}s)", flush=True)
            continue
        if r.returncode == 0:
            passed.append(t)
            print(f"  PASS     {t}", flush=True)
        elif r.returncode == SKIP_CODE:
            why = next((l.strip() for l in (r.stdout + r.stderr).splitlines()
                        if "SKIP" in l), "")
            skipped.append(t)
            print(f"  SKIP     {t}   {why[:90]}", flush=True)
        else:
            failed.append(t)
            body = (r.stdout + r.stderr).splitlines()
            hint = next((l.strip() for l in body
                         if l.strip().startswith("- ") or "Error" in l), "")
            print(f"  FAIL     {t}   {hint[:90]}", flush=True)

    total = len(tests)
    print(f"\n{'=' * 62}")
    print(f"  passed {len(passed)}/{total}   failed {len(failed)}   "
          f"skipped {len(skipped)}   timed out {len(timed_out)}")
    if failed:
        print("  failed:  " + ", ".join(failed))
    if skipped:
        print("  skipped: " + ", ".join(skipped))
    if timed_out:
        print("  timeout: " + ", ".join(timed_out))
    if not a.home:
        print(f"\n  (store was {home} — a temp dir, safe to delete)")
    return 1 if (failed or timed_out) else 0


if __name__ == "__main__":
    sys.exit(main())
