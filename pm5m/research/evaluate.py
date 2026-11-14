"""Logistic recalibration, time-ordered out-of-sample evaluation, taker-edge simulation, report."""
import math
from typing import Callable, Dict, List

import numpy as np

from .features import build_rows, sigma_global


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def design(rows: List[dict]) -> np.ndarray:
    return np.array([[logit(r["p_base"]), r["mom"], r["basis"]] for r in rows], dtype=float)


def _aug(X):
    return np.column_stack([np.ones(len(X)), X])


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-3, iters: int = 100) -> np.ndarray:
    A = _aug(X)
    w = np.zeros(A.shape[1])
    R = l2 * np.eye(A.shape[1])
    R[0, 0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(A @ w, -30, 30)))
        g = A.T @ (p - y) + R @ w
        H = (A * (p * (1 - p))[:, None]).T @ A + R
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(_aug(X) @ w, -30, 30)))


def trade_sim(rows: List[dict], qfun: Callable[[dict], tuple], rules) -> List[dict]:
    """qfun(row) -> (q_up, q_down): the probability estimate for each token.
    Buy the side whose edge (q - ask - fee) is largest and above threshold. One trade per window."""
    trades, done = [], set()
    for r in rows:
        if r["S"] in done:
            continue
        q_up, q_dn = qfun(r)
        best = None
        for side, ask, q in (("Up", r["ua"], q_up), ("Down", r["da"], q_dn)):
            if not (rules.price_min <= ask <= rules.price_max):
                continue
            fee = rules.taker_fee_rate * ask * (1 - ask)
            edge = q - ask - fee
            if edge > rules.min_edge and (best is None or edge > best[0]):
                best = (edge, side, ask, fee)
        if best:
            edge, side, ask, fee = best
            won = (r["y"] == 1) == (side == "Up")
            trades.append({"S": r["S"], "t": r["t"], "side": side, "ask": ask, "fee": fee, "edge": edge,
                           "won": won, "pnl": (1.0 if won else 0.0) - ask - fee})
            done.add(r["S"])
    return trades


def summarize(trades: List[dict], rules) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "verdict": "无交易信号"}
    pnl = np.array([t["pnl"] for t in trades])
    rng = np.random.default_rng(rules.seed)
    boots = rng.choice(pnl, size=(rules.bootstrap, n), replace=True).mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    if n < rules.min_trades:
        verdict = f"样本不足（{n} < 预设下限 {rules.min_trades}），结论无效"
    elif lo > 0:
        verdict = "候选优势：95% 区间全部为正，需在之后的新数据上重新确认"
    else:
        verdict = "未证明优势（95% 区间包含 0 或为负）"
    return {"n": n, "mean_pnl": float(pnl.mean()), "ci": (float(lo), float(hi)),
            "win_rate": float(np.mean([t["won"] for t in trades])),
            "avg_ask": float(np.mean([t["ask"] for t in trades])),
            "avg_fee": float(np.mean([t["fee"] for t in trades])), "verdict": verdict}


def brier(ps, ys) -> float:
    ps, ys = np.asarray(ps, float), np.asarray(ys, float)
    return float(np.mean((ps - ys) ** 2))


def calibration(rows: List[dict], key: str):
    edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        g = [r for r in rows if lo <= r[key] < hi]
        if g:
            out.append((lo, min(hi, 1.0), len(g), np.mean([r[key] for r in g]), np.mean([r["y"] for r in g])))
    return out


