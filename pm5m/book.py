"""Local order book + normalisation of Polymarket CLOB market-channel events."""
from typing import Dict, List, Optional


def _p(x) -> float:
    return round(float(x), 4)


class Book:
    def __init__(self):
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.has_snapshot = False
        self.ts = 0.0

    def snapshot(self, bids, asks, ts: float = 0.0) -> None:
        self.bids = {_p(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {_p(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.has_snapshot = True
        self.ts = ts

    def update(self, side: str, price, size, ts: float = 0.0) -> None:
        """side 'BUY' = bid side, 'SELL' = ask side. size is the NEW total size at the level."""
        d = self.bids if str(side).upper() == "BUY" else self.asks
        pr, sz = _p(price), float(size)
        if sz <= 0:
            d.pop(pr, None)
        else:
            d[pr] = sz
        self.ts = ts or self.ts

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def size_at(self, side: str, price: float) -> float:
        d = self.bids if str(side).upper() == "BUY" else self.asks
        return d.get(_p(price), 0.0)


def _ts(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e11 else v


def parse_market_events(msg) -> List[tuple]:
    """Normalise a CLOB market-channel message (dict or list of dicts).

    Returns tuples:
      ("book", asset_id, bids, asks, ts)
      ("change", asset_id, side, price, size, ts)
      ("trade", asset_id, price, size, side, ts)
      ("bba", asset_id, best_bid, best_ask, ts)
      ("resolved", info_dict)
    """
    out: List[tuple] = []
    if isinstance(msg, list):
        for m in msg:
            out.extend(parse_market_events(m))
        return out
    if not isinstance(msg, dict):
        return out
    et = msg.get("event_type")
    ts = _ts(msg.get("timestamp"))
    if et == "book":
        bids = msg.get("bids", msg.get("buys", [])) or []
        asks = msg.get("asks", msg.get("sells", [])) or []
        out.append(("book", str(msg.get("asset_id")), bids, asks, ts))
    elif et == "price_change":
        if "price_changes" in msg:
            for c in msg["price_changes"] or []:
                out.append(("change", str(c.get("asset_id")), c.get("side"), c.get("price"), c.get("size"), ts))
                if c.get("best_bid") is not None or c.get("best_ask") is not None:
                    out.append(("bba", str(c.get("asset_id")), c.get("best_bid"), c.get("best_ask"), ts))
        else:
            aid = str(msg.get("asset_id"))
            for c in msg.get("changes", []) or []:
                out.append(("change", aid, c.get("side"), c.get("price"), c.get("size"), ts))
    elif et == "last_trade_price":
        try:
            out.append(("trade", str(msg.get("asset_id")), float(msg["price"]), float(msg.get("size", 0) or 0),
                        msg.get("side"), ts))
        except (KeyError, TypeError, ValueError):
            pass
    elif et == "best_bid_ask":
        out.append(("bba", str(msg.get("asset_id")), msg.get("best_bid"), msg.get("best_ask"), ts))
    elif et == "market_resolved":
        out.append(("resolved", msg))
    return out
