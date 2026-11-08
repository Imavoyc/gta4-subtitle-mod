#!/usr/bin/env python3
"""整段错位检测：用「每条译文长度 vs 原文长度」的曲线做互相关。

为什么需要：翻译是分批做的，如果某一批的内容整体错了一位（模型把两条合并/跳过一条），
字幕会和画面完全对不上，但**单看中文是通顺的**，QA 的字长/CPS 也查不出来。
长度曲线的相关系数对错位很敏感：正常文件 corr@0 在 0.5 以上，错位的掉到 0.1 以下。

用法：
    srt_align_check.py a.en.srt a.zh.srt [b.en.srt b.zh.srt ...] [--min-corr 0.35]
退出码：1 = 有可疑文件。
"""
import argparse
import re
import sys

import numpy as np


def cues(path):
    out = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        # 允许"只有时间轴、译文为空"的块：空译文要占位（长度记 0），
        # 否则后面所有 cue 都会错开，长度曲线会把"少量空译"误判成"整段错位"
        if len(ls) >= 2 and "-->" in ls[1]:
            out.append(" ".join(ls[2:]))
    return out


def check(en_path, zh_path, lags=range(-8, 9)):
    en, zh = cues(en_path), cues(zh_path)
    n = min(len(en), len(zh))
    if n < 20:
        return None
    a = np.array([len(e.split()) for e in en[:n]], float)
    b = np.array([len(re.sub(r"\s", "", z)) for z in zh[:n]], float)
    c0 = float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else 0.0
    best_c, best_lag = -9.0, 0
    for lag in lags:
        if lag >= 0:
            x, y = a[:n - lag], b[lag:]
        else:
            x, y = a[-lag:], b[:n + lag]
        if len(x) < 20 or x.std() == 0 or y.std() == 0:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        if c > best_c:
            best_c, best_lag = c, lag
    return {"cues": n, "corr0": c0, "best_lag": best_lag, "best_corr": best_c}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="依次给 en srt 和 zh srt")
    ap.add_argument("--min-corr", type=float, default=0.35)
    a = ap.parse_args()
    paths = a.pairs
    if len(paths) % 2:
        print("参数要成对给：en zh [en zh ...]", file=sys.stderr)
        return 2
    bad = 0
    print(f"{'zh 文件':34s} {'cue':>5s} {'corr@0':>7s} {'最佳lag':>7s} {'corr':>6s}  判断")
    for i in range(0, len(paths), 2):
        r = check(paths[i], paths[i + 1])
        if not r:
            continue
        name = paths[i + 1].split("/")[-1]
        ok = r["corr0"] >= a.min_corr
        if not ok:
            bad += 1
        print(f"{name:34s} {r['cues']:5d} {r['corr0']:7.2f} {r['best_lag']:7d} "
              f"{r['best_corr']:6.2f}  {'OK' if ok else '★ 可疑：疑似整段错位，建议重译这个文件'}")
    print(f"-- {bad} 个可疑文件 / {len(paths)//2}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
