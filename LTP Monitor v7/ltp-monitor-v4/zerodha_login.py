"""Daily Zerodha login — run each morning:  python zerodha_login.py
1. Opens the Kite login URL (or prints it) — log in with your Zerodha creds
2. After login you land on your redirect URL with ?request_token=XXX
3. Paste that request_token here; it's exchanged for today's access_token
   and saved to ~/.ltp-monitor/config.json automatically."""
import hashlib, json, urllib.request, urllib.parse, webbrowser, getpass, config

cfg = config.load()
api_key = cfg.get("zerodha_api_key") or input("Kite API key: ").strip()
api_secret = getpass.getpass("Kite API secret (hidden): ").strip()
url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
print("\nOpening login page (or open manually):\n " + url)
try: webbrowser.open(url)
except Exception: pass
rt = input("\nAfter login, paste request_token (or the whole redirect URL): ").strip()
if "request_token=" in rt:   # full URL pasted — extract the param
    rt = urllib.parse.parse_qs(urllib.parse.urlparse(rt).query)["request_token"][0]
rt = rt.split("&")[0].strip()
checksum = hashlib.sha256((api_key + rt + api_secret).encode()).hexdigest()
body = urllib.parse.urlencode({"api_key": api_key, "request_token": rt,
                               "checksum": checksum}).encode()
req = urllib.request.Request("https://api.kite.trade/session/token",
    data=body, headers={"X-Kite-Version": "3",
    "Content-Type": "application/x-www-form-urlencoded"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())["data"]
except urllib.error.HTTPError as e:
    try: msg = json.loads(e.read()).get("message", "")
    except Exception: msg = ""
    print(f"\nKite rejected the exchange ({e.code}): {msg}")
    print("Most likely: request_token expired (they last ~5 min, single-use)")
    print("or API secret mismatch. Re-run and complete quickly.")
    raise SystemExit(1)
config.save({"zerodha_api_key": api_key,
             "zerodha_access_token": data["access_token"],
             "broker": "zerodha"})
print(f"\nSaved today's access token for {data.get('user_id')}. "
      "Restart the app (broker already set to Zerodha).")
