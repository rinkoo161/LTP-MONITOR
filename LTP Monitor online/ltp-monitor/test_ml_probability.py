"""v58.9 (item 12) — tests for genuine ML probability scoring trained
on the Shadow Journal, per roadmap item #7/#12 ("ML probability
scoring... dependent on the Shadow Journal accumulating sufficient
volume"). Distinct from ai_probability_engine.estimate_probability()
(bucketed historical win-rate counting, already built) — this trains
an actual logistic regression via plain-Python gradient descent, no
external ML dependencies, matching this project's established style.

`check_volume_sufficiency()` measures the ACTUAL current volume every
call rather than assuming a standing "not enough yet" — this is the
mechanism that answers the volume question directly and repeatably as
real data accumulates, rather than a one-time judgment call.

Run:  python3 test_ml_probability.py
"""
import json
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_probability as ml

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) volume sufficiency correctly gates on too few samples")
tiny = [([80, 0, 0, 1, 2.0], 1)] * 10
v1 = ml.check_volume_sufficiency(tiny)
check("insufficient with only 10 samples (below the 100 default)",
      v1["sufficient"] is False, str(v1))
check("the actual current count is reported, not just yes/no",
      v1["n"] == 10, str(v1["n"]))

print("\n2) volume sufficiency correctly gates on class imbalance even "
     "when the total count is high enough")
imbalanced = [([80, 0, 0, 1, 2.0], 1)] * 90 + [([20, 5, 0, 1, 0.5], 0)] * 10
v2 = ml.check_volume_sufficiency(imbalanced)
check("insufficient due to imbalance (90 wins / 10 losses) despite "
      "100 total samples clearing the raw count threshold",
      v2["sufficient"] is False and "imbalance" in v2["reason"], str(v2))

print("\n3) a genuinely sufficient, balanced dataset is correctly "
     "accepted")
balanced = [([80, 0, 0, 1, 2.0], 1)] * 50 + [([20, 5, 0, 1, 0.5], 0)] * 50
v3 = ml.check_volume_sufficiency(balanced)
check("sufficient with 50/50 balance at 100 total", v3["sufficient"] is True, str(v3))

print("\n4) the trained model learns the CORRECT direction for each "
     "feature from a synthetic dataset with a known underlying "
     "relationship (higher confidence/RR -> more likely win, more "
     "failed checks -> less likely win)")
random.seed(42)
data = []
for _ in range(300):
    confidence = random.uniform(50, 95)
    num_failed = random.choice([0, 1, 2, 3])
    is_s7 = random.choice([0.0, 1.0])
    direction_ce = random.choice([0.0, 1.0])
    rr = random.uniform(1.5, 3.5)
    true_p = 1 / (1 + 2.718281828 ** (-(0.08 * (confidence - 70) -
                                      0.8 * num_failed + 0.3 * (rr - 2))))
    label = 1 if random.random() < true_p else 0
    data.append(([confidence, num_failed, is_s7, direction_ce, rr], label))

X = [f for f, _ in data]
y = [l for _, l in data]
mins, ranges = ml._normalize_fit(X)
X_norm = [ml._normalize_apply(x, mins, ranges) for x in X]
weights, bias = ml.train_logistic_regression(X_norm, y)
model = {"weights": weights, "bias": bias, "mins": mins, "ranges": ranges}

check("confidence weight is positive (higher confidence -> higher "
      "predicted win probability)",
      weights[0] > 0, str(weights[0]))
check("num_failed_checks weight is negative (more failures -> lower "
      "predicted win probability)",
      weights[1] < 0, str(weights[1]))

print("\n5) the trained model correctly ranks a genuinely high-quality "
     "signal above a genuinely low-quality one")
p_high = ml.predict_proba(model, [90, 0, 0, 1, 3.0])
p_low = ml.predict_proba(model, [55, 3, 0, 1, 1.5])
check("high-quality signal predicted well above low-quality",
      p_high > p_low, f"{p_high:.3f} vs {p_low:.3f}")
check("predicted probabilities are valid probabilities (0-1)",
      0 <= p_high <= 1 and 0 <= p_low <= 1, f"{p_high}, {p_low}")

print("\n6) feature extraction: correctly builds a feature vector from "
     "a realistic shadow-journal entry")
entry = {"confidence": 80, "entry": 100, "stoploss": 85, "target1": 130,
        "signal": "BUY_CE", "failed_checks": ["x", "y"], "s7_gates": None}
f = ml.extract_features(entry)
check("feature vector has the right length and values",
      f == [80.0, 2.0, 0.0, 1.0, 2.0], str(f))

print("\n7) feature extraction correctly skips entries missing required "
     "fields, rather than guessing a default")
check("missing confidence -> None",
      ml.extract_features({"entry": 100, "stoploss": 85, "target1": 130,
                          "signal": "BUY_CE"}) is None)
check("a degenerate stop distance (entry <= stoploss) -> None, same "
      "guard used elsewhere in this codebase for this exact case",
      ml.extract_features({"confidence": 80, "entry": 100, "stoploss": 105,
                          "target1": 130, "signal": "BUY_CE"}) is None)
check("a WAIT signal (not actionable) -> None",
      ml.extract_features({"confidence": 80, "entry": 100, "stoploss": 85,
                          "target1": 130, "signal": "WAIT"}) is None)

print("\n8) end-to-end: reading a real shadow-journal file, correctly "
     "labeling REJECTED-and-resolved entries, skipping unresolved "
     "ones, and joining APPROVED entries against real closed trades")
