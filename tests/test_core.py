import base64, hashlib, hmac, json, math, os, random, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pm5m import model, parsers, clobauth
from pm5m.feeds import (signature_type_from_code, POLY_PROXY_CODE, SAFE_PROXY_CODE,
                       DEPOSIT_CODE_PREFIX, DEPOSIT_BEACON_PREFIX, EIP1167_PREFIX, EIP1167_SUFFIX)
from pm5m.book import Book, parse_market_events
from pm5m.hub import Hub, Buf
from pm5m.paper import PaperEngine, StrategyConfig
from pm5m.runner import Runner, WINDOW
from pm5m.store import DataStore, SecretBox, orders_to_csv


class TestModel(unittest.TestCase):
    def test_integral_step(self):
        s = [(0, 10.0), (10, 20.0), (20, 30.0)]
        self.assertAlmostEqual(model.integral_step(s, 0, 30), 10 * 10 + 20 * 10 + 30 * 10)
        self.assertAlmostEqual(model.integral_step(s, 5, 15), 10 * 5 + 20 * 5)
        self.assertAlmostEqual(model.integral_step(s, -5, 5), 10 * 10)   # back-fill with first sample
        self.assertIsNone(model.integral_step([], 0, 1))

    def test_continuity_at_60(self):
        B, T = 100.0, 1000.0
        sig = 1e-4
        hist = [(T - 60 + i, 100.0) for i in range(0, 61)]
        a = model.up_probability(B, T - 60.0001, T, 100.0, hist, sig)
        b = model.up_probability(B, T - 59.9999, T, 100.0, hist, sig)
        self.assertAlmostEqual(a["sd"], b["sd"], delta=1e-3 * a["sd"])

    def test_deterministic_at_end(self):
        T = 1000.0
        hist = [(T - 60 + i, 101.0) for i in range(60)]
        r = model.up_probability(100.0, T, T, 101.0, hist, 1e-4)
        self.assertEqual(r["p_up"], 1.0)
        r = model.up_probability(102.0, T, T, 101.0, hist, 1e-4)
        self.assertEqual(r["p_up"], 0.0)

    def _mc(self, tau, B, spot, hist_fn, sigma_rel, n=6000, seed=1):
        rnd = random.Random(seed)
        T = 1000.0
        now = T - tau
        hist = hist_fn(T, now)
        s_abs = sigma_rel * spot
        up = 0
        for _ in range(n):
            px = spot
            path = []
            t = now
            # simulate future at 1s steps (including partial first step)
            steps = int(math.ceil(tau))
            for k in range(1, steps + 1):
                px += s_abs * rnd.gauss(0, 1)
                path.append((now + k, px))
            # final TWAP over [T-60, T): known part + simulated, 1s sample-and-hold
            full = [x for x in hist if x[0] >= T - 60 and x[0] <= now] + [(a, b) for a, b in path if a >= T - 60 and a <= T]
            full.sort()
            integ = model.integral_step(full if full else [(now, spot)], T - 60, T)
            F = integ / 60.0
            up += F >= B
        return up / n

    def test_matches_monte_carlo(self):
        sig = 8.66e-5
        spot, B = 100000.0, 100000.0 + 40.0
        for tau in (120.0, 45.0, 20.0):
            hist_fn = lambda T, now: [(T - 130 + i, spot - 10 + 0.1 * i) for i in range(0, 131) if T - 130 + i <= now]
            hist = hist_fn(1000.0, 1000.0 - tau)
            res = model.up_probability(B, 1000.0 - tau, 1000.0, spot, hist, sig)
            mc = self._mc(tau, B, spot, hist_fn, sig)
            self.assertAlmostEqual(res["p_up"], mc, delta=0.03, msg=f"tau={tau}")

    def test_vol_estimator(self):
        v = model.VolEstimator()
        rnd = random.Random(3)
        px, ts = 100000.0, 0.0
        for _ in range(600):
            ts += 1.0
            px *= math.exp(1e-4 * rnd.gauss(0, 1))
            v.update(ts, px)
        self.assertAlmostEqual(v.sigma(5e-5, 0.01, 100), 1e-4, delta=3e-5)
        self.assertEqual(model.VolEstimator().sigma(0.123), 0.123)


