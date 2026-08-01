#!/usr/bin/env python3
"""forensic_replay.py — candle-by-candle post-mortem of the worst trades.

    python3 forensic_replay.py            # 6 worst futures trades
    python3 forensic_replay.py --n 10

Replays each losing trade minute by minute against the REAL index
candles (security_id 13/25/27/51, the broker backfill), answering four
questions the journal cannot:

  1. Was the entry taken INTO an adverse move, or did it have an edge
     that was later given back?  (MFE/MAE + bar-by-bar excursion)
  2. Was the entry chasing — buying the top / selling the bottom of the
     recent range?                       (entry percentile in prior 20 bars)
  3. Was the stop inside normal noise?   (stop distance vs ATR-14)
  4. Did the intended move arrive AFTER the stop?  (post-exit excursion)

CAVEAT, stated because it changes how the numbers read: futures trade at
a basis to spot (BANKNIFTY was ~+150 on 2026-07-30) and futures candles
are NOT archived — only their volume is. So the replay uses INDEX candles
shifted by the basis measured at entry. Direction, timing and excursion
are faithful; absolute levels are approximate to the basis drift.
"""
import argparse, json, os, sys, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history

SEC = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "SENSEX": "51"}
LOT = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "SENSEX": 20}


def bars_for(symbol, day):
    lo = int(dt.datetime.strptime(day, "%Y-%m-%d").timestamp())
    c = history._conn()
    rows = c.execute(
        "SELECT ts,o,h,l,c FROM candles WHERE security_id=? AND ts>=? AND ts<? "
        "AND c IS NOT NULL ORDER BY ts", (SEC[symbol], lo, lo + 86400)).fetchall()
    c.close()
    return rows


