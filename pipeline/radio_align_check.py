#!/usr/bin/env python3
"""精确验证「段内 `ms` ↔ 音频时间轴」：用 beep 把两条流对到同一个时钟上。

背景：`radio_cut_check.py` 靠"抓取起点差不多同时"来比时间，误差 1~2 s，不够。
这里在抓音频的同时**在固定时刻打一个 1.2 kHz 的 beep**（走同一个 sink → 会出现在
录音里），并让状态日志写带毫秒的壁钟（`radio_state_log.py --wallms`）。
于是：

    wall(t) = wall_beep + pad + 0.5*burst + (t - t_beep_in_audio)

beep 的位置把音频时间轴钉死后，再用状态里的 `ms/len` 外推出"引擎认为的换段时刻"，
和音频里真正的内容切换（能量凹陷 / 谱变化）对比，误差应该在几十毫秒级。

用法：
    radio_align_check.py --capture ~/tmp/<日期>/align1
"""
import argparse
import glob
import os
import sys
import wave

import numpy as np
from scipy import signal

BEEP_HZ = 1200.0


def load(path):
    w = wave.open(path, "rb")
    a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if w.getnchannels() > 1:
        a = a.reshape(-1, w.getnchannels()).mean(axis=1)
    w.close()
    return a


def find_beep(a, sr=48000):
    sos = signal.butter(4, [BEEP_HZ - 120, BEEP_HZ + 120], btype="band", fs=sr, output="sos")
    b = signal.sosfilt(sos, a)
    hop = 96                                     # 2 ms
    n = len(b) // hop * hop
    e = np.sqrt((b[:n].reshape(-1, hop) ** 2).mean(axis=1))
    k = int(np.argmax(e))
    return k * hop / sr, float(e[k]), float(np.median(e))


def find_boundary(a, t_pred, sr=48000, span=2.5):
    """在 t_pred 附近找能量凹陷最深的位置（段与段之间的淡出/淡入）。"""
    hop = int(0.02 * sr)
    n = len(a) // hop * hop
    rms = np.sqrt((a[:n].reshape(-1, hop) ** 2).mean(axis=1))
    i0 = max(1, int((t_pred - span) * sr / hop))
    i1 = min(len(rms) - 1, int((t_pred + span) * sr / hop))
    if i1 <= i0:
        return None
    seg = rms[i0:i1]
    k = int(np.argmin(seg))
    return (i0 + k) * hop / sr, float(seg[k]), float(np.median(rms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    a = ap.parse_args()
    d = a.capture
    pcm = load(os.path.join(d, "audio.wav"))
    sr = 48000
    print(f"音频 {len(pcm)/sr:.2f}s")

    t_beep, e_peak, e_med = find_beep(pcm)
    print(f"beep 在音频 {t_beep:.3f}s（峰 {e_peak:.0f} / 中位 {e_med:.0f}，"
          f"比值 {e_peak/max(e_med,1e-9):.1f}）")

    wall_beep = float(open(os.path.join(d, "beep_wall.txt")).read().strip())
    # beep.wav = 0.5 s 静音 + 0.08 s 蜂鸣 + 0.5 s 静音 → 蜂鸣中心在 paplay 后 0.54 s
    wall_of_cap_zero = wall_beep + 0.54 - t_beep

    rows = []
    for line in open(os.path.join(d, "state.tsv"), encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 8 or p[0] == "t":
            continue
        rows.append((float(p[0]), p[1], p[3], p[4], int(p[6]), int(p[7])))
    print(f"状态 {len(rows)} 行")

    # 找段切换：track 变 / ms 变小
    for i in range(1, len(rows)):
        if rows[i][2] == rows[i - 1][2] and rows[i][4] >= rows[i - 1][4]:
            continue
        prev = rows[i - 1]
        left = (prev[5] - prev[4]) / 1000.0           # 引擎认为还剩多少秒
        wall_edge = prev[0] + left
        pred = wall_edge - wall_of_cap_zero
        print(f"\n换段: {prev[3]} (ms {prev[4]}/{prev[5]}) → {rows[i][3]}")
        print(f"  引擎口径：{prev[3]} 还应剩 {left:.3f}s → 切换在音频 {pred:.3f}s")
        got = find_boundary(pcm, pred)
        if got:
            t_audio, dip, med = got
            print(f"  音频实测：能量最低点 {t_audio:.3f}s（{dip:.0f} vs 中位 {med:.0f}）"
                  f" → 差 {(t_audio - pred)*1000:+.0f} ms")
        break
    return 0


if __name__ == "__main__":
    sys.exit(main())
