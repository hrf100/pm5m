"""python -m pm5m.research --freeze --rules research_rules.json     # 冻结默认规则（只能做一次）
   python -m pm5m.research --rules research_rules.json --series ./data/series
   python -m pm5m.research --synth 400                             # 仅测试流水线，用合成数据"""
import argparse
import os
import sys

from .evaluate import render, run_pipeline
from .features import load_windows
from .rules import Rules, freeze, load


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m pm5m.research")
    ap.add_argument("--series", default="./data/series")
    ap.add_argument("--rules", default="./research_rules.json")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--synth", type=int, default=0)
    ap.add_argument("--report", default="./research_report.md")
    a = ap.parse_args(argv)
    if a.freeze:
        if os.path.exists(a.rules):
            sys.exit("规则文件已存在，拒绝覆盖。请新建版本文件。")
        freeze(Rules(), a.rules)
        print("已冻结规则：", a.rules)
        return
    rules = load(a.rules) if os.path.exists(a.rules) else Rules()
    if a.synth:
        from .synth import make_windows
        windows = make_windows(a.synth)
        print(f"[合成数据 {a.synth} 个窗口，仅用于测试流水线]")
    else:
        windows = load_windows(a.series)
        print(f"真实窗口数：{len(windows)}")
    res = run_pipeline(windows, rules)
    text = render(res, rules)
    print(text)
    with open(a.report, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\n报告已保存：{a.report}")


if __name__ == "__main__":
    main()