def ema(vals, n):
    if not vals:
        return []
    k, out = 2 / (n + 1), [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(bars, n=14):
    trs = []
    for i, (_, o, h, l, c) in enumerate(bars):
        prev = bars[i - 1][4] if i else c
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    if len(trs) < n:
        return sum(trs) / max(len(trs), 1)
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(bars, n=14):
    """Wilder ADX — the gate `futures_min_adx` is supposed to consult."""
    if len(bars) < n * 2:
        return None
    plus, minus, trs = [], [], []
    for i in range(1, len(bars)):
        _, o, h, l, c = bars[i]
        ph, pl, pc = bars[i - 1][2], bars[i - 1][3], bars[i - 1][4]
        up, dn = h - ph, pl - l
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    def smooth(x):
        s = [sum(x[:n])]
        for v in x[n:]:
            s.append(s[-1] - s[-1] / n + v)
        return s
    tr_s, p_s, m_s = smooth(trs), smooth(plus), smooth(minus)
    dxs = []
    for tr, p, m in zip(tr_s, p_s, m_s):
        if tr == 0:
            continue
        pdi, mdi = 100 * p / tr, 100 * m / tr
        if pdi + mdi:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def hhmmss(t):
    return dt.datetime.fromtimestamp(t).strftime("%H:%M")


def analyse(t, show_bars=14):
    sym = t["symbol"]
    day = str(t.get("closed_date"))
    entry = float(t["entry"])
    exit_px = float(t["ltp"])
    lots = int(t.get("lots") or 1)
    pnl = float(t.get("pnl") or 0)
    lot = LOT.get(sym, 75)
    side = "SHORT" if exit_px > entry and pnl < 0 else "LONG"
    if pnl > 0:
        side = "LONG" if exit_px > entry else "SHORT"
    sign = 1 if side == "LONG" else -1

    bars = bars_for(sym, day)
    if not bars:
        print(f"  no candles for {sym} {day}"); return None

    t_open = dt.datetime.strptime(day + " " + str(t.get("opened")), "%Y-%m-%d %H:%M:%S")
    t_exit = dt.datetime.fromisoformat(str(t.get("closed_at"))).replace(tzinfo=None)
    ts_in, ts_out = int(t_open.timestamp()), int(t_exit.timestamp())

    i_in = max(range(len(bars)), key=lambda i: -abs(bars[i][0] - ts_in))
    i_out = max(range(len(bars)), key=lambda i: -abs(bars[i][0] - ts_out))
    basis = entry - bars[i_in][4]           # futures premium to spot at entry

    print("=" * 78)
    print(f"  {sym} FUT {side} × {lots} lots   {day}  {t.get('opened')} → "
          f"{t_exit.strftime('%H:%M:%S')}   held {(ts_out-ts_in)//60}m")
    print(f"  entry {entry:,.1f}   exit {exit_px:,.1f}   P&L ₹{pnl:,.0f}"
          f"   MAE ₹{float(t.get('mae') or 0):,.0f}   MFE ₹{float(t.get('mfe') or 0):,.0f}")
    print(f"  exit reason: {t.get('reason')}")
    print(f"  (index basis at entry {basis:+.1f} pts — replay levels are spot+basis)")

    # ---------- pre-entry context ----------
    pre = bars[max(0, i_in - 20):i_in + 1]
    closes = [b[4] for b in pre]
    hi, lo = max(b[2] for b in pre), min(b[3] for b in pre)
    pct = 100 * (closes[-1] - lo) / (hi - lo) if hi > lo else 50
    e5, e13 = ema(closes, 5)[-1], ema(closes, 13)[-1]
    a14 = atr(bars[max(0, i_in - 30):i_in + 1])
    adx14 = adx(bars[max(0, i_in - 60):i_in + 1])
    drift = closes[-1] - closes[0]
    print(f"\n  PRE-ENTRY (20 bars to {hhmmss(bars[i_in][0])}):")
    print(f"    range {lo:,.1f}–{hi:,.1f}   entry sits at {pct:.0f}% of it"
          f"   ({'TOP — buying strength' if pct > 75 and side=='LONG' else ''}"
          f"{'BOTTOM — selling weakness' if pct < 25 and side=='SHORT' else ''}"
          f"{'against the extreme' if (pct>75 and side=='SHORT') or (pct<25 and side=='LONG') else ''})")
    print(f"    20-bar drift {drift:+.1f} pts   EMA5 {e5:,.1f} {'>' if e5>e13 else '<'} EMA13 {e13:,.1f}"
          f"   → short-term trend {'UP' if e5>e13 else 'DOWN'}")
    print(f"    ATR(14) {a14:.1f} pts   ADX(14) {adx14:.1f}" if adx14 else
          f"    ATR(14) {a14:.1f} pts   ADX unavailable")
    aligned = (side == "LONG" and e5 > e13) or (side == "SHORT" and e5 < e13)
    print(f"    entry {'WITH' if aligned else 'AGAINST'} the 5/13 EMA trend")

    # ---------- stop geometry ----------
    # 2026-08-01 — was scraping the stop out of the exit-reason TEXT,
    # which only worked for the 3 of 19 trades whose reason happened to
    # contain one. The record carries it: agents.trade_risk_fields()
    # returns initial_sl for a futures trade (the stop sizing was decided
    # against, before any trail moved it). Falls back to the old scrape
    # only for records written before initial_sl existed.
    import agents as _ag
    stop_px = _ag.trade_risk_fields(t).get("stop")
    if stop_px is None:
        r = str(t.get("reason") or "")
        if "stoploss (" in r:
            try: stop_px = float(r.split("stoploss (")[1].split(")")[0])
            except Exception: pass
    if stop_px:
        dist = abs(entry - stop_px)
        print(f"\n  STOP: {stop_px:,.2f} — {dist:.1f} pts away = {dist/a14:.2f}× ATR"
              f"   (risk/lot ₹{dist*lot:,.0f}, total ₹{dist*lot*lots:,.0f})")
        if dist < a14:
            print(f"    ⚠ stop is INSIDE one ATR — normal noise reaches it")

    # ---------- bar-by-bar ----------
    print(f"\n  BAR-BY-BAR (entry → exit):")
    print(f"    {'time':6} {'open':>9} {'high':>9} {'low':>9} {'close':>9} "
          f"{'MTM ₹':>10}  excursion")
    best = worst = 0.0
    for b in bars[i_in:i_out + 2]:
        ts, o, h, l, cl = b
        fo, fh, fl, fc = (x + basis for x in (o, h, l, cl))
        mtm = sign * (fc - entry) * lot * lots
        fav = sign * (fh - entry) if side == "LONG" else sign * (fl - entry)
        adv = sign * (fl - entry) if side == "LONG" else sign * (fh - entry)
        best, worst = max(best, fav * lot * lots), min(worst, adv * lot * lots)
        mark = ""
        if stop_px and ((side == "LONG" and fl <= stop_px) or (side == "SHORT" and fh >= stop_px)):
            mark = "  ← STOP TOUCHED"
        print(f"    {hhmmss(ts):6} {fo:9,.1f} {fh:9,.1f} {fl:9,.1f} {fc:9,.1f} "
              f"{mtm:10,.0f}{mark}")
    print(f"    best unrealised ₹{best:,.0f}   worst ₹{worst:,.0f}")
    if best <= 0:
        print(f"    ⚠ the trade was NEVER in profit — not one bar traded in its favour")

    # ---------- after the exit ----------
    post = bars[i_out + 1:i_out + 31]
    if post:
        ext = max(sign * ((b[2] if side == "LONG" else -b[3]) + (basis if side=="LONG" else -basis) - sign*entry) for b in post) if False else None
        moves = [sign * ((b[4] + basis) - entry) * lot * lots for b in post]
        print(f"\n  AFTER THE EXIT (next {len(post)} bars):")
        print(f"    best the idea would have reached: ₹{max(moves):,.0f}"
              f"   worst: ₹{min(moves):,.0f}")
        if max(moves) > abs(pnl):
            print(f"    → the DIRECTION was right but the STOP was too tight:"
                  f" it later moved ₹{max(moves):,.0f} in favour")
        elif max(moves) <= 0:
            print(f"    → the direction stayed wrong; the loss was the idea, not the timing")
    return {"sym": sym, "side": side, "pnl": pnl, "aligned": aligned, "pct": pct,
            "never_profit": best <= 0, "atr": a14, "adx": adx14,
            "stop_pts": abs(entry - stop_px) if stop_px else None,
            "held_min": (ts_out - ts_in) // 60,
            "post_best": max(moves) if post else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    a = ap.parse_args()
    rows = [json.loads(l) for l in
            open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
    fut = [t for t in rows if not t.get("leg") and t.get("opened") and t.get("closed_at")]
    worst = sorted(fut, key=lambda t: float(t.get("pnl") or 0))[:a.n]
    out = []
    for t in worst:
        r = analyse(t)
        if r: out.append(r)

    print("\n" + "=" * 78)
    print("  AGGREGATE")
    n = len(out)
    print(f"    trades examined: {n}   total ₹{sum(o['pnl'] for o in out):,.0f}")
    print(f"    entered AGAINST the 5/13 EMA trend: {sum(1 for o in out if not o['aligned'])}/{n}")
    print(f"    never showed a single tick of profit: {sum(1 for o in out if o['never_profit'])}/{n}")
    print(f"    median hold: {sorted(o['held_min'] for o in out)[n//2]} minutes")
    tight = [o for o in out if o['stop_pts'] and o['stop_pts'] < o['atr']]
    print(f"    stop inside 1×ATR (noise): {len(tight)}/{n}")
    lowadx = [o for o in out if o['adx'] and o['adx'] < 20]
    print(f"    ADX(14) < 20 at entry (no trend): {len(lowadx)}/{n}")
    rescued = [o for o in out if o['post_best'] and o['post_best'] > abs(o['pnl'])]
    print(f"    direction later proved right: {len(rescued)}/{n}")


if __name__ == "__main__":
    main()
