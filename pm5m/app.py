"""Web service + wiring. Run:  python -m pm5m --host 127.0.0.1 --port 8080"""
import argparse
import asyncio
import base64
import hmac
import json
import logging
import math
import os
import secrets
import statistics
import time
from typing import Dict, Optional

import aiohttp
from aiohttp import web

from . import parsers
from .feeds import Api, Feeds, ApiError
from .hub import Hub
from .paper import PaperEngine, StrategyConfig
from .runner import Runner, WINDOW
from .sysmon import SysMon
from .store import DataStore, orders_to_csv

log = logging.getLogger("pm5m")
STATIC = os.path.join(os.path.dirname(__file__), "static")
CSRF_HEADER = ("X-Requested-With", "pm5m")


def explicit_account() -> Optional[dict]:
    """Use funder and signature type only when both were set. Otherwise the caller looks them up."""
    funder = (os.environ.get("PM5M_FUNDER") or "").strip()
    sig = (os.environ.get("PM5M_SIG_TYPE") or "").strip()
    if not funder or not sig:
        return None
    return {"funder": funder, "sig_type": int(sig)}


def mask(s: Optional[str], keep: int = 4) -> str:
    if not s:
        return ""
    return s[:keep] + "…" + s[-keep:] if len(s) > 2 * keep + 2 else "…"


