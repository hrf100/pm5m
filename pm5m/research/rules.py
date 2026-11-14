"""Pre-registered research rules. Freeze BEFORE looking at the test split; a digest guards against edits."""
import hashlib
import json
import time
from dataclasses import asdict, dataclass


@dataclass
class Rules:
    version: str = "v2"
    # Decision window: seconds until settlement (tau = T - now). tau > 300 means before the window opens.
    # v2 (2026-11): full-window entry plus up to 120 s before open. v1 (band 20-150 s) was never frozen
    # to disk or run on real data, so it is replaced rather than kept as a comparison.
    tau_min: float = 8.0
    tau_max: float = 420.0
    sample_step: int = 5           # evaluate every N seconds (rows within a window are correlated)
    price_min: float = 0.02        # taker ask band
    price_max: float = 0.15
    min_edge: float = 0.02         # required (model prob - ask - fee)
    taker_fee_rate: float = 0.07   # documented crypto taker parameter; fee/share = rate*p*(1-p)
    mom_lookback: int = 30         # seconds, momentum feature
    basis_lookback: int = 5        # seconds, normaliser for Binance-Chainlink basis
    l2: float = 1e-3
    train_frac: float = 0.7        # first 70% of windows (by time) = train, rest = test
    min_trades: int = 200          # minimum test trades before any verdict
    bootstrap: int = 2000
    seed: int = 7

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def freeze(rules: Rules, path: str) -> None:
    d = asdict(rules)
    d["digest"] = rules.digest()
    d["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def load(path: str) -> Rules:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    digest = d.pop("digest", None)
    d.pop("frozen_at", None)
    r = Rules(**d)
    if digest != r.digest():
        raise ValueError("规则文件已被修改（摘要不匹配）。请新建版本文件，不要修改已冻结的规则。")
    return r
