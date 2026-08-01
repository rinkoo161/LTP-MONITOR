"""futures_research_api.py — state for the Futures Research page.

v59.0 §8 + item 30. This page is an EVIDENCE RECORD, not a deploy
surface. It exists so that nobody re-enables futures, or trusts an
options strategy's backtest, without first meeting the evidence that
said otherwise.

Every function here READS. Nothing in this module writes config, flips a
switch, or touches `strategy_versions.json`. `test_futures_research.py`
asserts that.

PROVISIONAL LABELS MUST REACH THE SCREEN. The ₹1,143 and the cost bias
both carry provenance strings in `promotion_gate`, and a payload that
carries provenance nobody renders is the same failure as not having it:
the number hardens into a constant because the caveat was invisible. So
these payloads put the provenance at the TOP LEVEL of each panel that
uses the number, not buried per-row, and the page renders it as a
visible footnote.
"""
import os
import time

import config


# --------------------------------------------------------------- panel 1
# The S11 worked example is PERMANENT, per explicit instruction. It is
# the cleanest demonstration in this codebase of a cost model changing a
# verdict: identical 325 trades, opposite conclusions.
S11_WORKED_EXAMPLE = {
    "strategy": "s11_momentum",
    "symbol": "NIFTY",
    "trades": 325,
    "flat_model_pnl": 63531,
    "notional_model_pnl": -31000,
    "flat_model_verdict": "PROFITABLE — would have been deployed",
    "notional_model_verdict": "FAIL — no edge above transaction costs",
    "note": ("Same 325 trades, same signals, same exits. The ONLY "
             "difference is the cost model: flat fee_per_lot=40 vs a "
             "notional-aware model. A flat per-lot fee understates a "
             "futures round trip by roughly 10x because futures taxes "
             "are levied on NOTIONAL, and NIFTY futures notional is "
             "~₹18 lakh per lot."),
}


def postmortem():
    """Phase 0 finding — exit histogram and the headline numbers."""
    out = {"available": False, "worked_example": S11_WORKED_EXAMPLE}
    try:
        import contextlib
        import io
        import futures_postmortem as fp
        trades = fp.load()
        if trades:
            # report() is a CLI first — it prints its whole analysis and
            # returns the dict at the end. Capturing stdout keeps an HTTP
            # handler from dumping a page of text into the server log on
            # every poll, without forking a second copy of the analysis.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                res = fp.report(trades, as_json=True) or {}
            out.update(res)
            out["available"] = True
        else:
            out["reason"] = ("no futures trades in the journal — futures "
                             "have been disabled since Phase 0")
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
    return out


# --------------------------------------------------------------- panel 9
def promotion_gate_table():
    """Every live strategy under the applied gate. THE options finding.

    Panel 3's chips cover futures, which are already disabled. This is
    the one that describes strategies trading right now.
    """
    import backtester as bt
    import promotion_gate as pg
    cfg = config.load()
    rows = []
    for name, node in sorted((bt.load_versions() or {}).items()):
        for sym, entry in sorted((node.get("symbols") or {}).items()):
            live = bool(entry.get("live_enabled")
                        and not entry.get("manually_disabled"))
            if not live:
                continue
            m = {}
            for v in entry.get("versions") or []:
                if v.get("v") == entry.get("active"):
                    m = v.get("results") or {}
            if not m.get("trades"):
                continue
            ok, d = pg.evaluate_entry(name, sym, m)
            rows.append({
                "strategy": name, "symbol": sym,
                "trades": d.get("trades"),
                "net_per_trade": _r(d.get("net_per_trade")),
                "cost_bias": _r(d.get("cost_bias")),
                "stat_margin": _r(d.get("stat_margin")),
                "required": _r(d.get("required")),
                "headroom": _r(d.get("headroom")),
                "t": (round(d["t_own"], 2) if d.get("t_own") is not None else None),
                "verdict": "PASS" if ok else "FAIL",
                "own_sd": _r(d.get("own_sd")),
                "own_sd_source": d.get("own_sd_source"),
                "legacy_required": _r(d.get("legacy_required")),
            })
    rows.sort(key=lambda r: -(r["headroom"] if r["headroom"] is not None else -1e9))
    ts = [r["t"] for r in rows if r["t"] is not None]
    return {
        "rows": rows,
        "passing": sum(1 for r in rows if r["verdict"] == "PASS"),
        "total": len(rows),
        "max_t": max(ts) if ts else None,
        "expected_max_t_pure_noise": 1.59,
        "headline": ("No strategy's edge is distinguishable from zero, and "
                     "measurement uncertainty is wide enough that one could "
                     "plausibly be above it."),
        "formula": "required = cost_bias + 2 x sqrt(own_sd^2/n + 1143^2/74)",
        # Rendered as a visible footnote — see module docstring.
        "sd_provenance": pg.PROXY_SD_PROVENANCE,
        "cost_provenance": pg.COST_PROVENANCE if hasattr(pg, "COST_PROVENANCE")
        else pg.COST_BIAS_PROVENANCE,
        "provisional": True,
    }


