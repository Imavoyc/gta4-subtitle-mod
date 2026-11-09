#!/usr/bin/env python3
"""把相邻 cue 之间的小空隙"补上"（延长前一条的结束时间），避免字幕一跳一跳。

背景：词级重定时后 cue 的边界来自真实词时间，句子之间的小停顿（0.2~1 s）就变成"没有字幕"，
插件在没有命中 cue 时会回落到"台名 · id"占位行 → 看起来像"字幕一闪一闪 / 没字幕"。
字幕惯例是：小停顿不该闪，长停顿才换行/消失。

    radio_bridge_gaps.py --dir <plugins/radio> --max-gap 1.2 [--max-total 8.0] [--suffix .srt]

只改 end（不碰 start），并且：
  * 空隙 ≤ max-gap 才桥接；
  * 桥接后单条时长不超过 max-total；
  * 同一通道（21.248 s）边界不跨越（跨通道就是另一段音频，桥接会串词）。
"""
import argparse
import glob
import os
import re

TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")
CLIP = 21.248


def sec_of(m):
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s, ms = int(t % 60), int(round((t - int(t)) * 1000))
    if ms >= 1000:
        s, ms = s + 1, ms - 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def process(path, max_gap, max_total):
    lines = open(path, encoding="utf-8", errors="surrogateescape").read().splitlines()
    idx = []
    for i, l in enumerate(lines):
        if "-->" in l:
            m = list(TS.finditer(l))
            if len(m) >= 2:
                idx.append((i, sec_of(m[0]), sec_of(m[1]), m[1].end()))
    changed = 0
    for k in range(len(idx) - 1):
        i, a, b, endpos = idx[k]
        ia, aa, ba, _ = idx[k + 1]
        gap = aa - b
        if 0 < gap <= max_gap and (aa - a) <= max_total:
            # 不跨通道边界
            if int(a // CLIP) != int(aa // CLIP):
                continue
            lines[i] = f"{ts(a)} --> {ts(aa - 0.02)}{lines[i][idx[k][3]:]}"
            changed += 1
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--max-gap", type=float, default=1.2)
    ap.add_argument("--max-total", type=float, default=8.0)
    ap.add_argument("--suffix", default=".srt")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.dir, f"*{a.suffix}")))
    n = f = 0
    for p in files:
        k = process(p, a.max_gap, a.max_total)
        if k:
            f += 1
            n += k
    print(f"{f}/{len(files)} 个文件，桥接 {n} 处空隙（≤{a.max_gap}s）")


if __name__ == "__main__":
    main()
