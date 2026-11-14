"""Paper-trading engine: resting maker bids on the cheap side, simulated fills, settlement.

Fill model (documented limitations in README):
  * We join the best bid. queue_ahead = visible size at that level when placed.
  * 'conservative': only real trade prints reduce the queue (cancels ahead are ignored).
  * 'optimistic'  : any print at <= our price fills us (queue ignored).
  * A print strictly below our bid means our (better-priced) bid would already have traded -> full fill.
  * Prints within latency_ms of placement are ignored.
  * Our order never moves the market.
"""
import math
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Dict, List, Optional


@dataclass
class StrategyConfig:
    enabled: bool = False
    unit_usd: float = 5.0
    min_price: float = 0.02
    max_price: float = 0.10
    min_remaining: float = 8.0         # do not place with less than this many seconds left
    max_remaining: float = 420.0       # ...or more than this (allows entry up to 120 s before open)
    margin: float = 0.02               # required (p_adj - price)
    discount: float = 0.85             # adverse-selection haircut on model probability
    ttl_s: float = 45.0
    cancel_below_remaining: float = 8.0
    max_orders_per_window: int = 1
    sides: str = "both"                # both | up | down
    sigma5m: float = 0.0015
    vol_mult: float = 1.0
    use_realized_vol: bool = True
    queue_model: str = "conservative"  # conservative | optimistic
    latency_ms: int = 300
    start_bankroll: float = 100.0
    max_exposure: float = 25.0
    taker_fee_rate: float = 0.07       # only used to estimate maker rebate
    rebate_share: float = 0.20
    rebate_realization: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        c = cls()
        for f in fields(cls):
            if f.name not in d:
                continue
            cur, new = getattr(c, f.name), d[f.name]
            try:
                if isinstance(cur, bool):
                    new = new if isinstance(new, bool) else str(new).lower() in ("1", "true", "yes", "on")
                else:
                    new = type(cur)(new)
            except (TypeError, ValueError):
                continue
            setattr(c, f.name, new)
        c.unit_usd = min(max(c.unit_usd, 1.0), 1000.0)
        c.min_price = min(max(c.min_price, 0.01), 0.98)
        c.max_price = min(max(c.max_price, c.min_price), 0.98)
        c.discount = min(max(c.discount, 0.0), 1.5)
        c.queue_model = c.queue_model if c.queue_model in ("conservative", "optimistic") else "conservative"
        c.sides = c.sides if c.sides in ("both", "up", "down") else "both"
        c.max_orders_per_window = max(1, min(c.max_orders_per_window, 5))
        return c

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Order:
    id: int
    slug: str
    side: str            # 'Up' | 'Down'
    token_id: str
    price: float
    shares: float
    placed_ts: float
    tau_at_place: float
    queue_ahead0: float
    model_p: float
    p_adj: float
    spot: float
    B: float
    sigma_rel: float
    binance: Optional[float] = None
    basis: Optional[float] = None
    queue_ahead: float = 0.0
    filled: float = 0.0
    cost: float = 0.0
    rebate: float = 0.0
    first_fill_ts: Optional[float] = None
    status: str = "open"           # open | partial | filled | cancelled | expired | aborted
    cancel_reason: str = ""
    settled: bool = False
    side_won: Optional[bool] = None
    official_side_won: Optional[bool] = None
    final_value: Optional[float] = None
    pnl: Optional[float] = None

    @property
    def remaining(self) -> float:
        return max(self.shares - self.filled, 0.0)

    @property
    def is_resting(self) -> bool:
        return self.status in ("open", "partial")