class TestParsers(unittest.TestCase):
    def test_polybolt(self):
        snap = {"v": 1, "channel": "price.crypto", "seq": 1, "ts": 1788973000000, "snapshot": True,
                "payload": {"symbol": "btcusd", "source": "chainlink", "data": [
                    {"timestamp": 1788972880000, "value": 64123.5, "full_accuracy_value": "64123.50000000"},
                    {"timestamp": 1788972881000, "value": 64125.1, "full_accuracy_value": "64125.10000000"}]}}
        out = parsers.parse_polybolt(snap)
        self.assertEqual(out[0][0], "spot"); self.assertAlmostEqual(out[1][2], 64125.1); self.assertEqual(out[1][3], "chainlink")
        live = {"v": 1, "channel": "price.crypto.twap", "seq": 3, "ts": 1788973002000,
                "payload": {"symbol": "btcusd", "value": 64120.7, "full_accuracy_value": "64120.70000000",
                            "timestamp": 1788973002000, "window_seconds": 60, "source": "chainlink"}}
        out = parsers.parse_polybolt(live)
        self.assertEqual(out, [("twap", 1788973002.0, 64120.7)])
        self.assertEqual(parsers.parse_polybolt({"op": "authed", "rid": "a1"})[0][0], "ack")
        self.assertEqual(parsers.parse_polybolt({"channel": "price.crypto", "payload": []}), [])
        eth = dict(live); eth["payload"] = dict(live["payload"], symbol="ethusd")
        self.assertEqual(parsers.parse_polybolt(eth), [])

    def test_legacy_and_binance(self):
        f = {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 1753314088421,
             "payload": {"symbol": "btc/usd", "timestamp": 1753314088395, "value": 67234.5}}
        self.assertEqual(parsers.parse_legacy_rtds(f)[0][2], 67234.5)
        self.assertEqual(parsers.parse_binance_trade({"p": "67000.10", "T": 1753314088000}), (1753314088.0, 67000.1))

    def test_gamma(self):
        d = {"slug": "btc-updown-5m-1", "conditionId": "0xabc", "clobTokenIds": json.dumps(["111", "222"]),
             "outcomes": json.dumps(["Up", "Down"]), "outcomePrices": json.dumps(["1", "0"]), "closed": True}
        m = parsers.parse_gamma_market(d)
        self.assertEqual((m["up"], m["down"], m["official_up"]), ("111", "222", True))
        d["outcomes"] = json.dumps(["Down", "Up"])
        m = parsers.parse_gamma_market(d)
        self.assertEqual((m["up"], m["down"], m["official_up"]), ("222", "111", False))
        d["closed"] = False; d["outcomePrices"] = json.dumps(["0.5", "0.5"])
        self.assertIsNone(parsers.parse_gamma_market(d)["official_up"])
        self.assertIsNone(parsers.parse_gamma_market({"clobTokenIds": "[]"}))

    def test_balance_positions(self):
        self.assertAlmostEqual(parsers.parse_balance({"balance": "2179236", "allowances": {}}), 2.179236)
        rows = parsers.parse_positions([{"title": "X", "outcome": "Up", "size": 10, "avgPrice": 0.07, "currentValue": 1.2, "cashPnl": 0.5}])
        self.assertEqual(rows[0]["outcome"], "Up")
        self.assertEqual(rows[0]["size"], 10)
        rows = parsers.parse_positions({"data": [{"title": "Y", "outcome": "Yes", "current_size": 2,
                                                 "avg_price": 0.4, "current_price": 0.5, "current_value": 1.0,
                                                 "total_pnl": 0.2, "redeemable": False}]})
        self.assertEqual(rows[0]["size"], 2)
        self.assertEqual(rows[0]["pnl"], 0.2)
        self.assertTrue(parsers.is_address("0x" + "a" * 40)); self.assertFalse(parsers.is_address("0x12"))


