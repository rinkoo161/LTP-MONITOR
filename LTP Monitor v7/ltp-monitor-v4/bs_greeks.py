"""bs_greeks.py — Black-Scholes pricing, greeks, and an implied-volatility
solver, using only the standard library (math.erf for the normal CDF —
no scipy/numpy dependency). Broker-independent: fills in IV/delta/
gamma/theta/vega whenever a broker doesn't provide them (observed with
Kotak and Zerodha; Dhan does provide them natively and takes priority
when present — this module is a fallback, not a replacement).

Standard assumptions, stated plainly rather than hidden:
  - European-style pricing (NSE/BSE index options are European, so
    this is actually exact, not an approximation, for NIFTY/BANKNIFTY/
    FINNIFTY/SENSEX index options specifically).
  - Risk-free rate defaults to India's ~7% (configurable).
  - No dividend yield term (index options don't need one the way
    single-stock options would).
"""
import math

RISK_FREE_RATE_DEFAULT = 0.07


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _d1_d2(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S, K, T, r, sigma, is_call):
    """Theoretical option price. T in years, sigma as a decimal (0.15
    not 15)."""
    if T <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return max(0.0, (S - K) if is_call else (K - S))
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(S, K, T, r, sigma, is_call):
    """Returns delta, gamma, theta (per day), vega (per 1% IV move)."""
    if T <= 0 or sigma <= 0:
        return {"delta": 1.0 if (is_call and S > K) else
               (-1.0 if (not is_call and S < K) else 0.0),
               "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    sqrt_T = math.sqrt(T)

    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1
    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100        # per 1 percentage-point IV move
    if is_call:
        theta = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        theta = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
           "theta": round(theta, 2), "vega": round(vega, 2)}


def implied_vol(price, S, K, T, r, is_call, lo=0.001, hi=5.0, tol=1e-4, max_iter=50):
    """Solve for sigma given a market price. Newton-Raphson using vega
    as the derivative, falling back to bisection if it misbehaves
    (vega can be near-zero for deep ITM/OTM, where Newton-Raphson can
    diverge or overshoot — bisection is slower but always converges
    for a monotonic function like BS price-vs-sigma)."""
    if T <= 0 or price <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price < intrinsic - 0.01:
        return None   # price below intrinsic value — not a valid quote

    sigma = 0.3   # reasonable starting guess
    for _ in range(max_iter):
        model_price = bs_price(S, K, T, r, sigma, is_call)
        diff = model_price - price
        if abs(diff) < tol:
            return round(sigma, 4)
        g = bs_greeks(S, K, T, r, sigma, is_call)
        vega_raw = g["vega"] * 100   # undo the per-1%-move scaling for the derivative
        if vega_raw < 1e-8:
            break
        sigma -= diff / vega_raw
        if sigma <= 0 or sigma > 10:
            break   # Newton-Raphson diverged — fall through to bisection

    # bisection fallback — always converges for a monotonic function
    lo_s, hi_s = lo, hi
    for _ in range(100):
        mid = (lo_s + hi_s) / 2
        p_mid = bs_price(S, K, T, r, mid, is_call)
        if abs(p_mid - price) < tol:
            return round(mid, 4)
        if p_mid < price:
            lo_s = mid
        else:
            hi_s = mid
    return round((lo_s + hi_s) / 2, 4)


def compute_for_leg(spot, strike, ltp, days_to_expiry, is_call,
                    risk_free_rate=RISK_FREE_RATE_DEFAULT):
    """Convenience wrapper: given what every broker's quote already
    gives us (spot, strike, LTP, days to expiry), return a complete
    {iv, delta, gamma, theta, vega} dict — or None if the inputs can't
    support a meaningful solve (e.g. zero LTP, expired contract)."""
    if not (spot and strike and ltp and days_to_expiry and days_to_expiry > 0):
        return None
    T = days_to_expiry / 365.0
    sigma = implied_vol(ltp, spot, strike, T, risk_free_rate, is_call)
    if sigma is None:
        return None
    greeks = bs_greeks(spot, strike, T, risk_free_rate, sigma, is_call)
    return {"iv": round(sigma * 100, 2), **greeks}
