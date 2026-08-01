"""v59.0 Phase B §5 — the basis residual.

Raw basis is mostly cost of carry: it widens with time to expiry and
collapses at expiry, so watching it reports the calendar. The residual is
the part carry does not explain, which is where positioning shows up.

Three of these checks exist because the failure mode is silence:

  * q = 0 must never be substituted. NIFTY ex-dates cluster Feb-Aug, so a
    zero-dividend assumption inflates fair basis in exactly those months
    and biases the residual the same way every year.
  * a cold start must not emit z = 0. "Not enough history" and "exactly
    average" are different statements that a chart cannot distinguish
    once both are the number 0.
  * the gate may only VETO. A signal that can cause a trade is a strategy;
    this is an observation with a veto.
"""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config and the basis table")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import basis_residual as br, config, history

print("1) fair basis, hand-computed from known inputs")
fair, meta = br.fair_basis(24000, 30, "NIFTY", r_pct=6.5, q_pct=1.2)
expect = 24000 * ((6.5 - 1.2) / 100) * (30 / 365)
check("spot 24000, r 6.5%, q 1.2%, 30d -> 104.548 pts",
      abs(fair - expect) < 1e-9, f"{fair:.4f} vs {expect:.4f}")
check("fair basis shrinks to zero at expiry",
      abs(br.fair_basis(24000, 0, "NIFTY")[0]) < 1e-12)
far = br.fair_basis(24000, 60, "NIFTY", r_pct=6.5, q_pct=1.2)[0]
check("and scales linearly with time to expiry", abs(far - 2 * expect) < 1e-9,
      f"{far:.3f} vs {2*expect:.3f}")
check("a dividend yield ABOVE the financing rate inverts the basis",
      br.fair_basis(24000, 30, "NIFTY", r_pct=3.0, q_pct=4.0)[0] < 0)

print("\n2) the dividend path never zero-substitutes")
q, approx = br.dividend_yield("NIFTY", 30)
check("with no calendar it uses the configured estimate, not 0",
      q == config.DEFAULTS["fut_dividend_yield_pct"] and q > 0, f"q={q}")
check("...and marks the result approx", approx is True)
config.save({"index_dividend_calendar": {"NIFTY": 2.5}})
q2, approx2 = br.dividend_yield("NIFTY", 30)
check("a real calendar figure is used and NOT marked approx",
      q2 == 2.5 and approx2 is False, f"q={q2} approx={approx2}")
config.save({"index_dividend_calendar": {}})
check("approx travels in the compute() payload",
      br.compute("NIFTY", 24000, 24100, 30, [])["approx"] is True)

print("\n3) z-score against a synthetic series")
hist = [10.0] * 100 + [20.0] * 100          # mean 15, sd ~5.01
config.save({"fut_residual_z_window": 200})
o = br.compute("NIFTY", 24000, 24000 + 0, 30, hist)
mean = sum(hist) / len(hist)
sd = math.sqrt(sum((x - mean) ** 2 for x in hist) / (len(hist) - 1))
manual = (o["residual"] - mean) / sd
check("z matches the hand-computed value", abs(o["residual_z"] - manual) < 1e-9,
      f"{o['residual_z']:.4f} vs {manual:.4f}")
check("mean and stdev are reported alongside", abs(o["mean"] - mean) < 1e-9
      and abs(o["stdev"] - sd) < 1e-9)
check("z_ready is True once the window is full", o["z_ready"] is True)

print("\n4) COLD START — no fake zero")
for n in (0, 1, 50, 199):
    c = br.compute("NIFTY", 24000, 24100, 30, [5.0] * n)
    if c["residual_z"] is not None:
        check(f"{n} samples -> z is None", False, str(c["residual_z"]))
        break
else:
    check("below the window, z is None — never 0.0", True, "0/1/50/199 samples")
check("z_ready is False on a cold start",
      br.compute("NIFTY", 24000, 24100, 30, [5.0] * 10)["z_ready"] is False)
check("a cold start does not divide by zero (no exception, no inf)", True)
flat = br.compute("NIFTY", 24000, 24100, 30, [7.0] * 200)
check("a FLAT history gives z=None, not inf or a fabricated 0",
      flat["residual_z"] is None, str(flat["residual_z"]))

print("\n5) the gate may only VETO")
check("no z -> cannot veto (returns allowed)", br.agrees("LONG", None) is True)
check("z inside the band -> allowed", br.agrees("LONG", 0.4) is True)
check("sustained discount VETOES a long", br.agrees("LONG", -2.0) is False)
check("sustained premium VETOES a short", br.agrees("SHORT", 2.0) is False)
check("a premium does NOT create a long signal", br.agrees("SHORT", 2.0) is False
      and br.agrees("LONG", 2.0) is True,
      "it permits, it never originates")
ok, why = br.gate_for("s11", "LONG", -3.0)
check("the gate is OFF by default for every strategy", ok is True, why)
config.save({"s11_require_basis_agreement": True})
ok2, why2 = br.gate_for("s11", "LONG", -3.0)
check("switched on, it vetoes", ok2 is False, why2)
ok3, why3 = br.gate_for("s11", "LONG", None)
check("switched on with no z, it still cannot veto", ok3 is True, why3)
config.save({"s11_require_basis_agreement": False})

print("\n6) persistence")
history.log_basis_residual("ZZBR", 1750000000, 24000, 24120, 120, 104.5,
                           15.5, None, 30, 6.5, 1.2, True)
rows = history.basis_residual_series("ZZBR")
check("a row round-trips", len(rows) == 1, str(len(rows)))
check("residual_z is stored NULL, not 0, on a cold start",
      rows[0]["residual_z"] is None, str(rows[0]["residual_z"]))
check("approx is persisted with the row", rows[0]["approx"] == 1)
history.log_basis_residual("ZZBR", 1750000060, 24000, 24130, 130, 104.5,
                           25.5, 1.9, 30, 6.5, 1.2, False)
rows2 = history.basis_residual_series("ZZBR")
check("series is oldest-first for the z window",
      rows2[0]["ts"] < rows2[1]["ts"], str([r["ts"] for r in rows2]))

print("\n7) registered in both places, clamped on read")
app_src = open("app.py").read()
for k in ("fut_financing_rate_pct", "fut_dividend_yield_pct",
          "fut_residual_z_window"):
    check(f"{k} in DEFAULTS", k in config.DEFAULTS)
    check(f"{k} on SettingsIn", k in app_src)
config.save({"fut_financing_rate_pct": 99})
check("an out-of-bounds rate clamps on read", br.param("fut_financing_rate_pct") == 12.0,
      str(br.param("fut_financing_rate_pct")))
config.save({"fut_financing_rate_pct": 6.5})

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
