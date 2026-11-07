#!/usr/bin/env python3
"""把「按台的电台 SRT」拆成「按段（单个音频文件）的 SRT」，供插件按 hash 选文件。

为什么可以这么简单：`radio_to_srt.py` 生成台级 SRT 时，每条 cue 的时间轴**就是**
它所在音频文件内部的时间（`t0 = 通道号 × 21.25s`），不同文件的 cue 只是落在
同一条轴上重叠而已（所以 QA 要 `--ignore-overlap`）。因此"按文件分组"之后
**时间一个都不用改**——每条 cue 已经是从该段 0 点算起的。

输出文件名用**资源 hash**（`radio_hash_table.py` 的 joaat 表，
`hash = joaat("<rpf 基名>_<文件名>")`），因为插件从内存里拿到的就是这个 hash：

    <out>/<hash>.en.srt
    <out>/<hash>.zh.srt

用法：
    radio_split_tracks.py --en ~/tmp/<日期>/radio-srt/radio_afro_beat.en.srt \
                          --zh ~/tmp/<日期>/gta4-zh/radio_afro_beat.zh.srt \
                          --map ~/tmp/<日期>/radio-srt/radio_afro_beat.map.tsv \
                          --out /path/to/plugins/radio
    # 一次处理整批：
    radio_split_tracks.py --en-dir ~/tmp/<日期>/radio-srt --zh-dir ~/tmp/<日期>/gta4-zh \
                          --out /path/to/plugins/radio
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import radio_hash_table as HT

TS_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path):
    """→ [(start, end, text)]"""
    cues = []
    if not os.path.exists(path):
        return cues
    cur = None
    have_time = False
    for line in open(path, encoding="utf-8", errors="replace"):
        l = line.rstrip("\r\n")
        if not l.strip():
            if have_time and cur and cur[2]:
                cues.append((cur[0], cur[1], cur[2].strip()))
            cur = None
            have_time = False
            continue
        if "-->" in l:
            m = list(TS_RE.finditer(l))
            if len(m) >= 2:
                cur = [_sec(m[0]), _sec(m[1]), ""]
                have_time = True
            continue
        if have_time and cur is not None:
            cur[2] += ("" if not cur[2] else "\n") + l
    if have_time and cur and cur[2]:
        cues.append((cur[0], cur[1], cur[2].strip()))
    return cues


def _sec(m):
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def ts(sec):
    sec = max(0.0, sec)
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s, ms = s + 1, ms - 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def load_map(path):
    """map.tsv → {cue_idx: (file, ch_or_idx)}（idx 用 1 起的 cue 序号）"""
    out = {}
    if not os.path.exists(path):
        return out
    for i, line in enumerate(open(path, encoding="utf-8")):
        if i == 0:
            continue
        p = line.rstrip("\n").split("\t")
        if len(p) < 2:
            continue
        key = p[1]
        f, _, rest = key.partition("#")
        out[int(p[0])] = f
    return out


def split_station(table, en_path, zh_path, map_path, out_dir, rpf_hint=None):
    """写 per-track SRT，返回 (写入文件数, 丢弃 cue 数, 没有 hash 的文件名集合)"""
    key2hash = {}          # 文件名(小写) -> hash（同名的文件在不同 rpf 里极少见）
    for h, e in table.items():
        key2hash.setdefault(e["file"].lower(), h)
    if rpf_hint:           # 有 rpf 名时用它精确匹配，避免同名冲突
        key2hash = {e["file"].lower(): h for h, e in table.items()
                    if e["rpf"] == rpf_hint}

    en = parse_srt(en_path)
    zh = parse_srt(zh_path)
    cmap = load_map(map_path)
    per = {}               # file -> {"en": [...], "zh": [...]}
    skipped = 0
    unknown = set()
    for i, (s, t, txt) in enumerate(en, 1):
        f = cmap.get(i)
        if not f:
            skipped += 1
            continue
        h = key2hash.get(f.lower())
        if not h:
            unknown.add(f)
            skipped += 1
            continue
        per.setdefault(h, {"en": [], "zh": []})["en"].append((s, t, txt))
    for i, (s, t, txt) in enumerate(zh, 1):
        f = cmap.get(i)
        if not f:
            continue
        h = key2hash.get(f.lower())
        if not h:
            continue
        per.setdefault(h, {"en": [], "zh": []})["zh"].append((s, t, txt))

    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for h, langs in per.items():
        for lang, cues in langs.items():
            if not cues:
                continue
            cues.sort(key=lambda c: c[0])
            with open(os.path.join(out_dir, f"{h}.{lang}.srt"), "w", encoding="utf-8") as f:
                for k, (s, t, txt) in enumerate(cues, 1):
                    f.write(f"{k}\n{ts(s)} --> {ts(t)}\n{txt}\n\n")
            n += 1
    return n, skipped, unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--en")
    ap.add_argument("--zh")
    ap.add_argument("--map")
    ap.add_argument("--en-dir", help="批量：目录里的 <station>.en.srt / .map.tsv")
    ap.add_argument("--zh-dir", help="批量：目录里的 <station>.zh.srt")
    ap.add_argument("--out", required=True)
    ap.add_argument("--table", default=HT.DEFAULT_TABLE)
    a = ap.parse_args()

    table = HT.load(a.table)
    if not table:
        sys.exit("hash 表是空的：先跑 radio_hash_table.py build")

    jobs = []
    if a.en_dir:
        for fn in sorted(os.listdir(a.en_dir)):
            if not fn.endswith(".en.srt"):
                continue
            st = fn[:-len(".en.srt")]
            jobs.append((os.path.join(a.en_dir, fn),
                         os.path.join(a.zh_dir or "", f"{st}.zh.srt") if a.zh_dir else "",
                         os.path.join(a.en_dir, f"{st}.map.tsv")))
    else:
        jobs.append((a.en, a.zh, a.map))

    total = 0
    for en, zh, mp in jobs:
        rpf = os.path.basename(en).split(".")[0]
        rpf = rpf if rpf.endswith(".rpf") else rpf + ".rpf"
        n, skipped, unknown = split_station(table, en, zh, mp, a.out, rpf_hint=rpf)
        total += n
        name = os.path.basename(en)
        print(f"{name:44s} {n:4d} 个 srt  {skipped:3d} 条 cue 无 hash")
        if unknown:
            print(f"    没有 hash 的文件（TOC 名不可解）：{sorted(unknown)[:6]}")
    print(f"合计写出 {total} 个文件 → {a.out}")


if __name__ == "__main__":
    main()
