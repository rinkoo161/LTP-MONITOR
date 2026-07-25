"""Strategy library — defined-risk credit spreads driven by OI walls.

Phase 1: two strategies, PAPER MODE ONLY.
  bull_put_spread : sell PE at S1 support wall, buy hedge 1-2 strikes below.
                    Works in trending-up and rangebound regimes.
  bear_call_spread: sell CE at R1 resistance wall, buy hedge above.
                    Works in trending-down and rangebound regimes.

Each evaluate() returns None (not eligible) or a spread dict:
  {name, symbol, legs:[{action,leg,strike,ltp,security_id}], credit,
   max_loss, width, short_strike, regime, reasons:[...]}
Amounts are per-share; multiply by qty for rupees.
"""

REGIME_FIT = {
    "bull_put_spread":  ("trending-up", "rangebound", "mixed"),
    "bear_call_spread": ("trending-down", "rangebound", "mixed"),
}

META = {
    "bull_put_spread": {
        "title": "Bull Put Spread (OI wall)",
        "desc": ("Sells the PE at the strongest support wall (S1) and buys a "
                 "lower PE as the hedge. Collects premium; profits if the "
                 "index stays above the wall. Time decay works for you."),
        "bias": "neutral-bullish",
    },
    "bear_call_spread": {
        "title": "Bear Call Spread (OI wall)",
        "desc": ("Sells the CE at the strongest resistance wall (R1) and buys "
                 "a higher CE as the hedge. Collects premium; profits if the "
                 "index stays below the wall. Time decay works for you."),
        "bias": "neutral-bearish",
    },
}


def _row(analysis, strike):
    return next((s for s in analysis.get("strikes", [])
                 if s["strike"] == strike), None)


def _wall(analysis, side):
    """First (strongest) signal line level for a side: 'R' or 'S'."""
    lines = (analysis.get("signal_lines") or {}).get(side) or []
    return lines[0]["level"] if lines else None


def evaluate(name, analysis, regime):
    """Return an entry-ready spread or None, with human-readable reasons."""
    if name not in META:
        return None
    reasons, spot = [], analysis.get("spot")
    reg = (regime or {}).get("regime", "unknown")
    if reg not in REGIME_FIT[name]:
        return {"eligible": False,
                "reasons": [f"regime '{reg}' not suited "
                            f"(needs {'/'.join(REGIME_FIT[name])})"]}
    strikes = sorted(s["strike"] for s in analysis.get("strikes", []))
    if len(strikes) < 4 or not spot:
        return {"eligible": False, "reasons": ["not enough chain data"]}
    gap = strikes[1] - strikes[0]

    if name == "bull_put_spread":
        wall = _wall(analysis, "S")
        side, hedge_dir = "pe", -1
        if not wall or wall >= spot:
            return {"eligible": False,
                    "reasons": ["no valid S1 support wall below spot"]}
        dist_ok = (spot - wall) >= gap * 0.8
        reasons.append(f"S1 wall {wall:.0f} is {spot - wall:.0f} pts below spot")
    else:
        wall = _wall(analysis, "R")
        side, hedge_dir = "ce", +1
        if not wall or wall <= spot:
            return {"eligible": False,
                    "reasons": ["no valid R1 resistance wall above spot"]}
        dist_ok = (wall - spot) >= gap * 0.8
        reasons.append(f"R1 wall {wall:.0f} is {wall - spot:.0f} pts above spot")
    if not dist_ok:
        return {"eligible": False,
                "reasons": reasons + ["wall too close to spot (< 0.8 strike gap)"]}

    hedge = wall + hedge_dir * gap * 2          # 2 strikes further out
    srow, hrow = _row(analysis, wall), _row(analysis, hedge)
    if not hrow:                                 # fall back to 1 strike out
        hedge = wall + hedge_dir * gap
        hrow = _row(analysis, hedge)
    if not (srow and hrow):
        return {"eligible": False,
                "reasons": reasons + ["hedge strike outside loaded chain"]}
    s_ltp = srow[side].get("ltp") or 0
    h_ltp = hrow[side].get("ltp") or 0
    credit = round(s_ltp - h_ltp, 2)
    width = abs(hedge - wall)
    max_loss = round(width - credit, 2)
    if credit <= 0 or max_loss <= 0:
        return {"eligible": False,
                "reasons": reasons + ["no net credit at current prices"]}
    if credit < width * 0.15:
        return {"eligible": False,
                "reasons": reasons + [f"credit ₹{credit} too thin "
                                      f"(<15% of ₹{width:.0f} width)"]}
    reasons.append(f"credit ₹{credit} vs max loss ₹{max_loss} "
                   f"({credit / width * 100:.0f}% of width)")
    return {
        "eligible": True, "name": name, "symbol": analysis["symbol"],
        "legs": [
            {"action": "SELL", "leg": side.upper(), "strike": wall,
             "ltp": s_ltp, "security_id": srow[side].get("security_id")},
            {"action": "BUY", "leg": side.upper(), "strike": hedge,
             "ltp": h_ltp, "security_id": hrow[side].get("security_id")},
        ],
        "credit": credit, "max_loss": max_loss, "width": width,
        "short_strike": wall, "regime": reg, "reasons": reasons,
    }
