#!/usr/bin/env python3
"""电台「段 → 字幕」索引：M4 同步落地的数据底座。

电台的字幕不是一条时间轴，而是**每个 wave（一段 21.25 s 的音频文件）× 若干 piece**：
`radio-srt/<台>.map.tsv` 里 `key = <wave>#<channel>.<piece>`，`t0/t1` 是**按文件顺序
拼起来的占位时间**（不同 wave 的时间轴互相重叠，所以 SRT 里会出现"时间重叠"，
那是数据模型的正常现象，不是错误）。

M4 拿到"正在播哪个 wave + 段内已播秒数"之后，就是在本索引里查：
  station → wave → 二分找 t0 <= offset < t1 的那条 → cue 号 → 显示译文。

产出：
  segment_index.json           完整索引（含译文文本、语言标记）
  segment_index.tsv            摊平的表格（便于 rg/grep 排查）

用法：
    radio_segment_index.py --srt-dir ~/tmp/<日期>/radio-srt \\
                           --zh-dir  ~/tmp/<日期>/gta4-zh \\
                           --out     ~/tmp/<日期>/gta4-radio-index
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

# 广告里有俄语/西语版本（RPF 名字里就写着），这些 cue 不该配中文字幕
LANG_RE = re.compile(r"(RUSSIAN|SPANISH)")
CLIP_SEC = 21.25          # 每个 channel 的长度（509952 字节 / 48 kHz IMA ADPCM）


def read_srt_texts(path):
    out = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        if len(ls) >= 3 and "-->" in ls[1]:
            out.append("\n".join(ls[2:]).strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt-dir", required=True, help="英文 SRT + map.tsv 所在目录")
    ap.add_argument("--zh-dir", required=True, help="中文 SRT 所在目录（可缺）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    stations = []
    for mapf in sorted(glob.glob(os.path.join(a.srt_dir, "*.map.tsv"))):
        st = os.path.basename(mapf).replace(".en.map.tsv", "").replace(".map.tsv", "")
        zh_path = os.path.join(a.zh_dir, f"{st}.zh.srt")
        zh_texts = read_srt_texts(zh_path) if os.path.exists(zh_path) else []
        rows = list(csv.DictReader(open(mapf, encoding="utf-8"), delimiter="\t"))
        waves = {}
        for n, r in enumerate(rows):
            key = r["key"]
            wave = key.split("#")[0]
            zh = zh_texts[n] if n < len(zh_texts) else ""
            # key = <wave>#<channel>.<piece>；**每个 channel 是独立的 21.25 s**，
            # 所以段内偏移要用 off0/off1（= t0/t1 减掉前面 channel 占掉的时间）
            ch = 0
            if "." in key:
                tail = key.split("#", 1)[1]
                try:
                    ch = int(tail.split(".")[0])
                except ValueError:
                    ch = 0
            base = ch * CLIP_SEC
            waves.setdefault(wave, {"lang": "ru" if "RUSSIAN" in wave else
                                            ("es" if "SPANISH" in wave else "en"),
                                    "cues": []})
            waves[wave]["cues"].append({"key": key, "cue": n + 1, "ch": ch,
                                        "t0": round(float(r["t0"]), 3),
                                        "t1": round(float(r["t1"]), 3),
                                        "off0": round(max(0.0, float(r["t0"]) - base), 3),
                                        "off1": round(max(0.0, float(r["t1"]) - base), 3),
                                        "zh": zh})
        n_cues = sum(len(w["cues"]) for w in waves.values())
        n_zh = sum(1 for w in waves.values() for c in w["cues"] if c["zh"])
        stations.append({"station": st, "waves": waves, "cues": n_cues, "with_zh": n_zh,
                         "zh_srt": os.path.basename(zh_path) if zh_texts else ""})

    json.dump(stations, open(os.path.join(a.out, "segment_index.json"), "w"),
              ensure_ascii=False)
    with open(os.path.join(a.out, "segment_index.tsv"), "w", encoding="utf-8") as f:
        f.write("station\twave\tlang\tcue\tch\tkey\toff0\toff1\tzh\n")
        for s in stations:
            for w, d in s["waves"].items():
                for c in d["cues"]:
                    f.write(f"{s['station']}\t{w}\t{d['lang']}\t{c['cue']}\t{c['ch']}\t"
                            f"{c['key']}\t{c['off0']:g}\t{c['off1']:g}\t"
                            f"{c['zh'].replace(chr(10), ' / ')}\n")

    print(f"{'台':26s} {'wave':>5s} {'cue':>5s} {'有译文':>6s} {'ru/es':>6s}")
    tw = tc = tz = tl = 0
    for s in stations:
        ru = sum(len(d["cues"]) for d in s["waves"].values() if d["lang"] != "en")
        print(f"{s['station']:26s} {len(s['waves']):5d} {s['cues']:5d} {s['with_zh']:6d} {ru:6d}")
        tw += len(s["waves"]); tc += s["cues"]; tz += s["with_zh"]; tl += ru
    print(f"{'合计':26s} {tw:5d} {tc:5d} {tz:6d} {tl:6d}")
    print("注意：同一 wave 的每个 channel 是独立的 21.25 s，查表请用 (wave, ch, off)")
    print(f"-> {a.out}/segment_index.json / .tsv", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
