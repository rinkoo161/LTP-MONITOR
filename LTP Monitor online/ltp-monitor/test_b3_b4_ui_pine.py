"""v58.50 — roadmap B3 (S8/S9 UI) and B4 (Pine parity oracles).

B3 is built in a deliberate ORDER: Settings subcards and the calibration
panel first, chart markers last. Reason — S9 has never fired, and the
argument for building its UI now is that the subcards and panel are what
you need to TUNE it toward firing. They unblock calibration rather than
dressing up a dead strategy. Markers, which only matter once it fires,
are still not built.

B4 follows the Strategy 7 precedent: every server-side marker must have
a matching Pine triangle on the same bar, with no gates in Pine, so a
disagreement is a geometry bug rather than a gating difference.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import config
H = open("static/dashboard.html").read()

print("1) B3 — Settings subcards")
for i in ("s_s8_enabled", "s_s8_auto", "s_s8_ed", "s_s8_hs", "s_s8_fhs",
          "s_s8_tideall", "s_s8_zz", "s_s8_rr",
          "s_ta_enabled", "s_ta_auto", "s_ta_minconf", "s_ta_slope",
          "s_ta_gmmatf", "s_ta_zz", "s_ta_callog"):
    check(f"input '{i}' exists", f'id="{i}"' in H)
# every input must be BOTH populated on load and sent on save, or the
# control silently does nothing — the failure mode that makes a settings
# UI worse than no settings UI.
ids = re.findall(r'id="(s_(?:s8|ta)_\w+)"', H)
missing_load = [i for i in ids if f'getElementById("{i}").checked=' not in H
                and f'getElementById("{i}").value=' not in H]
missing_save = [i for i in ids if H.count(f'getElementById("{i}")') < 2]
check("every S8/S9 control is populated on load", not missing_load, str(missing_load))
check("every S8/S9 control is sent on save", not missing_save, str(missing_save))
check("GMMA timeframe is a select, not free text", '<select id="s_ta_gmmatf"' in H)
check("the 5m option states why it is worse",
      "needs 325min of session" in H)
check("auto-deploy labels say what OFF means",
      H.count("never fires") >= 2)

print("\n2) B3 — calibration panel")
check("panel exists", 'id="calBody"' in H)
check("renderer exists", "async function loadCalibration()" in H)
check("reads the real endpoint", "/api/ta_elliott/calibration" in H)
check("a 0% hit rate is flagged, not shown neutrally",
      "threshold or availability defect" in H)
check("IMPULSE at 0% names the responsible setting",
      "bb_slope_eps is too high" in H)
check("dead divergence names the responsible setting",
      "tighten ta_zigzag_deviation_pct" in H)
check("uncomputable GMMA is called out",
      "does not exist for most of the session" in H)
check("raw distributions are shown, not just hit rates",
      "abs_bb_slope" in H and "pivots_5m" in H)
check("panel states the tuning ORDER",
      "leave <code>ta_min_confluence</code> until last" in H)
check("chart markers deliberately NOT built yet",
      "s8_markers_enabled" in config.DEFAULTS,
      "key still registered and still unconsumed — markers are last")

print("\n3) B4 — Pine parity oracles")
for f in ("pine/S8_ew_reversal_parity.pine", "pine/S9_ta_elliott_parity.pine"):
    check(f"{f} exists", os.path.exists(f))
S8 = open("pine/S8_ew_reversal_parity.pine").read()
S9 = open("pine/S9_ta_elliott_parity.pine").read()
for name, src in (("S8", S8), ("S9", S9)):
    check(f"{name} is ASCII-only (non-ASCII breaks the Pine lexer)",
          all(ord(c) < 128 for c in src),
          str([c for c in src if ord(c) >= 128][:5]))
    check(f"{name} declares //@version=5", "//@version=5" in src)
    check(f"{name} states it is an oracle, not a strategy",
          "PARITY ORACLE" in src)
# Assert it is not CALLED. The name appears in the comment explaining
# why it is avoided, and an over-strict match would force deleting that
# explanation to make the test pass.
_calls = [ln for ln in S8.splitlines()
          if ("ta.pivothigh(" in ln or "ta.pivotlow(" in ln)
          and not ln.strip().startswith("//")]
check("S8 does NOT call ta.pivothigh/pivotlow", not _calls,
      "a fixed-bar pivot is a DIFFERENT definition from our % zigzag")
check("S8 explains that hazard rather than just avoiding it",
      "different definition" in S8)
check("S8 ports the percentage zigzag", "devPct" in S8 and "pushPivot" in S8)
check("S8 emits only CONFIRMED pivots, like the server",
      "running\n// extreme is never emitted" in S8 or "never emitted" in S8)
check("S8 covers all three detectors",
      all(k in S8 for k in ("edBear", "hsBear", "fhsBull")))
check("S8 uses FIVE pivots for H&S, matching the server",
      "FIVE pivots, not six" in S8)
check("S8 interpolates the neckline", "slope" in S8 and "neck" in S8)
check("S8 states the one-way parity direction",
      "NOT the reverse" in S8)
check("S9 plots each of the seven components separately",
      all(k in S9 for k in ("cBbStall", "cGmma", "cZero", "cHidden",
                            "cRegular", "cRsi", "cAdx")))
check("S9 gets GMMA from 1m via request.security",
      'request.security(syminfo.tickerid, "1"' in S9,
      "the server computes GMMA on 1m; a 5m ribbon would agree with nothing")
check("S9 explains the 5m GMMA availability problem",
      "325 minutes" in S9)
check("S9 uses the 65-minute Tide, not the 195-minute one",
      "195-minute" in S9)
# Strip the trailing box-border pipes and comment prefixes before
# flattening, or the phrase stays broken by "// //" noise.
_flat = " ".join(re.sub(r"//|\|", " ", S9).split())
check("S9 omits the width-expansion condition the deck does not state",
      "not in the source deck" in _flat, _flat[_flat.find("width expansion"):][:90])
check("S9 exposes whether GMMA was computable at all",
      "gmma_computable" in S9)
check("both note that Pine applies NO gates",
      "no gates" in S8.lower() and "no gates" in S9.lower())

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
