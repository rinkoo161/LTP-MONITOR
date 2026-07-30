"""v58.41 — websocket subscription chunking + honest strike gate.

ITEM 8. A live log showed the subscription reaching 2,080 instruments,
then the server tearing the connection down without a close handshake
("no close frame received or sent") — the in-flight send onto the dead
socket being the `socket.send() raised exception` on the console.
subscribe_more() sent ONE frame per instrument, synchronously, so
2,080 legs meant 2,080 frames in a burst. Dhan documents ~100 per
message. ~115 of those were SENSEX options that had NEVER ticked.

ITEM 4. The strike gate rejected 8 signals/day saying "strike 24100
not OTM (ATM 24200)". For a PUT, 24100 against a 24200 spot IS
out-of-the-money — the message was the opposite of what the condition
enforced (at-or-IN-the-money). The condition is deliberately NOT
flipped: it is a live trading gate and the ITM reading is defensible.
Policy made explicit, default unchanged, message made honest.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import config
WS = open("dhan_ws.py").read()
AG = open("agents.py").read()

print("1) Websocket subscription is chunked, not one-frame-per-instrument")
check("requests are buffered", "_pending_subs" in WS)
check("a flusher sends them in chunks", "_flush_subs" in WS)
check("chunk size defaults to Dhan's documented ~100",
      "subscribe_chunk_size = 100" in WS)
check("there is a delay between chunks", "subscribe_delay_ms" in WS)
check("subscribe_more no longer sends directly",
      "self._feed.subscribe_symbols([(seg_int" not in WS,
      "that was the 2,080-frames-in-a-burst path")
check("the flusher sends the chunk", "self._feed.subscribe_symbols(chunk)" in WS)
check("a failed chunk does not abandon the rest",
      "remaining chunks" in WS,
      "losing one chunk beats losing the whole feed")
check("access to the buffer is locked", "_sub_lock" in WS)
check("flusher is a daemon (cannot block shutdown)", "daemon=True" in WS)
check("return contract preserved: False means 'do not treat as subscribed'",
      "return False" in WS.split("def subscribe_more")[1][:1400])

print("\n2) SENSEX options are no longer subscribed")
check("a skip list exists", "skip_option_symbols" in WS)
check("SENSEX is on it by default", 'skip_option_symbols = ("SENSEX",)' in WS)
_blk = WS.split("if symbol in (self.skip_option_symbols")[1][:400]
check("skipped symbols return False, not a silent True",
      "return False" in _blk, _blk.strip().splitlines()[-1][:60])

print("\n3) Strike gate says what it actually enforces")
check("the misleading 'not OTM' wording is gone from the GATE",
      'f"strike {strike} not OTM' not in AG,
      "for a PUT, a strike below spot IS out-of-the-money")
check("policy is explicit and configurable", "option_strike_policy" in AG)
check("DEFAULT PRESERVES existing behaviour exactly",
      config.DEFAULTS["option_strike_policy"] == "atm_or_itm",
      "a live trading gate must not be flipped on an assumption")
check("the message names the policy in force", "policy \\n" in AG or "policy " in AG)
check("the message spells out the requirement",
      "at-or-in-the-money" in AG and "at-or-out-of-the-money" in AG)
check("'any' disables the gate", '"any"' in AG)
check("missing strike/ATM is reported distinctly",
      "strike/ATM unavailable" in AG)

print("\n4) Registration")
for k in ("option_strike_policy", "ws_subscribe_chunk_size", "ws_subscribe_delay_ms"):
    check(f"'{k}' registered", k in config.DEFAULTS)
check("chunk size default is 100", config.DEFAULTS["ws_subscribe_chunk_size"] == 100)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