class TestBook(unittest.TestCase):
    def test_events(self):
        b = Book()
        evs = parse_market_events({"event_type": "book", "asset_id": "1", "timestamp": "1788973000123",
                                   "bids": [{"price": "0.07", "size": "100"}, {"price": "0.06", "size": "50"}],
                                   "asks": [{"price": "0.09", "size": "30"}]})
        _, aid, bids, asks, ts = evs[0]
        b.snapshot(bids, asks, ts)
        self.assertEqual((b.best_bid(), b.best_ask()), (0.07, 0.09))
        evs = parse_market_events({"event_type": "price_change", "timestamp": "1788973001000",
                                   "price_changes": [{"asset_id": "1", "price": "0.07", "size": "0", "side": "BUY"},
                                                     {"asset_id": "1", "price": "0.08", "size": "20", "side": "SELL"}]})
        for e in evs:
            if e[0] == "change":
                b.update(e[2], e[3], e[4])
        self.assertEqual((b.best_bid(), b.best_ask()), (0.06, 0.08))
        evs = parse_market_events([{"event_type": "last_trade_price", "asset_id": "1", "price": "0.06", "size": "5", "side": "SELL", "timestamp": "1788973002000"}])
        self.assertEqual(evs[0][:3], ("trade", "1", 0.06))


class TestAuth(unittest.TestCase):
    def test_l2_signature(self):
        secret = base64.urlsafe_b64encode(b"super-secret-key-bytes").decode()
        sig = clobauth.l2_signature(secret, 1700000000, "get", "/balance-allowance")
        exp = base64.urlsafe_b64encode(hmac.new(b"super-secret-key-bytes", b"1700000000GET/balance-allowance", hashlib.sha256).digest()).decode()
        self.assertEqual(sig, exp)
        h = clobauth.l2_headers("0xabc", {"apiKey": "k", "secret": secret, "passphrase": "p"}, 1700000000, "GET", "/x")
        self.assertEqual(set(h), {"POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_API_KEY", "POLY_PASSPHRASE"})

    def test_typed_data_shape(self):
        td = clobauth.l1_typed_data("0x" + "1" * 40, 1700000000, 0)
        self.assertEqual(td["domain"], {"name": "ClobAuthDomain", "version": "1", "chainId": 137})
        self.assertEqual(td["message"]["message"], "This message attests that I control the given wallet")

    def test_l1_sign_recovers_address(self):
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError:
            self.skipTest("eth-account not installed")
        pk = "0x" + "11" * 32
        addr, sig = clobauth.sign_l1(pk, 1700000000, 0)
        signable = encode_typed_data(full_message=clobauth.l1_typed_data(addr, 1700000000, 0))
        self.assertEqual(Account.recover_message(signable, signature=sig), addr)


class TestStore(unittest.TestCase):
    def test_secretbox(self):
        d = tempfile.mkdtemp()
        try:
            for pw in (None, "pw123"):
                sb = SecretBox(os.path.join(d, "s.json"), pw)
                sb.save({"apiKey": "k", "secret": "s"})
                self.assertEqual(sb.load()["apiKey"], "k")
                self.assertEqual(oct(os.stat(sb.path).st_mode & 0o777), "0o600")
                with open(sb.path) as fh:
                    raw = fh.read()
                self.assertEqual("secret" in raw and '"s"' in raw, pw is None)
            with self.assertRaises(Exception):
                SecretBox(sb.path, "wrong").load()
        finally:
            shutil.rmtree(d)

    def test_reset_archive(self):
        d = tempfile.mkdtemp()
        try:
            st = DataStore(d)
            st.save_orders([{"id": 1, "slug": "a", "side": "Up", "price": .05, "shares": 100, "cost": 0, "filled": 0}])
            st.append_event({"x": 1})
            dest = st.reset_records(archive=True)
            self.assertTrue(os.path.exists(os.path.join(dest, "paper_orders.json")))
            self.assertEqual(st.load_orders(), [])
            self.assertFalse(os.path.exists(st.events_path))
            st.save_orders([{"id": 2, "slug": "a", "side": "Up", "price": .05, "shares": 100, "cost": 0, "filled": 0}])
            self.assertIsNone(st.reset_records(archive=False))
            self.assertEqual(st.load_orders(), [])
        finally:
            shutil.rmtree(d)


