"""Network layer (aiohttp): REST helpers and websocket loops. All parsing lives in parsers.py/book.py."""
import asyncio
import json
import random
import time
from typing import Callable, Dict, List, Optional

import aiohttp

from . import clobauth, parsers
from .book import parse_market_events
from .hub import Hub
from .runner import Runner, WINDOW

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"
POLYBOLT_WS = "wss://ws-live-v2.polymarket.com/ws"
RTDS_WS = "wss://ws-live-data.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = ["wss://stream.binance.com:9443/ws/btcusdt@trade", "wss://data-stream.binance.vision/ws/btcusdt@trade"]
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"]
UA = {"User-Agent": "pm5m-paper/0.1"}
# Runtime shapes, not one pinned implementation. Extra bytes (compiler metadata, beacon args) are ignored.
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"
POLY_PROXY_CODE = EIP1167_PREFIX + "44e999d5c2f66ef0861317f9a4805ac2e90aeb4f" + EIP1167_SUFFIX
SAFE_PROXY_PREFIX = ("6080604052600080546001600160a01b0316813563530ca43760e11b1415602857808252602082f35b3682833781823684845af490503d82833e806041573d82fd5b"
                     "503d81f3")
SAFE_PROXY_CODE = SAFE_PROXY_PREFIX + "fea264697066735822122015938e3bf2c49f5df5c1b7f9569fa85cc5d6f3074bb258a2dc0c7e299bc9e33664736f6c63430008040033"
DEPOSIT_CODE_PREFIX = "363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3"
# ERC-1967 beacon proxy used by current Polymarket deposit wallets. Args are appended after this runtime.
DEPOSIT_BEACON_PREFIX = ("363d3d373d3d363d602036600436635c60da1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c"
                         "b3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3")


def signature_type_from_code(code: str) -> Optional[int]:
    """0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE, 3 deposit wallet. None if the runtime is unknown."""
    code = (code or "").lower().removeprefix("0x")
    if code == "":
        return 0
    if code.startswith(EIP1167_PREFIX) and code[60:60 + len(EIP1167_SUFFIX)] == EIP1167_SUFFIX:
        return 1
    if code.startswith(SAFE_PROXY_PREFIX):
        return 2
    if code.startswith(DEPOSIT_CODE_PREFIX) or code.startswith(DEPOSIT_BEACON_PREFIX):
        return 3
    return None


def polybolt_subscribe_frame(rid: str = "s1") -> dict:
    """Subscribe frame per https://ws-live-v2.polymarket.com/asyncapi.json (no provider pin)."""
    return {"op": "subscribe", "rid": rid, "subscriptions": [
        {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
        {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}},
    ]}


def polybolt_auth_frame(creds: dict, rid: str = "a1") -> dict:
    return {"op": "auth", "rid": rid, "auth": {"apiKey": creds["apiKey"], "secret": creds["secret"],
                                               "passphrase": creds["passphrase"]}}


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:160]}")
        self.status = status


