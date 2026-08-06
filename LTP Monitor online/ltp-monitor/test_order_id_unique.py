#!/usr/bin/env python3
"""test_order_id_unique.py — a paper order id must identify ONE position.

2026-08-06. It was `f"PAPER-{int(time.time())}"` — second resolution —
so any two positions opened in the same second shared one. Found while
purging duplicate journal closes: a filter keyed on order_id reported
FOUR duplicate groups, and two of them were not duplicates at all but
different instruments sharing an id:

    PAPER-1785143942 -> SENSEX + FINNIFTY + NIFTY futures  (14:49:02)
    PAPER-1785144544 -> NIFTY + SENSEX futures             (14:59:04)

A filter that trusted the id would have deleted five genuine trades.
The script refused to write because the group count did not match what
was expected, which is the only reason it was noticed.

Collisions across PROCESSES are not hypothetical either: on the same
day a restart left the previous process alive for three minutes and
both wrote to the same journal.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_order_id_unique")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents

print("1) ids minted in the same second are distinct")
ids = [agents.paper_order_id() for _ in range(2000)]
check("2000 ids in a tight loop are unique", len(set(ids)) == len(ids),
      f"{len(set(ids))}/{len(ids)} — the old form collided on ANY two "
      f"positions opened in the same second")
secs = {i.split("-")[1] for i in ids}
check("and they genuinely spanned few seconds", len(secs) <= 3,
      f"{len(secs)} distinct epoch values — if this were large the test "
      f"would be proving nothing about same-second collisions")

print("\n2) unique across threads")
# manual_trade() runs on the HTTP thread while cycle() runs on the agent
# thread, so concurrent minting is the normal case, not an edge one.
out = []
lock = threading.Lock()


def _w():
    local = [agents.paper_order_id() for _ in range(500)]
    with lock:
        out.extend(local)


ts = [threading.Thread(target=_w) for _ in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
check("2000 ids across 4 threads are unique", len(set(out)) == len(out),
      f"{len(set(out))}/{len(out)}")

print("\n3) it stays sortable and recognisable")
check("still prefixed PAPER-", all(i.startswith("PAPER-") for i in ids[:50]),
      "live order ids come from the broker; the prefix is what "
      "distinguishes a simulated fill")
check("epoch prefix retained so ids sort chronologically",
      all(i.split("-")[1].isdigit() for i in ids[:50]), ids[0])

print("\n4) both call sites use the helper, not the old literal")
HERE = os.path.dirname(os.path.abspath(__file__))
AG = open(os.path.join(HERE, "agents.py")).read()
# Match the ASSIGNMENT, not the string. Filtering "#" comments was not
# enough — the helper's own DOCSTRING quotes the old form to explain
# what it replaced, and a bare substring search flagged that. Third
# prose-matching slip of the day (after test_symbol_hold's gate label
# and test_mechanism_defects' prompt check). Precision beats a wider
# filter: an assignment cannot appear in prose.
import re as _re
_assigns = _re.findall(r'order_id\s*=\s*f"PAPER-\{int\(time\.time\(\)\)\}"', AG)
check("no remaining second-resolution id ASSIGNMENT", not _assigns,
      f"{_assigns} — one missed site reintroduces the collision there")
check("both paper paths mint through the helper",
      len(_re.findall(r'order_id\s*=\s*paper_order_id\(\)', AG)) == 2,
      "options and futures — a helper only half-adopted is worse than "
      "none, because the collision becomes intermittent")

print("\n5) the identity a journal filter should use")
# The real lesson is not just uniqueness: any dedup over historical
# records STILL has to include the instrument, because ids minted
# before this change are not unique and cannot be made so.
a = {"order_id": "PAPER-1", "symbol": "NIFTY", "strike": 24650.0, "leg": "CE"}
b = {"order_id": "PAPER-1", "symbol": "SENSEX", "strike": None, "leg": None}
key = lambda t: (t.get("order_id"), t.get("symbol"), t.get("strike"), t.get("leg"))
check("same id + different instrument is NOT the same position",
      key(a) != key(b),
      "five genuine 2026-07-27 futures trades were one filter away from "
      "deletion on this exact shape")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all order-id checks passed")
