"""news_validation.py — does the news impact classifier actually predict
movement, and which feeds earn their place?

v58.51 (roadmap B7). Two questions that had never been asked of real
data:

  1. When `classify_impact_window()` says a headline affects the 5m
     candle, does the 5m candle actually move more than usual?
  2. Of the ~9 RSS feeds, which ones produce headlines that precede
     movement, and which are noise?

WHY A BASELINE IS THE WHOLE POINT
---------------------------------
Measuring the move after a headline tells you nothing on its own. NIFTY
moves ~0.05% in a typical 5-minute window; a headline followed by a
0.06% move looks like a hit and is actually noise. So every event window
is scored as a PERCENTILE RANK against the distribution of all
same-length windows on the SAME DAY. A classifier with no predictive
power produces a median rank near 50; one that works produces a rank
well above it.

Same-day is deliberate: comparing a quiet Tuesday against an expiry-day
distribution would credit the classifier for volatility it did not
predict.

HONEST LIMITATION, stated up front
----------------------------------
`fetched_ts` is when the SYSTEM FETCHED the item, not when the market
learned the news. NewsAgent polls every 900s, so the true lag is
somewhere between 0 and 15 minutes and unknown per item. That biases
this measurement AGAINST the classifier — real reaction may have
happened before we saw the headline. A result showing no edge is
therefore inconclusive; a result showing a clear edge is meaningful
despite the lag. Interpret asymmetrically.
"""
import statistics

WINDOW_MINUTES = {"1m": 1, "5m": 5, "15m": 15}


def _window_move_pct(candles, start_ts, minutes):
    """Absolute high-low range over `minutes` from `start_ts`, as % of
    the price at the start. Range rather than close-to-close: a headline
    that spikes price and reverts still MOVED the market, and a
    close-to-close measure would score that as nothing.
    """
    end_ts = start_ts + minutes * 60
    win = [c for c in candles if start_ts <= c["ts"] < end_ts]
    if not win:
        return None
    hi = max(c["high"] for c in win)
    lo = min(c["low"] for c in win)
    base = win[0]["open"] or win[0]["close"]
    if not base:
        return None
    return (hi - lo) / abs(base) * 100.0


def _baseline_distribution(candles, minutes, step=5):
    """All same-length windows across the day, for percentile ranking."""
    out = []
    if not candles:
        return out
    for i in range(0, max(0, len(candles) - minutes), step):
        m = _window_move_pct(candles, candles[i]["ts"], minutes)
        if m is not None:
            out.append(m)
    return sorted(out)


def _percentile_rank(sorted_vals, v):
    if not sorted_vals:
        return None
    below = sum(1 for x in sorted_vals if x < v)
    return round(100.0 * below / len(sorted_vals), 1)


