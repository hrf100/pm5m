import math, os, sys, unittest
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pm5m import model


def mc_p_up(now, S, known, spot, sig_rel, N=20000, seed=3):
    """Monte Carlo on a 1 Hz grid: known = {t: px} for t <= now (relative to S); future = random walk."""
    rng = np.random.default_rng(seed)
    T = S + 300
    ts = np.arange(S - 60, T)
    steps = int(T - now)
    s_abs = sig_rel * spot
    inc = s_abs * rng.standard_normal((N, steps))
    fut = spot + np.cumsum(inc, axis=1)                      # prices at now+1 .. T
    P = np.empty((N, len(ts)))
    for j, t in enumerate(ts):
        if t <= now:
            P[:, j] = known[int(t)]
        else:
            P[:, j] = fut[:, int(t - now) - 1]
    B = P[:, :60].mean(axis=1)                                # [S-60, S)
    F = P[:, -60:].mean(axis=1)                               # [T-60, T)
    return float(np.mean(F >= B))


class TestGeneralModel(unittest.TestCase):
    def test_in_window_sd_matches_old_formula(self):
        s_rel, spot = 8.66e-5, 100.0
        for tau in (120.0, 75.0, 45.0, 10.0):
            now = 300.0 - tau
            hist = [(t, spot) for t in range(-70, 301) if t <= now]
            old = model.up_probability(99.8, now, 300.0, spot, [(t, spot) for t in range(235, 301) if t <= now], s_rel)
            new = model.p_up_general(now, 0.0, spot, hist, s_rel)
            self.assertAlmostEqual(new["sd"], old["sd"], places=9, msg=f"tau={tau}")

    def test_pre_open_matches_monte_carlo(self):
        rng = np.random.default_rng(11)
        sig = 8.66e-5
        # build a known history from -120 to now=-30 (relative to S=0), with a small trend
        known = {}
        px = 100000.0
        for t in range(-120, -29):
            px *= math.exp(sig * rng.standard_normal() + 2e-6)
            known[t] = px
        now = -30.0
        spot = known[-30]
        samples = [(t, known[t]) for t in known]
        ana = model.p_up_general(now, 0.0, spot, samples, sig)
        mc = mc_p_up(now, 0.0, known, spot, sig)
        self.assertAlmostEqual(ana["p_up"], mc, delta=0.02)
        self.assertFalse(ana["B_known"])

    def test_in_window_matches_monte_carlo(self):
        rng = np.random.default_rng(12)
        sig = 8.66e-5
        known = {}
        px = 100000.0
        for t in range(-70, 201):
            px *= math.exp(sig * rng.standard_normal())
            known[t] = px
        now = 200.0
        spot = known[200]
        samples = [(t, known[t]) for t in known]
        ana = model.p_up_general(now, 0.0, spot, samples, sig)
        mc = mc_p_up(now, 0.0, known, spot, sig)
        self.assertAlmostEqual(ana["p_up"], mc, delta=0.02)

    def test_variance_decreases_towards_end(self):
        sig, spot = 8.66e-5, 100.0
        sds = []
        for now in (-120, -60, 0, 120, 240, 280):
            hist = [(t, spot) for t in range(-70, 301) if t <= now]
            sds.append(model.p_up_general(now, 0.0, spot, hist, sig)["sd"])
        self.assertTrue(all(a >= b - 1e-12 for a, b in zip(sds, sds[1:])), sds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
