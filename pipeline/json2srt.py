#!/usr/bin/env python3
"""segments.json -> SRT (no translation; English passthrough).

usage: json2srt.py segments.json [offset_sec] out.srt
"""
import json
import sys


def ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s, ms = int(t % 60), int(round((t - int(t)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    seg_path, out_path = sys.argv[1], sys.argv[3]
    offset = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    segs = json.load(open(seg_path, encoding="utf-8"))["segments"]
    with open(out_path, "w", encoding="utf-8") as f:
        for n, s in enumerate(segs, 1):
            text = " ".join(s["text"].split())
            if not text:
                continue
            f.write(f"{n}\n{ts(offset + s['start'])} --> {ts(offset + s['end'])}\n{text}\n\n")
    print(f"{len(segs)} cues -> {out_path}")


if __name__ == "__main__":
    main()
