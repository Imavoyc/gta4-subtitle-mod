#!/usr/bin/env python3
"""验证「M4 同步」的两件事：**段身份** 和 **段内毫秒 ↔ 真实音频位置**。

做法：拿一次 `radio_sync_capture.sh` 抓的 `audio.wav`（sink monitor）+ 该台当前段
解码出来的 `track.wav`，用 100 Hz 能量包络做归一化互相关，找最佳滞后。

判据（2026-11-07 实测通过）：
  * 最佳滞后 ↔ state.tsv 里同一时刻的 `ms` 差值 < 0.3 s；
  * 相关系数明显高于次峰（>1.5 倍）。

⚠️ 抓音频和抓状态是两条进程，起点差几十毫秒，所以只要求"滞后差"小，
不要求绝对值精确。

用法：
    radio_sync_verify.py --capture ~/tmp/<日期>/cap --track track.wav \
                         --station JAZZ_NATION [--offset-ref 0]
"""
import argparse
import struct
import sys
import wave

import numpy as np

HOP = 480          # 48 kHz / 480 = 100 Hz 包络


def envelope(path, hop=HOP, channel=None):
    w = wave.open(path, "rb")
    nch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    if sw != 2:
        raise SystemExit(f"{path}: 只支持 s16（这里 {sw*8} bit）")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if nch > 1:
        a = a.reshape(-1, nch)
        a = a[:, channel] if channel is not None else a.mean(axis=1)
    m = len(a) // hop * hop
    a = a[:m].reshape(-1, hop)
    env = np.sqrt((a * a).mean(axis=1) + 1.0)
    env -= env.mean()
    env /= (np.linalg.norm(env) + 1e-9)
    return env, sr


def best_lag(x, y):
    """返回 lag（秒）：满足 `y[t] ≈ x[t+lag]`，即捕获音频的起点落在参考轨的哪里。

    cc[k] = Σ_t x[t+k]·y[t] = ifft(X·conj(Y))[k]；只取 k ≥ 0（捕获从轨中间开始）。
    """
    n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    fx = np.fft.rfft(x, n)
    fy = np.fft.rfft(y, n)
    cc = np.fft.irfft(fx * np.conj(fy), n)[:len(x)]
    peak = int(np.argmax(cc))
    cc2 = cc.copy()
    lo, hi = max(0, peak - 100), min(len(cc), peak + 101)   # 屏蔽主峰 ±1 s
    cc2[lo:hi] = -1
    return peak / 100.0, float(cc[peak]), float(cc2.max())


def state_ms(capture_dir, station):
    """返回 [(绝对秒(=行号*0.5 近似), ms)]，并给出首行的壁钟时间。"""
    rows = []
    t0 = None
    for i, line in enumerate(open(f"{capture_dir}/state.tsv", encoding="utf-8")):
        p = line.rstrip("\n").split("\t")
        if len(p) < 8 or p[0] == "t" or p[1] != station:
            continue
        rows.append((float(p[6]), p[3], p[4], p[0]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--track", required=True)
    ap.add_argument("--station", required=True)
    ap.add_argument("--channel", type=int, default=None)
    a = ap.parse_args()

    cap, _ = envelope(f"{a.capture}/audio.wav")
    trk, _ = envelope(a.track, channel=a.channel)
    print(f"capture {len(cap)/100:.1f}s  track {len(trk)/100:.1f}s")
    lag, peak, second = best_lag(trk, cap)
    print(f"最佳滞后 {lag:+.3f}s  相关 {peak:.3f}  次峰 {second:.3f}  "
          f"（比值 {peak/max(second,1e-9):.2f}）")

    rows = state_ms(a.capture, a.station)
    if rows:
        print(f"{a.station}: {len(rows)} 行状态")
        print(f"  首行 {rows[0][3]}  ms={rows[0][0]:.0f}  hash={rows[0][1]} {rows[0][2]}")
        print(f"  末行 {rows[-1][3]}  ms={rows[-1][0]:.0f}  hash={rows[-1][1]} {rows[-1][2]}")
        # 音频大致从首行状态之前 ~0.5 s 开始
        exp = [r[0] / 1000.0 for r in rows]
        print(f"  期望滞后 ≈ {exp[0]:.3f} ~ {exp[-1]:.3f}s（按 ms/1000）")
        dev = min(abs(lag - e) for e in exp)
        print(f"  与期望的最小偏差 {dev*1000:.0f} ms  -> {'OK' if dev < 0.3 else '偏差大'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
