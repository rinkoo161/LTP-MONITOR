"""daily_marks.py — "already ran today" that survives a restart.

2026-08-08. LearningAgent's daily maintenance was gated on
`self.bus.get("chain_prune_done") != today`. The Bus is an IN-MEMORY
blackboard, so the marker dies with the process and the job is not
once-per-day at all — it is once-per-RESTART.

Measured, from activity.log: `chain_snapshots retention` ran NINE times
between 00:01 and 00:20 while v59.53..v59.58 were being deployed. Each
run scans a 752,254-row table in a 470 MB database, and each held the
write lock long enough to push other writers past their 30s
`busy_timeout`:

    [00:17:58] market_data ⚠ NIFTY: futures OI archive FAILED
               (RuntimeError: database is locked)

and every one of those nine runs did NOTHING — `tier2_thinned: 0,
tier3_thinned: 0`, because nothing is old enough to thin yet.

That also explains the distribution of "database is locked" in the log:
15 on 2026-07-26, none for eleven days, then a cluster today. It tracks
restart frequency, not load.

Stored as a small JSON file rather than a row in history.db ON PURPOSE:
the problem being fixed IS contention on that database, and a marker
that has to take the write lock to record "I finished writing" would be
contending for exactly the resource it is meant to protect.

`stamp` is a caller-supplied string — a date for daily jobs, a week id
for weekly ones — so this covers both without knowing which is which.
"""
import json
import os
import sys

import store

PATH = store.path("daily_marks.json")


def _load():
    if not os.path.exists(PATH):
        return {}
    try:
        d = json.load(open(PATH))
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError) as e:
        # A corrupt marker file must not wedge maintenance forever. Fall
        # back to "nothing has run", which re-runs the job — the same
        # behaviour as before this module existed, and safe because
        # every job it guards is idempotent. Reported, never silent.
        print(f"  ⚠ daily_marks unreadable ({type(e).__name__}: {e}) — "
              f"treating as empty, maintenance will re-run", file=sys.stderr)
        return {}


def done(key, stamp):
    """Has `key` already completed for this stamp (date / week id)?"""
    return _load().get(key) == stamp


def mark(key, stamp):
    """Record that `key` completed for this stamp. Best effort: a failure
    here costs an extra run next restart, which is the old behaviour, so
    it must never propagate into the caller's maintenance block."""
    try:
        d = _load()
        d[key] = stamp
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, PATH)          # atomic; no torn file on a crash
        return True
    except Exception as e:
        print(f"  ⚠ daily_marks.mark({key}) FAILED "
              f"({type(e).__name__}: {e}) — the job will re-run on the "
              f"next restart", file=sys.stderr)
        return False


def all_marks():
    return dict(_load())
