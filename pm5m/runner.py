"""Runner: window management, B capture (settlement), series sampling, strategy loop, settlement.

Strategy now evaluates BOTH the current window and the next one (pre-open), using the general
model in model.p_up_general, which does not require B to be known yet.
IO-free: the network layer feeds the Hub and calls runner.tick(now) a few times per second.
"""
import math
import time
from typing import Dict, List, Optional

from . import model
from .hub import Hub
from .paper import PaperEngine
from .store import DataStore

WINDOW = model.WINDOW_S
PRE_S = 180          # record and trade up to 180 s before a window opens


class Runner:
    def __init__(self, hub: Hub, engine: PaperEngine, store: Optional[DataStore] = None,
                 record_series: bool = True):
        self.hub = hub
        self.engine = engine
        self.store = store
        self.record_series = record_series
        self.cur_S: Optional[int] = None
        self.markets: Dict[int, dict] = {}
        self.B: Dict[int, dict] = {}            # realised TWAP start level (settlement), captured at S+1
        self.series: Dict[int, dict] = {}
        self.pending_settle: List[int] = []
        self.need_official: Dict[int, float] = {}
        self.final: Dict[int, dict] = {}
        self.last_sample_sec: Optional[int] = None
        self.model_snapshot: Optional[dict] = None
        self.strategy_msg = "未启动"
        self.hub.trade_listeners.append(self._on_trade)

    # ---------- helpers ----------
    @staticmethod
    def slug_for(S: int) -> str:
        return f"btc-updown-5m-{S}"

    def set_market(self, S: int, info: dict) -> None:
        self.markets[S] = info

    def set_official(self, S: int, up_won: bool) -> None:
        self.engine.reconcile(self.slug_for(S), up_won)
        self.need_official.pop(S, None)
        if S in self.final:
            self.final[S]["official_up"] = up_won

    def _on_trade(self, token: str, price: float, size: float, ts: float) -> None:
        self.engine.on_trade(token, price, size, ts)

    def current_tokens(self) -> List[str]:
        out = []
        for S in (self.cur_S, (self.cur_S or 0) + WINDOW):
            m = self.markets.get(S)
            if m:
                out += [m["up"], m["down"]]
        return out

    # ---------- tick ----------
    def tick(self, now: float) -> None:
        S = int(now // WINDOW) * WINDOW
        if self.cur_S != S:
            self._rollover(S, now)
        self._capture_B(S, now)
        if self.last_sample_sec != int(now):
            self.last_sample_sec = int(now)
            self._sample(now)
        self._process_settlements(now)
        self._strategy(now)

    def _rollover(self, S: int, now: float) -> None:
        old = self.cur_S
        self.cur_S = S
        self.series.setdefault(S, self._new_series(S))
        if old is not None:
            self.pending_settle.append(old)
        for k in [k for k in self.series if k < S - 3 * WINDOW]:
            del self.series[k]
        for k in [k for k in self.markets if k < S - 3 * WINDOW]:
            del self.markets[k]
        self.hub.drop_old_books(self.current_tokens())

    @staticmethod
    def _new_series(S: int) -> dict:
        return {"slug": Runner.slug_for(S), "S": S, "T": S + WINDOW, "B": None, "B_source": None,
                "t": [], "spot": [], "twap": [], "binance": [], "up_bid": [], "up_ask": [],
                "dn_bid": [], "dn_ask": [], "p_up": []}

    def _capture_B(self, S: int, now: float) -> None:
        if S in self.B:
            return
        pt = self.hub.twap.nearest(S, tol=2.5)
        if pt is not None and now >= S + 1.0:
            self._set_B(S, pt[1], "chainlink_twap60", pt[0])
            return
        if now >= S + 6.0:
            v = model.local_twap(self.hub.spot.since(S - 70), S)
            if v is not None:
                self._set_B(S, v, "local_spot_mean", S)

    def _set_B(self, S: int, value: float, source: str, ts: float) -> None:
        self.B[S] = {"value": value, "source": source, "ts": ts}
        ser = self.series.get(S)
        if ser:
            ser["B"], ser["B_source"] = value, source

    def _books(self, S: int):
        m = self.markets.get(S)
        if not m:
            return None, None
        return self.hub.books.get(m["up"]), self.hub.books.get(m["down"])

    def _sample(self, now: float) -> None:
        spot, tw, bn = self.hub.spot.last(), self.hub.twap.last(), self.hub.binance.last()
        for Sw in (self.cur_S, self.cur_S + WINDOW):
            if now < Sw - PRE_S or now >= Sw + WINDOW:
                continue
            ser = self.series.setdefault(Sw, self._new_series(Sw))
            ub, db = self._books(Sw)

            def r(x, n=4):
                return None if x is None else round(x, n)
            snap = self.model_snapshot
            ser["t"].append(round(now - Sw, 1))
            ser["spot"].append(r(spot[1], 2) if spot else None)
            ser["twap"].append(r(tw[1], 2) if tw else None)
            ser["binance"].append(r(bn[1], 2) if bn else None)
            ser["up_bid"].append(r(ub.best_bid()) if ub else None)
            ser["up_ask"].append(r(ub.best_ask()) if ub else None)
            ser["dn_bid"].append(r(db.best_bid()) if db else None)
            ser["dn_ask"].append(r(db.best_ask()) if db else None)
            ser["p_up"].append(r(snap["p_up"], 4) if snap and snap.get("S") == Sw else None)

    # ---------- settlement ----------
    def _process_settlements(self, now: float) -> None:
        for S in list(self.pending_settle):
            T = S + WINDOW
            B = self.B.get(S)
            if B is None:
                self.pending_settle.remove(S)
                self._save_series(S)
                continue
            pt = self.hub.twap.nearest(T, tol=2.5)
            final, src = None, None
            if pt is not None and now >= T + 1.0:
                final, src = pt[1], "chainlink_twap60"
            elif now >= T + 15.0:
                v = model.local_twap(self.hub.spot.since(T - 70), T)
                if v is not None:
                    final, src = v, "local_spot_mean"
            if final is None:
                if now >= T + 60.0:
                    self.pending_settle.remove(S)
                    self.need_official[S] = now
                    self._save_series(S)
                continue
            up_won = final >= B["value"]
            n = self.engine.settle(self.slug_for(S), up_won, final)
            self.final[S] = {"value": final, "source": src, "B": B["value"], "up": up_won, "orders": n}
            self.pending_settle.remove(S)
            self.need_official[S] = now
            self._save_series(S)

    def _save_series(self, S: int) -> None:
        ser = self.series.get(S)
        if not (self.store and self.record_series and ser and len(ser["t"]) >= 30):
            return
        doc = dict(ser)
        doc["final"] = self.final.get(S)
        self.store.save_series(ser["slug"], doc)

    # ---------- strategy ----------
    def _strategy(self, now: float) -> None:
        eng = self.engine
        msgs, snap = [], None
        for Sw, label in ((self.cur_S, "本窗"), (self.cur_S + WINDOW, "下窗")):
            ctx = self.build_ctx(Sw, now)
            if Sw == self.cur_S:
                snap = ctx.get("model") or snap
            if ctx.get("error"):
                msgs.append(f"{label}: {ctx['error']}")
                continue
            if not eng.cfg.enabled:
                continue
            eng.manage(ctx, now)
            msgs.append(f"{label}: {eng.evaluate(ctx, now)}")
        if snap:
            self.model_snapshot = snap
        self.strategy_msg = (" | ".join(msgs) if eng.cfg.enabled else "已停止（仅观察）")

    def level_offset(self, now: float) -> Optional[float]:
        """Chainlink level minus spot level, from the latest TWAP-60 point vs the spot average over the same 60 s.
        Lets the spot-based integrals use the same price level as the settlement TWAP."""
        tw = self.hub.twap.last()
        if tw is None or now - tw[0] > 6.0:
            return None
        pts = [p for p in self.hub.spot.since(tw[0] - 60) if p[0] <= tw[0]]
        if len(pts) < 20:
            return None
        avg = model.integral_step(pts, tw[0] - 60, tw[0])
        return None if avg is None else tw[1] - avg / 60.0

    def build_ctx(self, S: int, now: float) -> dict:
        hub, cfg = self.hub, self.engine.cfg
        T = S + WINDOW
        spot = hub.spot.last()
        if spot is None or now - spot[0] > 6.0:
            return {"error": "现货数据过期/缺失"}
        off = self.level_offset(now)
        if off is None:
            return {"error": "缺少 Chainlink TWAP60 数据（结算依据），不交易"}
        sigma_default = cfg.sigma5m / math.sqrt(WINDOW)
        sigma = hub.vol.sigma(sigma_default) if cfg.use_realized_vol else sigma_default
        hist = [(t, p + off) for t, p in hub.spot.since(S - 70)]
        res = model.p_up_general(now, S, spot[1] + off, hist, sigma, cfg.vol_mult)
        if res is None:
            return {"error": "历史现货不足（需要窗口开始前 60 秒的数据）"}
        res["S"] = S
        res["sigma_rel"] = sigma
        res["sigma5m_eff"] = sigma * math.sqrt(WINDOW)
        m = self.markets.get(S)
        if m is None:
            return {"error": "尚未获取该窗口市场信息", "model": res}
        ub, db = self._books(S)
        if ub is None or db is None or not ub.has_snapshot or not db.has_snapshot:
            return {"error": "等待盘口快照", "model": res}
        bn = hub.binance.last()
        basis = (bn[1] - spot[1]) if bn and now - bn[0] < 10 else None
        return {"slug": self.slug_for(S), "tau": T - now, "B": res["B_mean"], "spot": spot[1] + off,
                "sigma_rel": sigma, "p_up": res["p_up"], "tokens": {"Up": m["up"], "Down": m["down"]},
                "books": {"Up": ub, "Down": db}, "binance": bn[1] if bn else None, "basis": basis, "model": res}
