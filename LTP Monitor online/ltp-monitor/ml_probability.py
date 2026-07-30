"""ml_probability.py — genuine ML probability scoring trained on the
Shadow Journal, per roadmap item #7/#12 ("ML probability scoring...
dependent on the Shadow Journal accumulating sufficient volume").

Distinct from ai_probability_engine.estimate_probability() (that
function buckets historical trades by 3 coarse dimensions and counts
win rate per bucket — simple, already built). This module trains an
actual logistic regression over several engineered features, learning
continuous relationships a fixed 3-bucket count can't capture (e.g.
"each extra failed check reduces win probability by X, holding
confidence constant").

No external ML dependencies (no numpy/sklearn) — matches this
project's established dependency-free style throughout. Logistic
regression via plain-Python batch gradient descent is small enough to
implement correctly and verify by hand, and is genuinely "ML" (a
learned, weighted combination of features optimized against real
outcomes), not just re-labeled bucketed counting.

THE VOLUME QUESTION THIS MODULE ANSWERS DIRECTLY: rather than a
standing assumption that there still isn't enough Shadow Journal data,
`check_volume_sufficiency()` counts the ACTUAL current volume every
time it's called and reports a real number against a real threshold —
"insufficient" is a measured fact refreshed on every call, not a
permanent assumption inherited from when this was first scoped.
"""
import json
import math
import os
import store
import time


SHADOW_PATH = store.path("shadow_signals.jsonl")

# Feature order is fixed and documented here once — every function
# that builds or consumes a feature vector must agree on this order.
FEATURE_NAMES = ("confidence", "num_failed_checks", "is_s7", "direction_ce", "risk_reward")


def _sigmoid(z):
    z = max(min(z, 30), -30)   # avoid float overflow on extreme inputs
    return 1.0 / (1.0 + math.exp(-z))


def extract_features(entry):
    """Turns one shadow-journal entry into a fixed-order feature vector
    (see FEATURE_NAMES). Returns None if the entry is missing fields
    needed to compute a feature (skipped from training/prediction
    rather than filled with a guessed default)."""
    confidence = entry.get("confidence")
    entry_px = entry.get("entry")
    stoploss = entry.get("stoploss")
    target1 = entry.get("target1")
    signal = entry.get("signal")
    if confidence is None or entry_px is None or stoploss is None or \
            target1 is None or signal not in ("BUY_CE", "BUY_PE"):
        return None
    risk = entry_px - stoploss
    if risk <= 0:
        return None   # degenerate stop distance, same guard used elsewhere
    risk_reward = (target1 - entry_px) / risk
    num_failed = len(entry.get("failed_checks") or [])
    is_s7 = 1.0 if entry.get("s7_gates") is not None else 0.0
    direction_ce = 1.0 if signal == "BUY_CE" else 0.0
    return [float(confidence), float(num_failed), is_s7, direction_ce, risk_reward]


def _normalize_fit(X):
    """Min-max normalization fitted on the training set — returns
    (mins, ranges) to apply the SAME transform to any future input at
    prediction time (fitting normalization separately per prediction
    would make the model's learned weights meaningless)."""
    n_features = len(X[0])
    mins = [min(row[j] for row in X) for j in range(n_features)]
    maxs = [max(row[j] for row in X) for j in range(n_features)]
    ranges = [(maxs[j] - mins[j]) or 1.0 for j in range(n_features)]
    return mins, ranges


def _normalize_apply(x, mins, ranges):
    return [(xi - m) / r for xi, m, r in zip(x, mins, ranges)]


def train_logistic_regression(X, y, lr=0.3, epochs=300, l2=0.01):
    """Plain batch gradient descent, L2-regularized. X: list of
    already-normalized feature vectors. y: list of 0/1 labels. Returns
    (weights, bias)."""
    n = len(X)
    n_features = len(X[0])
    weights = [0.0] * n_features
    bias = 0.0
    for _ in range(epochs):
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for xi, yi in zip(X, y):
            z = bias + sum(w * x for w, x in zip(weights, xi))
            pred = _sigmoid(z)
            err = pred - yi
            for j in range(n_features):
                grad_w[j] += err * xi[j]
            grad_b += err
        for j in range(n_features):
            weights[j] -= lr * (grad_w[j] / n + l2 * weights[j])
        bias -= lr * (grad_b / n)
    return weights, bias