def mkbook(bid, bid_sz, ask, ask_sz=50):
    b = Book()
    b.snapshot([{"price": str(bid), "size": str(bid_sz)}], [{"price": str(ask), "size": str(ask_sz)}])
    return b


def mkctx(slug="s", tau=60.0, p_up=0.9, ub=None, db=None):
    return {"slug": slug, "tau": tau, "B": 100.0, "spot": 101.0, "sigma_rel": 1e-4, "p_up": p_up,
            "tokens": {"Up": "U", "Down": "D"}, "books": {"Up": ub or mkbook(0.9, 100, 0.92), "Down": db or mkbook(0.07, 200, 0.09)},
            "binance": None, "basis": None}


CHEAP_SIDE = dict(min_price=0.02, max_price=0.10, max_remaining=420.0, discount=0.85, loss_mult=1.0,
                  min_gap_usd=0.0, max_sigma5m=0.0, max_disagree=0.0, fast_move_usd=0.0)


class TestPaper(unittest.TestCase):
    def eng(self, **kw):
        cfg = StrategyConfig(enabled=True, **dict(CHEAP_SIDE, **kw))
        return PaperEngine(cfg)

    def test_place_and_fill_queue(self):
        e = self.eng()
        msg = e.evaluate(mkctx(p_up=0.9), 1000.0)    # Down model p = 0.10 -> p_adj .085 >= .07+.02? no
        self.assertIn("优势不足", msg)
        e = self.eng(discount=1.0, margin=0.01)
        msg = e.evaluate(mkctx(p_up=0.9), 1000.0)
        self.assertIn("已挂单 Down", msg)
        o = e.orders[0]
        self.assertEqual((o.price, o.queue_ahead0), (0.07, 200.0))
        self.assertAlmostEqual(o.shares, math.floor(5 / 0.07 * 100) / 100)
        e.on_trade("D", 0.07, 150, 1001.0)            # eats queue partly
        self.assertEqual(o.filled, 0)
        self.assertEqual(o.queue_ahead, 50.0)
        e.on_trade("D", 0.07, 100, 1001.1)            # 50 queue + 50 to us
        self.assertAlmostEqual(o.filled, 50.0)
        self.assertEqual(o.status, "partial")
        e.on_trade("D", 0.06, 10, 1002.0)             # print below our bid -> rest fills
        self.assertEqual(o.status, "filled")
        self.assertAlmostEqual(o.filled, o.shares)
        # latency: print right after placement is ignored
        e2 = self.eng(discount=1.0, margin=0.01)
        e2.evaluate(mkctx(p_up=0.9), 2000.0)
        e2.on_trade("D", 0.06, 1000, 2000.1)
        self.assertEqual(e2.orders[0].filled, 0)

    def test_settle_pnl_and_stats(self):
        e = self.eng(discount=1.0, margin=0.01)
        e.evaluate(mkctx(slug="w1", p_up=0.9), 1000.0)
        o = e.orders[0]
        e.on_trade("D", 0.06, 1000, 1005.0)
        cost = o.shares * 0.07
        e.settle("w1", up_won=False, final_value=99.0)    # Down wins
        self.assertTrue(o.side_won)
        self.assertAlmostEqual(o.pnl, o.shares - cost + o.rebate, places=3)
        e.evaluate(mkctx(slug="w2", p_up=0.9), 2000.0)
        o2 = e.orders[1]
        e.on_trade("D", 0.06, 1000, 2005.0)
        e.settle("w2", up_won=True, final_value=101.0)    # Down loses
        self.assertFalse(o2.side_won)
        self.assertAlmostEqual(o2.pnl, -o2.shares * 0.07 + o2.rebate, places=3)
        st = e.stats()
        self.assertEqual((st["filled"], st["wins"]), (2, 1))
        # reconcile: official says Up won in w1 -> pnl flips
        e.reconcile("w1", official_up=True)
        self.assertFalse(o.side_won)
        self.assertLess(o.pnl, 0)

    def test_unfilled_recorded_with_outcome(self):
        e = self.eng(discount=1.0, margin=0.01)
        e.evaluate(mkctx(slug="w1", p_up=0.9), 1000.0)
        e.settle("w1", up_won=False, final_value=1.0)
        o = e.orders[0]
        self.assertEqual((o.status, o.filled, o.pnl, o.side_won), ("expired", 0, 0.0, True))
        self.assertEqual(e.stats()["unfilled_flip_rate"], 1.0)

    def test_ttl_edge_and_limits(self):
        e = self.eng(discount=1.0, margin=0.01, ttl_s=10)
        e.evaluate(mkctx(p_up=0.9), 1000.0)
        e.manage(mkctx(p_up=0.9), 1011.0)
        self.assertEqual(e.orders[0].status, "cancelled")
        self.assertEqual(e.orders[0].cancel_reason, "ttl")
        self.assertIn("上限", e.evaluate(mkctx(p_up=0.9), 1012.0))        # 1 order per window
        e = self.eng(discount=1.0, margin=0.01)
        e.evaluate(mkctx(p_up=0.9), 1000.0)
        e.manage(mkctx(p_up=0.99), 1001.0)                              # model now says Down p=0.01
        self.assertEqual(e.orders[0].cancel_reason, "edge_lost")
        e = self.eng(discount=1.0, margin=0.01, max_exposure=3.0)
        self.assertIn("敞口", e.evaluate(mkctx(p_up=0.9), 1000.0))
        e = self.eng()
        self.assertIn("不在入场时段", e.evaluate(mkctx(tau=500), 1000.0))

    def test_book_cross_fills(self):
        e = self.eng(discount=1.0, margin=0.01)
        e.evaluate(mkctx(p_up=0.9), 1000.0)
        crossed = mkctx(p_up=0.9, db=mkbook(0.06, 10, 0.07))
        e.manage(crossed, 1001.0)
        self.assertEqual(e.orders[0].status, "filled")

    def test_restart_marks_aborted_and_reset(self):
        e = self.eng(discount=1.0, margin=0.01)
        e.evaluate(mkctx(p_up=0.9), 1000.0)
        rows = e.dump()
        e2 = self.eng()
        e2.load(rows)
        self.assertEqual(e2.orders[0].status, "aborted")
        self.assertEqual(e2.next_id, 2)
        e2.reset()
        self.assertEqual((e2.orders, e2.next_id), ([], 1))
        self.assertIn("id", orders_to_csv(rows).splitlines()[0])

    def test_favourite_defaults_and_filters(self):
        fav = lambda **kw: PaperEngine(StrategyConfig(enabled=True, **kw))
        ub = mkbook(0.85, 100, 0.86)
        ctx = lambda **kw: {**mkctx(p_up=0.97, ub=ub), "gap": 30.0, "bn_move": 0.0, **kw}
        e = fav()
        self.assertIn("已挂单 Up @0.85", e.evaluate(ctx(), 1000.0))   # p_adj = 1-0.03*1.5 = .955 >= .87
        self.assertIn("领先", fav().evaluate(ctx(gap=10.0), 1000.0))
        self.assertIn("急动", fav().evaluate(ctx(bn_move=-30.0), 1000.0))
        self.assertIn("波动过大", fav().evaluate(dict(ctx(), sigma_rel=0.01), 1000.0))
        self.assertIn("乐观太多", fav().evaluate(dict(ctx(), p_up=0.999, books={"Up": mkbook(0.75, 100, 0.76),
                                                                             "Down": mkbook(0.2, 10, 0.25)}), 1000.0))
        self.assertIn("优势不足", fav().evaluate(dict(ctx(), p_up=0.9), 1000.0))  # .85 < .87
        e.manage(ctx(bn_move=-40.0), 1001.0)
        self.assertEqual(e.orders[0].cancel_reason, "fast_move")

    def test_buckets(self):
        e = PaperEngine(StrategyConfig(enabled=True))
        ub = mkbook(0.85, 1, 0.86)
        ctx = lambda slug: dict(mkctx(slug=slug, p_up=0.97, ub=ub), gap=30.0, bn_move=0.0)
        for i, up in enumerate((True, True, False)):
            e.evaluate(ctx(f"w{i}"), 1000.0 + i * 300)
            e.on_trade("U", 0.84, 1000, 1001.0 + i * 300)
            e.settle(f"w{i}", up_won=up, final_value=None)
        b = e.stats()["buckets"]
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0]["lo"], b[0]["n"], b[0]["wins"]), (0.85, 3, 2))
        self.assertAlmostEqual(b[0]["avg_price"], 0.85)

    def test_config_clamps(self):
        c = StrategyConfig.from_dict({"unit_usd": "5", "min_price": "0.02", "max_price": "0.1", "enabled": "true", "queue_model": "bogus", "junk": 1})
        self.assertEqual((c.unit_usd, c.max_price, c.enabled, c.queue_model), (5.0, 0.1, True, "conservative"))


