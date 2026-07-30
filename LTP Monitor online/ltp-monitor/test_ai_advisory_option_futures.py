"""v58.22+ — tests for the new option/futures AI advisory functions,
per explicit request: "AI analysis should also provide the input to
open trade, future and spread about the next possible market move and
take decision."

Spreads already had an AI HOLD/EXIT advisory (_spread_ai_check).
Single-leg options ("open trade") and futures had no equivalent. This
adds both, mirroring the spread pattern exactly (advisory-only by
default, same 5-minute cadence, same separate auto-exit opt-in), and
adds a shared _market_move_context() helper (reusing the existing
regime/MTF-confluence read every directional strategy gate already
depends on) so all three advisories factor in where price may move
next, not just each position's own static numbers. Also retrofits the
existing spread advisory's prompt to include this same context.

Run:  python3 test_ai_advisory_option_futures.py
"""
import json
import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents
import config

src = open("agents.py").read()
cfg_src = open("config.py").read()
app_src = open("app.py").read()

print("1) source-level: config keys registered in both DEFAULTS and "
     "SettingsIn for both new advisories")
for key, default in [("option_ai_auto_exit_enabled", "False"),
                     ("option_ai_exit_confidence_threshold", "75"),
                     ("futures_ai_auto_exit_enabled", "False"),
                     ("futures_ai_exit_confidence_threshold", "75")]:
    check(f'"{key}" in config.py DEFAULTS', f'"{key}": {default}' in cfg_src)
    field_type = "bool" if "enabled" in key else "int"
    check(f"{key} registered in SettingsIn",
          f"{key}: {field_type} | None = None" in app_src)

print("\n2) source-level: both new advisory functions exist and are "
     "wired into their respective monitors' else-branch (fires only "
     "when no rule-based exit reason already fired, same as spreads)")
check("_option_ai_check function is defined",
      "def _option_ai_check(self, p, sym, ltp):" in src)
check("_option_ai_check is called from _monitor_one's else branch",
      "self._option_ai_check(p, sym, ltp)" in src)
check("_futures_ai_check function is defined",
      "def _futures_ai_check(self, p, sym, ltp):" in src)
check("_futures_ai_check is called from _monitor_futures's else branch",
      "self._futures_ai_check(p, sym, ltp)" in src)

print("\n3) source-level: the shared market-move context helper exists "
     "and is used by ALL THREE advisories (spread retrofitted, option "
     "and futures built with it from the start)")
check("_market_move_context helper is defined",
      "def _market_move_context(self, sym):" in src)
check("spread advisory's prompt includes the market-move context "
     "(retrofitted, not just the new ones)",
      "market_ctx = self._market_move_context(sp[\"symbol\"])" in src)
check("option advisory's prompt includes the market-move context",
      "market_ctx = self._market_move_context(sym)" in src and
      src.count("market_ctx = self._market_move_context(sym)") >= 2)


class FakeBus:
    def __init__(self):
        self.data = {}
        self.logs = []
        self.alerts = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, val):
        self.data[key] = val

    def log(self, name, msg):
        self.logs.append(msg)

    def alert(self, level, name, sym, msg):
        self.alerts.append(msg)


def make_fake_execution_agent():
    fake_self = types.SimpleNamespace()
    fake_self.bus = FakeBus()
    fake_self.name = "execution"
    fake_self.bus.set("regime:NIFTY", {
        "regime": "trending-down", "confidence": 82, "confluence": "mixed-bear",
        "session_change_pct": -0.8, "adx": 24.5, "allowed_signals": ["BUY_PE"],
    })
    # A plain SimpleNamespace doesn't inherit ExecutionAgent's methods —
    # bind this one explicitly so self._market_move_context(sym) calls
    # from inside the functions under test actually resolve, instead of
    # silently failing (caught by that function's own try/except) and
    # masking every downstream assertion as "the LLM was never called".
    fake_self._market_move_context = lambda sym: agents.ExecutionAgent._market_move_context(fake_self, sym)
    return fake_self


print("\n4) BEHAVIORAL VERIFICATION: _market_move_context builds a "
     "real string from actual regime data, and handles missing data "
     "gracefully rather than silently omitting it")
fake_self = make_fake_execution_agent()
ctx = agents.ExecutionAgent._market_move_context(fake_self, "NIFTY")
check("context includes the actual regime classification",
      "trending-down" in ctx, ctx)
check("context includes the MTF confluence read",
      "mixed-bear" in ctx, ctx)
check("context includes the directionally-allowed signals",
      "BUY_PE" in ctx, ctx)

ctx_missing = agents.ExecutionAgent._market_move_context(fake_self, "SENSEX")
check("missing regime data produces a clear note, not an empty/silent "
     "string",
      "no regime" in ctx_missing.lower(), ctx_missing)

print("\n5) BEHAVIORAL VERIFICATION: option AI advisory — mocked LLM "
     "response confirms the prompt includes market context, the "
     "advisory is recorded, an alert fires on a confident EXIT call, "
     "but the position is NOT closed with auto-exit disabled (default)")
fake_self2 = make_fake_execution_agent()
fake_self2.exit = lambda reason, symbol=None: results.append(
    ("exit() should NOT have been called with auto-exit disabled", False))
captured_prompts = []

def fake_generate_json(prompt, max_tokens=600):
    captured_prompts.append(prompt)
    return json.dumps({"advice": "EXIT", "confidence": 90,
                      "why": "regime reversing against the position"}), "local", None

