#!/usr/bin/env python3
"""量一下"字幕 cue 和真实说话"贴不贴（VAD 口径），用来比较新旧时间轴。

判据：一个通道里，**cue 覆盖时段内**"有语音"的比例（precision），以及
"语音时段内"被 cue 覆盖的比例（recall）。旧时间轴（按字数等比切）一般是
precision 0.6~0.8；换成词级时间后应该能到 0.9 以上，而且 cue 之间不会把静音算进去。

用法：
    radio_cue_vad_check.py --seg-dir ~/tmp/<日期>/seg-all --tracks A --station radio_plr \
                           [--tracks-b B]      # 给两份就对比
"""
import argparse
import glob
import os
import re
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import radio_hash_table as HT          # noqa: E402

IVAUD2PCM = os.environ.get("IVAUD2PCM", "/tmp/ivaud2pcm")
TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _sec(s):
    m = TS.search(s)
    if not m:
        return 0.0
    h, mi, sec, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + sec + ms / 1000.0


def load_cues(path):
    cues, cur = [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        if "-->" in line:
            a, b = line.split("-->")
            cur = (_sec(a), _sec(b.split()[0] if b.split() else b))
            cues.append(cur)
    return cues


def onset_curve(pcm, sr=48000, hop=480):
    """10 ms 能量包络的"上升量"（只保留正梯度），用来找音节起点。"""
    m = len(pcm) // hop * hop
    e = np.sqrt((pcm[:m].reshape(-1, hop).astype(np.float64) ** 2).mean(axis=1))
    if len(e) < 4:
        return np.zeros(0), hop / sr
    d = np.diff(np.log1p(e), prepend=e[0])
    d[d < 0] = 0
    return d, hop / sr


def check(track_dir, station, seg_dir, limit=6):
    table = HT.load()
    files = sorted(glob.glob(os.path.join(track_dir, "*.en.srt")))
    key2 = {h: e for h, e in table.items()}
    files = [f for f in files
             if key2.get(os.path.basename(f).split(".")[0], {}).get("rpf") == station + ".rpf"][:limit]
    num = den = 0.0
    alloff = []
    for f in files:
        h = os.path.basename(f).split(".")[0]
        e = key2.get(h)
        if not e:
            continue
        seg = os.path.join(seg_dir, station, e["file"])
        if not os.path.exists(seg):
            continue
        cues = load_cues(f)
        if not cues:
            continue
        # cue 的绝对时间是"文件内时间"，先按 21.248 s 归到通道，再逐个通道取起振曲线
        CLIP = 21.248
        by_ch = {}
        for c in cues:
            by_ch.setdefault(int(c[0] // CLIP), []).append(c)
        hit = 0
        offs = []
        for ch, cl in sorted(by_ch.items()):
            raw = "/tmp/_vad.raw"
            if subprocess.run([IVAUD2PCM, seg, raw, "--ch", str(ch)],
                              capture_output=True).returncode:
                continue
            pcm = np.fromfile(raw, dtype="<i2")
            os.unlink(raw)
            oc, dt = onset_curve(pcm)
            if len(oc) == 0:
                continue
            thr = np.percentile(oc, 97) * 0.5
            peaks = np.where((oc[1:-1] >= thr) & (oc[1:-1] >= oc[:-2]) & (oc[1:-1] >= oc[2:]))[0] + 1
            base = ch * CLIP
            for s, _en in cl:
                rel = s - base
                if len(peaks) == 0:
                    continue
                d = np.abs(peaks * dt - rel)
                k = int(np.argmin(d))
                offs.append(float(d[k]))
                if d[k] <= 0.12:
                    hit += 1
        num += hit
        den += len(cues)
        alloff.extend(offs)
        print(f"  {e['file']:26s} cue {len(cues):3d} 条，起点与最近能量起振相差"
              f" {np.median(offs) if offs else -1:.2f}s（±0.12s 内 {hit/max(len(cues),1)*100:3.0f}%）")
    print(f"{track_dir}\n  → 起振对齐率(±0.12s) {num/max(den,1)*100:.1f}%（{int(num)}/{int(den)}），"
          f"中位偏差 {np.median(alloff) if alloff else -1:.2f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg-dir", required=True)
    ap.add_argument("--station", required=True)
    ap.add_argument("--tracks", required=True)
    ap.add_argument("--limit", type=int, default=6)
    a = ap.parse_args()
    check(a.tracks, a.station, a.seg_dir, a.limit)


if __name__ == "__main__":
    main()
