"""Black-Scholes pricing, delta and implied volatility.

Ported from `reference/trading_skills/src/trading_skills/black_scholes.py`
(staskh, MIT). The formulas, the dividend-yield handling and the
Newton-Raphson-with-bisection-fallback IV solver are upstream's; the normal
distribution is ours.

Why we do not import their file: it depends on ``scipy.stats.norm`` for two
functions — the standard normal CDF and PDF. scipy is a ~90MB wheel with a
BLAS/LAPACK stack behind it, and it would land in the Cloud Run image for the
sake of ``norm.cdf``. ``math.erf`` gives the same CDF to double precision in
three lines, so the dependency stays out of ``requirements.txt``.

Why we need it at all: Alpaca's free options feed is ``indicative``, and it
returns neither greeks nor implied volatility (the OPRA feed that carries them
needs a signed exchange agreement). Every delta in the options stage is
therefore computed here, from the quote, exactly the way upstream computes it
from a Yahoo chain — upstream also derives IV from the market price rather than
trusting the vendor's ``impliedVolatility`` column, for the same reason.

RESEARCH ONLY: these are pricing helpers, not an execution path.
"""

from __future__ import annotations

import math

# One trading year of *calendar* days: yields and time-to-expiry are quoted on a
# 365-day basis throughout this package, matching upstream.
DAYS_PER_YEAR = 365.0


def norm_cdf(x: float) -> float:
    """Standard normal CDF. ``scipy.stats.norm.cdf`` without scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal PDF. ``scipy.stats.norm.pdf`` without scipy."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0):
    """d1 and d2 for Black-Scholes (``q`` = continuous dividend yield)."""
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    return d1, d1 - sigma * sqrt_T


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str, q: float = 0.0
) -> float:
    """Black-Scholes price. ``option_type`` is 'call' or 'put'.

    An expired or zero-vol option collapses to intrinsic value, which is what
    makes the IV solver below safe to run on a deep-ITM quote.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type == "call" else max(0.0, K - S)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Vega in price units per 1.0 of vol (not per 1%) — the solver's derivative."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    return S * math.exp(-q * T) * norm_pdf(d1) * math.sqrt(T)


def bs_delta(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str, q: float = 0.0
) -> float:
    """Black-Scholes delta. Puts are negative; callers compare on the absolute value."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0

    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return math.exp(-q * T) * norm_cdf(d1)
    return math.exp(-q * T) * (norm_cdf(d1) - 1.0)


def bs_theta(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str, q: float = 0.0
) -> float:
    """Theta per calendar day — what the seller collects for holding overnight."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_q, disc_r = math.exp(-q * T), math.exp(-r * T)
    decay = -S * disc_q * norm_pdf(d1) * sigma / (2 * math.sqrt(T))
    if option_type == "call":
        return (decay - r * K * disc_r * norm_cdf(d2) + q * S * disc_q * norm_cdf(d1)) / DAYS_PER_YEAR
    return (decay + r * K * disc_r * norm_cdf(-d2) - q * S * disc_q * norm_cdf(-d1)) / DAYS_PER_YEAR


# The solver's search box. Upstream's bounds: an IV below 0.1% or above 500% is
# a broken quote, not a volatility.
IV_MIN, IV_MAX = 0.001, 5.0
_IV_SEED = 0.30


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    q: float = 0.0,
    max_iterations: int = 100,
    tolerance: float = 1e-6,
) -> float | None:
    """Solve for IV from a traded price. Newton-Raphson, bisection fallback.

    Returns ``None`` when the price cannot come from this contract — most often
    a quote below intrinsic value, which the indicative feed does produce on
    stale or one-sided books. Silently returning a floor IV there would hand the
    caller a confident, wrong delta, so the candidate is dropped instead.
    """
    if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    # No volatility can price an option below its own intrinsic value.
    intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
    if market_price < intrinsic - tolerance:
        return None

    sigma = _IV_SEED
    for _ in range(max_iterations):
        price = bs_price(S, K, T, r, sigma, option_type, q)
        vega = bs_vega(S, K, T, r, sigma, q)
        if vega < 1e-10:
            return _bisect_iv(market_price, S, K, T, r, option_type, q, max_iterations, tolerance)

        diff = price - market_price
        if abs(diff) < tolerance:
            return sigma
        sigma = min(max(sigma - diff / vega, IV_MIN), IV_MAX)

    return _bisect_iv(market_price, S, K, T, r, option_type, q, max_iterations, tolerance)


def _bisect_iv(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    q: float,
    max_iterations: int,
    tolerance: float,
) -> float | None:
    """Bisection fallback.

    Deviation from upstream: upstream returns the midpoint of whatever bracket
    it ends on, even when the target price lies outside the bracket entirely —
    so an unpriceable quote comes back as a plausible-looking 2.5 IV. We check
    the bracket first and return ``None`` when the price is not attainable.
    """
    low, high = IV_MIN, IV_MAX
    if bs_price(S, K, T, r, high, option_type, q) < market_price - tolerance:
        return None  # even 500% vol cannot reach this price

    for _ in range(max_iterations):
        mid = (low + high) / 2
        price = bs_price(S, K, T, r, mid, option_type, q)
        if abs(price - market_price) < tolerance:
            return mid
        if price > market_price:
            high = mid
        else:
            low = mid
    return (low + high) / 2
