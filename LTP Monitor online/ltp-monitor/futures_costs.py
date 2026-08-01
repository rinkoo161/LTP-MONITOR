"""futures_costs.py — notional-aware round-trip cost for index futures.

v59.0 Phase 0 §3.2. `fee_per_lot = 40` is an options-shaped assumption:
a flat rupee charge per lot. Futures STT is a PERCENTAGE OF NOTIONAL, so
on one NIFTY lot at 24,800 (₹18.6 lakh notional) the sell-side STT alone
is ~₹372 — against a model charging ₹40 for the whole round trip.

That matters beyond accounting. `is_live_enabled()` reads backtest
profitability, so every futures number produced under the flat model is
inflated and a strategy could be promoted to live on costs that are an
order of magnitude too low. This is the same class of defect
`warn_if_costs_disabled()` was written to catch, one level subtler: not
zero cost, but wrong-shaped cost.

LOT SIZE IS NOT HARDCODED. It resolves from the Dhan scrip master, and
a failed lookup RAISES — per the standing rule, because a silently
defaulted lot size corrupts every expectancy number on the page without
ever throwing. `config["lot_sizes"]` is compared against it and a
mismatch warns loudly, but never overrides it.

All rates are config-driven, registered in both config.DEFAULTS and
SettingsIn, and CLAMPED ON READ — a 15x-levered instrument is the last
place to trust a value that was only validated when it was written.
"""
import threading

import config

# name -> (default, lo, hi). Read-time clamping uses these, so an
# out-of-bounds value already sitting in config.json cannot leak into a
# cost number.
RATES = {
    "fut_brokerage_per_order": (20.0, 0.0, 100.0),
    "fut_stt_sell_pct":        (0.0002, 0.0, 0.001),
    "fut_exchange_txn_pct":    (0.0000173, 0.0, 0.0001),
    "fut_sebi_turnover_pct":   (0.000001, 0.0, 0.00001),
    "fut_stamp_duty_pct":      (0.00002, 0.0, 0.0001),
    "fut_gst_pct":             (0.18, 0.0, 0.30),
    "fut_slippage_points":     (1.0, 0.0, 10.0),
}

_lot_cache = {}
_lock = threading.Lock()


class LotSizeUnavailable(RuntimeError):
    """Raised rather than defaulting. See the module docstring."""


