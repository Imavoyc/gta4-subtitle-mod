#!/usr/bin/env python3
"""把英文 SRT 台词逐条译成中文 SRT（时间轴不动），字符集按**我们自己的字库**校验。

和 `translate.py` 的区别：
  * 输入是现成的 SRT（不用重新跑 whisper），只翻译；
  * 字符集来源是 `subtitle_atlas_metrics.bin`（自绘字库，ASCII + 中文标点 +
    GB2312 一级 3755 字），不再是汉化包的 char_table.dat —— 自绘渲染已经完全不
    依赖汉化补丁了；
  * 不在字库里的字：已知全角→半角映射先替换，其余丢弃并逐条写进 report。

usage: zh_from_srt.py in.srt out.srt [batch]
"""
import json
import os
import re
import struct
import sys
import time
import urllib.request

API = "https://gpustack.rock-chips.com/v1/chat/completions"
MODEL = "deepseek-v4.1-flash"
METRICS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "subtitle_atlas_metrics.bin")

# 字库缺这些全角符号时的等价替换
REPLACE = {
    0x3000: " ", 0x2014: "-", 0x2013: "-", 0xFF5E: "~",
    0x300C: "「", 0x300D: "」",
}


def load_charset():
    d = open(METRICS, "rb").read()
    magic, w, h, px, line_h, n = struct.unpack_from("<IIIffI", d, 0)
    assert magic == 0x54414253, "metrics magic mismatch"
    cps = set()
    off = 24
    for _ in range(n):
        cp, slot = struct.unpack_from("<II", d, off)
        off += 8
        cps.add(cp)
    return cps


def chat(messages, tries=4):
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "reasoning_effort": "low",
    }).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + os.environ["GPUSTACK_API_KEY"]})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            print(f"    api {time.time()-t0:.1f}s", file=sys.stderr)
            return text.strip()
        except Exception as e:  # noqa: BLE001
            print(f"    api try{i}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise RuntimeError("api failed")


def parse_srt(path):
    cues = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        if len(ls) < 3:
            continue
        m = re.match(r"([\d:,]+)\s*-->\s*([\d:,]+)", ls[1])
        if not m:
            continue
        cues.append((m.group(1), m.group(2), " ".join(x.strip() for x in ls[2:])))
    return cues


def main():
    src, dst = sys.argv[1], sys.argv[2]
    batch = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    charset = load_charset()
    cues = parse_srt(src)
    print(f"{src}: {len(cues)} cues, charset {len(charset)} cps", file=sys.stderr)

    out, report = [], []
    for b0 in range(0, len(cues), batch):
        part = cues[b0:b0 + batch]
        listing = "\n".join(f"{i+1}|{c[2]}" for i, c in enumerate(part))
        ans = chat([
            {"role": "system",
             "content": (
                 "你是游戏字幕翻译，把英文台词逐条译成简体中文口头字幕。要求：\n"
                 "1) 口语化、简洁，一条不超过 24 个汉字；\n"
                 "2) 只用常用简体汉字和中文标点（，。、？！：；“”（）《》…—等），"
                 "不要用生僻字、不要夹杂拼音、不要用英文原词（人名可音译成常用字）；\n"
                 "3) 不要解释、不要加编号以外的符号、不要合并或拆分条目；\n"
                 "4) 只输出 `编号|译文` 的行，条数与输入完全一致。")},
            {"role": "user", "content": listing},
        ])
        got = {}
        for line in ans.splitlines():
            line = line.strip()
            if "|" in line:
                k, _, v = line.partition("|")
                if k.strip().isdigit():
                    got[int(k.strip())] = v.strip()
        for i, (t0, t1, en) in enumerate(part):
            zh = got.get(i + 1, "")
            n = b0 + i + 1
            if not zh:
                report.append(f"!! cue {n} untranslated: {en[:50]}")
                zh = en
            keep, bad = [], []
            for ch in zh:
                cp = ord(ch)
                if cp < 0x80 or cp in charset:
                    keep.append(ch)
                elif cp in REPLACE:
                    keep.append(REPLACE[cp])
                else:
                    bad.append(f"{ch} U+{cp:04X}")
            if bad:
                report.append(f"cue {n}: dropped {', '.join(bad)} | {zh}")
            out.append((t0, t1, "".join(keep)))
        print(f"  {b0+len(part)}/{len(cues)}", file=sys.stderr)

    with open(dst, "w", encoding="utf-8") as f:
        for n, (t0, t1, text) in enumerate(out, 1):
            f.write(f"{n}\n{t0} --> {t1}\n{text}\n\n")
    print(f"{len(out)} cues -> {dst}", file=sys.stderr)
    if report:
        open(dst + ".report.txt", "w", encoding="utf-8").write("\n".join(report) + "\n")
        print(f"{len(report)} issue(s) -> {dst}.report.txt", file=sys.stderr)


if __name__ == "__main__":
    main()
