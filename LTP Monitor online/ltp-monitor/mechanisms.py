"""mechanisms.py — the economic mechanism register (third-eye Tier 2).

A strategy without a mechanism is a pattern found in noise: patterns
found in noise backtest beautifully and fail forward. The reference
question every entry here must answer is

    WHO takes the other side of this trade, and WHY do they accept a
    negative expected return?

Legitimate categories: risk-transfer (the counterparty pays to offload
risk), constraint (they are forced to trade), information/processing
asymmetry (least plausible for retail in liquid index derivatives, and
it decays fastest), liquidity-provision (an infrastructure business,
not a strategy). "Momentum persists" and "the indicator crossed" are
NOT mechanisms — they are correlations awaiting one.

`promotion_gate.evaluate_entry()` DENIES live promotion for any
strategy whose entry here is missing or `category: "unstated"`. That is
the pre-registration rule made structural: results without a stated
mechanism are exploratory — they generate hypotheses, they cannot
support a live-promotion decision. Editing an entry from "unstated" to
a real category is a deliberate research act: state the counterparty,
the reason, and the date, BEFORE looking at fresh results.

This registry is honest, not aspirational. Most entries below say
"unstated" because that is the truth as of 2026-08-09.
"""

MECHANISMS = {
    "bull_put_spread": {
        "category": "risk-transfer",
        "counterparty": ("Put buyers below spot: hedgers and event-"
                         "protection buyers paying above fair value to "
                         "offload downside tail risk they do not want."),
        "statement": ("The spread seller is paid a premium for bearing "
                      "bounded tail risk while the index holds above a "
                      "support wall. The premium is real and durable — "
                      "AND it comes with genuine tail exposure. Caveat "
                      "recorded with the mechanism: the harvested share "
                      "of the premium must clear the edge-feasibility "
                      "bar (2× round-trip cost); the measured record so "
                      "far has NOT shown the captured premium exceeding "
                      "friction at the current hold times."),
        "registered": "2026-08-09",
    },
    "bear_call_spread": {
        "category": "risk-transfer",
        "counterparty": ("Call buyers above spot: upside chasers and "
                         "short-covering hedgers paying above fair value "
                         "for convex upside exposure."),
        "statement": ("Mirror of bull_put_spread: premium for bearing "
                      "bounded upside tail risk below a resistance wall. "
                      "Same caveat: capture must clear 2× costs; the "
                      "record has not yet shown it."),
        "registered": "2026-08-09",
    },
    # ------------------------------------------------------------------
    # Everything below is UNSTATED: the honest answer to "who loses to
    # this and why" has not been written down. Until it is, these are
    # exploratory — paper-only by gate, whatever their backtests say.
    # ------------------------------------------------------------------
    "orb": {"category": "unstated",
            "statement": "Opening-range breakout — 'early momentum "
                         "persists' is a correlation, not a counterparty."},
    "vwap_pullback": {"category": "unstated",
                      "statement": "Mean-reversion to VWAP — no stated "
                                   "reason the other side donates."},
    "ema_mtf": {"category": "unstated",
                "statement": "EMA crossover with MTF confirm — a "
                             "deterministic function of past price; "
                             "needs an unusually specific mechanism to "
                             "be credible, none stated."},
    "sg_ema": {"category": "unstated",
               "statement": "ema_mtf plus structure/AI gates — gates "
                            "filter a signal; they do not supply the "
                            "missing counterparty."},
    "momentum_confluence": {"category": "unstated",
                            "statement": "Indicator confluence — "
                                         "correlation stack, no mechanism."},
    "ew_reversal": {"category": "unstated",
                    "statement": "Elliott-wave reversal patterns — no "
                                 "stated forced flow behind the shape."},
    "ta_elliott": {"category": "unstated",
                   "statement": "GMMA/Elliott state machine — same as "
                                "ew_reversal."},
    "momentum_buy": {"category": "unstated",
                     "statement": "Legacy momentum option buy."},
    "oi_composite": {"category": "unstated",
                     "statement": "OI buildup composite — OI change is "
                                  "net of all participants and does not "
                                  "reveal direction or intent by itself."},
    "mtf_confluence": {"category": "unstated",
                       "statement": "Multi-timeframe confluence — "
                                    "correlation stack."},
    "s11_momentum": {"category": "unstated",
                     "statement": "Futures momentum (recorded live: "
                                  "27.5% win, −₹597/trade expectancy)."},
    "s12_vwap_reversion": {"category": "unstated",
                           "statement": "Futures VWAP reversion."},
    "s13_orb": {"category": "unstated",
                "statement": "Futures ORB."},
    "s14_existing": {"category": "unstated",
                     "statement": "Futures port of the existing signal."},
}


def get(name):
    return MECHANISMS.get(name)


def stated(name):
    """True only when a real mechanism category is registered."""
    m = MECHANISMS.get(name)
    return bool(m and m.get("category") and m["category"] != "unstated")
