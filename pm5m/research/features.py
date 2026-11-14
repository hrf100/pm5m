"""Feature construction. Every feature at index i uses only information with timestamp <= t_i."""
import glob
import json
import math
import os
from typing import List, Optional

from pm5m import model

WINDOW = 300


def load_windows(series_dir: str) -> List[dict]:
    out = []
    for p in glob.glob(os.path.join(series_dir, "*.json")):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("B") is None or not d.get("t") or not d.get("final") or d["final"].get("up") is None:
            continue
        out.append(d)
    out.sort(key=lambda d: d["S"])
    return out


def valid_points(w: dict):
    return [(t, s) for t, s in zip(w["t"], w["spot"]) if s is not None]


def sigma_global(windows: List[dict]) -> Optional[float]:
    """Per-sqrt(second) relative vol from consecutive 1 Hz spot returns. Fit on TRAIN only."""
    acc, n = 0.0, 0
    for w in windows:
        pts = valid_points(w)
        for (t0, s0), (t1, s1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if 0 < dt <= 2.0 and s0 > 0 and s1 > 0:
                r = math.log(s1 / s0)
                acc += r * r / dt
                n += 1
    return math.sqrt(acc / n) if n else None


def row_features(w: dict, i: int, sigma: float, rules, pts=None) -> Optional[dict]:
    """Relative time: window opens at 0, settlement at 300. t may be negative (pre-open).
    Uses only observations with time <= t_i."""
    pts = pts if pts is not None else valid_points(w)
    t, s = w["t"][i], w["spot"][i]
    if s is None or t is None or sigma is None:
        return None
    tau = WINDOW - t
    if not (rules.tau_min <= tau <= rules.tau_max):
        return None
    ua, da, ub, db = w["up_ask"][i], w["dn_ask"][i], w["up_bid"][i], w["dn_bid"][i]
    if None in (ua, da, ub, db):
        return None
    samples = [(tt, ss) for tt, ss in pts if -70 <= tt <= t]
    base = model.p_up_general(t, 0.0, s, samples, sigma)
    if base is None:
        return None
    L = rules.mom_lookback
    cand = [(abs(tt - (t - L)), ss) for tt, ss in pts if tt <= t and abs(tt - (t - L)) <= 3]
    if not cand:
        return None
    mom = math.log(s / min(cand)[1]) / (sigma * math.sqrt(L))
    bn = w["binance"][i]
    if bn is None:
        return None
    basis = math.log(bn / s) / (sigma * math.sqrt(rules.basis_lookback))
    return {"S": w["S"], "t": t, "tau": tau, "p_base": base["p_up"], "mom": mom, "basis": basis,
            "ua": ua, "da": da, "ub": ub, "db": db, "y": 1 if w["final"]["up"] else 0}


def build_rows(windows: List[dict], sigma: float, rules) -> List[dict]:
    rows = []
    for w in windows:
        pts = valid_points(w)
        for i in range(0, len(w["t"]), rules.sample_step):   # grid by index (1 Hz series)
            r = row_features(w, i, sigma, rules, pts)
            if r:
                rows.append(r)
    return rows