class PaperEngine:
    def __init__(self, cfg: Optional[StrategyConfig] = None,
                 on_change: Optional[Callable[[], None]] = None,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg or StrategyConfig()
        self.orders: List[Order] = []
        self.next_id = 1
        self.on_change = on_change or (lambda: None)
        self.on_event = on_event or (lambda e: None)

    # ---------- persistence ----------
    def load(self, rows: List[dict]) -> None:
        valid = {f.name for f in fields(Order)}
        self.orders = []
        for r in rows:
            try:
                o = Order(**{k: v for k, v in r.items() if k in valid})
            except TypeError:
                continue
            if not o.settled and o.status in ("open", "partial", "filled"):
                if o.filled > 0:
                    o.status = "aborted"  # restart while a position was open: outcome unknown
                else:
                    o.status = "aborted"
                o.cancel_reason = "restart"
            self.orders.append(o)
        self.next_id = max([o.id for o in self.orders], default=0) + 1

    def dump(self) -> List[dict]:
        return [asdict(o) for o in self.orders]

    def reset(self) -> None:
        self.orders = []
        self.next_id = 1
        self._changed()

    def _changed(self) -> None:
        self.on_change()

    def _event(self, kind: str, o: Order, **kw) -> None:
        self.on_event({"t": time.time(), "ev": kind, "id": o.id, "slug": o.slug, "side": o.side, **kw})

    # ---------- helpers ----------
    def rebate_per_share(self, price: float) -> float:
        c = self.cfg
        return c.rebate_share * c.rebate_realization * c.taker_fee_rate * price * (1.0 - price)

    def exposure(self) -> float:
        tot = 0.0
        for o in self.orders:
            if o.settled or (o.status in ("cancelled", "expired", "aborted") and o.filled == 0):
                continue
            if o.is_resting:
                tot += o.price * o.remaining + o.cost
            elif o.status == "filled" or o.filled > 0:
                tot += o.cost
        return tot

    def realized(self) -> float:
        return sum(o.pnl or 0.0 for o in self.orders if o.settled)

    def bankroll(self) -> float:
        return self.cfg.start_bankroll + self.realized()

    def set_enabled(self, on: bool) -> None:
        self.cfg.enabled = on
        if not on:
            self.cancel_all("stopped")

    # ---------- decision ----------
    def evaluate(self, ctx: dict, now: float) -> str:
        """Maybe place paper bids. ctx keys: slug, tau, B, spot, sigma_rel, p_up, tokens{Up,Down},
        books{Up,Down}, binance, basis. Returns a short status string for the UI."""
        c = self.cfg
        tau = ctx["tau"]
        if tau > c.max_remaining or tau < c.min_remaining:
            return f"不在入场时段（剩余 {tau:.0f}s，需 {c.min_remaining:.0f}-{c.max_remaining:.0f}s）"
        placed_here = [o for o in self.orders if o.slug == ctx["slug"]]
        if len(placed_here) >= c.max_orders_per_window:
            return "本窗口已达下单上限"
        sides = ["Up", "Down"] if c.sides == "both" else [c.sides.capitalize()]
        reasons = []
        for side in sides:
            book = ctx["books"].get(side)
            if book is None or not book.has_snapshot:
                reasons.append(f"{side}: 无盘口")
                continue
            bid, ask = book.best_bid(), book.best_ask()
            if bid is None:
                reasons.append(f"{side}: 无买盘")
                continue
            if not (c.min_price <= bid <= c.max_price):
                reasons.append(f"{side}: 买一 {bid:.2f} 不在 {c.min_price:.2f}-{c.max_price:.2f}")
                continue
            if ask is not None and ask <= bid:
                reasons.append(f"{side}: 盘口交叉")
                continue
            p = ctx["p_up"] if side == "Up" else 1.0 - ctx["p_up"]
            p_adj = p * c.discount
            if p_adj < bid + c.margin:
                reasons.append(f"{side}: 优势不足（模型 {p_adj:.3f} < 价格 {bid:.2f}+{c.margin:.2f}）")
                continue
            shares = math.floor(c.unit_usd / bid * 100) / 100.0
            if shares < 5:
                reasons.append(f"{side}: 股数 {shares} < 5")
                continue
            if self.exposure() + shares * bid > c.max_exposure + 1e-9:
                reasons.append("敞口已达上限")
                continue
            o = Order(id=self.next_id, slug=ctx["slug"], side=side, token_id=ctx["tokens"][side], price=bid,
                      shares=shares, placed_ts=now, tau_at_place=tau, queue_ahead0=book.size_at("BUY", bid),
                      model_p=p, p_adj=p_adj, spot=ctx["spot"], B=ctx["B"], sigma_rel=ctx["sigma_rel"],
                      binance=ctx.get("binance"), basis=ctx.get("basis"))
            o.queue_ahead = o.queue_ahead0
            self.next_id += 1
            self.orders.append(o)
            self._event("place", o, price=bid, shares=shares, tau=tau, model_p=p, p_adj=p_adj,
                        queue_ahead=o.queue_ahead0)
            self._changed()
            return f"已挂单 {side} @{bid:.2f} x{shares}"
        return "；".join(reasons) if reasons else "无信号"

    # ---------- management ----------
    def manage(self, ctx: dict, now: float) -> None:
        c = self.cfg
        for o in self.orders:
            if not o.is_resting or o.slug != ctx.get("slug"):
                continue
            book = ctx["books"].get(o.side)
            if book is not None and book.has_snapshot:
                ask = book.best_ask()
                if ask is not None and ask <= o.price + 1e-9 and now - o.placed_ts >= c.latency_ms / 1000.0:
                    self._fill(o, o.remaining, o.price, now)  # book crossed us
                    continue
            if now - o.placed_ts > c.ttl_s:
                self._cancel(o, "ttl")
            elif ctx["tau"] < c.cancel_below_remaining:
                self._cancel(o, "too_late")
            else:
                p = ctx["p_up"] if o.side == "Up" else 1.0 - ctx["p_up"]
                if p * c.discount < o.price + c.margin * 0.5:
                    self._cancel(o, "edge_lost")

    def cancel_all(self, reason: str) -> None:
        for o in self.orders:
            if o.is_resting:
                self._cancel(o, reason)

    def _cancel(self, o: Order, reason: str) -> None:
        """Stop resting. A partially filled order keeps its filled part (status 'filled')."""
        o.cancel_reason = reason
        o.status = "filled" if o.filled > 0 else "cancelled"
        self._event("cancel", o, reason=reason, filled=o.filled)
        self._changed()

    # ---------- fills ----------
    def on_trade(self, token_id: str, price: float, size: float, ts: float) -> None:
        c = self.cfg
        for o in self.orders:
            if not o.is_resting or o.token_id != token_id:
                continue
            if ts < o.placed_ts + c.latency_ms / 1000.0:
                continue
            if price > o.price + 1e-9:
                continue
            if c.queue_model == "optimistic":
                self._fill(o, min(size, o.remaining), o.price, ts)
            elif price < o.price - 1e-9:
                self._fill(o, o.remaining, o.price, ts)
            else:
                avail = size
                if o.queue_ahead > 0:
                    take = min(avail, o.queue_ahead)
                    o.queue_ahead -= take
                    avail -= take
                if avail > 0:
                    self._fill(o, min(avail, o.remaining), o.price, ts)

    def _fill(self, o: Order, qty: float, price: float, ts: float) -> None:
        qty = round(min(qty, o.remaining), 4)
        if qty <= 0:
            return
        o.filled = round(o.filled + qty, 4)
        o.cost += qty * price
        o.rebate += qty * self.rebate_per_share(price)
        if o.first_fill_ts is None:
            o.first_fill_ts = ts
        o.status = "filled" if o.remaining <= 1e-9 else "partial"
        self._event("fill", o, qty=qty, price=price, filled=o.filled)
        self._changed()

    # ---------- settlement ----------
    def settle(self, slug: str, up_won: bool, final_value: Optional[float]) -> int:
        n = 0
        for o in self.orders:
            if o.slug != slug or o.settled or o.status == "aborted":
                continue
            if o.is_resting:
                o.cancel_reason = o.cancel_reason or "window_end"
                o.status = "filled" if o.filled > 0 else "expired"
            self._finalize(o, up_won, final_value)
            n += 1
        if n:
            self._changed()
        return n

    def _finalize(self, o: Order, up_won: bool, final_value: Optional[float]) -> None:
        won = (o.side == "Up") == up_won
        o.side_won = won
        o.final_value = final_value
        o.settled = True
        payout = o.filled if won else 0.0
        o.pnl = round(payout - o.cost + o.rebate, 4) if o.filled > 0 else 0.0
        self._event("settle", o, side_won=won, pnl=o.pnl, filled=o.filled)

    def reconcile(self, slug: str, official_up: bool) -> int:
        """Apply the official (Gamma) outcome; recompute PnL if it differs from our provisional one."""
        n = 0
        for o in self.orders:
            if o.slug != slug or not o.settled:
                continue
            official_won = (o.side == "Up") == official_up
            o.official_side_won = official_won
            if o.side_won != official_won:
                self._event("mismatch", o, provisional=o.side_won, official=official_won)
                self._finalize(o, official_up, o.final_value)
                o.official_side_won = official_won
            n += 1
        if n:
            self._changed()
        return n

    # ---------- stats ----------
    def stats(self) -> dict:
        settled = [o for o in self.orders if o.settled]
        filled = [o for o in settled if o.filled > 0]
        unfilled = [o for o in settled if o.filled == 0]
        wins = [o for o in filled if o.side_won]
        avg_price = sum(o.cost for o in filled) / sum(o.filled for o in filled) if filled else None
        avg_rebate = sum(o.rebate for o in filled) / sum(o.filled for o in filled) if filled else 0.0
        return {
            "signals": len(self.orders),
            "settled": len(settled),
            "filled": len(filled),
            "fill_rate": (len(filled) / len(settled)) if settled else None,
            "wins": len(wins),
            "win_rate": (len(wins) / len(filled)) if filled else None,
            "avg_price": avg_price,
            "breakeven": (avg_price - avg_rebate) if avg_price is not None else None,
            "unfilled_flip_rate": (sum(1 for o in unfilled if o.side_won) / len(unfilled)) if unfilled else None,
            "pnl": round(self.realized(), 4),
            "bankroll": round(self.bankroll(), 4),
            "exposure": round(self.exposure(), 4),
            "resting": sum(1 for o in self.orders if o.is_resting),
        }
