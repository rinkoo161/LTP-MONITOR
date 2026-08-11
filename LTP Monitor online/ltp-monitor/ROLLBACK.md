# ROLLBACK — restoring the verified-stable baseline

**Baseline:** tag `stable-baseline-2026-08-11` = branch `main-stable` = commit
`df340a5` (LTP Monitor **v59.79**; all test suites green; live-paper running).

Created per `ltp-monitor-claude-code-brief.md` Kickoff item 1. The procedure
below was **executed and verified**, not just written — see *Verification* at the
bottom for the actual transcript of a mutate-and-restore round trip.

> **Git repo root is `/Users/user/Documents/Stock Tools`**, one level ABOVE
> `ltp-monitor/`. Every command here is written to be run from that root.
> Running them from inside `ltp-monitor/` silently addresses the wrong paths.

---

## The 10-second version

```bash
cd "/Users/user/Documents/Stock Tools"
git stash push -u -m "pre-rollback $(date +%F-%H%M)"   # keep anything uncommitted
git checkout main-stable                                # or: git checkout stable-baseline-2026-08-11
```

Then restart the app (see *Restarting after a rollback*).

---

## Case 1 — "the working tree is broken, nothing is committed"

Discard uncommitted damage, keep the branch where it is:

```bash
cd "/Users/user/Documents/Stock Tools"
git stash push -u -m "broken $(date +%F-%H%M)"   # recoverable: git stash list / git stash pop
git status --short                               # expect: clean (plus untracked scratch)
```

`git stash push -u` is used instead of `git checkout -- .` so the broken state is
**recoverable**. Diagnosing a bug is easier with the evidence than without it.

## Case 2 — "bad commits are on `main`, and they are NOT pushed"

```bash
cd "/Users/user/Documents/Stock Tools"
git log --oneline stable-baseline-2026-08-11..main    # exactly what will be dropped
git reset --hard stable-baseline-2026-08-11
```

## Case 3 — "bad commits are on `main` and ALREADY PUSHED"

Do **not** force-push shared history. Revert forward instead — it keeps the
record of what happened, which matters for a system whose whole point is an
honest audit trail:

```bash
cd "/Users/user/Documents/Stock Tools"
git revert --no-commit stable-baseline-2026-08-11..main
git commit -m "revert to stable-baseline-2026-08-11 (see incident notes)"
git push origin main
```

## Case 4 — "I only need ONE file back"

```bash
cd "/Users/user/Documents/Stock Tools"
git checkout stable-baseline-2026-08-11 -- "LTP Monitor online/ltp-monitor/agents.py"
```

## Case 5 — "I need to inspect the baseline without disturbing anything"

```bash
cd "/Users/user/Documents/Stock Tools"
git worktree add /tmp/ltp-baseline stable-baseline-2026-08-11
# ...look around in /tmp/ltp-baseline...
git worktree remove /tmp/ltp-baseline
```

---

## Restarting after a rollback

Code and runtime state are **separate**, and a rollback only moves the code:

```bash
cd "/Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor"
pkill -f "python.*app.py"
sleep 3
nohup ./venv/bin/python3 app.py > ~/.ltp-monitor/app.out 2>&1 &
sleep 8
curl -s http://127.0.0.1:8000/api/version      # expect the rolled-back version
```

**What a code rollback does NOT undo** — everything in `~/.ltp-monitor/` is
outside the repo and survives untouched:

| State | File | Note |
|---|---|---|
| Settings/credentials | `config.json` | A rolled-back version may not know newer keys; `config.save()` drops unknown keys, so **re-check Settings after rolling back across a config change**. |
| Trade record | `trades.jsonl` | Restatements keep timestamped backups beside it. |
| Strategy versions | `strategy_versions.json` | `clean_versions.py` keeps `.pre-clean-*` backups. |
| Open positions | `open_state.json` | Re-seeded on start; reconciled against the broker in live mode. |
| Market history | `history.db` | 460 MB+; never rebuilt by a rollback. |

If a rollback is needed because of a **data** problem rather than a code problem,
restore the relevant backup file explicitly; the git history does not contain it.

---

## Verification (performed 2026-08-11)

The brief requires this procedure to be *tested*, not asserted. Transcript:

```
$ git checkout -b scratch-rollback-verify stable-baseline-2026-08-11
$ shasum -a 256 "LTP Monitor online/ltp-monitor/agents.py"
  SHA-BEFORE : 2dc74ebf2e51d415cbf8f30f9febc141bd41e810ae414305f430b8a7ace0f6d4
$ echo "# deliberate corruption for rollback verification" >> ".../agents.py"
  SHA-MUTATED: c5ae56612792a49a1c1735ff9389de8c0d960dc982d28924540afa506d7533b4
$ git checkout -- "LTP Monitor online/ltp-monitor/agents.py"
  SHA-AFTER  : 2dc74ebf2e51d415cbf8f30f9febc141bd41e810ae414305f430b8a7ace0f6d4
$ git checkout main && git branch -D scratch-rollback-verify
```

`SHA-AFTER == SHA-BEFORE` and both differ from `SHA-MUTATED`.

Result: **byte-identical restore confirmed** (see `test_rollback.py`, which
re-runs this round trip programmatically so the guarantee is checked by CI
rather than remembered).
