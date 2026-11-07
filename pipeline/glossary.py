#!/usr/bin/env python3
"""字幕术语表：加载 / 候选抽取 / 一致性检查。

表格是 TSV（`#` 注释行），四列：原文 / 译名 / 类型 / 备注
    原文        译名             类型    备注
    Weazel News Weazel 新闻      机构    电视台名
    Liberty City 自由城          地名    官方译名

用法：
    glossary.py --suggest x.en.srt [--top 60]      # 抽专名候选（人工填中文）
    glossary.py --check x.en.srt x.zh.srt          # 检查译文是否遵守术语表
    glossary.py --list                             # 打印当前术语表
"""
import argparse
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(HERE, "glossary.tsv")

STOPWORDS = set("""a an the and or but if then than that this these those there here
i you he she it we they me him her us them my your his its our their
is are was were be been being am do does did doing have has had having
will would can could should shall may might must not no yes so as at by
for from in into of off on onto out over to up with without about above
after again all also any because before below between both during each
few more most other some such only own same too very just now new old
get got go going gone come came came know knew think thought want wanted
like look see say said tell told take took make made give gave
hi hey oh ah uh um yeah okay ok well what who when where why how
one two three four five six seven eight nine ten
mr mrs ms dr sir madam man men guy guys boy girl kid kids
fuck fucking shit damn hell god jesus christ ass
""".split())

TOKEN = re.compile(r"[A-Za-z][A-Za-z'’\-]*")


def load(path=DEFAULT):
    """-> list of dict(term, zh, kind, note)"""
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        term, zh, kind, note = (p.strip() for p in parts[:4])
        if term and zh:
            out.append({"term": term, "zh": zh, "kind": kind, "note": note})
    return out


def as_prompt_block(terms, max_items=40):
    """给模型的术语表片段：`原文 = 译名`，Subtitle Edit 也是这个格式。"""
    if not terms:
        return ""
    lines = [f"{t['term']} = {t['zh']}" for t in terms[:max_items]]
    return "术语表（必须照此翻译）：\n" + "\n".join(lines)


def relevant_terms(terms, text):
    """只发与本批文本相关的术语（krillinai 的做法：术语带适用句子）。"""
    low = text.lower()
    return [t for t in terms if t["term"].lower() in low]


def suggest(en_srt, top=60, min_count=2):
    """抽专名候选：连续大写词（1~4 词），只保留**句中**出现过的
    （排除句首的 Listen/Remember/That's 这类误报），且不含缩写撇号。"""
    texts = read_srt_texts(en_srt)
    cnt = Counter()
    mid = Counter()
    for t in texts:
        # 句首标记：把 . ! ? 之后的位置当作"句首"，其他的算"句中"
        starts = {m.end() for m in re.finditer(r"[.!?]\s*", t)}
        for n in (1, 2, 3, 4):
            words = list(TOKEN.finditer(t))
            for i in range(len(words) - n + 1):
                seq = [w.group(0) for w in words[i:i + n]]
                if any("'" in w or "’" in w for w in seq):
                    continue
                if any(w.lower() in STOPWORDS for w in seq):
                    continue
                if n > 1 and not all(w[:1].isupper() for w in seq):
                    continue
                if n == 1 and not seq[0][:1].isupper():
                    continue
                phrase = " ".join(seq)
                cnt[phrase] += 1
                if words[i].start() not in starts and i > 0:
                    mid[phrase] += 1
    keep = []
    for phrase, c in cnt.most_common(top * 8):
        if c < min_count or mid.get(phrase, 0) < 1:
            continue
        if any(phrase != k and phrase in k for k in keep):
            continue
        keep.append(phrase)
        if len(keep) >= top:
            break
    return keep


def read_srt_texts(path):
    out = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        if len(ls) >= 3 and "-->" in ls[1]:
            out.append(" ".join(x.strip() for x in ls[2:]))
    return out


def check(en_srt, zh_srt, terms):
    """术语一致性：原文命中术语但译文没有规定译名。"""
    en = read_srt_texts(en_srt)
    zh = read_srt_texts(zh_srt)
    issues = []
    for i, (e, z) in enumerate(zip(en, zh), 1):
        for t in relevant_terms(terms, e):
            if t["zh"] and t["zh"] not in z:
                issues.append((i, t["term"], t["zh"], z))
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", default=DEFAULT)
    ap.add_argument("--suggest", metavar="EN_SRT")
    ap.add_argument("--check", nargs=2, metavar=("EN_SRT", "ZH_SRT"))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args()

    if a.list:
        terms = load(a.glossary)
        for t in terms:
            print(f"{t['term']}\t{t['zh']}\t{t['kind']}\t{t['note']}")
        print(f"-- {len(terms)} terms", file=sys.stderr)
    elif a.suggest:
        for phrase in suggest(a.suggest, a.top):
            print(f"{phrase}\t\t\t")   # 空着译名/类型，等人工填
    elif a.check:
        terms = load(a.glossary)
        bad = check(a.check[0], a.check[1], terms)
        for i, term, want, got in bad:
            print(f"cue {i}: 术语 '{term}' 应为 '{want}'，实际: {got}")
        print(f"-- {len(bad)} term violation(s), {len(terms)} terms", file=sys.stderr)
        return 1 if bad else 0
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