class App:
    def __init__(self, data_dir: str):
        self.store = DataStore(data_dir, os.environ.get("PM5M_SECRET_PASSPHRASE") or None)
        self.settings = self.store.load_settings()
        self.hub = Hub()
        cfg = StrategyConfig.from_dict(self.settings.get("strategy", {}))
        cfg.enabled = False                      # never auto-start trading logic after a restart
        self.dirty = False
        self.engine = PaperEngine(cfg, on_change=self._mark_dirty, on_event=self._event)
        self.engine.load(self.store.load_orders())
        self.runner = Runner(self.hub, self.engine, self.store, record_series=self.settings.get("record_series", True))
        self.secrets: dict = {}
        try:
            self.secrets = self.store.secrets.load()
        except Exception as e:
            log.error("cannot read secrets: %s", e)
        self.wallet_info: dict = {"updated": None, "errors": {}}
        self.geo: dict = {}
        self.last_error = ""
        self.session: Optional[aiohttp.ClientSession] = None
        self.api: Optional[Api] = None
        self.feeds: Optional[Feeds] = None
        self.tasks = []
        self.failures: Dict[str, list] = {}
        self.password = self._load_password()
        # PM5M_TRUST_PROXY=1 only when a reverse proxy sets X-Forwarded-*.
        self.trust_proxy = os.environ.get("PM5M_TRUST_PROXY") == "1"
        self.allow_web_key = os.environ.get("PM5M_ALLOW_WEB_KEY") == "1"
        self.allowlist = {x.strip() for x in os.environ.get("PM5M_IP_ALLOWLIST", "").split(",") if x.strip()}
        self.http_out = 0
        self.sysmon = SysMon(self.store.dir, self._feed_bytes, lambda: self.http_out)

    # ---------------- helpers ----------------
    def _feed_bytes(self) -> int:
        return sum(v.bytes for v in self.hub.status.values())

    def client_ip(self, request) -> str:
        if self.trust_proxy:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()    # last hop = the one our proxy appended
        return request.remote or "?"

    def _mark_dirty(self) -> None:
        self.dirty = True

    def _event(self, ev: dict) -> None:
        try:
            self.store.append_event(ev)
        except OSError as e:
            self.last_error = f"event log: {e}"

    def _load_password(self) -> str:
        pw = os.environ.get("PM5M_PASSWORD")
        path = os.path.join(self.store.dir, "web_password.txt")
        if pw:
            return pw
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        pw = secrets.token_urlsafe(12)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pw)
        print(f"\n[pm5m] 首次启动：已生成网页登录密码（用户名 admin）：{pw}\n"
              f"[pm5m] 保存在 {path}（权限 600）。也可用环境变量 PM5M_PASSWORD 指定。\n", flush=True)
        return pw

    def creds(self) -> Optional[dict]:
        c = self.secrets.get("creds")
        return c if c and c.get("apiKey") else None

    def save_secrets(self) -> None:
        self.store.secrets.save(self.secrets)

    def save_settings(self) -> None:
        self.settings["strategy"] = self.engine.cfg.to_dict()
        self.store.save_settings(self.settings)

    # ---------------- background tasks ----------------
    async def runner_loop(self) -> None:
        while True:
            try:
                self.runner.tick(time.time())
            except Exception as e:
                self.last_error = f"runner: {e!r}"
                log.exception("runner tick failed")
            await asyncio.sleep(0.25)

    async def sysmon_loop(self) -> None:
        while True:
            try:
                self.sysmon.sample()
            except Exception as e:
                self.last_error = f"sysmon: {e!r}"
            await asyncio.sleep(2)

    async def _derive_env_key(self) -> None:
        """Derive CLOB API credentials from PM5M_PRIVATE_KEY (env). The key itself is never stored."""
        try:
            from .clobauth import address_from_key
            pk = os.environ["PM5M_PRIVATE_KEY"].strip()
            pk = pk if pk.startswith("0x") else "0x" + pk
            signer = address_from_key(pk)
            creds = await self.api.derive_creds(pk)
            acct = explicit_account() or await self.api.resolve_account(signer)
            self.secrets = {"signer": signer, "funder": acct["funder"], "sig_type": acct["sig_type"], "creds": creds}
            self.save_secrets()
            self.last_error = ""
            asyncio.create_task(self.feeds.reconnect_polybolt())
            asyncio.create_task(self.refresh_wallet())
        except Exception as e:
            self.last_error = f"env key derive: {type(e).__name__}: {str(e)[:150]}"

    async def persist_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            self.flush()

    def flush(self) -> None:
        if self.dirty:
            self.dirty = False
            try:
                self.store.save_orders(self.engine.dump())
            except OSError as e:
                self.last_error = f"save orders: {e}"

    async def wallet_loop(self) -> None:
        while True:
            try:
                await self.refresh_wallet()
            except Exception as e:
                self.last_error = f"wallet: {e!r}"
            await asyncio.sleep(60)

    async def refresh_wallet(self) -> None:
        info = {"updated": time.time(), "errors": {}}
        s = self.secrets
        creds, signer = self.creds(), s.get("signer")
        funder = s.get("funder") or signer
        if creds and signer:
            try:
                info["clob_balance"] = await self.api.clob_balance(signer, creds, int(s.get("sig_type", 0)))
            except Exception as e:
                info["errors"]["clob_balance"] = str(e)[:200]
        if funder:
            for key, fn in (("onchain_pusd", self.api.onchain_pusd), ("positions_value", self.api.value),
                            ("positions", self.api.positions)):
                try:
                    info[key] = await fn(funder)
                except Exception as e:
                    info["errors"][key] = str(e)[:200]
        self.wallet_info = info

    async def geo_loop(self) -> None:
        while True:
            try:
                self.geo = await self.api.geoblock()
            except Exception as e:
                self.geo = {"error": str(e)[:200]}
            await asyncio.sleep(300)

    # ---------------- web: auth ----------------
    @web.middleware
    async def auth_mw(self, request: web.Request, handler):
        ip = self.client_ip(request)
        if self.allowlist and ip not in self.allowlist:
            return web.Response(status=403, text="forbidden")
        now = time.time()
        fails = [t for t in self.failures.get(ip, []) if now - t < 300]
        self.failures[ip] = fails
        if len(fails) >= 10:
            return web.Response(status=429, text="too many failed attempts, retry later")
        ok = False
        h = request.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(user, "admin") & hmac.compare_digest(pw, self.password)
            except Exception:
                ok = False
        if not ok:
            if h:
                fails.append(now)
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="pm5m"'}, text="auth required")
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
            return web.json_response({"error": "missing X-Requested-With header"}, status=403)
        resp = await handler(request)
        self.http_out += len(getattr(resp, "body", None) or b"")
        return resp

    def transport_ok(self, request: web.Request) -> bool:
        if os.environ.get("PM5M_ALLOW_INSECURE") == "1":
            return True
        if self.trust_proxy:
            return request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return request.secure or request.remote in ("127.0.0.1", "::1")

    # ---------------- web: handlers ----------------
    async def h_index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    def window_info(self, now: float) -> dict:
        S = int(now // WINDOW) * WINDOW
        m = self.runner.markets.get(S)
        B = self.runner.B.get(S)
        return {"slug": self.runner.slug_for(S), "start": S, "end": S + WINDOW, "remaining": round(S + WINDOW - now, 1),
                "B": B["value"] if B else None, "B_source": B["source"] if B else None,
                "market_ok": bool(m), "official": None}

    async def h_state(self, request):
        now = time.time()
        hub, run = self.hub, self.runner
        S = int(now // WINDOW) * WINDOW
        spot, tw, bn = hub.spot.last(), hub.twap.last(), hub.binance.last()
        books = {}
        m = run.markets.get(S)
        if m:
            for side, tok in (("Up", m["up"]), ("Down", m["down"])):
                b = hub.books.get(tok)
                if b and b.has_snapshot:
                    bb, ba = b.best_bid(), b.best_ask()
                    books[side] = {"bid": bb, "ask": ba, "bid_size": b.size_at("BUY", bb) if bb else None,
                                   "ask_size": b.size_at("SELL", ba) if ba else None}
        ms = run.model_snapshot if run.model_snapshot and run.model_snapshot.get("S") == S else None
        orders = self.engine.orders
        w = self.wallet_info
        state = {
            "now": now, "mode": "PAPER", "window": self.window_info(now),
            "prices": {"spot": spot[1] if spot else None, "spot_age": round(now - spot[0], 1) if spot else None,
                       "spot_source": hub.spot_source, "twap60": tw[1] if tw else None,
                       "binance": bn[1] if bn else None,
                       "basis": (bn[1] - spot[1]) if bn and spot else None},
            "books": books,
            "model": ms and {"p_up": ms["p_up"], "p_down": ms["p_down"], "gap": ms["gap"], "sd": ms["sd"],
                             "sigma5m_eff": ms.get("sigma5m_eff")},
            "feeds": {k: v.to_dict(now) for k, v in hub.status.items()},
            "strategy": {"enabled": self.engine.cfg.enabled, "msg": run.strategy_msg, "cfg": self.engine.cfg.to_dict()},
            "stats": self.engine.stats(),
            "open_orders": [self._order_view(o) for o in orders if o.is_resting],
            "recent_orders": [self._order_view(o) for o in orders[-40:][::-1]],
            "wallet": {"has_creds": bool(self.creds()), "signer": self.secrets.get("signer"),
                       "funder": self.secrets.get("funder"), "sig_type": self.secrets.get("sig_type", 0),
                       "api_key": mask(self.secrets.get("creds", {}).get("apiKey")),
                       "key_stored": bool(self.secrets.get("private_key")),
                       "secrets_encrypted": self.store.secrets.encrypted,
                       "updated": w.get("updated"), "clob_balance": w.get("clob_balance"),
                       "onchain_pusd": w.get("onchain_pusd"), "positions_value": w.get("positions_value"),
                       "positions": w.get("positions") or [], "errors": w.get("errors", {})},
            "geo": self.geo, "last_error": self.last_error, "errors": self.feeds.errors if self.feeds else {},
            "binance_enabled": self.settings.get("binance_enabled", True),
            "record_series": self.settings.get("record_series", True),
            "pending_official": len(run.need_official),
            "allow_web_key": self.allow_web_key,
            "last_final": ({"slug": run.slug_for(max(run.final)), **run.final[max(run.final)]} if run.final else None),
        }
        return web.json_response(state)

    @staticmethod
    def _order_view(o) -> dict:
        return {"id": o.id, "slug": o.slug, "side": o.side, "price": o.price, "shares": o.shares, "filled": o.filled,
                "status": o.status, "reason": o.cancel_reason, "placed_ts": o.placed_ts, "tau": o.tau_at_place,
                "queue": o.queue_ahead0, "model_p": o.model_p, "settled": o.settled, "won": o.side_won,
                "pnl": o.pnl, "first_fill_ts": o.first_fill_ts}

    async def h_series(self, request):
        now = time.time()
        S = int(now // WINDOW) * WINDOW
        ser = self.runner.series.get(S)
        if not ser:
            return web.json_response({"S": S, "t": []})
        out = dict(ser)
        diffs = [b - s for b, s in zip(ser["binance"], ser["spot"]) if b is not None and s is not None]
        off = statistics.median(diffs) if diffs else 0.0
        out["binance_adj"] = [None if b is None else round(b - off, 2) for b in ser["binance"]]
        out["orders"] = [{"t": round(o.placed_ts - S, 1), "price": o.price, "side": o.side, "status": o.status,
                          "fill_t": round(o.first_fill_ts - S, 1) if o.first_fill_ts else None}
                         for o in self.engine.orders if o.slug == ser["slug"]]
        return web.json_response(out)

    async def h_settings_get(self, request):
        return web.json_response({"strategy": self.engine.cfg.to_dict(),
                                  "binance_enabled": self.settings.get("binance_enabled", True),
                                  "record_series": self.settings.get("record_series", True)})

    async def h_settings_post(self, request):
        body = await request.json()
        merged = self.engine.cfg.to_dict()
        merged.update(body.get("strategy", {}))
        new = StrategyConfig.from_dict(merged)
        new.enabled = self.engine.cfg.enabled
        self.engine.cfg = new
        for k in ("binance_enabled", "record_series"):
            if k in body:
                self.settings[k] = bool(body[k])
        self.runner.record_series = self.settings.get("record_series", True)
        self.save_settings()
        return web.json_response({"ok": True, "strategy": new.to_dict()})

    async def h_strategy(self, request):
        on = request.match_info["action"] == "start"
        self.engine.set_enabled(on)
        self.save_settings()
        return web.json_response({"ok": True, "enabled": on})

    async def h_wallet_post(self, request):
        body = await request.json()
        pk = (body.get("private_key") or "").strip()
        try:
            if pk:
                from .clobauth import address_from_key
                pk = pk if pk.startswith("0x") else "0x" + pk
                signer = address_from_key(pk)
                creds = await self.api.derive_creds(pk)
                acct = await self.api.resolve_account(signer)
                self.secrets = {"signer": signer, "funder": acct["funder"], "sig_type": acct["sig_type"], "creds": creds}
            elif body.get("api_key"):
                signer = (body.get("signer_address") or "").strip()
                if not parsers.is_address(signer):
                    return web.json_response({"error": "手动凭据需要填写签名钱包地址 signer_address"}, status=400)
                acct = await self.api.resolve_account(signer)
                self.secrets = {"signer": signer, "funder": acct["funder"], "sig_type": acct["sig_type"],
                                "creds": {"apiKey": body["api_key"].strip(), "secret": body["api_secret"].strip(),
                                          "passphrase": body["api_passphrase"].strip()}}
            else:
                return web.json_response({"error": "请提供私钥"}, status=400)
        except ApiError as e:
            return web.json_response({"error": f"Polymarket 拒绝了请求：{e}"}, status=502)
        except ImportError:
            return web.json_response({"error": "缺少依赖 eth-account：pip install eth-account"}, status=500)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {str(e)[:200]}"}, status=500)
        finally:
            pk = None
            body.pop("private_key", None)
        self.save_secrets()
        asyncio.create_task(self.feeds.reconnect_polybolt())
        asyncio.create_task(self.refresh_wallet())
        return web.json_response({"ok": True, "signer": self.secrets["signer"], "funder": self.secrets["funder"],
                                  "key_stored": bool(self.secrets.get("private_key"))})

    async def h_wallet_delete(self, request):
        self.secrets = {}
        self.store.secrets.clear()
        self.wallet_info = {"updated": None, "errors": {}}
        asyncio.create_task(self.feeds.reconnect_polybolt())
        return web.json_response({"ok": True})

    async def h_wallet_refresh(self, request):
        await self.refresh_wallet()
        return web.json_response({"ok": True})

    async def h_sys(self, request):
        return web.json_response(self.sysmon.summary())

    async def h_export(self, request):
        text = orders_to_csv(self.engine.dump())
        return web.Response(text=text, content_type="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="paper_orders.csv"'})

    async def h_reset(self, request):
        body = await request.json()
        if not body.get("confirm"):
            return web.json_response({"error": "confirm required"}, status=400)
        self.engine.cancel_all("reset")
        self.engine.reset()
        self.dirty = False
        dest = self.store.reset_records(archive=bool(body.get("archive", True)), include_series=bool(body.get("include_series", False)))
        return web.json_response({"ok": True, "archived_to": dest})

    async def h_selftest(self, request):
        res = []

        async def t(name, coro):
            t0 = time.time()
            try:
                detail = await coro
                res.append({"name": name, "ok": True, "detail": str(detail)[:200], "ms": int((time.time() - t0) * 1000)})
            except Exception as e:
                res.append({"name": name, "ok": False, "detail": str(e)[:200] or type(e).__name__, "ms": int((time.time() - t0) * 1000)})

        async def geo():
            g = await self.api.geoblock()
            self.geo = g
            if g.get("blocked"):
                raise RuntimeError(f"此 IP 被限制下单: {g.get('country')} {g.get('region', '')}")
            return f"可用 {g.get('country')} {g.get('ip')}"

        async def skew():
            s = await self.api.clock_skew()
            if abs(s) > 1.0:
                raise RuntimeError(f"服务器时钟偏差 {s:+.2f}s，请启用 NTP/chrony")
            return f"偏差 {s:+.3f}s"

        async def gamma():
            S = int(time.time() // WINDOW) * WINDOW
            m = await self.api.gamma_market(self.runner.slug_for(S))
            if not m:
                raise RuntimeError("当前窗口市场未找到")
            return f"{m['slug']} up={mask(m['up'], 6)}"

        async def rpc():
            addr = self.secrets.get("funder") or self.secrets.get("signer") or "0x" + "0" * 40
            return f"pUSD({mask(addr, 6)}) = {await self.api.onchain_pusd(addr):.4f}"

        await t("地域限制 geoblock", geo())
        await t("服务器时钟", skew())
        await t("Gamma 当前市场", gamma())
        await t("Polygon RPC / pUSD", rpc())
        for k, v in self.hub.status.items():
            ok = v.state == "connected" and v.last_msg and time.time() - v.last_msg < 30
            res.append({"name": f"feed:{k}", "ok": bool(ok), "detail": f"{v.state} {v.error or v.extra}"[:200], "ms": 0})
        return web.json_response(res)

    # ---------------- lifecycle ----------------
    def make_web(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_mw], client_max_size=64 * 1024)
        r = app.router
        r.add_get("/", self.h_index)
        r.add_get("/api/state", self.h_state)
        r.add_get("/api/series", self.h_series)
        r.add_get("/api/settings", self.h_settings_get)
        r.add_post("/api/settings", self.h_settings_post)
        r.add_post("/api/strategy/{action:(start|stop)}", self.h_strategy)
        r.add_post("/api/wallet", self.h_wallet_post)
        r.add_delete("/api/wallet", self.h_wallet_delete)
        r.add_post("/api/wallet/refresh", self.h_wallet_refresh)
        r.add_get("/api/export/orders.csv", self.h_export)
        r.add_get("/api/sys", self.h_sys)
        r.add_post("/api/reset", self.h_reset)
        r.add_post("/api/selftest", self.h_selftest)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app

    async def on_startup(self, app) -> None:
        self.session = aiohttp.ClientSession()
        self.api = Api(self.session)
        self.feeds = Feeds(self.hub, self.runner, self.api, self.session, self.creds,
                           lambda: self.settings.get("binance_enabled", True))
        if os.environ.get("PM5M_PRIVATE_KEY") and not self.creds():
            self.tasks.append(asyncio.create_task(self._derive_env_key()))
        for fn in (self.runner_loop, self.persist_loop, self.wallet_loop, self.geo_loop, self.sysmon_loop,
                   self.feeds.polybolt_loop, self.feeds.binance_loop, self.feeds.market_loop,
                   self.feeds.markets_loop):
            self.tasks.append(asyncio.create_task(fn()))

    async def on_cleanup(self, app) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.engine.cancel_all("shutdown")
        self.dirty = True
        self.flush()
        if self.session:
            await self.session.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PM5M_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PM5M_PORT", "8080")))
    ap.add_argument("--data-dir", default=os.environ.get("PM5M_DATA", "./data"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = App(a.data_dir)
    web.run_app(app.make_web(), host=a.host, port=a.port, access_log=None)


if __name__ == "__main__":
    main()