entries = [
    {"id": "NIFTY-1", "ts": "2026-07-27T10:00:00", "symbol": "NIFTY",
    "signal": "BUY_CE", "strike": 24000, "entry": 100, "stoploss": 85,
    "target1": 130, "target2": 160, "confidence": 80, "verdict": "REJECTED",
    "checks": ["x"], "failed_checks": ["x"], "source": "AI", "s7_gates": None,
    "resolution": "would_have_hit_target1"},
    {"id": "NIFTY-2", "ts": "2026-07-27T10:05:00", "symbol": "NIFTY",
    "signal": "BUY_PE", "strike": 24000, "entry": 90, "stoploss": 75,
    "target1": 120, "target2": 150, "confidence": 60, "verdict": "REJECTED",
    "checks": ["x", "y"], "failed_checks": ["x", "y"], "source": "AI",
    "s7_gates": None, "resolution": "would_have_hit_stoploss"},
    {"id": "NIFTY-3", "ts": "2026-07-27T10:10:00", "symbol": "NIFTY",
    "signal": "BUY_CE", "strike": 24000, "entry": 95, "stoploss": 80,
    "target1": 125, "target2": 155, "confidence": 70, "verdict": "REJECTED",
    "checks": ["x"], "failed_checks": ["x"], "source": "AI", "s7_gates": None,
    "resolution": "pending"},
    {"id": "NIFTY-4", "ts": "2026-07-27T11:00:00", "symbol": "NIFTY",
    "signal": "BUY_CE", "strike": 24000, "entry": 100, "stoploss": 85,
    "target1": 130, "target2": 160, "confidence": 85, "verdict": "APPROVED",
    "checks": ["x"], "failed_checks": [], "source": "AI", "s7_gates": None,
    "resolution": "taken"},
    {"id": "NIFTY-5", "ts": "2026-07-27T12:00:00", "symbol": "NIFTY",
    "signal": "BUY_CE", "strike": 24000, "entry": 100, "stoploss": 85,
    "target1": 130, "target2": 160, "confidence": 90, "verdict": "APPROVED",
    "checks": ["x"], "failed_checks": [], "source": "AI", "s7_gates": None,
    "resolution": "taken"},
]
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
    for e in entries:
        tf.write(json.dumps(e) + "\n")
    path = tf.name

approved_ts_4 = time.mktime(time.strptime("2026-07-27T11:00:00"[:19], "%Y-%m-%dT%H:%M:%S"))
closed_trades = [
    {"symbol": "NIFTY", "opened_ts": approved_ts_4 + 30, "pnl": 500},   # matches entry 4
    # deliberately NO matching closed trade for entry 5 (still open, or untracked)
]
try:
    labeled = ml.load_labeled_data(path, closed_trades)
    check("exactly 3 usable labeled entries (2 resolved-rejected + 1 "
          "matched-approved; the pending one and the unmatched "
          "approved one are correctly excluded)",
          len(labeled) == 3, str(len(labeled)))
    labels_found = sorted(l for _, l in labeled)
    check("labels are correct: one win from would_have_hit_target1, "
          "one loss from would_have_hit_stoploss, one win from the "
          "matched real trade",
          labels_found == [0, 1, 1], str(labels_found))
finally:
    os.unlink(path)

print("\n9) an APPROVED entry with NO matching closed trade at all is "
     "correctly excluded (not guessed at), even with an empty "
     "closed_trades list")
entries_no_match = [entries[3]]   # the APPROVED one, no closed trade at all
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
    for e in entries_no_match:
        tf.write(json.dumps(e) + "\n")
    path2 = tf.name
try:
    labeled2 = ml.load_labeled_data(path2, [])
    check("zero labeled entries when no closed trades exist to match against",
          len(labeled2) == 0, str(len(labeled2)))
finally:
    os.unlink(path2)

print("\n10) top-level orchestration (train_model): correctly reports "
     "untrained status with insufficient real data, without crashing")
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
    tf.write(json.dumps(entries[0]) + "\n")
    path3 = tf.name
try:
    result = ml.train_model(shadow_path=path3, closed_trades=[], min_samples=100)
    check("trained is False with genuinely insufficient data",
          result["trained"] is False, str(result["trained"]))
    check("model is None when not trained (no fabricated model)",
          result["model"] is None)
    check("the volume dict reports the real count (1)",
          result["volume"]["n"] == 1, str(result["volume"]))
finally:
    os.unlink(path3)

print("\n11) API endpoint: reports honest status via a real HTTP call, "
     "even with zero shadow-journal data present in this environment")
from fastapi.testclient import TestClient
import app as app_module
app_module.pilot.bus.set("closed_trades", [])
client = TestClient(app_module.app)
r11 = client.get("/api/ml-probability/status")
d11 = r11.json()
check("endpoint returns ok:True", d11.get("ok") is True, str(d11.get("ok")))
check("trained is False with no real shadow journal data on this machine",
      d11.get("trained") is False, str(d11.get("trained")))
check("volume dict present and honest", "volume" in d11 and "n" in d11["volume"],
      str(d11.get("volume")))

print("\n12) source-level guard: LearningAgent's daily cycle actually "
     "wires this in, persisting status/model to the bus for later reads")
agents_src = open("agents.py").read()
check("daily cycle calls ml.train_model with the real closed_trades",
      "ml_result = ml.train_model(closed_trades=all_trades)" in agents_src)
check("volume status persisted to the bus every day, trained or not",
      'self.bus.set("ml_probability_status", ml_result["volume"])' in agents_src)
check("the trained model itself is persisted once available",
      'self.bus.set("ml_probability_model", ml_result["model"])' in agents_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