def validate(events, candles_by_day, min_samples=5):
    """Score the classifier and each feed.

    `events`         tracked events (need fetched_ts and impact_windows)
    `candles_by_day` {"YYYY-MM-DD": [index candles with ts/open/high/low]}

    Returns a dict with per-window accuracy, per-feed scores and an
    explicit verdict. Never raises on sparse data — it reports
    insufficiency instead, because "we cannot tell yet" is a real and
    useful answer that a silently-computed number would hide.
    """
    import datetime as _dt
    per_window = {w: [] for w in WINDOW_MINUTES}
    per_feed = {}
    unclassified = []
    baselines = {}
    skipped = {"no_ts": 0, "no_candles": 0, "outside_session": 0}

    for e in events:
        ts = e.get("fetched_ts")
        if not ts:
            skipped["no_ts"] += 1
            continue
        day = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        candles = candles_by_day.get(day)
        if not candles:
            skipped["no_candles"] += 1
            continue
        windows = e.get("impact_windows") or []
        feed = e.get("source") or e.get("feed") or "unknown"
        feed_rec = per_feed.setdefault(feed, {"items": 0, "classified": 0,
                                              "ranks": []})
        feed_rec["items"] += 1

        # An event the classifier says will NOT move price is still
        # scored: it is the control group. Without it there is no way to
        # tell a working classifier from one that labels everything.
        targets = windows or ["5m"]
        scored_any = False
        for w in targets:
            mins = WINDOW_MINUTES.get(w)
            if not mins:
                continue
            key = (day, mins)
            if key not in baselines:
                baselines[key] = _baseline_distribution(candles, mins)
            move = _window_move_pct(candles, ts, mins)
            if move is None:
                continue
            rank = _percentile_rank(baselines[key], move)
            if rank is None:
                continue
            scored_any = True
            rec = {"rank": rank, "move_pct": round(move, 4),
                   "title": (e.get("title") or "")[:90], "feed": feed,
                   "category": e.get("category"), "day": day}
            if windows:
                per_window[w].append(rec)
                feed_rec["classified"] += 1
                feed_rec["ranks"].append(rank)
            else:
                unclassified.append(rec)
        if not scored_any:
            skipped["outside_session"] += 1

    def _summarise(recs):
        if len(recs) < min_samples:
            return {"n": len(recs), "insufficient": True,
                    "note": f"need >= {min_samples} to say anything"}
        ranks = [r["rank"] for r in recs]
        return {"n": len(recs),
                "median_rank": round(statistics.median(ranks), 1),
                "mean_rank": round(statistics.mean(ranks), 1),
                "pct_above_70": round(100.0 * sum(1 for r in ranks if r >= 70)
                                      / len(ranks), 1),
                "insufficient": False}

    windows_out = {w: _summarise(v) for w, v in per_window.items()}
    control = _summarise(unclassified)

    feeds_out = {}
    for feed, rec in sorted(per_feed.items()):
        if len(rec["ranks"]) < min_samples:
            feeds_out[feed] = {"items": rec["items"],
                               "classified": rec["classified"],
                               "insufficient": True}
            continue
        med = statistics.median(rec["ranks"])
        feeds_out[feed] = {
            "items": rec["items"], "classified": rec["classified"],
            "median_rank": round(med, 1),
            # A feed whose flagged items land at a median rank near 50 is
            # indistinguishable from picking windows at random. That is
            # the objective version of "review the feeds for relevance".
            "verdict": ("adds signal" if med >= 65 else
                        "marginal" if med >= 55 else
                        "no better than random"),
        }

    classified_all = [r for v in per_window.values() for r in v]
    overall = _summarise(classified_all)
    verdict = "insufficient data"
    if not overall.get("insufficient") and not control.get("insufficient"):
        lift = overall["median_rank"] - control["median_rank"]
        verdict = (f"classifier median rank {overall['median_rank']} vs "
                   f"control {control['median_rank']} -> "
                   + ("REAL EDGE" if lift >= 10 else
                      "WEAK" if lift >= 4 else
                      "NO MEASURABLE EDGE") + f" (lift {lift:+.1f})")
    return {
        "verdict": verdict,
        "overall_classified": overall,
        "control_unclassified": control,
        "by_window": windows_out,
        "by_feed": feeds_out,
        "skipped": skipped,
        "caveat": ("fetched_ts is when the system FETCHED the item, not "
                   "when the market learned it; NewsAgent polls every "
                   "900s so the lag is 0-15min and unknown per item. "
                   "This biases the measurement AGAINST the classifier, "
                   "so a null result is inconclusive while a positive "
                   "one is meaningful."),
    }


def load_and_validate(symbol="NIFTY", days=10, limit=2000, min_samples=5):
    """Convenience wrapper: pull events + candles from the local store."""
    import datetime as _dt
    import history
    import news_engine as ne
    events = ne.read_tracked_events(limit=limit)
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).timestamp()
    events = [e for e in events if (e.get("fetched_ts") or 0) >= cutoff]
    by_day = {}
    for e in events:
        ts = e.get("fetched_ts")
        if not ts:
            continue
        d = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if d not in by_day:
            try:
                by_day[d] = history.day_index_candles(symbol.upper(), d)
            except Exception:
                by_day[d] = []
    out = validate(events, by_day, min_samples=min_samples)
    out["symbol"] = symbol.upper()
    out["events_considered"] = len(events)
    out["days_with_candles"] = sum(1 for v in by_day.values() if v)
    return out


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--min-samples", type=int, default=5)
    a = ap.parse_args()
    print(json.dumps(load_and_validate(a.symbol, a.days,
                                       min_samples=a.min_samples), indent=1))