def predict_proba(model, features):
    """model: {"weights":..., "bias":..., "mins":..., "ranges":...}.
    features: a raw (unnormalized) feature vector, same order as
    FEATURE_NAMES."""
    x = _normalize_apply(features, model["mins"], model["ranges"])
    z = model["bias"] + sum(w * xi for w, xi in zip(model["weights"], x))
    return _sigmoid(z)


def _match_shadow_to_real_trade(entry, closed_trades, tolerance_sec=300):
    """For an APPROVED shadow entry, finds the corresponding real closed
    trade (same entry-time proximity matching already proven for
    backtester.audit_today() — reused here, not reimplemented) and
    returns its actual win/loss label (pnl > 0 -> 1, else 0), or None
    if no matching closed trade is found (trade may still be open, or
    genuinely wasn't tracked)."""
    try:
        shadow_ts = time.mktime(time.strptime(entry["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None
    for t in closed_trades:
        if t.get("symbol") != entry.get("symbol"):
            continue
        t_ts = t.get("opened_ts")
        if t_ts is None:
            continue
        if abs(t_ts - shadow_ts) <= tolerance_sec:
            return 1 if t.get("pnl", 0) > 0 else 0
    return None


def load_labeled_data(shadow_path=None, closed_trades=None):
    """Reads the shadow journal and produces (features, label) pairs
    with a genuine, known outcome — REJECTED entries use their own
    already-resolved hindsight outcome (would_have_hit_target1 -> win,
    would_have_hit_stoploss -> loss; pending/unresolved_timeout are
    skipped, no usable label), APPROVED entries are joined against
    real closed trades for their actual outcome. Returns a list of
    (feature_vector, label) tuples."""
    shadow_path = shadow_path or SHADOW_PATH
    closed_trades = closed_trades or []
    if not os.path.exists(shadow_path):
        return []
    labeled = []
    with open(shadow_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            resolution = entry.get("resolution")
            label = None
            if resolution == "would_have_hit_target1":
                label = 1
            elif resolution == "would_have_hit_stoploss":
                label = 0
            elif entry.get("verdict") == "APPROVED":
                label = _match_shadow_to_real_trade(entry, closed_trades)
            if label is None:
                continue
            features = extract_features(entry)
            if features is None:
                continue
            labeled.append((features, label))
    return labeled


def check_volume_sufficiency(labeled_data, min_samples=100, min_per_class=20):
    """Measures the ACTUAL current volume every time this is called —
    not a standing assumption. Returns {"sufficient": bool, "n":,
    "n_wins":, "n_losses":, "reason": str}."""
    n = len(labeled_data)
    n_wins = sum(1 for _, y in labeled_data if y == 1)
    n_losses = n - n_wins
    if n < min_samples:
        return {"sufficient": False, "n": n, "n_wins": n_wins, "n_losses": n_losses,
               "reason": f"only {n} labeled outcomes so far (need >= {min_samples})"}
    if n_wins < min_per_class or n_losses < min_per_class:
        return {"sufficient": False, "n": n, "n_wins": n_wins, "n_losses": n_losses,
               "reason": f"class imbalance — {n_wins} wins / {n_losses} losses "
                        f"(need >= {min_per_class} of each)"}
    return {"sufficient": True, "n": n, "n_wins": n_wins, "n_losses": n_losses,
           "reason": f"{n} labeled outcomes ({n_wins} wins / {n_losses} losses) — "
                    f"sufficient to train"}


def train_model(shadow_path=None, closed_trades=None, min_samples=100,
                min_per_class=20, lr=0.3, epochs=300, l2=0.01):
    """Top-level orchestration: checks volume FIRST, only trains if
    sufficient. Returns {"trained": bool, "volume": {...}, "model": {...}
    or None}."""
    labeled = load_labeled_data(shadow_path, closed_trades)
    volume = check_volume_sufficiency(labeled, min_samples, min_per_class)
    if not volume["sufficient"]:
        return {"trained": False, "volume": volume, "model": None}
    X = [f for f, _ in labeled]
    y = [l for _, l in labeled]
    mins, ranges = _normalize_fit(X)
    X_norm = [_normalize_apply(x, mins, ranges) for x in X]
    weights, bias = train_logistic_regression(X_norm, y, lr, epochs, l2)
    model = {"weights": weights, "bias": bias, "mins": mins, "ranges": ranges,
            "feature_names": list(FEATURE_NAMES), "trained_on_n": len(labeled)}
    return {"trained": True, "volume": volume, "model": model}
