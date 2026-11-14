"""Connection self-test. Run on the target server:

    python -m pm5m.selftest            # uses env vars below; prints a report and writes pm5m_selftest_report.txt

Environment (all optional; missing ones are reported as SKIP):
  PM5M_PRIVATE_KEY        wallet key, used once to derive CLOB API credentials (not stored, not printed)
  PM5M_API_KEY / PM5M_API_SECRET / PM5M_API_PASSPHRASE / PM5M_SIGNER   manual credentials instead of the key
  PM5M_FUNDER             Polymarket profile address (funder), needed for balance/position checks
  PM5M_SIG_TYPE           0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE (default), 3 DEPOSIT_WALLET

Secrets are never printed: only masked prefixes/suffixes.
"""
import asyncio
import json
import sys
import time
from collections import Counter

import aiohttp

from . import parsers
from .feeds import (BINANCE_WS, MARKET_WS, POLYBOLT_WS, Api, ApiError, polybolt_auth_frame,
                    polybolt_subscribe_frame)
from .book import parse_market_events
from .runner import WINDOW
import os

LINES = []


def out(status: str, name: str, detail: str = "") -> None:
    line = f"[{status:4}] {name}" + (f" — {detail}" if detail else "")
    LINES.append(line)
    print(line, flush=True)


def mask(s):
    if not s:
        return ""
    return s[:4] + "…" + s[-4:] if len(s) > 10 else "…"


