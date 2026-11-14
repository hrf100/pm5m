"""Handler-level smoke tests. Uses a tiny aiohttp stub when aiohttp is not installed,
so the Python logic of app.py can be exercised without network access."""
import asyncio, base64, json, os, shutil, sys, tempfile, time, types, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import aiohttp  # noqa
    from aiohttp import web  # noqa
except ImportError:
    aio = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")

    class Response:
        def __init__(self, text=None, status=200, headers=None, content_type=None, body=None):
            self.text, self.status, self.headers = text, status, headers or {}

    def json_response(d, status=200):
        return Response(json.dumps(d), status)

    class Application:
        def __init__(self, **kw):
            self.router = types.SimpleNamespace(add_get=lambda *a, **k: None, add_post=lambda *a, **k: None,
                                                add_delete=lambda *a, **k: None)
            self.on_startup, self.on_cleanup = [], []

    web.middleware = lambda f: f
    web.Response, web.json_response, web.Application = Response, json_response, Application
    web.FileResponse = lambda p: Response("file:" + p)
    web.Request = object
    web.run_app = lambda *a, **k: None
    aio.web = web
    for n in ("ClientSession", "ClientTimeout", "ClientWebSocketResponse"):
        setattr(aio, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
    aio.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"], sys.modules["aiohttp.web"] = aio, web

from pm5m.app import App, mask  # noqa: E402
from pm5m.book import Book  # noqa: E402


class Req:
    def __init__(self, body=None, headers=None, remote="127.0.0.1", match=None, method="POST"):
        self._b = body or {}
        self.headers = headers or {}
        self.remote, self.secure, self.method = remote, False, method
        self.match_info = match or {}

    async def json(self):
        return self._b


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def body(resp):
    return json.loads(resp.text)


class TestHandlers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["PM5M_PASSWORD"] = "pw"
        self.app = App(self.d)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_change_password_persists_over_env(self):
        r = run(self.app.h_password(Req({"current": "wrong", "new": "newpassword"})))
        self.assertEqual(r.status, 403)
        r = run(self.app.h_password(Req({"current": "pw", "new": "short"})))
        self.assertEqual(r.status, 400)
        r = run(self.app.h_password(Req({"current": "pw", "new": "newpassword"})))
        self.assertEqual(r.status, 200)
        with open(os.path.join(self.d, "web_password.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "newpassword")
        again = App(self.d)          # PM5M_PASSWORD is still "pw"; the file wins
        self.assertEqual(again.password, "newpassword")

    def test_auth(self):
        async def ok_handler(r):
            return sys.modules["aiohttp"].web.json_response({"ok": 1})
        good = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
        r = run(self.app.auth_mw(Req(headers={}, method="GET"), ok_handler))
        self.assertEqual(r.status, 401)
        r = run(self.app.auth_mw(Req(headers=good, method="GET"), ok_handler))
        self.assertEqual(r.status, 200)
        r = run(self.app.auth_mw(Req(headers=good, method="POST"), ok_handler))     # CSRF header missing
        self.assertEqual(r.status, 403)
        good2 = dict(good, **{"X-Requested-With": "pm5m"})
        r = run(self.app.auth_mw(Req(headers=good2, method="POST"), ok_handler))
        self.assertEqual(r.status, 200)
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        for _ in range(10):
            run(self.app.auth_mw(Req(headers=bad, method="GET"), ok_handler))
        r = run(self.app.auth_mw(Req(headers=good, method="GET"), ok_handler))
        self.assertEqual(r.status, 429)

    def test_state_series_settings_reset(self):
        a = self.app
        now = time.time()
        S = int(now // 300) * 300
        a.runner.set_market(S, {"up": "U", "down": "D"})
        t0 = S - 70
        t = t0
        while t <= now:                       # continuous history from S-70 to now (model needs it)
            a.hub.on_spot(t, 100000.0, "chainlink")
            if t >= S - 60:
                a.hub.on_twap(t, 100000.0)    # Chainlink TWAP level (settlement reference)
            t += 1.0
        a.hub.on_binance(now, 100005.0)
        for tok, bid in (("U", 0.9), ("D", 0.07)):
            a.hub.book(tok).snapshot([{"price": str(bid), "size": "100"}], [{"price": str(bid + .02), "size": "50"}])
        a.runner.B[S] = {"value": 99950.0, "source": "test", "ts": S}
        a.runner.tick(now)
        st = body(run(a.h_state(Req(method="GET"))))
        self.assertEqual(st["mode"], "PAPER")
        self.assertEqual(st["books"]["Down"]["bid"], 0.07)
        self.assertIsNotNone(st["model"])
        self.assertFalse(st["wallet"]["has_creds"])
        se = body(run(a.h_series(Req(method="GET"))))
        self.assertEqual(len(se["t"]), 1)
        # settings
        r = body(run(a.h_settings_post(Req({"strategy": {"unit_usd": 7, "max_price": 0.12}, "binance_enabled": False}))))
        self.assertEqual((r["strategy"]["unit_usd"], r["strategy"]["max_price"]), (7.0, 0.12))
        self.assertFalse(a.settings["binance_enabled"])
        self.assertTrue(os.path.exists(a.store.settings_path))
        # start/stop
        run(a.h_strategy(Req(match={"action": "start"})))
        self.assertTrue(a.engine.cfg.enabled)
        run(a.h_strategy(Req(match={"action": "stop"})))
        self.assertFalse(a.engine.cfg.enabled)
        # create an order then export / reset
        a.engine.cfg.enabled, a.engine.cfg.discount, a.engine.cfg.margin = True, 1.0, 0.0
        a.engine.cfg.min_remaining, a.engine.cfg.max_remaining = 0, 400
        a.runner.tick(now + 0.3)
        self.assertTrue(a.engine.orders, a.runner.strategy_msg)
        a.flush()
        self.assertTrue(os.path.exists(a.store.orders_path))
        csv_resp = run(a.h_export(Req(method="GET")))
        self.assertIn("slug", csv_resp.text if hasattr(csv_resp, "text") else "slug")
        r = body(run(a.h_reset(Req({"confirm": True, "archive": True}))))
        self.assertTrue(r["ok"])
        self.assertEqual(a.engine.orders, [])
        self.assertEqual(run(a.h_reset(Req({}))).status, 400)

    def test_wallet_validation_and_transport_gate(self):
        a = self.app
        r = run(a.h_wallet_post(Req({"signature_type": 9})))
        self.assertEqual(r.status, 400)
        r = run(a.h_wallet_post(Req({"signature_type": 0, "funder": "0x123"})))
        self.assertEqual(r.status, 400)
        r = run(a.h_wallet_post(Req({})))  # nothing to save
        self.assertEqual(r.status, 400)
        # manual credentials path (no private key involved)
        class FakeFeeds:
            errors = {}
            async def reconnect_polybolt(self): pass
        class FakeApi:
            async def resolve_account(self, signer):
                return {"funder": "0x" + "cd" * 20, "sig_type": 2}
            async def clob_balance(self, *a): return 12.5
            async def onchain_pusd(self, a): return 7.0
            async def value(self, a): return 3.0
            async def positions(self, a): return []
        a.feeds, a.api = FakeFeeds(), FakeApi()
        signer = "0x" + "ab" * 20
        async def go():
            r = await a.h_wallet_post(Req({"signature_type": 2, "signer_address": signer, "funder": "0x" + "cd" * 20,
                                           "api_key": "k", "api_secret": "s", "api_passphrase": "p"}))
            await asyncio.sleep(0.05)
            return r
        r = run(go())
        self.assertEqual(r.status, 200)
        self.assertTrue(a.creds())
        raw = open(a.store.secrets.path).read()
        self.assertNotIn("private_key", raw)
        self.assertEqual(oct(os.stat(a.store.secrets.path).st_mode & 0o777), "0o600")
        run(a.refresh_wallet())
        self.assertEqual(a.wallet_info["clob_balance"], 12.5)
        st = body(run(a.h_state(Req(method="GET"))))
        self.assertEqual(st["wallet"]["onchain_pusd"], 7.0)
        self.assertNotIn('"secret"', json.dumps(st))      # secrets never leave the server
        run(a.h_wallet_delete(Req(method="DELETE")))
        self.assertFalse(a.creds())

    def test_mask(self):
        self.assertEqual(mask("abcdefghijklmnop", 4), "abcd…mnop")
        self.assertEqual(mask(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPublicAccess(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["PM5M_PASSWORD"] = "pw"
        os.environ["PM5M_TRUST_PROXY"] = "1"
        os.environ["PM5M_IP_ALLOWLIST"] = "203.0.113.7"
        self.app = App(self.d)

    def tearDown(self):
        for k in ("PM5M_TRUST_PROXY", "PM5M_IP_ALLOWLIST"):
            os.environ.pop(k, None)
        shutil.rmtree(self.d)

    def test_client_ip_uses_last_forwarded_hop(self):
        r = Req(headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
        self.assertEqual(self.app.client_ip(r), "203.0.113.7")

    def test_allowlist_blocks_others(self):
        good = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(), "X-Forwarded-For": "203.0.113.7"}
        bad = dict(good, **{"X-Forwarded-For": "198.51.100.9"})
        async def ok(r): return sys.modules["aiohttp"].web.json_response({"ok": 1})
        self.assertEqual(run(self.app.auth_mw(Req(headers=good, method="GET"), ok)).status, 200)
        self.assertEqual(run(self.app.auth_mw(Req(headers=bad, method="GET"), ok)).status, 403)

    def test_https_required_behind_proxy_for_private_key(self):
        self.app.allow_web_key = True
        r = Req({"signature_type": 0, "private_key": "0x" + "11" * 32}, headers={"X-Forwarded-Proto": "http"}, remote="127.0.0.1")
        self.assertFalse(self.app.transport_ok(r))
        r = Req({}, headers={"X-Forwarded-Proto": "https"}, remote="127.0.0.1")
        self.assertTrue(self.app.transport_ok(r))

    def test_page_accepts_private_key(self):
        from pm5m import clobauth
        self.app.allow_web_key = False
        class FakeFeeds:
            errors = {}
            async def reconnect_polybolt(self): pass
        class FakeApi:
            async def derive_creds(self, pk):
                return {"apiKey": "k", "secret": "s", "passphrase": "p"}
            async def resolve_account(self, signer):
                return {"funder": "0x" + "cd" * 20, "sig_type": 3}
            async def onchain_pusd(self, a): return 0
            async def value(self, a): return 0
            async def positions(self, a): return []
            async def clob_balance(self, *a): return 0
        self.app.feeds, self.app.api = FakeFeeds(), FakeApi()
        orig = clobauth.address_from_key
        clobauth.address_from_key = lambda pk: "0x" + "ab" * 20
        try:
            r = run(self.app.h_wallet_post(Req({"private_key": "0x" + "11" * 32}, remote="8.8.8.8")))
        finally:
            clobauth.address_from_key = orig
        self.assertEqual(r.status, 200)
        self.assertEqual(self.app.secrets["funder"], "0x" + "cd" * 20)
        self.assertNotIn("private_key", open(self.app.store.secrets.path).read())

    def test_sys_endpoint(self):
        r = run(self.app.h_sys(Req(method="GET")))
        body = json.loads(r.text)
        self.assertIn("stats", body)
        self.assertIn("totals_since_start", body)
