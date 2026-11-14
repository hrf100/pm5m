"""Shared in-memory market-data state (fed by the network layer, read by runner/UI)."""
import bisect
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from .book import Book
from .model import VolEstimator


class Buf:
    """Time series of (ts_seconds, price), bounded."""

    def __init__(self, maxlen: int = 1500, min_gap: float = 0.0, one_per_sec: bool = False):
        self.d = deque(maxlen=maxlen)
        self.min_gap = min_gap
        self.one_per_sec = one_per_sec

    def append(self, ts: float, px: float) -> bool:
        if self.d:
            last_ts = self.d[-1][0]
            if ts < last_ts - 1e-6:
                return False  # out of order (e.g. snapshot replay): ignore
            if self.one_per_sec and int(ts) == int(last_ts):
                self.d[-1] = (ts, px)
                return True
            if ts - last_ts < self.min_gap:
                self.d[-1] = (ts, px)
                return True
        self.d.append((ts, px))
        return True

    def last(self) -> Optional[Tuple[float, float]]:
        return self.d[-1] if self.d else None

    def since(self, ts: float) -> List[Tuple[float, float]]:
        return [x for x in self.d if x[0] >= ts]

    def nearest(self, ts: float, tol: float) -> Optional[Tuple[float, float]]:
        if not self.d:
            return None
        keys = [x[0] for x in self.d]
        i = bisect.bisect_left(keys, ts)
        cands = []
        if i < len(keys):
            cands.append(self.d[i])
        if i > 0:
            cands.append(self.d[i - 1])
        best = min(cands, key=lambda x: abs(x[0] - ts))
        return best if abs(best[0] - ts) <= tol else None


class FeedStatus:
    def __init__(self, name: str):
        self.name = name
        self.state = "init"
        self.error = ""
        self.last_msg = 0.0
        self.count = 0
        self.bytes = 0
        self.extra = ""

    def ok(self, extra: str = "") -> None:
        self.state = "connected"
        self.error = ""
        if extra:
            self.extra = extra

    def msg(self, size: int = 0) -> None:
        self.last_msg = time.time()
        self.count += 1
        self.bytes += size

    def fail(self, err: str) -> None:
        self.state = "error"
        self.error = str(err)[:300]

    def to_dict(self, now: float) -> dict:
        age = None if not self.last_msg else round(now - self.last_msg, 1)
        return {"name": self.name, "state": self.state, "error": self.error, "age_s": age,
                "count": self.count, "bytes": self.bytes, "extra": self.extra}


class Hub:
    FEEDS = ("polybolt", "market_ws", "binance")

    def __init__(self):
        self.spot = Buf(1500, min_gap=0.2)
        self.twap = Buf(1500, min_gap=0.2)
        self.binance = Buf(1500, one_per_sec=True)
        self.spot_source: Optional[str] = None
        self.vol = VolEstimator()
        self.books: Dict[str, Book] = {}
        self.status: Dict[str, FeedStatus] = {n: FeedStatus(n) for n in self.FEEDS}
        self.trade_listeners: List[Callable] = []
        self.book_listeners: List[Callable] = []

    # --- spot / twap / binance ---
    def on_spot(self, ts: float, px: float, source: Optional[str] = None) -> None:
        if self.spot.append(ts, px):
            self.vol.update(ts, px)
        if source:
            self.spot_source = source

    def on_twap(self, ts: float, px: float) -> None:
        self.twap.append(ts, px)

    def on_binance(self, ts: float, px: float) -> None:
        self.binance.append(ts, px)

    # --- books ---
    def book(self, token: str) -> Book:
        b = self.books.get(token)
        if b is None:
            b = self.books[token] = Book()
        return b

    def on_market_events(self, events: list) -> None:
        for ev in events:
            kind = ev[0]
            if kind == "book":
                _, aid, bids, asks, ts = ev
                self.book(aid).snapshot(bids, asks, ts)
                for cb in self.book_listeners:
                    cb(aid)
            elif kind == "change":
                _, aid, side, price, size, ts = ev
                try:
                    self.book(aid).update(side, price, size, ts)
                except (TypeError, ValueError):
                    continue
                for cb in self.book_listeners:
                    cb(aid)
            elif kind == "trade":
                _, aid, price, size, side, ts = ev
                for cb in self.trade_listeners:
                    cb(aid, price, size, ts or time.time())

    def drop_old_books(self, keep_tokens: List[str]) -> None:
        for k in [k for k in self.books if k not in keep_tokens]:
            del self.books[k]
