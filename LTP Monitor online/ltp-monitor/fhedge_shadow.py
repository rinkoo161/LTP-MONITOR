"""fhedge_shadow.py — what the futures delta hedge WOULD have done.

v59.0 Phase D, SHADOW ONLY. Nothing in this module places an order, in
live or in paper. It observes the real S5/S6 credit spreads as they run
and records what a hedge would have done alongside them.

WHAT THE HEDGE IS FOR (spec §7). When spot breaches a bull-put or
bear-call short strike the position is short gamma, and one future is
faster and cheaper than legging out of a spread whose bid-ask has just
widened.

    trigger  breach by fhedge_trigger_buffer_pct of the spread width
    size     spread net delta from bs_greeks, whole lots, capped by
             fhedge_max_lots
    unwind   strike reclaimed by the same buffer, OR the parent spread
             closed, OR end of day

THE INVARIANT THIS MODULE EXISTS TO INSTRUMENT

    "if the parent spread closes, the hedge closes in the same cycle"

A hedge that fails to unwind is not a small accounting error. It is a
naked directional futures position on a 15x instrument that nobody
chose — strictly worse than the short-gamma problem the hedge was added
to solve. A passing unit test does not establish that this holds against
real spread lifecycles, because a test only exercises the cases someone
thought to write.

So instead of asserting it, this measures it, every cycle, against live
data. On each parent close it asks a sharper question than "did the
hedge close":

    would the hedge have unwound on its OWN rules in this cycle?

If not, the forced-close rule is the ONLY thing standing between the
book and a naked future — the rule is load-bearing right there — and the
record carries the MARGIN by which the hedge missed self-unwinding: how
many points spot still is from reclaiming the strike, and how many
minutes remained to EOD. A small margin is a near-miss; a large one
means the forced close carried the entire weight of the invariant. Both
are worth seeing, and neither shows up in a green test.

Forty sessions of this before paper orders are even discussed. Live is
not on the table.
"""
import json
import os
import time

import config
import store

SHADOW_PATH = store.path("fhedge_shadow.jsonl")

BOUNDS = {
    "fhedge_trigger_buffer_pct": (0.0, 0.5),
    "fhedge_max_lots": (1, 5),
    "fhedge_min_parent_lots": (1, 10),
}
DEFAULTS = {
    "fhedge_trigger_buffer_pct": 0.10,
    "fhedge_max_lots": 2,
    # v59.0 item 28. A vertical's net delta peaks near the short strike
    # at roughly 0.15-0.5 per share and collapses toward zero as the
    # breach deepens. On a ONE-lot NIFTY spread that is 11-37 shares
    # against a 75-share futures lot — so any hedge that fires at all is
    # 2x-6.8x the delta it is supposed to neutralise. That is not a
    # hedge, it is a directional futures position wearing a hedge's
    # label: precisely the failure this phase exists to prevent, arriving
    # through SIZING rather than through the unwind invariant.
    #
    # Two independent defences, because either alone can be defeated:
    #   - lots are floored (see hedge_lots), so a fractional requirement
    #     rounds to ZERO and no position is opened;
    #   - and the parent must be at least this many lots before a hedge
    #     may fire at all, so the floor is never the only thing between
    #     the book and an over-hedge.
    "fhedge_min_parent_lots": 3,
}
# EOD unwind, IST. The hedge must be flat before the spread's own
# square-off, not at the same moment as it.
EOD_HH, EOD_MM = 15, 20


def param(name, cfg=None):
    """Clamped on READ, per the standing rule."""
    lo, hi = BOUNDS[name]
    cfg = cfg if cfg is not None else config.load()
    try:
        v = float(cfg.get(name, DEFAULTS[name]))
    except (TypeError, ValueError):
        v = DEFAULTS[name]
    v = max(lo, min(hi, v))
    return int(v) if name in ("fhedge_max_lots", "fhedge_min_parent_lots") else v


def buffer_points(sp, cfg=None):
    """The trigger/reclaim buffer in index points.

    Expressed as a percentage OF THE SPREAD WIDTH, so a 50-point NIFTY
    spread and a 500-point BANKNIFTY spread get proportionate buffers
    rather than one absolute number that is noise on one and a wall on
    the other.
    """
    return (sp.get("width") or 0) * param("fhedge_trigger_buffer_pct", cfg) / 100.0


def breach_points(sp, spot):
    """How far spot is PAST the short strike. Negative = not breached.

    A bull put is short a PE below spot, so it is breached when spot
    falls THROUGH the strike; a bear call is short a CE above spot and is
    breached when spot rises through it. One expression, sign flipped by
    which leg is short — the same shape `_monitor_spreads` uses for its
    own breach check, deliberately, so the two cannot drift apart.
    """
    if not spot or not sp.get("short_strike"):
        return None
    short_leg = (sp.get("legs") or [{}])[0].get("leg")
    k = sp["short_strike"]
    return (k - spot) if short_leg == "PE" else (spot - k)


def net_delta(sp, spot, chain, cfg=None, dte=None):
    """Net delta of the spread in SHARES, or None if it can't be solved.

    Signed from the book's point of view: a bull put that has been
    breached is net LONG delta (it loses as spot falls), so the hedge
    that offsets it is SHORT futures.

    Returns None rather than a guess when IV won't solve. Substituting a
    nominal delta here would size a real futures position off a number
    nobody computed, which is the failure mode this whole engagement is
    about.
    """
    import bs_greeks
    if not spot or dte is None or dte <= 0:
        return None
    total = 0.0
    for leg in sp.get("legs") or []:
        ltp = leg.get("ltp") or leg.get("entry")
        g = bs_greeks.compute_for_leg(spot, leg.get("strike"), ltp, dte,
                                      leg.get("leg") == "CE")
        if not g or g.get("delta") is None:
            return None
        sign = -1.0 if leg.get("action") == "SELL" else 1.0
        total += sign * g["delta"]
    return total * (sp.get("qty") or 0)