pos = {"symbol": "NIFTY", "strike": 24000, "leg": "CE", "entry": 50,
      "stoploss": 30, "target1": 70, "target2": 90, "pnl": -200,
      "ai_ts": 0}

with patch.dict("sys.modules", {"llm": types.SimpleNamespace(generate_json=fake_generate_json)}):
    agents.ExecutionAgent._option_ai_check(fake_self2, pos, "NIFTY", 45)

check("the LLM was actually called (prompt captured)",
      len(captured_prompts) == 1, str(len(captured_prompts)))
check("the prompt includes the market-move context (not just static "
     "position numbers)",
      "trending-down" in captured_prompts[0], captured_prompts[0][:200])
check("the advisory was recorded on the position",
      pos.get("ai_advice") is not None and "EXIT" in pos["ai_advice"],
      str(pos.get("ai_advice")))
check("an alert fired for the confident EXIT call",
      len(fake_self2.bus.alerts) == 1, str(fake_self2.bus.alerts))
check("the position was NOT actually closed (auto-exit disabled by "
     "default)",
      not any("should NOT have been called" in l for l, ok in results if not ok))

print("\n6) BEHAVIORAL VERIFICATION: with auto-exit explicitly enabled, "
     "a confident EXIT call DOES close the position via the correct "
     "exit function")
fake_self3 = make_fake_execution_agent()
exit_calls = []
fake_self3.exit = lambda reason, symbol=None: exit_calls.append((reason, symbol))
pos2 = {"symbol": "NIFTY", "strike": 24000, "leg": "CE", "entry": 50,
       "stoploss": 30, "target1": 70, "target2": 90, "pnl": -200,
       "ai_ts": 0}

with patch("config.load", return_value={**config.DEFAULTS,
                                        "option_ai_auto_exit_enabled": True,
                                        "option_ai_exit_confidence_threshold": 75}):
    with patch.dict("sys.modules",
                    {"llm": types.SimpleNamespace(generate_json=fake_generate_json)}):
        agents.ExecutionAgent._option_ai_check(fake_self3, pos2, "NIFTY", 45)

check("with auto-exit enabled, the position WAS closed via exit()",
      len(exit_calls) == 1, str(exit_calls))
check("the exit reason references the AI advisory",
      len(exit_calls) == 1 and "AI advisory" in exit_calls[0][0],
      str(exit_calls))

print("\n7) BEHAVIORAL VERIFICATION: futures AI advisory — same "
     "confirmation, independently, for the futures-specific function")
fake_self4 = make_fake_execution_agent()
fake_self4.bus.set("regime:FINNIFTY", {
    "regime": "trending-down", "confidence": 82, "confluence": "mixed-bear",
    "session_change_pct": -0.8, "adx": 24.5, "allowed_signals": ["BUY_PE"],
})
fake_self4.exit_future = lambda sym, reason="manual exit": results.append(
    ("exit_future() should NOT have been called with auto-exit disabled", False))
fut_pos = {"symbol": "FINNIFTY", "side": "SHORT", "lots": 1, "entry": 26135.1,
          "sl": 26221.35, "target": 25926.02, "pnl": -1500, "ai_ts": 0}
captured_prompts2 = []

def fake_generate_json2(prompt, max_tokens=600):
    captured_prompts2.append(prompt)
    return json.dumps({"advice": "EXIT", "confidence": 88,
                      "why": "momentum turning against short"}), "local", None

with patch.dict("sys.modules",
                {"llm": types.SimpleNamespace(generate_json=fake_generate_json2)}):
    agents.ExecutionAgent._futures_ai_check(fake_self4, fut_pos, "FINNIFTY", 26177.0)

check("futures advisory: LLM was called with market context included",
      len(captured_prompts2) == 1 and "trending-down" in captured_prompts2[0],
      str(captured_prompts2[:1]))
check("futures advisory: recorded on the position",
      fut_pos.get("ai_advice") is not None and "EXIT" in fut_pos["ai_advice"])
check("futures advisory: alert fired, position not auto-closed by "
     "default",
      len(fake_self4.bus.alerts) == 1 and
      not any("should NOT have been called" in l for l, ok in results if not ok))

print("\n8) BEHAVIORAL VERIFICATION: 5-minute cadence guard — calling "
     "again immediately does NOT re-invoke the LLM")
captured_prompts3 = []

def fake_generate_json3(prompt, max_tokens=600):
    captured_prompts3.append(prompt)
    return json.dumps({"advice": "HOLD", "confidence": 60, "why": "no change"}), "local", None

fake_self5 = make_fake_execution_agent()
pos3 = {"symbol": "NIFTY", "strike": 24000, "leg": "CE", "entry": 50,
       "stoploss": 30, "target1": 70, "target2": 90, "pnl": 10,
       "ai_ts": 0}
with patch.dict("sys.modules",
                {"llm": types.SimpleNamespace(generate_json=fake_generate_json3)}):
    agents.ExecutionAgent._option_ai_check(fake_self5, pos3, "NIFTY", 51)
    agents.ExecutionAgent._option_ai_check(fake_self5, pos3, "NIFTY", 51)

check("the LLM was called exactly once across two immediate calls "
     "(cadence guard working)",
      len(captured_prompts3) == 1, str(len(captured_prompts3)))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
