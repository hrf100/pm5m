"""Synthetic windows for testing the pipeline ONLY. Results on synthetic data say nothing about the market."""
import math
import random
from typing import List

from pm5m import model

WINDOW = 300


def make_windows(n: int, seed: int = 1, mom_coef: float = 0.0, lead: int = 2, sigma5m: float = 0.0015,
                 spread: float = 0.02, noise: float = 0.02, S0: int = 1_800_000_000, pre: int = 180) -> List[dict]:
    """Series cover t in [-pre, 300): pre-open samples included, as the live recorder does."""
    rnd = random.Random(seed)
    S0 -= S0 % WINDOW
    sig = sigma5m / math.sqrt(WINDOW)
    head = pre + 120                      # history before the first sampled second
    start = S0 - head
    total = head + n * WINDOW + lead + 1
    logp = [math.log(100000.0)]
    for _ in range(1, total):
        drift = mom_coef * (logp[-1] - logp[-31]) / 30.0 if len(logp) > 31 else 0.0
        logp.append(logp[-1] + drift + sig * rnd.gauss(0, 1))
    P = [math.exp(x) for x in logp]
    out = []
    for j in range(n):
        S = S0 + j * WINDOW
        o = S - start
        B = sum(P[o - 60:o]) / 60.0
        ts = list(range(-pre, WINDOW))
        spot_full = [(tt, P[o + tt]) for tt in range(-70, WINDOW)]     # sorted, for slicing
        spot = [P[o + t] for t in ts]
        binance = [P[o + t + lead] for t in ts]
        up_bid, up_ask, dn_bid, dn_ask, p_mkt = [], [], [], [], []
        for t in ts:
            k = t + 70 + 1
            samples = spot_full[: max(k, 0)]
            res = model.p_up_general(t, 0.0, P[o + t], samples, sig)
            pm = res["p_up"]
            p_mkt.append(pm)
            ua = min(max(pm + spread / 2 + rnd.gauss(0, noise), 0.01), 0.99)
            da = min(max(1 - pm + spread / 2 + rnd.gauss(0, noise), 0.01), 0.99)
            up_ask.append(round(ua, 4)); up_bid.append(round(ua - spread, 4))
            dn_ask.append(round(da, 4)); dn_bid.append(round(da - spread, 4))
        final_val = sum(P[o + 240:o + 300]) / 60.0
        out.append({"slug": f"btc-updown-5m-{S}", "S": S, "T": S + WINDOW, "B": B, "B_source": "synthetic",
                    "t": [float(t) for t in ts], "spot": spot, "twap": [None] * len(ts),
                    "binance": binance, "up_bid": up_bid, "up_ask": up_ask, "dn_bid": dn_bid, "dn_ask": dn_ask,
                    "p_up": [None] * len(ts), "final": {"up": final_val >= B, "value": final_val, "B": B}})
    return out
