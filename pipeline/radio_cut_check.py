#!/usr/bin/env python3
"""验证「段内 `ms` 就是音频在**本段时间轴**上的位置」——用**段切换时刻**当标尺。

为什么不用互相关：GTA IV 的电台音频在引擎里被重度处理（EQ/压缩/混音），
波形和能量包络都对不齐（实测互相关 0.01~0.06，纯噪声），但**内容切换的时刻**
在频谱上是硬切，用 spectral flux 能稳定检出来。

判据：
  * 抓一段"还剩 8~12 秒"的段（`radio_state_log.py --interval 0.25` 同时记状态）；
  * 状态里 `ms` 归零 / `track` 换名的时刻 = 引擎认为的切点；
  * 音频里 spectral flux 的主峰 = 真实听到的切点；
  * 两者差 < 0.5 s ⇒ `ms` 与音频时间轴一致（字幕时间基可用）。

用法：
    radio_cut_check.py --capture ~/tmp/<日期>/cut1 [--verbose]
"""
import argparse
import os
import sys
import wave

import numpy as np


def load_audio(path):
    w = wave.open(path, "rb")
    a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if w.getnchannels() > 1:
        a = a.reshape(-1, w.getnchannels()).mean(axis=1)
    w.close()
    return a


def flux_cut(a, sr=48000, n=2048, hop=480):
    """返回 (切点秒, 主峰强度, 次峰强度)。"""
    win = np.hanning(n)
    frames = [a[i:i + n] * win for i in range(0, len(a) - n, hop)]
    if not frames:
        return 0.0, 0.0, 0.0
    S = np.log1p(np.abs(np.fft.rfft(np.array(frames), axis=1)))
    d = np.diff(S, axis=0)
    f = np.maximum(d, 0).sum(axis=1)          # 只数"新增能量"，抗衰减
    f = f - f.mean()
    k = int(np.argmax(f))
    f2 = f.copy()
    f2[max(0, k - 100):k + 101] = -1          # 屏蔽主峰 ±1 s
    return (k + 1) * hop / sr, float(f[k]), float(f2.max())


def state_switch(path):
    """从 state.tsv 找第一处 "track 变了 或 ms 变小" 的时刻（相对首行，秒）。"""
    rows = []
    for line in open(path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 8 or p[0] == "t":
            continue
        rows.append((p[0], p[3], p[4], int(p[6]), int(p[7])))   # t, hash, track, ms, len
    if len(rows) < 2:
        return None, rows
    t0 = rows[0][0]
    base = _hms(t0)
    for i in range(1, len(rows)):
        same = rows[i][1] == rows[i - 1][1]
        if not same or rows[i][3] < rows[i - 1][3]:
            return _hms(rows[i][0]) - base, rows
    return None, rows


def _hms(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    wav = os.path.join(a.capture, "audio.wav")
    st = os.path.join(a.capture, "state.tsv")
    if not (os.path.exists(wav) and os.path.exists(st)):
        sys.exit(f"{a.capture} 里没有 audio.wav / state.tsv")

    pcm = load_audio(wav)
    cut_s, peak, second = flux_cut(pcm)
    sw_s, rows = state_switch(st)
    dur = len(pcm) / 48000
    print(f"音频 {dur:.1f}s；flux 切点 {cut_s:.2f}s（峰 {peak:.1f}，次峰 {second:.1f}，"
          f"比值 {peak/max(second,1e-9):.2f}）")
    if rows:
        print(f"状态 {len(rows)} 行：首行 {rows[0][0]} {rows[0][2]} ms={rows[0][3]}/{rows[0][4]}")
        print(f"                   末行 {rows[-1][0]} {rows[-1][2]} ms={rows[-1][3]}/{rows[-1][4]}")
    if sw_s is None:
        print("状态里没看到段切换（这次抓的段没结束？）")
        return 1
    print(f"引擎态切点 {sw_s:.2f}s（相对首行状态）")
    # 抓音频比抓状态略早开始，两者起点差 ~0.1-1 s
    d = cut_s - sw_s
    print(f"差值 {d*1000:+.0f} ms  -> {'OK' if abs(d) < 0.6 else '偏差大（需查起点偏移）'}")
    if a.verbose:
        for r in rows[::4]:
            print("   ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
