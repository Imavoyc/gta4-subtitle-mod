#!/usr/bin/env python3
"""Translate Whisper segments to Chinese subtitles via the internal GPUStack
DeepSeek endpoint, then validate every character against the 汉化 font atlas
(plugins/GTA4.CHS/char_table.dat, 3518 codepoints — anything outside it renders
as nothing, see docs/gta4-subtitle.md).

usage: translate.py segments.json [offset_sec] out.srt
"""
import json
import os
import struct
import sys
import time
import urllib.request

API = "https://gpustack.rock-chips.com/v1/chat/completions"
MODEL = "deepseek-v4.1-flash"
ATLAS = ("/home/ydb/.steam/steam/steamapps/common/Grand Theft Auto IV/GTAIV"
         "/plugins/GTA4.CHS/char_table.dat")

# full-width punctuation -> half-width/ASCII fallbacks when the glyph is missing
REPLACE = {
    0x3010: "[", 0x3011: "]", 0xFF08: "(", 0xFF09: ")", 0x201C: '"', 0x201D: '"',
    0x2018: "'", 0x2019: "'", 0x2026: "...", 0x2014: "-", 0x2013: "-", 0xFF1B: ";",
    0x3000: " ",
}


def load_atlas():
    d = open(ATLAS, "rb").read()
    return set(struct.unpack_from("<%dI" % (len(d) // 4), d, 0))


def chat(messages, tries=3):
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
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
            print(f"  api ok {time.time()-t0:.1f}s", file=sys.stderr)
            return text.strip()
        except Exception as e:  # noqa: BLE001
            print(f"  api try{i}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise RuntimeError("api failed")


def srt_ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s, ms = int(t % 60), int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    seg_path, out_path = sys.argv[1], sys.argv[3]
    offset = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    segs = json.load(open(seg_path, encoding="utf-8"))["segments"]
    atlas = load_atlas()
    print(f"{len(segs)} segments, atlas {len(atlas)} cps", file=sys.stderr)

    report = []
    cues = []
    B = 10  # segments per request
    for b0 in range(0, len(segs), B):
        batch = segs[b0:b0 + B]
        listing = "\n".join(f"{i+1}|{s['text']}" for i, s in enumerate(batch))
        ans = chat([
            {"role": "system",
             "content": "你是游戏字幕翻译。把给出的英文台词逐条译成简体中文口语字幕："
                        "简短口语化、符合美式电视节目语气；不要解释、不要加引号。"
                        "只输出行，格式严格为 `编号|译文`，条数与输入一致。"},
            {"role": "user", "content": listing},
        ])
        got = {}
        for line in ans.splitlines():
            line = line.strip()
            if "|" in line:
                k, _, v = line.partition("|")
                if k.strip().isdigit():
                    got[int(k.strip())] = v.strip()
        for i, s in enumerate(batch):
            zh = got.get(i + 1, "")
            if not zh:
                report.append(f"!! missing translation for seg {b0+i+1}: {s['text'][:40]}")
                continue
            # charset check / replace
            out_cps = []
            bad = []
            for ch in zh:
                cp = ord(ch)
                if cp < 0x80 or cp in atlas:
                    out_cps.append(ch)
                elif cp in REPLACE:
                    out_cps.append(REPLACE[cp])
                    bad.append(f"{ch} U+{cp:04X}>{REPLACE[cp]}")
                else:
                    out_cps.append("")
                    bad.append(f"{ch} U+{cp:04X} dropped")
            text = "".join(out_cps)
            if bad:
                report.append(f"seg {b0+i+1}: " + ", ".join(bad))
            cues.append((offset + s["start"], offset + s["end"], text))

    with open(out_path, "w", encoding="utf-8") as f:
        for n, (a, b_, text) in enumerate(cues, 1):
            f.write(f"{n}\n{srt_ts(a)} --> {srt_ts(b_)}\n{text}\n\n")
    print(f"{len(cues)} cues -> {out_path}", file=sys.stderr)
    if report:
        print("\n".join(report), file=sys.stderr)
        open(out_path + ".report.txt", "w", encoding="utf-8").write("\n".join(report))


if __name__ == "__main__":
    main()