def hedge_lots(nd, lot_size, cfg=None):
    """(lots, capped, over_hedge_ratio) offsetting `nd` shares of delta.

    LOTS ARE FLOORED, NEVER ROUNDED TO NEAREST. `math.floor` of 0.9 lots
    is zero — no hedge — and that is the correct answer. Rounding to
    nearest would open a 75-share futures position to neutralise 68
    shares of delta, and at 0.5 lots it would open 75 against 37: a 2x
    directional bet booked as risk reduction. Flooring can only ever
    UNDER-hedge, which leaves the original short-gamma exposure partly
    intact — strictly the safer error, because that exposure is the one
    the trader actually chose.

    `over_hedge_ratio` is hedged_shares / required_shares. Under flooring
    it is <= 1 by construction; it is recorded anyway, on every trigger
    AND every block, because a ratio silently drifting toward or past 1
    is the first sign this reasoning has been broken by a later change.
    Without the column, 40 sessions of sparse output reads as "working"
    even if every firing entry were 3x its delta.
    """
    if not nd or not lot_size:
        return 0, None, None
    raw = abs(nd) / float(lot_size)
    cap = param("fhedge_max_lots", cfg)
    lots = int(raw)                      # floor. See docstring.
    capped = None
    if lots > cap:
        capped = f"{lots} -> {cap} (fhedge_max_lots)"
        lots = cap
    ratio = (lots * float(lot_size)) / abs(float(nd))
    return lots, capped, round(ratio, 3)


def parent_lots_ok(sp, cfg=None):
    """(ok, why) — is the parent spread big enough to hedge at all?"""
    need = param("fhedge_min_parent_lots", cfg)
    have = sp.get("lots") or 0
    if have >= need:
        return True, None
    return False, (f"parent spread is {have} lot(s), below "
                   f"fhedge_min_parent_lots={need} — a hedge here would be "
                   f"directional, not risk-reducing")


def would_unwind(sp, spot, cfg=None, now=None):
    """Would the hedge unwind on its OWN rules this cycle?

    Deliberately EXCLUDES the parent-closed rule. That omission is the
    point: comparing this against the parent's actual close is what
    reveals whether the forced-close rule is load-bearing, and by how
    much. Returns (unwind, reason, margin).
    """
    buf = buffer_points(sp, cfg)
    bp = breach_points(sp, spot)
    t = time.localtime(now if now is not None else time.time())
    mins_to_eod = (EOD_HH * 60 + EOD_MM) - (t.tm_hour * 60 + t.tm_min)
    margin = {"reclaim_gap_pts": (round(bp + buf, 2) if bp is not None else None),
              "minutes_to_eod": mins_to_eod}
    if mins_to_eod <= 0:
        return True, "eod", margin
    if bp is not None and bp <= -buf:
        return True, f"strike reclaimed (spot {spot:.0f}, {-bp:.0f}pts clear)", margin
    return False, None, margin


def write(rec):
    """Append one shadow record. Fail LOUD — a silent write failure here
    would present an empty journal as 'the hedge never triggered'."""
    rec = dict(rec)
    rec.setdefault("ts", time.time())
    rec.setdefault("kind", "fhedge")
    line = json.dumps(rec)          # raises TypeError on unserialisable
    os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
    with open(SHADOW_PATH, "a") as f:
        f.write(line + "\n")
    return rec


def read(limit=2000):
    if not os.path.exists(SHADOW_PATH):
        return []
    out = []
    with open(SHADOW_PATH) as f:
        for line in f.readlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def sessions_observed(recs=None):
    """Distinct IST session dates present. 40 gates the paper discussion."""
    recs = read() if recs is None else recs
    return sorted({time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0)))
                   for r in recs if r.get("ts")})


def invariant_report(recs=None):
    """The only number Phase D is really about.

    `forced_only` counts cycles where a parent spread closed while a
    hedge was open AND the hedge would not have unwound by its own rules
    — every one of those is a cycle where the forced-close rule was the
    sole thing preventing a naked future.
    """
    recs = read() if recs is None else recs
    closes = [r for r in recs if r.get("event") == "parent_close"]
    active = [r for r in closes if r.get("hedge_active")]
    forced = [r for r in active if not r.get("independently_unwound")]
    gaps = [r["margin"]["reclaim_gap_pts"] for r in forced
            if (r.get("margin") or {}).get("reclaim_gap_pts") is not None]
    # item 28 — the sizing watch. Flooring makes ratio <= 1 by
    # construction, so anything above 1 means the floor was defeated.
    trig = [r for r in recs if r.get("event") == "trigger"]
    ratios = [r["over_hedge_ratio"] for r in trig
              if r.get("over_hedge_ratio") is not None]
    return {
        "sessions": len(sessions_observed(recs)),
        "sessions_required": 40,
        "parent_closes": len(closes),
        "closes_with_hedge_open": len(active),
        "forced_only": len(forced),
        "violations": sum(1 for r in active if not r.get("hedge_closed_same_cycle")),
        "closest_near_miss_pts": min(gaps) if gaps else None,
        "widest_margin_pts": max(gaps) if gaps else None,
        "triggers": len(trig),
        "max_over_hedge_ratio": max(ratios) if ratios else None,
        "over_hedged": sum(1 for x in ratios if x > 1.0),
    }
