#!/usr/bin/env python3
"""给一批 per-track SRT 的 cue 时间加一个统一偏移（正数 = 字幕晚一点出现）。

用途：实机试听时发现"字幕整体偏早/偏晚"，但插件里的换算公式不好再动（改 .asi 要重启）。
SRT 是 2 秒热加载的，所以直接改文件最快：插件每 2 秒按 mtime 重新读。

    radio_shift_cues.py --dir <plugins/radio 里 srt 所在目录> --bias 0.25
    radio_shift_cues.py --dir ... --bias -0.2 --suffix .zh.srt     # 只动中文
    radio_shift_cues.py --dir ... --report-only                    # 只看当前分布

为了可回滚：每次偏移都累加到目录里的 `.bias.json`，`--reset` 按累计值原样退回。
"""
import argparse
import glob
import json
import os
import re

TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


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


def shift_file(path, bias):
    out = []
    n = 0
    for line in open(path, encoding="utf-8", errors="surrogateescape"):
        if "-->" in line:
            ms = list(TS.finditer(line))
            if len(ms) >= 2:
                a, b = sec_of(ms[0]) + bias, sec_of(ms[1]) + bias
                if b <= a:
                    b = a + 0.3
                rest = line[ms[1].end():]
                line = f"{ts(a)} --> {ts(b)}{rest}"
                n += 1
        out.append(line)
    if n:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(out)
        os.replace(tmp, path)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--bias", type=float, default=0.0, help="秒；正数 = 字幕整体晚出现")
    ap.add_argument("--suffix", default=".srt", help="只处理匹配后缀，如 .zh.srt")
    ap.add_argument("--reset", action="store_true", help="按 .bias.json 里累计值退回")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()

    state_path = os.path.join(a.dir, ".bias.json")
    total = 0.0
    if os.path.exists(state_path):
        total = float(json.load(open(state_path)).get("total", 0.0))

    files = sorted(glob.glob(os.path.join(a.dir, f"*{a.suffix}")))
    if a.report_only:
        print(f"{len(files)} 个文件，当前累计偏移 {total:+.2f}s")
        return 0

    bias = -total if a.reset else a.bias
    touched = 0
    n = 0
    for p in files:
        k = shift_file(p, bias)
        if k:
            touched += 1
            n += k
    newtotal = total + bias
    json.dump({"total": round(newtotal, 4)}, open(state_path, "w"))
    print(f"{touched} 个文件 / {n} 条 cue 偏移 {bias:+.2f}s；累计 {newtotal:+.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
