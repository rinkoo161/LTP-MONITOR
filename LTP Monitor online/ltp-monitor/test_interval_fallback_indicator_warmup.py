"""v58.17+ — tests for a real bug found from a live screenshot: when a
non-DB-backed interval (60m/"1h") falls back to 5m data (because the
market is closed, or the live REST call otherwise fails), the
indicator warm-up logic kept treating the connection as still
requesting "60"/non-DB-backed — skipping the 400-bar DB warm-up that
5m data genuinely has available. MACD/RSI need ~26-34 bars of lookback
before producing a value; with no warm-up, that lookback ate into the
visible range itself, visually delaying the indicators by exactly that
many bars ("indicators misplaced").

Also covers the paired frontend fix: the "1h" button stayed visually
active even though 5m data was actually being shown — the status text
was honest about it, the button wasn't.

Run:  python3 test_interval_fallback_indicator_warmup.py
"""
import json
import os
import re
import sys
import time
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) source-level: the fallback tier now updates interval/"
     "db_backed_interval to reflect what was ACTUALLY delivered, not "
     "what was originally requested")
app_src = open("app.py").read()
fallback_block_start = app_src.index('fallback_source = "db_5m_most_recent_session"')
fallback_block = app_src[fallback_block_start:fallback_block_start + 2000]
check('interval is reassigned to "5" right after the fallback fires',
      'interval = "5"' in fallback_block)
check("db_backed_interval is reassigned to True right after the "
     "fallback fires",
      "db_backed_interval = True" in fallback_block)

print("\n2) frontend: the active interval button updates to reflect "
     "what was delivered (msg.interval), without silently overwriting "
     "lwCurrentInterval itself (which drives future reconnect "
     "requests — the person's actual preference should be retried, "
     "not abandoned after one fallback)")
h = open("static/dashboard.html").read()
check("the history handler checks msg.interval against lwCurrentInterval",
      "msg.interval && msg.interval!==lwCurrentInterval" in h)
check("lwCurrentInterval is NOT reassigned inside this handler (the "
     "user's actual request must survive for future reconnects)",
      h.count("lwCurrentInterval=msg.interval") == 0)

print("\n3) JS syntax still valid")
import subprocess
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/r5_test_dash.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/r5_test_dash.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n4) END-TO-END: a real websocket connection requesting 60m, "
     "with the live REST path unavailable (simulating a closed "
     "market) and a controlled 5m most-recent-session series "
     "available, correctly reports interval='5' in its history "
     "message — not '60' — confirming the actual delivered granularity "
     "is what gets reported, not the originally requested one")
import app as app_module
from fastapi.testclient import TestClient

# Build a realistic, non-degenerate 5m candle series (varying closes)
# for the fallback tier to find.
fake_5m_candles = [
    {"time": 1753600000 + i * 300, "open": 100 + i * 0.1, "high": 100.5 + i * 0.1,
     "low": 99.5 + i * 0.1, "close": 100.2 + i * 0.1}
    for i in range(80)
]

with patch("history.most_recent_session_candles") as mock_most_recent, \
     patch("app.dhan_client") as mock_dhan_client:
    def most_recent_side_effect(security_id):
        if security_id.endswith("_5m"):
            return fake_5m_candles
        return []
    mock_most_recent.side_effect = most_recent_side_effect
    mock_dhan_client.return_value = None   # simulate "no broker client" -> REST tier fails cleanly

    client = TestClient(app_module.app)
    try:
        with client.websocket_connect("/ws/candles/NIFTY?interval=60") as ws:
            msg = ws.receive_json()
            check("received a history message",
                  msg.get("type") == "history")
            check("candles were actually delivered via the fallback "
                 "(not an empty chart)",
                  len(msg.get("candles", [])) > 0, str(len(msg.get("candles", []))))
            check("the reported interval is '5' (what was ACTUALLY "
                 "delivered), not '60' (what was originally requested)",
                  msg.get("interval") == "5", str(msg.get("interval")))
            check("the source is correctly labeled as the 5m "
                 "most-recent-session fallback",
                  msg.get("source") == "db_5m_most_recent_session",
                  str(msg.get("source")))
    except Exception as e:
        check("websocket connection and fallback completed without error",
              False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
