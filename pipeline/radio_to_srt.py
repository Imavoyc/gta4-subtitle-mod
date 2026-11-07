#!/usr/bin/env python3
"""把电台转写结果（asr.jsonl / banks.jsonl）整理成**按台的 EN SRT + 键映射**。

为什么要 SRT：翻译/校对/QA 三套工具都是围绕 SRT 写的（术语表、上下文批次、
Netflix 阈值、Web 校对）。电台的一条记录（一个 21.25 秒片段，或台呼文件里的一句）
就当一个 cue；时间用"片段在文件内的序号 × 21.25s"或台呼的 (t0,t1)，纯占位，
翻译不关心绝对时间，但**键映射必须留着**（以后 M4 同步要靠它把译文绑到 (file, ch)）。

输出：
    <out>/<station>.en.srt      给 translate_srt.py 用
    <out>/<station>.map.tsv     cue 序号 -> station/file/ch（或银行文件的 t0/t1）
用法：
    radio_to_srt.py --asr .../asr.jsonl --banks .../banks.jsonl --out ~/tmp/<日期>/radio-srt
"""
import argparse
import difflib
import json
import os
import re
from collections import defaultdict

CLIP = 21.25
DJ_RE = re.compile(r"^(ID_|SOLO_|INTRO|TIME|TO|GENERAL)")


def ts(sec):
    sec = max(0.0, sec)
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s, ms = s + 1, ms - 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def dedupe_loop(text):
    """片段常常是"同一句念两遍"（21.25 秒窗口里循环一次）→ 压缩成一遍。

    判据：按句子切开后，前半 ≈ 后半（difflib ≥0.8）就保留前半。
    """
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    n = len(sents)
    if n >= 4 and n % 2 == 0:
        h = n // 2
        if difflib.SequenceMatcher(None, " ".join(sents[:h]),
                                  " ".join(sents[h:])).ratio() >= 0.8:
            return " ".join(sents[:h])
    return text.strip()


def split_cues(text, t0, t1, max_chars=70, max_sec=12.0):
    """把一个片段/一句台词按句子切成 1~n 条 cue（时间按字数比例分）。

    电台一条记录常是 21.25 秒的整段独白，直接当一条 cue 会让模型压得过狠
    （实测 40 词压成 14 个字）。切开以后每条接近正常字幕长度，以后 M4 同步
    也能逐条显示。
    """
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    if not sents:
        return [(text.strip(), t0, t1)]
    groups, cur = [], []
    for s0 in sents:
        trial = cur + [s0]
        if cur and (sum(len(x) + 1 for x in trial) > max_chars):
            groups.append(cur)
            cur = [s0]
        else:
            cur = trial
    if cur:
        groups.append(cur)
    total = sum(len(" ".join(g)) for g in groups) or 1
    out, acc = [], 0.0
    for g in groups:
        share = len(" ".join(g)) / total
        d = (t1 - t0) * share
        out.append((" ".join(g), acc + t0, min(t1, acc + t0 + d)))
        acc += d
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", required=True)
    ap.add_argument("--banks", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    per = defaultdict(list)   # station -> [ (key, text, t0, t1) ]
    for line in open(a.asr, encoding="utf-8"):
        r = json.loads(line)
        if not r["text"].strip():
            continue
        t0 = r["ch"] * CLIP
        txt = dedupe_loop(r["text"])
        for k, (piece, a0, a1) in enumerate(split_cues(txt, t0, t0 + CLIP), 1):
            per[r["station"]].append((f"{r['file']}#{r['ch']}.{k}", piece, a0, a1))
    if a.banks:
        for line in open(a.banks, encoding="utf-8"):
            r = json.loads(line)
            if not r["text"].strip():
                continue
            t0 = r["t0"]
            txt = dedupe_loop(r["text"])
            for k, (piece, a0, a1) in enumerate(split_cues(txt, t0, r["t1"]), 1):
                per[r["station"]].append((f"{r['file']}#{r['idx']}.{k}", piece, a0, a1))

    total = 0
    for st, items in sorted(per.items()):
        items.sort(key=lambda x: (x[0], x[2]))
        srt = os.path.join(a.out, f"{st}.en.srt")
        mp = os.path.join(a.out, f"{st}.map.tsv")
        with open(srt, "w", encoding="utf-8") as f, open(mp, "w", encoding="utf-8") as m:
            m.write("idx\tkey\tt0\tt1\n")
            for i, (key, text, t0, t1) in enumerate(items, 1):
                f.write(f"{i}\n{ts(t0)} --> {ts(t1)}\n{text}\n\n")
                m.write(f"{i}\t{key}\t{t0:.2f}\t{t1:.2f}\n")
        total += len(items)
        print(f"{st:26s} {len(items):4d} 条")
    print(f"合计 {total} 条")


if __name__ == "__main__":
    main()
