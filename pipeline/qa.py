#!/usr/bin/env python3
"""字幕 QA：按 Netflix 简体中文 TTSG 的阈值检查译文，出报告（也可给校对工具用）。

检查项（阈值见 docs/gta4-translation-pipeline.md）：
  * 每行字数 >18 错 / >16 警       * 行数 >2 错
  * 阅读速度 CPS >11 错 / >9 警    * 单条时长 <0.8s 警
  * 相邻条时间重叠 错              * 字库外字符 错（会画不出来）
  * 未翻译（与原文同）/ 占位符      * 术语表违规

用法：
    qa.py x.en.srt x.zh.srt [--glossary g.tsv] [--json out.json] [--quiet]
退出码：0 无错误；1 有错误。
"""
import argparse
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
METRICS = os.path.join(HERE, "subtitle_atlas_metrics.bin")
sys.path.insert(0, HERE)
import glossary as gloss  # noqa: E402

MAX_LINE = 16
MAX_LINE_HARD = 18
MAX_LINES = 2
MAX_CPS = 9
MAX_CPS_HARD = 11
MIN_DUR = 0.8


def load_charset():
    d = open(METRICS, "rb").read()
    magic, w, h, px, line_h, n = struct.unpack_from("<IIIffI", d, 0)
    assert magic == 0x54414253
    cps, off = set(), 24
    for _ in range(n):
        cp, slot = struct.unpack_from("<II", d, off)
        off += 8
        cps.add(cp)
    return cps


def parse_srt(path):
    cues = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        if len(ls) < 2 or "-->" not in ls[1]:
            continue
        a, b = [x.strip() for x in ls[1].split("-->")]
        cues.append({"t0": a, "t1": b, "text": [x.strip() for x in ls[2:]] or [""]})
    return cues


def secs(t):
    h, m, rest = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def check(en_srt, zh_srt, terms, ignore_overlap=False):
    """ignore_overlap：电台 SRT 的"一条时间轴"其实是多段并行（同一 wave 的不同 channel
    各占 21.25 s，时间轴互相重叠），重叠是数据模型的正常现象，不是错误。"""
    en = parse_srt(en_srt)
    zh = parse_srt(zh_srt)
    charset = load_charset()
    rows = []
    for i in range(max(len(en), len(zh))):
        e = en[i] if i < len(en) else {"text": [""], "t0": "0", "t1": "0"}
        z = zh[i] if i < len(zh) else {"text": [""], "t0": "0", "t1": "0"}
        text = "\n".join(z["text"])
        issues = []
        lines = z["text"] if z["text"] else [""]
        if not text.strip():
            issues.append(("err", "译文为空"))
        if len(lines) > MAX_LINES:
            issues.append(("err", f"行数 {len(lines)} > {MAX_LINES}"))
        for ln in lines:
            n = len(re.sub(r"\s", "", ln))
            if n > MAX_LINE_HARD:
                issues.append(("err", f"行长 {n} > {MAX_LINE_HARD}"))
            elif n > MAX_LINE:
                issues.append(("warn", f"行长 {n} > {MAX_LINE}"))
        try:
            dur = max(0.1, secs(z["t1"]) - secs(z["t0"]))
        except Exception:  # noqa: BLE001
            dur = 0.1
        c = len(re.sub(r"\s", "", text)) / dur
        if c > MAX_CPS_HARD:
            issues.append(("err", f"CPS {c:.1f} > {MAX_CPS_HARD}"))
        elif c > MAX_CPS:
            issues.append(("warn", f"CPS {c:.1f} > {MAX_CPS}"))
        if dur < MIN_DUR:
            issues.append(("warn", f"时长 {dur:.2f}s < {MIN_DUR}"))
        en_text = " ".join(e["text"])
        if text.strip() and text.strip() == en_text.strip():
            issues.append(("err", "疑似未翻译（与原文相同）"))
        if re.search(r"TODO|\||\[\[", text):
            issues.append(("err", "含占位符"))
        bad = sorted({ch for ch in text if ord(ch) >= 0x80 and ord(ch) not in charset})
        if bad:
            issues.append(("err", "字库外字符: " + " ".join(f"{b}(U+{ord(b):04X})" for b in bad)))
        for t in gloss.relevant_terms(terms, en_text):
            # 只对**中文译名**报术语提醒：拉丁译名（Weazel/CNT/WKTT）出现英文原词即算合规，
            # 且 cue 常是被切开的半句，合法省略不报
            if not re.search(r"[\u4e00-\u9fa5]", t["zh"]):
                continue
            if t["zh"] not in text and re.search(r"[\u4e00-\u9fa5]", text):
                issues.append(("warn", f"术语 '{t['term']}' 应为 '{t['zh']}'"))
        if i + 1 < len(zh) and not ignore_overlap:
            if secs(z["t1"]) > secs(zh[i + 1]["t0"]) + 0.001:
                issues.append(("err", "与下一条时间重叠"))
        rows.append({"i": i + 1, "t0": z["t0"], "t1": z["t1"], "en": en_text,
                     "zh": text, "cps": round(c, 2), "issues": issues})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("en_srt")
    ap.add_argument("zh_srt")
    ap.add_argument("--glossary", default=gloss.DEFAULT)
    ap.add_argument("--json")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--ignore-overlap", action="store_true",
                    help="电台 SRT：忽略相邻条时间重叠（多段并行时间轴的正常现象）")
    a = ap.parse_args()
    terms = gloss.load(a.glossary)
    rows = check(a.en_srt, a.zh_srt, terms, a.ignore_overlap)
    nerr = sum(1 for r in rows for s, _ in r["issues"] if s == "err")
    nwarn = sum(1 for r in rows for s, _ in r["issues"] if s == "warn")
    for r in rows:
        for sev, msg in r["issues"]:
            if not a.quiet or sev == "err":
                print(f"cue {r['i']:4d} [{sev}] {msg}  | {r['zh']}")
    print(f"-- {len(rows)} cues, {nerr} error(s), {nwarn} warning(s)", file=sys.stderr)
    if a.json:
        json.dump(rows, open(a.json, "w"), ensure_ascii=False)
        print(f"-- json -> {a.json}", file=sys.stderr)
    return 1 if nerr else 0


if __name__ == "__main__":
    sys.exit(main())