def rate(name, cfg=None):
    """A cost rate, clamped to its bounds AT READ TIME."""
    if name not in RATES:
        raise KeyError(f"unknown futures cost rate {name!r}")
    default, lo, hi = RATES[name]
    cfg = cfg if cfg is not None else config.load()
    try:
        v = float(cfg.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def lot_size(symbol, cfg=None):
    """Contract size from the Dhan scrip master.

    The scrip master is authoritative and a failed lookup RAISES — never
    guess. config["lot_sizes"] is compared against it and a mismatch is
    reported loudly, but never wins: a wrong contract size scales every
    rupee figure derived from it and throws nothing.
    """
    sym = (symbol or "").upper()
    cfg = cfg if cfg is not None else config.load()
    with _lock:
        if sym in _lot_cache:
            return _lot_cache[sym]
    # The scrip master is AUTHORITATIVE. An earlier draft of this function
    # preferred config["lot_sizes"] as an "operator override", which is
    # exactly the standing rule's failure mode — and it is not
    # hypothetical here: on 2026-08-01 config said NIFTY 75 / FINNIFTY 65
    # while the live Aug-2026 contracts are 65 / 60. Preferring config
    # would have kept every NIFTY futures rupee figure 15% too large and
    # thrown nothing. Exchange lot sizes are revised periodically to hold
    # contract value in a band; a static map goes stale silently.
    try:
        import dhan_scrip_master as sm
        row = sm.get_current_future_detailed(sym)
        # this API returns (row, csv_text) in some versions and a bare row
        # in others — unwrap defensively rather than assume one shape
        if isinstance(row, tuple):
            row = row[0]
        if isinstance(row, list):
            row = row[0] if row else None
    except Exception as e:
        raise LotSizeUnavailable(
            f"scrip master lookup failed for {sym}: {type(e).__name__}: {e}") from e
    ls = (row or {}).get("lot_size")
    try:
        ls = int(float(ls))
    except (TypeError, ValueError):
        ls = 0
    if not ls:
        raise LotSizeUnavailable(
            f"no lot size for {sym} in the Dhan scrip master — refusing to "
            f"default, because a guessed contract size silently corrupts "
            f"every cost and expectancy number derived from it")
    stale = (cfg.get("lot_sizes") or {}).get(sym)
    if stale and int(stale) != ls:
        # Loud, once per symbol per process. Silence here is the whole bug.
        print(f"  [futures_costs] WARNING: config.lot_sizes[{sym}]={stale} but the "
              f"scrip master says {ls} ({100*(int(stale)-ls)/ls:+.0f}%). Using {ls}. "
              f"Every rupee figure computed from the stale value is wrong by that "
              f"factor and throws nothing.")
    with _lock:
        _lot_cache[sym] = (ls, "dhan scrip master")
    return ls, "dhan scrip master"


def cost_round_trip(symbol, entry, exit_px, lots=1, cfg=None, lot=None):
    """Total round-trip cost in RUPEES for `lots` lots.

    Formula per spec §3.2. Returns a float; raises LotSizeUnavailable if
    the contract size cannot be established.
    """
    cfg = cfg if cfg is not None else config.load()
    ls = int(lot) if lot else lot_size(symbol, cfg)[0]
    lots = max(1, int(lots or 1))
    qty = ls * lots
    entry_notional = float(entry) * qty
    exit_notional = float(exit_px) * qty
    turnover = entry_notional + exit_notional

    brokerage = rate("fut_brokerage_per_order", cfg) * 2
    stt = rate("fut_stt_sell_pct", cfg) * exit_notional      # sell side only
    exch = rate("fut_exchange_txn_pct", cfg) * turnover
    sebi = rate("fut_sebi_turnover_pct", cfg) * turnover
    stamp = rate("fut_stamp_duty_pct", cfg) * entry_notional
    gst = rate("fut_gst_pct", cfg) * (brokerage + exch + sebi)
    slip = rate("fut_slippage_points", cfg) * qty * 2        # both sides

    return brokerage + stt + exch + sebi + stamp + gst + slip


def breakdown(symbol, entry, exit_px, lots=1, cfg=None, lot=None):
    """Same computation, itemised — what Panel 8 renders."""
    cfg = cfg if cfg is not None else config.load()
    if lot:
        ls, src = int(lot), "caller"
    else:
        ls, src = lot_size(symbol, cfg)
    lots = max(1, int(lots or 1))
    qty = ls * lots
    en, ex = float(entry) * qty, float(exit_px) * qty
    turnover = en + ex
    b = rate("fut_brokerage_per_order", cfg) * 2
    e = rate("fut_exchange_txn_pct", cfg) * turnover
    s = rate("fut_sebi_turnover_pct", cfg) * turnover
    items = {
        "brokerage": b,
        "stt_sell": rate("fut_stt_sell_pct", cfg) * ex,
        "exchange_txn": e,
        "sebi_turnover": s,
        "stamp_duty": rate("fut_stamp_duty_pct", cfg) * en,
        "gst": rate("fut_gst_pct", cfg) * (b + e + s),
        "slippage": rate("fut_slippage_points", cfg) * qty * 2,
    }
    total = sum(items.values())
    statutory = total - items["slippage"]
    return {
        "symbol": (symbol or "").upper(), "lot_size": ls, "lot_size_source": src,
        "lots": lots, "qty": qty, "entry": float(entry), "exit": float(exit_px),
        "entry_notional": en, "exit_notional": ex,
        "items": items, "total_rupees": total,
        "total_points": total / qty if qty else None,
        "statutory_rupees": statutory,
        "statutory_points": statutory / qty if qty else None,
    }


def warn_if_flat_cost_model(log=lambda m: None, cfg=None):
    """Extension of backtester.warn_if_costs_disabled for futures.

    Fires when a futures replay is about to run while the flat
    `fee_per_lot` model is what would be charged. A zero fee is caught by
    the existing warner; this catches a fee that is merely the WRONG
    SHAPE, which is harder to notice and understates futures cost by
    roughly an order of magnitude.
    """
    cfg = cfg if cfg is not None else config.load()
    flat = cfg.get("fee_per_lot", 40)
    try:
        ls, _ = lot_size("NIFTY", cfg)
        real = cost_round_trip("NIFTY", 24800, 24800, 1, cfg, lot=ls)
    except LotSizeUnavailable:
        return None
    charged = float(flat) * 2
    if charged and real / charged > 3:
        msg = (f"futures replay would charge fee_per_lot ₹{charged:,.0f} per "
               f"round trip, but the notional model says ₹{real:,.0f} "
               f"({real/charged:.1f}x). Every futures expectancy computed "
               f"this way is inflated, and is_live_enabled() reads these "
               f"numbers. Use futures_costs.cost_round_trip().")
        log("WARNING: " + msg)
        return msg
    return None
