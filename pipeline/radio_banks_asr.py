#!/usr/bin/env python3
"""补转写：电台的「台呼/转场」类音频（INTRO / TIME / TO / GENERAL）。

这些文件的容器头和普通段不一样（没有固定的 509952 字节通道），实测结构是
`[0x1c(头长28) ... 0x0c/0x16/0x18(条数?) ... payload_offset ...]`，
payload 起就是**一整条 IMA ADPCM 流**（22~104 秒，里面是多句 DJ 台词，中间有停顿）。

做法：整条解出来 → 按能量静音切片（每片 ≤8 s）→ 每片单独 ASR（带电台名 prompt）
→ 每片一条记录，带相对时间 t0/t1（后续做字幕时能直接用）。

输出：`<outdir>/banks.jsonl`
"""
import argparse
import json
import os
import re
import struct
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("ivaud2wav", os.path.join(HERE, "ivaud2wav.py"))
ivaud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ivaud)
import night_radio_asr as asrmod  # noqa: E402

G = "/home/ydb/.steam/debian-installation/steamapps/common/Grand Theft Auto IV/GTAIV"
SFX = os.path.join(G, "pc/audio/sfx")
EXE = os.path.join(G, "GTAIV.exe")
BANK_NAMES = {"INTRO", "TIME", "TO", "GENERAL"}


def split_speech(pcm, rate=48000, win=0.05, min_len=0.6, max_len=8.0, pad=0.15):
    a = np.asarray(pcm, dtype=np.int16).astype(np.float32)
    n = int(rate * win)
    if len(a) < n:
        return []
    frames = np.abs(a[:len(a) // n * n]).reshape(-1, n).mean(axis=1)
    thr = max(60.0, frames.max() * 0.02)
    act = frames > thr
    segs, st = [], None
    for i, v in enumerate(act):
        if v and st is None:
            st = i
        elif not v and st is not None:
            if (i - st) * win >= min_len:
                segs.append([st * win, i * win])
            st = None
    if st is not None:
        segs.append([st * win, len(act) * win])
    # 加 pad 并合并相邻，超长的再硬切
    out = []
    for a0, a1 in segs:
        a0, a1 = max(0.0, a0 - pad), a1 + pad
        if out and a0 - out[-1][1] < 0.3:
            out[-1][1] = a1
        else:
            out.append([a0, a1])
    fixed = []
    for a0, a1 in out:
        while a1 - a0 > max_len:
            fixed.append([a0, a0 + max_len])
            a0 += max_len
        fixed.append([a0, a1])
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    jsonl = os.path.join(a.outdir, "banks.jsonl")
    done = set()
    if os.path.exists(jsonl):
        for line in open(jsonl, encoding="utf-8"):
            try:
                r = json.loads(line)
                done.add((r["station"], r["file"], r["idx"]))
            except Exception:  # noqa: BLE001
                pass
    from pyrpfiv import RPFParser
    only = set(x for x in a.only.split(",") if x) or None
    fout = open(jsonl, "a", encoding="utf-8")
    n = 0
    t0 = time.time()
    for rpf in sorted(os.listdir(SFX)):
        if not (rpf.startswith("radio_") and rpf.endswith(".rpf")):
            continue
        base = rpf[:-4]
        if only and base not in only:
            continue
        pr = RPFParser(os.path.join(SFX, rpf), EXE)
        pr.build_file_list()
        tmp = os.path.join(a.outdir, "_tmp")
        os.makedirs(tmp, exist_ok=True)
        for p in pr.paths:
            name = os.path.basename(p["path"])
            if name not in BANK_NAMES:
                continue
            pr.extract_file(p["path"], tmp)
            raw = os.path.join(tmp, name)
            if not os.path.exists(raw):
                continue
            data = open(raw, "rb").read()
            os.unlink(raw)
            if ivaud.parse_header(data):
                continue          # 普通段，主脚本已处理
            h = struct.unpack_from("<16I", data, 0)
            off = h[6]
            if not (0x20 <= off < len(data)):
                print(f"  跳过 {base}/{name}（off={off:#x}）", file=sys.stderr)
                continue
            pcm = ivaud.decode_channel(data[off:])
            if not pcm:
                continue
            segs = split_speech(pcm)
            print(f"{base}/{name}: {len(pcm)/48000:.1f}s -> {len(segs)} 句", file=sys.stderr)
            for i, (s0, s1) in enumerate(segs):
                key = (base, name, i)
                if key in done:
                    continue
                p16 = asrmod.downsample(np.asarray(pcm[int(s0 * 48000):int(s1 * 48000)],
                                                   dtype=np.int16))
                prompt = asrmod.STATION_HINT.get(base, "Liberty City radio") + ". " + asrmod.NAME_HINT
                try:
                    text = asrmod.asr(asrmod.wav_bytes(p16), prompt)
                except Exception as e:  # noqa: BLE001
                    print(f"  ASR 失败 {base}/{name}#{i}: {e}", file=sys.stderr)
                    continue
                rec = {"station": base, "file": name, "idx": i, "t0": round(s0, 2),
                       "t1": round(s1, 2), "text": text, "t": int(time.time())}
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                n += 1
    fout.close()
    print(f"完成 {n} 句（{(time.time()-t0)/60:.1f} min）-> {jsonl}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
