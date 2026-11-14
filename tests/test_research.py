import json, os, shutil, sys, tempfile, unittest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pm5m.research import evaluate, features, rules as R, synth


class TestLogistic(unittest.TestCase):
    def test_recovers_coefficients(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(20000, 2))
        true = np.array([0.3, 1.0, -0.5])
        p = 1 / (1 + np.exp(-(true[0] + X @ true[1:])))
        y = (rng.random(20000) < p).astype(float)
        w = evaluate.fit_logistic(X, y, l2=1e-6)
        self.assertTrue(np.allclose(w, true, atol=0.06), w)


class TestNoLookahead(unittest.TestCase):
    def test_features_ignore_future(self):
        rules = R.Rules()
        ws = synth.make_windows(3, seed=5)
        sigma = features.sigma_global(ws)
        w = ws[1]
        i = w["t"].index(200.0)  # t=200 -> tau=100, inside band
        base = features.row_features(w, i, sigma, rules)
        self.assertIsNotNone(base)
        w2 = json.loads(json.dumps(w))
        for k in range(i + 1, len(w2["t"])):
            w2["spot"][k] = w2["spot"][k] * 1.05      # perturb the future only
            w2["binance"][k] = w2["binance"][k] * 0.95
            w2["up_ask"][k] = 0.5
        again = features.row_features(w2, i, sigma, rules)
        for key in ("p_base", "mom", "basis", "ua", "da"):
            self.assertAlmostEqual(base[key], again[key], places=12, msg=key)


class TestRulesDigest(unittest.TestCase):
    def test_freeze_and_tamper(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "r.json")
            R.freeze(R.Rules(), p)
            self.assertEqual(R.load(p).digest(), R.Rules().digest())
            with open(p) as f:
                doc = json.load(f)
            doc["min_edge"] = 0.0
            with open(p, "w") as f:
                json.dump(doc, f)
            with self.assertRaises(ValueError):
                R.load(p)
        finally:
            shutil.rmtree(d)


class TestPipeline(unittest.TestCase):
    def test_runs_end_to_end_on_synthetic(self):
        ws = synth.make_windows(400, seed=3, mom_coef=0.0)
        res = evaluate.run_pipeline(ws, R.Rules(min_trades=10, bootstrap=200))
        self.assertGreater(res["n_test_rows"], 50)
        self.assertEqual(len(res["sims"]), 3)
        null = res["sims"]["市场中间价（零优势对照，各代币自身中间价）"]
        self.assertEqual(null["n"], 0)   # ask >= mid + spread/2 > mid, so mid-based edge is always below fees
        text = evaluate.render(res, R.Rules())
        self.assertIn("结论", text)

    def test_trade_sim_fees_and_outcome(self):
        rules = R.Rules(min_edge=0.0, price_min=0.01, price_max=0.99)
        rows = [{"S": 0, "t": 10, "ua": 0.5, "da": 0.1, "y": 0, "p_hat": 0.3}]
        tr = evaluate.trade_sim(rows, lambda r: (r["p_hat"], 1 - r["p_hat"]), rules)
        self.assertEqual(len(tr), 1)
        t = tr[0]
        self.assertEqual(t["side"], "Down")                 # Down: 0.7 - 0.1 - fee > Up edge
        fee = 0.07 * 0.1 * 0.9
        self.assertAlmostEqual(t["pnl"], 1.0 - 0.1 - fee)   # y=0 -> Down wins


if __name__ == "__main__":
    unittest.main(verbosity=2)
