"""Daily Kotak Neo login — run each morning before starting the app:
    python kotak_login.py
Does TOTP+MPIN login and saves the day's session (token/sid/baseUrl)
into ~/.ltp-monitor/config.json so the app can use Kotak all day."""
import getpass, json, urllib.request, config

def post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

cfg = config.load()
tok = cfg.get("kotak_access_token") or getpass.getpass("Access Token (hidden): ").strip()
mob = cfg.get("kotak_mobile") or input("Mobile (+91...): ").strip()
ucc = cfg.get("kotak_ucc") or input("UCC (e.g. W1DZT): ").strip()
totp = input("TOTP: ").strip()
h = {"Authorization": tok, "neo-fin-key": "neotradeapi",
     "Content-Type": "application/json"}
d = post("https://mis.kotaksecurities.com/login/1.0/tradeApiLogin", h,
         {"mobileNumber": mob, "ucc": ucc, "totp": totp})["data"]
print("View session OK")
mpin = getpass.getpass("MPIN (hidden): ").strip()
h2 = dict(h, Auth=d["token"], sid=d["sid"])
d2 = post("https://mis.kotaksecurities.com/login/1.0/tradeApiValidate", h2,
          {"mpin": mpin})["data"]
config.save({"kotak_access_token": tok, "kotak_mobile": mob, "kotak_ucc": ucc,
             "kotak_session_token": d2["token"], "kotak_sid": d2["sid"],
             "kotak_base_url": d2["baseUrl"]})
print(f"Trade session saved. baseUrl={d2['baseUrl']}")
print("Select 'Kotak Neo' as broker in Settings and restart the app.")