async def polybolt_probe(session, creds, seconds: float = 25.0):
    stats = {"frames": Counter(), "spot": [], "twap": [], "acks": [], "errors": [], "first_keys": {}}
    async with session.ws_connect(POLYBOLT_WS, heartbeat=None, max_msg_size=0, receive_timeout=40) as ws:
        out("INFO", "PolyBolt 已连接", POLYBOLT_WS)
        await ws.send_json(polybolt_auth_frame(creds))
        t_end = time.time() + seconds
        authed = False
        while time.time() < t_end:
            try:
                msg = await ws.receive(timeout=max(0.1, t_end - time.time()))
            except asyncio.TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
                continue
            f = json.loads(msg.data)
            if "op" in f:
                stats["acks"].append(f)
                if f["op"] == "authed" and not authed:
                    authed = True
                    await ws.send_json(polybolt_subscribe_frame())
                if f["op"] == "error":
                    stats["errors"].append(f)
                continue
            ch = f.get("channel")
            stats["frames"][ch] += 1
            stats["first_keys"].setdefault(ch, sorted(f.keys()))
            for ev in parsers.parse_polybolt(f):
                if ev[0] == "spot":
                    stats["spot"].append((ev[1], ev[2], ev[3]))
                elif ev[0] == "twap":
                    stats["twap"].append((ev[1], ev[2]))
        code = ws.close_code
    return stats, code


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        api = Api(session)
        pk = os.environ.get("PM5M_PRIVATE_KEY", "").strip()
        funder = os.environ.get("PM5M_FUNDER", "").strip()
        sig_type = int(os.environ.get("PM5M_SIG_TYPE", "2"))
        out("INFO", "时间", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))

        # 1. geoblock
        try:
            g = await api.geoblock()
            if g.get("blocked"):
                out("FAIL", "地域限制", f"此 IP 被限制下单：country={g.get('country')} ip={g.get('ip')}")
            else:
                out("PASS", "地域限制", f"country={g.get('country')} ip={g.get('ip')}")
        except Exception as e:
            out("FAIL", "地域限制接口", repr(e)[:200])

        # 2. clock
        try:
            skew = await api.clock_skew()
            out("PASS" if abs(skew) <= 1.0 else "WARN", "服务器时钟偏差", f"{skew:+.3f}s")
        except Exception as e:
            out("FAIL", "CLOB /time", repr(e)[:200])

        # 3. current market
        S = int(time.time() // WINDOW) * WINDOW
        slug = f"btc-updown-5m-{S}"
        mk = None
        try:
            mk = await api.gamma_market(slug)
            if mk:
                out("PASS", "Gamma 当前市场", f"{slug} up={mask(mk['up'])} down={mask(mk['down'])}")
            else:
                out("WARN", "Gamma 当前市场", f"{slug} 未找到（可能是窗口切换瞬间，重试）")
        except Exception as e:
            out("FAIL", "Gamma", repr(e)[:200])

        # 4. credentials
        creds, signer = None, os.environ.get("PM5M_SIGNER", "").strip()
        if pk:
            try:
                from .clobauth import address_from_key
                pk = pk if pk.startswith("0x") else "0x" + pk
                signer = address_from_key(pk)
                creds = await api.derive_creds(pk)
                out("PASS", "API 凭据推导（私钥）", f"signer={mask(signer)} apiKey={mask(creds['apiKey'])}")
            except Exception as e:
                out("FAIL", "API 凭据推导（私钥）", f"{type(e).__name__}: {str(e)[:200]}")
        elif os.environ.get("PM5M_API_KEY"):
            creds = {"apiKey": os.environ["PM5M_API_KEY"], "secret": os.environ.get("PM5M_API_SECRET", ""),
                     "passphrase": os.environ.get("PM5M_API_PASSPHRASE", "")}
            out("PASS", "API 凭据（手动）", f"apiKey={mask(creds['apiKey'])}")
        else:
            out("SKIP", "API 凭据", "未设置 PM5M_PRIVATE_KEY 或 PM5M_API_KEY")

        # 5. PolyBolt
        if creds:
            try:
                stats, code = await polybolt_probe(session, creds, 25.0)
                ok_auth = any(a.get("op") == "authed" for a in stats["acks"])
                subs = [a.get("channel") for a in stats["acks"] if a.get("op") == "subscribed"]
                errs = [e for e in stats["errors"]]
                out("PASS" if ok_auth else "FAIL", "PolyBolt 鉴权", "authed" if ok_auth else f"acks={stats['acks'][:3]}")
                out("PASS" if len(subs) == 2 else "FAIL", "PolyBolt 订阅确认", f"subscribed={subs} errors={errs}")
                out("INFO", "PolyBolt 帧计数（25 秒）", json.dumps(dict(stats["frames"])))
                for ch, keys in stats["first_keys"].items():
                    out("INFO", f"PolyBolt 首帧字段 {ch}", ",".join(keys))
                if stats["spot"]:
                    src = Counter(x[2] for x in stats["spot"])
                    out("INFO", "现货来源 price.crypto", json.dumps(dict(src)))
                if stats["twap"]:
                    out("PASS", "Chainlink TWAP60 数据", f"{len(stats['twap'])} 个点，最新 {stats['twap'][-1][1]:.2f}")
                else:
                    out("FAIL", "Chainlink TWAP60 数据", "25 秒内没有收到 TWAP 点（结算依据缺失，不能交易）")
                if stats["spot"] and stats["twap"]:
                    diff = stats["twap"][-1][1] - stats["spot"][-1][1]
                    out("INFO", "TWAP 与现货差值", f"{diff:+.2f} USD（应接近 0，过大需检查）")
                out("INFO", "PolyBolt 关闭码", str(code))
            except Exception as e:
                out("FAIL", "PolyBolt 连接", f"{type(e).__name__}: {str(e)[:200]}")

        # 6. Binance
        got = 0
        for url in BINANCE_WS:
            try:
                async with session.ws_connect(url, heartbeat=20, max_msg_size=0, receive_timeout=15) as ws:
                    t_end = time.time() + 8
                    while time.time() < t_end:
                        m = await ws.receive(timeout=max(0.1, t_end - time.time()))
                        if m.type == aiohttp.WSMsgType.TEXT and parsers.parse_binance_trade(json.loads(m.data)):
                            got += 1
                if got:
                    out("PASS", "Binance 逐笔成交", f"{url} 8 秒内 {got} 笔")
                    break
            except Exception as e:
                out("WARN", "Binance", f"{url}: {type(e).__name__} {str(e)[:120]}")
        if not got:
            out("FAIL", "Binance", "两个地址均无数据（参考源不可用，可关闭）")

        # 7. CLOB balance (L2)
        if creds and signer:
            try:
                bal = await api.clob_balance(signer, creds, sig_type)
                out("PASS", "CLOB 可用余额（L2 鉴权）", f"${bal:.2f}" if bal is not None else "返回无余额字段")
            except Exception as e:
                out("FAIL", "CLOB 余额", f"{type(e).__name__}: {str(e)[:200]}")
        else:
            out("SKIP", "CLOB 余额", "需要凭据与签名地址")

        # 8. on-chain pUSD
        addr = funder or signer
        if addr:
            try:
                out("PASS", "链上 pUSD 余额", f"{mask(addr)} = {await api.onchain_pusd(addr):.4f}")
            except Exception as e:
                out("FAIL", "Polygon RPC / pUSD", str(e)[:200])
        else:
            out("SKIP", "链上 pUSD 余额", "需要 PM5M_FUNDER 或签名地址")

        # 9. CLOB market channel (format NOT yet verified against docs)
        if mk:
            try:
                async with session.ws_connect(MARKET_WS, heartbeat=None, receive_timeout=15) as ws:
                    await ws.send_json({"assets_ids": [mk["up"], mk["down"]], "type": "market",
                                        "custom_feature_enabled": True})
                    n = 0
                    t_end = time.time() + 8
                    while time.time() < t_end:
                        m = await ws.receive(timeout=max(0.1, t_end - time.time()))
                        if m.type == aiohttp.WSMsgType.TEXT and m.data not in ("PONG", "pong"):
                            n += len(parse_market_events(json.loads(m.data)))
                out("PASS" if n else "WARN", "CLOB 市场频道", f"8 秒内解析出 {n} 个事件（订阅格式待对照文档）")
            except Exception as e:
                out("FAIL", "CLOB 市场频道", f"{type(e).__name__}: {str(e)[:200]}")

    fails = sum(1 for l in LINES if l.startswith("[FAIL"))
    with open("pm5m_selftest_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")
    print(f"\n共 {fails} 项失败。报告已写入 pm5m_selftest_report.txt，请把此文件内容发回。")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
