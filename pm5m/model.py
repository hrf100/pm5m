"""Flip-probability model for Polymarket 5-minute crypto up/down markets.

Rule (international site, since 2026-08-14): the market compares two Chainlink
BTC/USD 60-second TWAPs:
    price to beat  B = TWAP over [S-60, S)   (S = window start)
    final value    F = TWAP over [T-60, T)   (T = S + 300)
Up wins if F >= B, otherwise Down wins.

Model: Brownian motion, zero drift, per-sqrt-second vol sigma (relative).
  * tau >= 60 s left:  F ~ N(spot, s^2 * (tau - 60 + 60/3))
  * tau  < 60 s left:  part of F is already known from the 1 Hz price history,
        F ~ N((known_integral + tau*spot)/60, s^2 * tau^3 / (3*60^2))
Pure stdlib; no network.
"""
import math
from typing import List, Optional, Tuple

WINDOW_S = 300
TWAP_S = 60


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def integral_step(samples: List[Tuple[float, float]], a: float, b: float) -> Optional[float]:
    """Integral over [a, b] of a sample-and-hold price path.

    samples: ascending [(ts, price)]. The price in force at time a is the last
    sample with ts <= a (back-filled with the first sample if none).
    Returns None when there are no samples, 0.0 when b <= a.
    """
    if not samples:
        return None
    if b <= a:
        return 0.0
    px = None
    t_prev = a
    total = 0.0
    for ts, p in samples:
        if ts <= a:
            px = p
            continue
        if ts >= b:
            break
        if px is None:
            px = p
        total += px * (ts - t_prev)
        t_prev = ts
        px = p
    if px is None:
        px = samples[0][1]
    total += px * (b - t_prev)
    return total


def local_twap(samples: List[Tuple[float, float]], end_ts: float, length: float = TWAP_S,
               min_coverage: float = 0.5) -> Optional[float]:
    """TWAP over [end_ts-length, end_ts) computed from 1 Hz samples (approximation)."""
    if not samples:
        return None
    a = end_ts - length
    inside = [s for s in samples if a <= s[0] < end_ts]
    if len(inside) < min_coverage * length * 0.8 and len(inside) < 20:
        return None
    integ = integral_step(samples, a, end_ts)
    return None if integ is None else integ / length


def up_probability(B: float, now: float, T: float, spot: float,
                   samples: List[Tuple[float, float]], sigma_rel: float,
                   vol_mult: float = 1.0) -> Optional[dict]:
    """P(final 60s TWAP >= B). Returns dict or None if data is insufficient."""
    if B is None or spot is None or spot <= 0:
        return None
    tau = max(T - now, 0.0)
    s = sigma_rel * vol_mult * spot  # absolute vol per sqrt(second)
    if tau >= TWAP_S:
        mu = spot
        var = s * s * (tau - TWAP_S + TWAP_S / 3.0)
    else:
        known = integral_step(samples, T - TWAP_S, now)
        if known is None:
            return None
        mu = (known + tau * spot) / TWAP_S
        var = s * s * tau ** 3 / (3.0 * TWAP_S ** 2)
    sd = math.sqrt(max(var, 0.0))
    if sd < 1e-9:
        p_up = 1.0 if mu >= B else 0.0
    else:
        p_up = 1.0 - norm_cdf((B - mu) / sd)
    return {"p_up": p_up, "p_down": 1.0 - p_up, "mu": mu, "sd": sd, "gap": mu - B, "tau": tau}


class VolEstimator:
    """EWMA estimate of relative vol per sqrt(second) from irregular ~1 Hz prices."""

    def __init__(self, halflife_s: float = 90.0):
        self.halflife = halflife_s
        self.var_rate = None  # relative variance per second
        self.prev = None
        self.n = 0

    def update(self, ts: float, px: float) -> None:
        if self.prev is not None:
            pts, ppx = self.prev
            dt = ts - pts
            if dt >= 0.2 and ppx > 0 and px > 0:
                r = math.log(px / ppx)
                sample = r * r / dt
                lam = 0.5 ** (dt / self.halflife)
                self.var_rate = sample if self.var_rate is None else lam * self.var_rate + (1 - lam) * sample
                self.n += 1
                self.prev = (ts, px)
        else:
            self.prev = (ts, px)

    def sigma(self, default: float, lo_mult: float = 0.5, hi_mult: float = 4.0, min_n: int = 30) -> float:
        if self.var_rate is None or self.n < min_n:
            return default
        est = math.sqrt(self.var_rate)
        return min(max(est, default * lo_mult), default * hi_mult)


# ---------------------------------------------------------------------------
# General model: valid at any time, including BEFORE the window opens (B not yet known).
#
# Settlement: Up iff F >= B, F = mean P over [T-60, T), B = mean P over [S-60, S), T = S + 300.
# The two 60s windows are disjoint, so D = F - B has variance Var(F) + Var(B).
# Given the price now (P0) and known history, the future is P0 + Y(u), Y a Brownian motion
# with variance s^2 * u (zero drift). For a future interval [al, be] (0 <= al <= be):
#   Var( integral_al^be Y ) = s^2 * (K(be,be) - 2 K(al,be) + K(al,al)),   K(x,y) = x^2 y/2 - x^3/6 (x<=y)
# ---------------------------------------------------------------------------

def _K(x: float, y: float) -> float:
    if x > y:
        x, y = y, x
    return x * x * y / 2.0 - x ** 3 / 6.0


def window_moments(a: float, b: float, now: float, spot: float, samples, s: float):
    """Mean and variance of (1/60)*integral_a^b P(t) dt given information up to `now`.
    samples: ascending [(t, px)] with absolute (window-relative) seconds. s: absolute vol per sqrt(s)."""
    mean, var = 0.0, 0.0
    if now > a:
        if not samples or samples[0][0] > a + 2.0:
            return None
        known = integral_step([p for p in samples if p[0] <= now], a, min(b, now))
        if known is None:
            return None
        mean += known / 60.0
    if b > now:
        al = max(a, now) - now
        be = b - now
        mean += spot * (be - al) / 60.0
        var = s * s / 3600.0 * (_K(be, be) - 2.0 * _K(al, be) + _K(al, al))
    return mean, var


def p_up_general(now: float, S: float, spot: float, samples, sigma_rel: float, vol_mult: float = 1.0):
    """P(Up) for the window starting at S, evaluated at time `now` (may be before S)."""
    if spot is None or spot <= 0:
        return None
    T = S + WINDOW_S
    s = sigma_rel * vol_mult * spot
    fb = window_moments(S - TWAP_S, S, now, spot, samples, s)
    ff = window_moments(T - TWAP_S, T, now, spot, samples, s)
    if fb is None or ff is None:
        return None
    mB, vB = fb
    mF, vF = ff
    mu = mF - mB
    sd = math.sqrt(max(vF + vB, 0.0))
    p = (1.0 if mu >= 0 else 0.0) if sd < 1e-9 else norm_cdf(mu / sd)
    return {"p_up": p, "p_down": 1.0 - p, "mu": mu, "sd": sd, "gap": mu, "tau": T - now,
            "B_mean": mB, "B_known": now >= S}
