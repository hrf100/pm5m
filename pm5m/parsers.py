"""Pure parsers for external payloads (PolyBolt, legacy RTDS, Binance, Gamma, CLOB, Data API)."""
import json
import re
from typing import List, Optional


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ts_s(x) -> float:
    v = _f(x) or 0.0
    return v / 1000.0 if v > 1e11 else v


def parse_polybolt(frame: dict) -> List[tuple]:
    """Parse a PolyBolt (wss://ws-live-v2.polymarket.com/ws) frame.

    Returns tuples:
      ("ack", dict)                                  op acks / error acks
      ("spot", ts_s, price, source)                  price.crypto btcusd
      ("twap", ts_s, price)                          price.crypto.twap btcusd (60s)
    """
    if not isinstance(frame, dict):
        return []
    if "op" in frame:
        return [("ack", frame)]
    ch = frame.get("channel")
    pl = frame.get("payload")
    if ch not in ("price.crypto", "price.crypto.twap") or pl is None:
        return []
    if isinstance(pl, list):  # empty snapshot for cold symbol
        return []
    if str(pl.get("symbol", "")).lower() != "btcusd":
        return []
    kind = "spot" if ch == "price.crypto" else "twap"
    source = pl.get("source")
    pts = pl["data"] if isinstance(pl.get("data"), list) else [pl]
    out = []
    for p in pts:
        px = _f(p.get("full_accuracy_value"))
        if px is None:
            px = _f(p.get("value"))
        ts = _ts_s(p.get("timestamp"))
        if px is None or ts <= 0:
            continue
        out.append(("spot", ts, px, source) if kind == "spot" else ("twap", ts, px))
    return out


def parse_legacy_rtds(frame: dict) -> List[tuple]:   # retained for reference; feeds no longer use it
    """Legacy RTDS topic crypto_prices_chainlink -> ("spot", ts, price, 'chainlink')."""
    if not isinstance(frame, dict):
        return []
    if frame.get("topic") != "crypto_prices_chainlink":
        return []
    pl = frame.get("payload") or {}
    if str(pl.get("symbol", "")).lower() != "btc/usd":
        return []
    out = []
    pts = pl["data"] if isinstance(pl.get("data"), list) else [pl]
    for p in pts:
        px = _f(p.get("value"))
        ts = _ts_s(p.get("timestamp"))
        if px is not None and ts > 0:
            out.append(("spot", ts, px, "chainlink"))
    return out


def parse_binance_trade(msg: dict) -> Optional[tuple]:
    """Binance @trade / @aggTrade -> (ts_s, price)."""
    if not isinstance(msg, dict):
        return None
    px = _f(msg.get("p"))
    ts = _ts_s(msg.get("T") or msg.get("E"))
    if px is None or ts <= 0:
        return None
    return ts, px


def _jlist(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except ValueError:
            return []
    return []


def parse_gamma_market(d: dict) -> Optional[dict]:
    """Gamma /markets/slug/{slug} -> normalised dict (tokens mapped by outcome label)."""
    if not isinstance(d, dict):
        return None
    ids = [str(i) for i in _jlist(d.get("clobTokenIds"))]
    outs = [str(o).strip().lower() for o in _jlist(d.get("outcomes"))]
    if len(ids) != 2 or len(outs) != 2 or set(outs) != {"up", "down"}:
        return None
    m = dict(zip(outs, ids))
    prices = [_f(p) for p in _jlist(d.get("outcomePrices"))]
    official_up = None
    if len(prices) == 2 and None not in prices:
        pm = dict(zip(outs, prices))
        if (d.get("closed") or str(d.get("umaResolutionStatus", "")).lower() == "resolved"):
            if pm["up"] >= 0.99 and pm["down"] <= 0.01:
                official_up = True
            elif pm["down"] >= 0.99 and pm["up"] <= 0.01:
                official_up = False
    return {"slug": d.get("slug"), "condition_id": d.get("conditionId"), "up": m["up"], "down": m["down"],
            "closed": bool(d.get("closed")), "official_up": official_up, "question": d.get("question")}


def parse_balance(d: dict) -> Optional[float]:
    """CLOB /balance-allowance -> collateral balance in USD (6-decimal base units)."""
    if not isinstance(d, dict) or "balance" not in d:
        return None
    v = _f(d["balance"])
    return None if v is None else v / 1e6


def parse_positions(rows) -> List[dict]:
    out = []
    if isinstance(rows, dict):
        rows = rows.get("data")
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": r.get("title") or r.get("slug"),
            "outcome": r.get("outcome"),
            "size": _f(r.get("current_size", r.get("size"))),
            "avg_price": _f(r.get("avg_price", r.get("avgPrice"))),
            "cur_price": _f(r.get("current_price", r.get("curPrice"))),
            "value": _f(r.get("current_value", r.get("currentValue"))),
            "pnl": _f(r.get("total_pnl", r.get("unrealized_pnl", r.get("cashPnl")))),
            "redeemable": bool(r.get("redeemable")),
        })
    return out


ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_address(s) -> bool:
    return isinstance(s, str) and bool(ADDR_RE.match(s))
