#!/usr/bin/env python3
"""gate_report.py — every live strategy under the corrected promotion gate.

v59.0 item 17. Reports only. Reads the persisted backtest entries that
`backtester.is_live_enabled()` actually consults, so this describes the
LIVE state, not a fresh replay that might disagree with it.

Nothing here writes to strategy_versions.json or flips any switch.
"""
import sys
import math
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt
import config
import promotion_gate as pg


def main():
    cfg = config.load()
    min_conf = cfg.get("pa_min_trades_for_confidence", 15)
    vers = bt.load_versions()
    rows = []
    for name, node in sorted(vers.items()):
        for sym, entry in sorted((node.get("symbols") or {}).items()):
            live = bool(entry.get("live_enabled") and
                        not entry.get("manually_disabled"))
            m = {}
            for v in entry.get("versions") or []:
                if v.get("v") == entry.get("active"):
                    m = v.get("results") or {}
            trades = m.get("trades") or 0
            net = m.get("net_pnl") or 0
            if not live and not trades:
                continue
            ok, d = pg.evaluate_entry(name, sym, m)
            d["live_now"] = live
            d["old_gate"] = (trades >= min_conf and net > 0)
            # Item 21a — DECONVOLVE the proxy noise. The observed
            # per-trade sd contains the P&L model's own error, since
            # every trade's pnl came out of that model:
            #     observed^2 = true^2 + proxy^2
            # so the strategy's TRUE dispersion is smaller, and t against
            # it is LARGER. Removing the proxy noise entirely is the most
            # generous treatment available — if t still misses 2 there,
            # fixing the P&L model cannot rescue the strategy.
            #
            # THE ESTIMATOR IS ONLY STABLE WHEN own_sd COMFORTABLY EXCEEDS
            # THE NOISE FLOOR. As own_sd -> 1143+, true_sd -> 0 and
            # t_deconv -> infinity: momentum_confluence FINNIFTY has
            # own_sd 1,158, so a naive deconvolution claims 97% of its
            # entire P&L variance is model noise and reports t=5.13 off an
            # edge of ₹35/trade. That is the estimator exploding, not a
            # strategy working. Guarded at own_sd >= 1.5 x proxy (proxy
            # variance <= 44% of total); below that it is reported as
            # unstable rather than as a number.
            osd = d.get("own_sd")
            d["deconv_stable"] = bool(osd and osd >= 1.5 * pg.PROXY_SD_PER_TRADE)
            tv = (osd ** 2 - pg.PROXY_SD_PER_TRADE ** 2) if osd else None
            d["true_sd"] = (tv ** 0.5 if (tv and tv > 0 and d["deconv_stable"])
                            else None)
            d["t_deconv"] = ((d["net_per_trade"] / (d["true_sd"] / math.sqrt(trades)))
                             if d["true_sd"] else None)
            rows.append(d)

    live_rows = [r for r in rows if r["live_now"]]
    k_now = pg.deflation_k()
    print(f"\n  PROMOTION GATE — {len(live_rows)} live strategies")
    # v59.66 — this header used to print the SUPERSEDED quadrature form
    # while the code below applied the calibrated one (third-eye Tier 1).
    # It now prints what is actually applied, k included.
    print(f"  required = cost_bias + k*sqrt(SE_edge² + sd²/cal_n)   [APPLIED]")
    print(f"  k = {k_now:.2f} (deflated max-of-N, pre-commit floor "
          f"{pg.PRECOMMITTED_K}) · sd = ₹{pg.PROXY_SD_PER_TRADE:,.0f}/trade "
          f"(PROVISIONAL) · scored on the OUT-OF-SAMPLE window only\n")
    hdr = (f"  {'strategy':22} {'symbol':10} {'n':>5} {'days':>5} {'net/tr':>9} "
           f"{'cost':>7} {'stat':>8} {'req':>9} {'headroom':>10}  verdict")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    # Denied entries carry headroom=None (a 0-trade live entry used to
    # crash this sort and the row format); they sort last and render
    # their denial reason instead of fake zeros.
    for r in sorted(live_rows,
                    key=lambda x: (-x["headroom"]
                                   if x.get("headroom") is not None
                                   else float("inf"))):
        if r.get("required") is None:
            print(f"  {(r.get('strategy') or '?'):22} "
                  f"{(r.get('symbol') or '?'):10} "
                  f"{(r.get('trades') or 0):>5}       DENIED — "
                  f"{r.get('reason', 'no detail')}")
            continue
        v = "PASS" if r["headroom"] > 0 else "FAIL"
        print(f"  {r['strategy']:22} {r['symbol']:10} {r['trades']:>5} "
              f"{(r.get('n_days') or 0):>5} "
              f"{r['net_per_trade']:>9,.0f} {r['cost_bias']:>7,.0f} "
              f"{r['stat_margin']:>8,.0f} {r['required']:>9,.0f} "
              f"{r['headroom']:>+10,.0f}  {v}")
    spread_live = [r for r in live_rows
                   if r.get("strategy") in ("bull_put_spread",
                                            "bear_call_spread")]
    if spread_live:
        print("\n  NOTE: spread verdicts above are ADVISORY — the spread "
              "live path is disabled by design (_auto_spreads runs only "
              "in paper_mode and enter_spread refuses live orders), so "
              "this gate currently guards no spread order.")

    npass = sum(1 for r in live_rows if (r.get("headroom") or 0) > 0)
    stat_only = sum(1 for r in live_rows if r["passes_stat_only"])
    print(f"\n  under the corrected gate : {npass}/{len(live_rows)} pass")
    print(f"  under the OLD gate       : "
          f"{sum(1 for r in live_rows if r['old_gate'])}/{len(live_rows)} pass")
    print(f"\n  WHICH CONCLUSION RESTS ON WHAT (item 20 input):")
    print(f"    statistical margin alone, cost correction ignored : "
          f"{stat_only}/{len(live_rows)} pass")
    print(f"    statistical margin + cost bias                    : "
          f"{npass}/{len(live_rows)} pass")
    flips = [r for r in live_rows
             if r["passes_stat_only"] and (r.get("headroom") or 0) <= 0]
    if flips:
        print("    disqualified BY THE COST CORRECTION only (weaker claim):")
        for r in flips:
            print(f"      {r['strategy']} {r['symbol']}")
    else:
        print("    none are disqualified by the cost correction alone —")
        print("    every failure is a failure of the statistical margin,")
        print("    which is the more robust argument.")

    print(f"\n  IS THE EDGE EVEN DISTINGUISHABLE FROM ZERO?  (item 21)")
    print(f"  t needs NEITHER error model. t_deconv removes the proxy noise")
    print(f"  entirely (true_sd = sqrt(own^2 - {pg.PROXY_SD_PER_TRADE:.0f}^2)) — the most")
    print(f"  generous treatment the data allows.\n")
    print(f"    {'strategy':22} {'symbol':10} {'own sd':>9} {'true sd':>9} "
          f"{'t':>7} {'t_deconv':>9}")
    for r in sorted(live_rows, key=lambda x: -(x["t_own"] or -99)):
        sd = f"{r['own_sd']:,.0f}" if r["own_sd"] else "n/a"
        ts = f"{r['true_sd']:,.0f}" if r["true_sd"] else "unstable"
        t = f"{r['t_own']:.2f}" if r["t_own"] is not None else "n/a"
        td = f"{r['t_deconv']:.2f}" if r["t_deconv"] is not None else "unstable"
        print(f"    {r['strategy']:22} {r['symbol']:10} {sd:>9} {ts:>9} "
              f"{t:>7} {td:>9}")

    stable = [r for r in live_rows if r["t_deconv"] is not None]
    reached = [r for r in stable if r["t_deconv"] >= 2]
    print(f"\n    deconvolution is stable for {len(stable)}/{len(live_rows)} "
          f"(own_sd >= 1.5 x {pg.PROXY_SD_PER_TRADE:.0f})")
    if stable:
        print(f"    highest t_deconv among those       : "
              f"{max(r['t_deconv'] for r in stable):.2f}")
    print(f"    reaching t=2 with proxy noise removed: "
          f"{len(reached)}/{len(stable)}")
    if not reached:
        print("    => where the estimator is trustworthy, removing the P&L")
        print("       model's error ENTIRELY still leaves every t below 2.")
        print("       Fixing the P&L model cannot rescue any of them.")
    unstable = [r for r in live_rows if r["t_deconv"] is None]
    if unstable:
        print(f"\n    the other {len(unstable)} cannot be deconvolved at all — their")
        print("    own_sd sits at or below the ₹1,143 noise floor:")
        for r in unstable:
            print(f"      {r['strategy']:22} {r['symbol']:10} own_sd "
                  f"{r['own_sd']:,.0f}" if r["own_sd"] else
                  f"      {r['strategy']} {r['symbol']}")
        print("    That is itself a finding: ₹1,143 cannot be a universal")
        print("    additive noise component when whole strategies show LESS")
        print("    total P&L variance than the claimed noise. It bounds how")
        print("    far the 4-session measurement generalises.")

    # Item 21b — multiple comparisons, on the RAW t. The deconvolved
    # figures are not comparable to a standard-normal benchmark: the
    # deconvolution is a variance-shrinking transform applied unevenly
    # across the eleven, so its maximum is not the max of eleven draws
    # from anything.
    ts_all = [r["t_own"] for r in live_rows if r["t_own"] is not None]
    if ts_all:
        mx = max(ts_all)
        print(f"\n  MULTIPLE COMPARISONS (item 21b)")
        print(f"    expected max of {len(ts_all)} standard normals : ~1.59")
        print(f"    observed max                        : {mx:.2f}")
        print(f"    {'BELOW' if mx < 1.59 else 'above'} what pure noise predicts.")
        print(f"    CAVEAT: the eleven are NOT independent — they share")
        print(f"    symbols, periods and in places the same underlying moves,")
        print(f"    which LOWERS the 1.59 benchmark by an amount this data")
        print(f"    cannot quantify. The comparison is directional, not exact.")

    print(f"\n  APPLIED vs SUPERSEDED MARGIN (item 26)")
    print(f"    {'strategy':22} {'symbol':10} {'net/tr':>8} {'superseded':>11} "
          f"{'APPLIED':>9}")
    _strict = 0
    for r in sorted(live_rows, key=lambda x: -(x.get("net_per_trade") or 0)):
        lg = r.get("legacy_required")
        if lg is None:
            continue
        if r["required"] > lg:
            _strict += 1
        print(f"    {r['strategy']:22} {r['symbol']:10} "
              f"{r['net_per_trade']:>8,.0f} {lg:>11,.0f} "
              f"{r['required']:>9,.0f}")
    print(f"\n    the applied form is stricter on {_strict}/{len(live_rows)}")

    # ---- item 27: how wide is the uncertainty, really? ----------------
    # The headline must not read as "no strategy has an edge". Measurement
    # uncertainty is wide enough that one could plausibly sit above the
    # line, and the record has to show how close.
    SD_REAL, SD_PROXY, BIAS, BIAS_SE = 1307.0, 1413.0, 102.0, 133.0
    print(f"\n  ITEM 27 — SENSITIVITY: how close is the nearest strategy?")
    print(f"  If the +₹{BIAS:.0f} bias were real AND own_sd were rescaled by")
    print(f"  sd_real/sd_proxy ({SD_REAL:.0f}/{SD_PROXY:.0f}), t would move to:")
    print(f"\n    {'strategy':22} {'symbol':10} {'t':>6} {'t_adj':>7}")
    _above = 0
    for r in sorted(live_rows, key=lambda x: -(x["t_own"] or -99))[:4]:
        if not r["own_sd"]:
            continue
        adj_sd = r["own_sd"] * SD_REAL / SD_PROXY
        t_adj = (r["net_per_trade"] + BIAS) / (adj_sd / math.sqrt(r["trades"]))
        if t_adj >= 2:
            _above += 1
        print(f"    {r['strategy']:22} {r['symbol']:10} "
              f"{r['t_own']:>6.2f} {t_adj:>7.2f}"
              f"{'   ABOVE 2' if t_adj >= 2 else ''}")
    print(f"\n    NOT APPLIED. The bias is ₹{BIAS:.0f} +/- ₹{BIAS_SE:.0f} — not")
    print(f"    distinguishable from zero — and propagating a point estimate")
    print(f"    that is itself indistinguishable from zero is the substitution")
    print(f"    we keep refusing. Shown so the next session sees how close it sits.")
    band = 2 * BIAS_SE
    swamped = [r for r in live_rows
               if r.get("net_per_trade") is not None
               and abs(r["net_per_trade"]) < band]
    print(f"\n    the 2-SE bias band alone (+/-₹{band:.0f}) exceeds the entire")
    print(f"    net-per-trade figure of {len(swamped)}/{len(live_rows)} strategies.")

    print(f"\n  PROVENANCE")
    print(f"    sd   {pg.PROXY_SD_PROVENANCE}")
    print(f"    cost {pg.COST_BIAS_PROVENANCE}")


if __name__ == "__main__":
    main()