def _r(v, nd=0):
    return round(v, nd) if isinstance(v, (int, float)) else None


# --------------------------------------------------------------- panel 8
def cost_readout():
    """Lot size with its source, round-trip cost, and drag on gross edge.

    Exists because a stale lot size or tax rate silently corrupts every
    expectancy number on this page without ever throwing — which is
    exactly what fee_per_lot = 40 was doing.
    """
    import futures_costs as fc
    import options_costs as oc
    cfg = config.load()
    out = {"symbols": [], "flat_fee_per_lot": cfg.get("fee_per_lot", 40)}
    SPOT = {"NIFTY": 24300, "BANKNIFTY": 57300, "FINNIFTY": 26300,
            "SENSEX": 80000}
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        row = {"symbol": sym}
        cfg_lot = (cfg.get("lot_sizes") or {}).get(sym)
        row["config_lot_size"] = cfg_lot
        try:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                # lot_size() returns (size, source), not a bare int.
                row["lot_size"], row["lot_source"] = fc.lot_size(sym, cfg)
        except Exception as e:
            # A failed lookup must be visible, never silently defaulted.
            row["lot_size"] = cfg_lot
            row["lot_source"] = f"config fallback — scrip master failed: {e}"
        # This is the exact failure the panel exists for: a stale lot size
        # throws nothing and silently rescales every rupee on the page.
        if cfg_lot and row["lot_size"] and cfg_lot != row["lot_size"]:
            row["lot_mismatch"] = (
                f"config says {cfg_lot}, scrip master says {row['lot_size']} "
                f"({100*(cfg_lot-row['lot_size'])/row['lot_size']:+.0f}%) — "
                f"every rupee figure derived from the config value is wrong "
                f"by that factor")
        lot = row["lot_size"] or 0
        s = SPOT.get(sym, 24000)
        if lot:
            prem = s * 0.005
            b = oc.cost_round_trip(prem, prem, lot, legs=1, cfg=cfg,
                                   halfspread=0.325)
            row["option_rt_rupees"] = round(b["total"])
            row["option_rt_statutory"] = round(b["statutory"])
            row["option_rt_spread"] = round(b["spread"])
            row["flat_model_rupees"] = round(oc.flat_model_cost(1, cfg=cfg))
            row["understatement"] = round(b["total"] - row["flat_model_rupees"])
        out["symbols"].append(row)
    out["spread_distribution"] = {
        "median_points": 0.65, "mean_points": 2.27, "max_points": 15.80,
        "n": 73,
        "note": ("Heavily right-skewed: mean is 3.5x median and the tail "
                 "reaches 24x. Every fixed-spread assumption is the wrong "
                 "SHAPE, so the figures above are a LOWER bound on cost."),
    }
    return out


# --------------------------------------------------------------- panel 7
def hedge_monitor():
    """Phase D shadow state. Nothing here has ever placed an order."""
    import fhedge_shadow as fh
    recs = fh.read()
    rep = fh.invariant_report(recs)
    return {
        "shadow_only": True,
        "orders_placed": 0,
        "report": rep,
        "sessions_observed": rep.get("sessions"),
        "sessions_required": rep.get("sessions_required"),
        "recent": recs[-50:],
        "note": ("SHADOW ONLY — no order is placed in live or paper. "
                 "40 sessions are required before paper orders are even "
                 "discussed. A vertical's net delta peaks near the short "
                 "strike at ~0.15-0.5/share, so a one-lot spread can never "
                 "size a whole hedge lot: sparse output is the instrument "
                 "working, not failing."),
    }


# --------------------------------------------------------------- panel 2
def state_strip(bus=None):
    """Per-symbol futures state, including the basis residual."""
    out = []
    if bus is None:
        return out
    for sym in bus.get("symbols", []) or []:
        obs = bus.get(f"basis_residual:{sym}") or {}
        a = bus.get(f"analysis:{sym}") or {}
        reg = bus.get(f"regime:{sym}") or {}
        out.append({
            "symbol": sym,
            "spot": a.get("spot"),
            "future": obs.get("future"),
            "basis": _r(obs.get("actual_basis"), 2),
            "fair_basis": _r(obs.get("fair_basis"), 2),
            "residual": _r(obs.get("residual"), 2),
            "residual_z": (round(obs["residual_z"], 2)
                           if obs.get("residual_z") is not None else None),
            "z_ready": obs.get("z_ready", False),
            "z_samples": obs.get("z_samples"),
            "z_window": obs.get("z_window"),
            # Never hide a data limitation — rendered as an amber dot.
            "approx": obs.get("approx", True),
            "regime": reg.get("regime"),
        })
    return out


def research_state(bus=None):
    """Everything the page needs in one call."""
    cfg = config.load()
    return {
        "ts": time.time(),
        "futures_strategy_enabled": bool(cfg.get("futures_strategy_enabled")),
        "futures_live_enabled": bool(cfg.get("futures_live_enabled")),
        "read_only": True,
        "postmortem": postmortem(),
        "gate": promotion_gate_table(),
        "costs": cost_readout(),
        "hedge": hedge_monitor(),
        "state": state_strip(bus),
    }