class TestRunnerEndToEnd(unittest.TestCase):
    def test_full_window(self):
        d = tempfile.mkdtemp()
        try:
            store = DataStore(d)
            hub = Hub()
            eng = PaperEngine(StrategyConfig(enabled=True, **dict(CHEAP_SIDE, discount=1.0, margin=0.01, min_remaining=10,
                                                                  max_remaining=120, sigma5m=0.0015, use_realized_vol=False)))
            run = Runner(hub, eng, store)
            S = 1_800_000_000 - (1_800_000_000 % 300)      # a window start
            T = S + 300
            run.set_market(S, {"up": "U", "down": "D"})
            run.set_market(S + 300, {"up": "U2", "down": "D2"})
            price = lambda t: 100000.0 + (0 if t < S else min((t - S) * 0.4, 80.0)) # drifts up 80 over the window
            # feed 1 Hz spot + twap + books from S-130 to T+5
            now = S - 130.0
            while now <= T + 4:
                px = price(now)
                hub.on_spot(now, px, "chainlink")
                # TWAP60 stream value = mean of price over last 60 s
                tw = sum(price(now - k) for k in range(60)) / 60.0
                hub.on_twap(now, tw)
                hub.on_binance(now, px + 5.0)
                if now >= S - 5:
                    tau = T - now
                    # Down token gets cheap late: bid 0.07, queue 120
                    down_bid = 0.07 if tau < 150 else 0.30
                    hub.on_market_events([
                        ("book", "U", [{"price": "0.90", "size": "100"}], [{"price": "0.93", "size": "50"}], now),
                        ("book", "D", [{"price": str(down_bid), "size": "120"}], [{"price": str(down_bid + 0.02), "size": "50"}], now),
                    ])
                run.tick(now)
                # a big sell print through our bid shortly after placement (tau ~ 110)
                if abs((T - now) - 110) < 0.5:
                    hub.on_market_events([("trade", "D", 0.07, 300.0, "SELL", now), ("trade", "D", 0.06, 50.0, "SELL", now + 0.4)])
                now += 1.0
            # after T+2 settlement should have happened
            self.assertIn(S, run.B)
            self.assertAlmostEqual(run.B[S]["value"], sum(price(S - k) for k in range(60)) / 60.0, delta=0.5)
            self.assertEqual(len(eng.orders), 1)
            o = eng.orders[0]
            self.assertEqual(o.side, "Down")
            self.assertTrue(o.settled)
            self.assertGreater(o.filled, 0)
            self.assertEqual(o.side_won, False)               # price rose -> Up won -> our Down lost
            self.assertLess(o.pnl, 0)
            self.assertTrue(run.final[S]["up"])
            # series saved
            self.assertTrue(os.path.exists(os.path.join(d, "series", f"btc-updown-5m-{S}.json")))
            # official reconcile (agrees)
            run.set_official(S, True)
            self.assertTrue(eng.orders[0].official_side_won is False)
        finally:
            shutil.rmtree(d)