class Api:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def get(self, url: str, params=None, headers=None, timeout: float = 10.0):
        h = dict(UA)
        h.update(headers or {})
        async with self.s.get(url, params=params, headers=h, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            if r.status != 200:
                raise ApiError(r.status, text)
            try:
                return json.loads(text), r.headers
            except ValueError:
                return text, r.headers

    async def post(self, url: str, json_body=None, headers=None, timeout: float = 10.0):
        h = dict(UA)
        h.update(headers or {})
        async with self.s.post(url, json=json_body, headers=h, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
            if r.status != 200:
                raise ApiError(r.status, text)
            return json.loads(text), r.headers

    # ---- public ----
    async def geoblock(self) -> dict:
        d, _ = await self.get(GEOBLOCK)
        return d

    async def gamma_market(self, slug: str) -> Optional[dict]:
        try:
            d, _ = await self.get(f"{GAMMA}/markets/slug/{slug}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return parsers.parse_gamma_market(d)

    async def positions(self, addr: str) -> List[dict]:
        d, _ = await self.get(f"{DATA_API}/positions", params={"user": addr, "sizeThreshold": "0", "limit": "200"})
        return parsers.parse_positions(d)

    async def value(self, addr: str) -> Optional[float]:
        d, _ = await self.get(f"{DATA_API}/value", params={"user": addr})
        if isinstance(d, list) and d:
            return parsers._f(d[0].get("value"))
        return None

    async def eth_code(self, addr: str) -> str:
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [addr, "latest"]}
        last = None
        for rpc in POLYGON_RPCS:
            try:
                d, _ = await self.post(rpc, body)
                if isinstance(d, dict) and "result" in d:
                    return d["result"]
                last = d.get("error") if isinstance(d, dict) else d
            except Exception as e:
                last = e
        raise RuntimeError(f"polygon rpc failed: {last}")

    async def resolve_account(self, signer: str) -> dict:
        """Funder address and signature type for a signer. Only the private key is required upstream."""
        funder = signer
        try:
            d, _ = await self.get(f"{GAMMA}/public-profile", params={"address": signer})
            pw = d.get("proxyWallet") if isinstance(d, dict) else None
            if isinstance(pw, str) and parsers.is_address(pw):
                funder = pw
        except ApiError as e:
            if e.status not in (400, 404):
                raise
        if funder.lower() == signer.lower():
            return {"funder": signer, "sig_type": 0}
        sig = signature_type_from_code(await self.eth_code(funder))
        if sig is None:
            raise RuntimeError(f"无法识别资金地址的合约类型: {funder}")
        if sig == 0:
            sig = 3    # proxy address with no code yet: current Polymarket accounts are deposit wallets
        return {"funder": funder, "sig_type": sig}

    async def onchain_pusd(self, addr: str) -> float:
        data = "0x70a08231" + addr.lower().replace("0x", "").rjust(64, "0")
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": PUSD, "data": data}, "latest"]}
        last = None
        for rpc in POLYGON_RPCS:
            try:
                d, _ = await self.post(rpc, body)
                if "result" in d:
                    return int(d["result"], 16) / 1e6
                last = d.get("error")
            except Exception as e:  # try next RPC
                last = e
        raise RuntimeError(f"polygon rpc failed: {last}")

    async def clock_skew(self) -> float:
        t0 = time.time()
        async with self.s.get(f"{CLOB}/time", headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            text = (await r.text()).strip()
            t1 = time.time()
            try:
                server = float(text)
            except ValueError:
                from email.utils import parsedate_to_datetime
                server = parsedate_to_datetime(r.headers["Date"]).timestamp()
        return server - (t0 + t1) / 2.0

    # ---- authenticated (L1 / L2) ----
    async def derive_creds(self, private_key: str) -> dict:
        ts = int(time.time())
        h = clobauth.l1_headers(private_key, ts, 0)
        try:
            d, _ = await self.get(f"{CLOB}/auth/derive-api-key", headers=h)
            if isinstance(d, dict) and d.get("apiKey"):
                return {"apiKey": d["apiKey"], "secret": d["secret"], "passphrase": d["passphrase"]}
        except ApiError:
            pass
        ts = int(time.time())
        h = clobauth.l1_headers(private_key, ts, 0)
        d, _ = await self.post(f"{CLOB}/auth/api-key", headers=h)
        if not (isinstance(d, dict) and d.get("apiKey")):
            raise RuntimeError("create api key returned no credentials")
        return {"apiKey": d["apiKey"], "secret": d["secret"], "passphrase": d["passphrase"]}

    async def clob_balance(self, signer: str, creds: dict, sig_type: int) -> Optional[float]:
        params = {"asset_type": "COLLATERAL", "signature_type": str(sig_type)}
        try:   # refresh the CLOB-side balance/allowance cache first; ignore failures
            ts = int(time.time())
            await self.get(f"{CLOB}/balance-allowance/update", params=params,
                           headers=clobauth.l2_headers(signer, creds, ts, "GET", "/balance-allowance/update"))
        except Exception:
            pass
        ts = int(time.time())
        d, _ = await self.get(f"{CLOB}/balance-allowance", params=params,
                              headers=clobauth.l2_headers(signer, creds, ts, "GET", "/balance-allowance"))
        return parsers.parse_balance(d)


class Feeds:
    def __init__(self, hub: Hub, runner: Runner, api: Api, session: aiohttp.ClientSession,
                 get_creds: Callable[[], Optional[dict]], binance_enabled: Callable[[], bool]):
        self.hub, self.runner, self.api, self.session = hub, runner, api, session
        self.get_creds = get_creds
        self.binance_enabled = binance_enabled
        self.polybolt_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.errors: Dict[str, str] = {}
        self._market_retry: Dict[int, float] = {}
        self._official_poll: Dict[int, float] = {}

    async def reconnect_polybolt(self) -> None:
        ws = self.polybolt_ws
        if ws is not None and not ws.closed:
            await ws.close()

    # ------------------------------------------------------------ PolyBolt (spot + Chainlink TWAP60)
    async def polybolt_loop(self) -> None:
        """PolyBolt: price.crypto (btcusd, currently Pyth) and price.crypto.twap (btcusd, 60 s, Chainlink).
        Gated channels need CLOB API credentials sent in an auth frame before subscribing."""
        st = self.hub.status["polybolt"]
        backoff = 2.0
        while True:
            creds = self.get_creds()
            if not creds:
                st.state, st.error = "no_creds", "未设置 API 凭据，PolyBolt 无法订阅"
                await asyncio.sleep(3)
                continue
            try:
                st.state = "connecting"
                async with self.session.ws_connect(POLYBOLT_WS, heartbeat=None, max_msg_size=0,
                                                   receive_timeout=70, headers=UA) as ws:
                    self.polybolt_ws = ws
                    await ws.send_json(polybolt_auth_frame(creds))
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        st.msg(len(msg.data))
                        try:
                            frame = json.loads(msg.data)
                        except ValueError:
                            continue
                        for ev in parsers.parse_polybolt(frame):
                            if ev[0] == "ack":
                                f = ev[1]
                                op = f.get("op")
                                if op == "authed":
                                    backoff = 2.0
                                    await ws.send_json(polybolt_subscribe_frame())
                                elif op == "subscribed":
                                    st.ok(f"subscribed {f.get('channel')}")
                                elif op == "error":
                                    code = str(f.get("code", ""))
                                    st.fail(f"{code} ({f.get('channel', '')})")
                                    if code.startswith("auth_"):
                                        await ws.close()
                            elif ev[0] == "spot":
                                self.hub.on_spot(ev[1], ev[2], ev[3])
                                self.hub.spot_source = ev[3]
                            elif ev[0] == "twap":
                                self.hub.on_twap(ev[1], ev[2])
                    code = ws.close_code
                    st.fail(f"closed code={code}")
                    if code == 4008:
                        backoff = 60.0
                    elif code == 1008:
                        backoff = 5.0       # access token expired; refresh credentials first
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st.fail(repr(e))
            finally:
                self.polybolt_ws = None
            await asyncio.sleep(backoff + random.random() * 2)
            backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------ Binance (reference only)
    async def binance_loop(self) -> None:
        st = self.hub.status["binance"]
        i = 0
        while True:
            if not self.binance_enabled():
                st.state, st.error = "disabled", ""
                await asyncio.sleep(3)
                continue
            url = BINANCE_WS[i % len(BINANCE_WS)]
            try:
                st.state = "connecting"
                async with self.session.ws_connect(url, heartbeat=20, max_msg_size=0, receive_timeout=30, headers=UA) as ws:
                    st.ok(url)
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        r = parsers.parse_binance_trade(json.loads(msg.data))
                        if r:
                            st.msg(len(msg.data))
                            self.hub.on_binance(r[0], r[1])
                        if not self.binance_enabled():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st.fail(repr(e))
                i += 1
            await asyncio.sleep(3 + random.random())

    # ------------------------------------------------------------ Polymarket CLOB market channel
    async def market_loop(self) -> None:
        st = self.hub.status["market_ws"]
        while True:
            tokens = self.runner.current_tokens()
            if not tokens:
                st.state, st.error = "init", "等待市场信息"
                await asyncio.sleep(1)
                continue
            subscribed = set(tokens)
            try:
                st.state = "connecting"
                async with self.session.ws_connect(MARKET_WS, heartbeat=None, max_msg_size=0, receive_timeout=40, headers=UA) as ws:
                    await ws.send_json({"assets_ids": list(subscribed), "type": "market", "custom_feature_enabled": True})
                    st.ok(f"{len(subscribed)} tokens")
                    helper = asyncio.create_task(self._market_helper(ws, subscribed))
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            if msg.data in ("PONG", "pong"):
                                continue
                            st.msg(len(msg.data))
                            try:
                                data = json.loads(msg.data)
                            except ValueError:
                                continue
                            self.hub.on_market_events(parse_market_events(data))
                    finally:
                        helper.cancel()
                st.fail(f"closed code={ws.close_code}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st.fail(repr(e))
            await asyncio.sleep(1 + random.random())

    async def _market_helper(self, ws, subscribed: set) -> None:
        """Send PING, add newly needed tokens dynamically, force reconnect if a new token never gets a book."""
        last_ping = 0.0
        waiting: Dict[str, float] = {}
        while not ws.closed:
            now = time.time()
            if now - last_ping >= 8:
                last_ping = now
                await ws.send_str("PING")
            new = [t for t in self.runner.current_tokens() if t not in subscribed]
            if new:
                await ws.send_json({"assets_ids": new, "operation": "subscribe"})
                subscribed.update(new)
                for t in new:
                    waiting[t] = now
            for t, t0 in list(waiting.items()):
                b = self.hub.books.get(t)
                if b is not None and b.has_snapshot:
                    del waiting[t]
                elif now - t0 > 6:
                    await ws.close()     # dynamic subscribe did not work -> reconnect with the full list
                    return
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ Gamma market discovery + official results
    async def markets_loop(self) -> None:
        while True:
            now = time.time()
            S = int(now // WINDOW) * WINDOW
            for k in (S, S + WINDOW):
                if k in self.runner.markets or now < self._market_retry.get(k, 0):
                    continue
                try:
                    info = await self.api.gamma_market(self.runner.slug_for(k))
                    if info:
                        self.runner.set_market(k, info)
                    else:
                        self._market_retry[k] = now + 5
                except Exception as e:
                    self.errors["gamma"] = repr(e)
                    self._market_retry[k] = now + 5
            for k, t0 in list(self.runner.need_official.items()):
                if now - t0 > 25 * 60:
                    self.runner.need_official.pop(k, None)
                    continue
                if now < self._official_poll.get(k, 0) or now < k + WINDOW + 20:
                    continue
                self._official_poll[k] = now + 20
                try:
                    info = await self.api.gamma_market(self.runner.slug_for(k))
                    if info and info["official_up"] is not None:
                        self.runner.set_official(k, info["official_up"])
                except Exception as e:
                    self.errors["gamma_official"] = repr(e)
            await asyncio.sleep(1.0)