def run_pipeline(windows: List[dict], rules) -> dict:
    windows = sorted(windows, key=lambda d: d["S"])
    k = int(len(windows) * rules.train_frac)
    train_w, test_w = windows[:k], windows[k:]
    sigma = sigma_global(train_w)
    if sigma is None:
        raise ValueError("训练集没有可用的现货序列")
    tr = build_rows(train_w, sigma, rules)
    te = build_rows(test_w, sigma, rules)
    if len(tr) < 50 or not te:
        raise ValueError(f"数据不足：训练行 {len(tr)}，测试行 {len(te)}")
    w = fit_logistic(design(tr), np.array([r["y"] for r in tr], float), rules.l2)
    for r, p in zip(te, predict(w, design(te))):
        r["p_hat"] = float(p)
    for r in te:
        r["mid_up"] = (r["ua"] + r["ub"]) / 2.0
        r["mid_dn"] = (r["da"] + r["db"]) / 2.0
    y = [r["y"] for r in te]
    brier_block = {
        "基础模型 p_base（未校准）": brier([r["p_base"] for r in te], y),
        "逻辑回归校准 p_hat": brier([r["p_hat"] for r in te], y),
        "市场中间价 mid（基准）": brier([r["mid_up"] for r in te], y),
    }
    sims = {
        "逻辑回归 p_hat（决策规则）": summarize(trade_sim(te, lambda r: (r["p_hat"], 1 - r["p_hat"]), rules), rules),
        "基础模型 p_base（无校准）": summarize(trade_sim(te, lambda r: (r["p_base"], 1 - r["p_base"]), rules), rules),
        "市场中间价（零优势对照，各代币自身中间价）": summarize(trade_sim(te, lambda r: (r["mid_up"], r["mid_dn"]), rules), rules),
    }
    return {"sigma": sigma, "coef": w, "n_train_w": len(train_w), "n_test_w": len(test_w),
            "n_train_rows": len(tr), "n_test_rows": len(te), "brier": brier_block,
            "cal": calibration(te, "p_hat"), "sims": sims, "verdict": sims["逻辑回归 p_hat（决策规则）"]["verdict"]}


def render(res: dict, rules) -> str:
    L = [f"# 样本外检验报告 · 规则 {rules.version} · 摘要 {rules.digest()}", ""]
    L.append(f"- 窗口：训练 {res['n_train_w']} 个 / 测试 {res['n_test_w']} 个（按时间切分）")
    L.append(f"- 样本行：训练 {res['n_train_rows']} / 测试 {res['n_test_rows']}（每 {rules.sample_step} 秒一行，行间相关）")
    L.append(f"- 全局 σ（每√秒，仅训练集）：{res['sigma']:.6f}")
    L.append(f"- 逻辑回归系数 [截距, logit(p_base), 动量z, 基差z]：{[round(float(c), 4) for c in res['coef']]}")
    L.append("")
    L.append("## 概率质量（测试集 Brier，越小越好）")
    for k, v in res["brier"].items():
        L.append(f"- {k}：{v:.4f}")
    L.append("")
    L.append("## 校准（测试集，逻辑回归输出）")
    L.append("| 区间 | 样本数 | 平均预测 | 实际翻转率 |")
    L.append("|---|---|---|---|")
    for lo, hi, n, mp, my in res["cal"]:
        L.append(f"| {lo:.2f}–{hi:.2f} | {n} | {mp:.3f} | {my:.3f} |")
    L.append("")
    L.append("## 交易模拟（测试集，taker 吃单，每窗口最多一笔，扣费）")
    L.append("| 策略 | 笔数 | 每股平均盈亏 $ | 95% 区间 | 胜率 | 平均买价 |")
    L.append("|---|---|---|---|---|---|")
    for name, s in res["sims"].items():
        if s["n"] == 0:
            L.append(f"| {name} | 0 | - | - | - | - |")
        else:
            L.append(f"| {name} | {s['n']} | {s['mean_pnl']:+.4f} | [{s['ci'][0]:+.4f}, {s['ci'][1]:+.4f}] | "
                     f"{s['win_rate']:.3f} | {s['avg_ask']:.3f} |")
    L.append("")
    L.append(f"## 结论：{res['verdict']}")
    L.append("")
    L.append("说明：零优势对照的每股期望应为负（卖一高于中间价，再加手续费），若明显为正说明数据或模拟有问题。"
             "候选优势不等于可交易：还需纸面成交验证（排队与逆向选择）。")
    return "\n".join(L)