class TestAccountCode(unittest.TestCase):
    def test_signature_type_from_runtime(self):
        self.assertEqual(signature_type_from_code("0x"), 0)
        self.assertEqual(signature_type_from_code(""), 0)
        self.assertEqual(signature_type_from_code(POLY_PROXY_CODE), 1)
        self.assertEqual(signature_type_from_code("0x" + SAFE_PROXY_CODE), 2)
        self.assertEqual(signature_type_from_code(DEPOSIT_CODE_PREFIX + "ab" * 20), 3)
        self.assertEqual(signature_type_from_code("0x" + DEPOSIT_BEACON_PREFIX + "00" * 64), 3)
        self.assertEqual(signature_type_from_code(EIP1167_PREFIX + "ab" * 20 + EIP1167_SUFFIX), 1)
        self.assertIsNone(signature_type_from_code("deadbeef"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPolyBoltSpec(unittest.TestCase):
    """Frames must match https://ws-live-v2.polymarket.com/asyncapi.json (checked by hand against the spec)."""
    def test_subscribe_frame(self):
        from pm5m.feeds import polybolt_subscribe_frame
        f = polybolt_subscribe_frame("s1")
        self.assertEqual(f["op"], "subscribe")
        chans = {s["channel"]: s["filter"] for s in f["subscriptions"]}
        self.assertEqual(chans["price.crypto"], {"symbol": "btcusd"})
        self.assertEqual(chans["price.crypto.twap"], {"symbol": "btcusd", "window_seconds": 60})
        self.assertNotIn("provider", json.dumps(f))

    def test_auth_frame(self):
        from pm5m.feeds import polybolt_auth_frame
        f = polybolt_auth_frame({"apiKey": "k", "secret": "s", "passphrase": "p"}, "a1")
        self.assertEqual(f, {"op": "auth", "rid": "a1", "auth": {"apiKey": "k", "secret": "s", "passphrase": "p"}})

    def test_twap_and_spot_frames_parse(self):
        twap_batch = {"v": 1, "channel": "price.crypto.twap", "seq": 1, "ts": 1788973000000, "snapshot": True,
                      "payload": {"symbol": "btcusd", "source": "chainlink", "window_seconds": 60,
                                  "data": [{"value": 64120.7, "full_accuracy_value": "64120.70000000", "timestamp": 1788972990000}]}}
        ev = parsers.parse_polybolt(twap_batch)
        self.assertEqual(ev[0][0], "twap")
        self.assertAlmostEqual(ev[0][2], 64120.7)
        spot_live = {"v": 1, "channel": "price.crypto", "seq": 9, "ts": 1788973001000,
                     "payload": {"symbol": "btcusd", "value": 64121.0, "full_accuracy_value": "64121.00000000",
                                 "timestamp": 1788973001000, "source": "pyth"}}
        ev = parsers.parse_polybolt(spot_live)
        self.assertEqual((ev[0][0], ev[0][3]), ("spot", "pyth"))
        self.assertEqual(parsers.parse_polybolt({"op": "error", "code": "auth_invalid", "rid": "a1"})[0][0], "ack")
