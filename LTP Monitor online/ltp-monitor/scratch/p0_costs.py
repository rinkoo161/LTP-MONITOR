#!/usr/bin/env python3
"""Part 0.2 — all-in round-trip cost from FIRST PRINCIPLES.

Written independently of options_costs.py so the comparison is a check,
not the model validating itself. Statutory rates are the published
Indian F&O option rates as of 2025-26; each is stated so it can be
disputed line by line.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, options_costs

BROKERAGE_PER_ORDER = 20.0     # Dhan F&O flat
STT_SELL_PREMIUM    = 0.0010   # 0.10% on SELL premium (options)
EXCH_TXN            = 0.00050  # NSE ~0.0495%, rounded; on premium turnover both sides
SEBI                = 0.000001 # Rs 10 per crore
STAMP_BUY           = 0.00003  # 0.003% on BUY side only
GST                 = 0.18     # on brokerage + exchange + SEBI

def leg_cost(prem_in, prem_out, lot, halfspread):
    buy_not, sell_not = prem_in * lot, prem_out * lot
    brok = BROKERAGE_PER_ORDER * 2
    stt  = STT_SELL_PREMIUM * sell_not
    exch = EXCH_TXN * (buy_not + sell_not)
    sebi = SEBI * (buy_not + sell_not)
    stamp= STAMP_BUY * buy_not
    gst  = GST * (brok + exch + sebi)
    statutory = brok + stt + exch + sebi + stamp + gst
    slip = halfspread * lot * 2          # cross the spread on entry AND exit
    return statutory, slip

cfg = config.load()
LOT = cfg["lot_sizes"]["NIFTY"]
HS  = options_costs.rate("opt_halfspread_points", cfg)
flat = cfg.get("fee_per_lot", 40)

print(f"  NIFTY lot {LOT}, halfspread {HS} pts, fee_per_lot {flat}\n")

print("  A) 1-lot NIFTY weekly ATM option, bought 150 -> sold 160")
s, sl = leg_cost(150.0, 160.0, LOT, HS)
print(f"     statutory Rs {s:7.2f}   slippage Rs {sl:7.2f}   TOTAL Rs {s+sl:7.2f}")
own = options_costs.cost_round_trip(150.0, 160.0, LOT, legs=1, cfg=cfg)
print(f"     options_costs.py says      statutory {own['statutory']:7.2f}  spread {own['spread']:7.2f}  total {own['total']:7.2f}")
print(f"     flat model would charge    Rs {flat*2:7.2f}")
print(f"     RATIO real/flat            {(s+sl)/(flat*2):.2f}x   (statutory alone {s/(flat*2):.2f}x)\n")

print("  B) 1-lot bull put spread: SELL ATM 150, BUY OTM hedge 60")
s1, sl1 = leg_cost(150.0, 140.0, LOT, HS)     # short leg: sold then bought back
s2, sl2 = leg_cost(60.0, 52.0, LOT, HS)       # long hedge leg
print(f"     short leg  statutory {s1:7.2f}  slippage {sl1:7.2f}")
print(f"     hedge leg  statutory {s2:7.2f}  slippage {sl2:7.2f}")
print(f"     TOTAL      statutory {s1+s2:7.2f}  slippage {sl1+sl2:7.2f}  = Rs {s1+s2+sl1+sl2:7.2f}")
own2 = options_costs.cost_round_trip(150.0, 140.0, LOT, legs=2, cfg=cfg)
print(f"     options_costs.py (legs=2)  total Rs {own2['total']:7.2f}")
print(f"     flat model (fee x lots x4) Rs {flat*4:7.2f}")
print(f"     RATIO real/flat            {(s1+s2+sl1+sl2)/(flat*4):.2f}x\n")

print("  NOTE on STT: charged on SELL premium for options. For a LONG that is")
print("  exercised/settled ITM, STT is levied on INTRINSIC VALUE, which for a")
print("  deep-ITM weekly can dwarf the premium-based figure above. This system")
print("  squares off intraday at 15:23 and never carries to expiry, so the")
print("  intrinsic-value case does not arise here — but any strategy that HELD")
print("  to expiry would need it modelled. Neither model above covers it.")
