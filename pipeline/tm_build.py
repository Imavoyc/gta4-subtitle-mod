#!/usr/bin/env python3
"""b 站人工听译语料（pairs.jsonl）→ 翻译记忆（TM）+ 术语候选。

背景：`bili_cn_corpus.py` 从「赤佬听译」的视频里 OCR 出**英中上下双行**，
得到 `{bvid,t,en,zh}` 的对照语料（人工译文，比机翻更贴节目语气）。
本工具把它变成**能直接被翻译管线吃的**东西：

    1. 清洗（去「听译」前缀、说话人标签、括号注释、up 主水印/吐槽）；
    2. 去重、筛明显不成立的对（词数/字数比例、纯英文、过短）；
    3. 与我们的英文 SRT（qwen3-asr 稿）**模糊对齐**（difflib 词级，默认 ≥0.85），
       支持"他的一行 = 我们的多行"（同视频相邻 2~3 条拼接成一个候选）；
    4. 贪心 1:1 指配（一个候选只填一条 cue，避免同一句译文被复用三遍）；
    5. 产出
         tm.tsv                       翻译记忆（en_norm \t zh \t bvid \t t）
         tm_all.jsonl                 全部清洗后的对（带 flags，供复查）
         align/<频道>.tm.jsonl        每条 cue 的对齐结果（sim/来源/类型）
         glossary_candidates.tsv      术语候选（含出现次数与例句，等人工/夜间模型填译名）
         report.md                    每个 SRT 的覆盖率表

用法：
    tm_build.py build --corpus ~/tmp/<date>/cn-corpus2/pairs.jsonl \\
                      --out ~/tmp/<date>/gta4-tm [--srt a.en.srt ...] [--min-sim 0.85]
    tm_build.py check --tm ~/tmp/<date>/gta4-tm/tm.tsv --glossary glossary.tsv

注意：本工具**不发任何网络请求**（术语译名靠夜间模型 + 人工确认，见 docs）。
"""
import argparse
import collections
import difflib
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import glossary as gloss  # noqa: E402

# ---------------------------------------------------------------- 归一化 / 清洗
_APOS = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_PREFIX = re.compile(r"^\s*(听译|听譯|手搓翻译|手翻|机翻|翻译|字幕)\s*[:：]?\s*")
_LABEL = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z]{1,6})\s*[：:]\s*")
_PAREN = re.compile(r"[（(][^)）]{2,40}[)）]")
_WATERMARK = re.compile(r"(赤佬|bilibili|b站|B站|末代赤佬|字幕组|未经作者授权)", re.I)
_COMMENT = re.compile(r"(大家|笑点|注意|这里|原文|意思是|翻译一下|懂的都懂|这句)")
_PUNCT = re.compile(r"[\s，。！？、,.!?…—―~\"“”‘’()（）]+")
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]+")
_GARBAGE = re.compile(r"[@#$%^&*_=+|<>{}\[\]\\®™©°±]{1,}")
_WM_TOKENS = {"bili", "bilibili", "bizi", "bilibi", "bilibilian"}


def norm_en(s):
    """英文归一化：小写、只留字母数字撇号、压空白、去掉 b 站水印词。

    对齐和查表都用它 —— 水印词（`bili bili`/`bizi bizi`，up 主压在画面上）会让
    相似度凭空掉一截，必须先去掉。
    """
    s = (s or "").translate(_APOS).lower()
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    toks = [w for w in s.split() if w not in _WM_TOKENS]
    return " ".join(toks)


def clean_zh(zh):
    """去掉 up 主的标注/水印/吐槽，返回 (干净中文, flags)。

    flags 只用来统计和复查，不改变可用性：`comment`/`watermark` 视为脏。
    """
    flags = []
    z = (zh or "").strip()
    if _PREFIX.match(z):
        z = _PREFIX.sub("", z)
        flags.append("prefix")
    m = _LABEL.match(z)
    if m and m.group(1) not in ("注", "笑点", "译注", "字幕"):
        flags.append("label:" + m.group(1))
        z = z[m.end():]
    if _EMOJI.search(z):
        flags.append("emoji")
        z = _EMOJI.sub("", z)
    if _GARBAGE.search(z):
        flags.append("garbage")
        z = _GARBAGE.sub("", z)
    if _PAREN.search(z):
        flags.append("paren")
        z = _PAREN.sub("", z)
    if _WATERMARK.search(z):
        flags.append("watermark")
        z = _WATERMARK.sub("", z)
    if re.search(r"[A-Za-z]{2,}", z):
        flags.append("latin")        # 品牌/缩写保留英文是正常的（Weazel/CNT）
    if _COMMENT.search(z) and len(z) > 14:
        flags.append("comment")
    z = re.sub(r"\s+", " ", z).strip(" 　")
    return z, flags


def zh_len(s):
    return len(_PUNCT.sub("", s or ""))


def pair_ok(en, zh):
    """一对是否值得进 TM。规则都来自实测分布（见 docs 里"语料落地"一节的表）。"""
    w = len(en.split())
    zl = zh_len(zh)
    if zl < 1:
        return False, "empty"
    if w < 3:
        return False, "short-en"        # 少于 3 词只会撞出假命中
    ratio = zl / w
    if ratio > 3.5:
        return False, "long-zh"         # 中文明显多于译文（夹了吐槽/解释）
    if ratio < 0.5:
        return False, "short-zh"        # 他漏译/被上行截断
    return True, ""


# ---------------------------------------------------------------- 语料加载
def load_corpus(path):
    """pairs.jsonl -> 清洗去重后的条目列表（保持视频内时间序）。"""
    rows = []
    seen = set()
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        en = norm_en(r.get("en"))
        zh, flags = clean_zh(r.get("zh"))
        if not en or not zh:
            continue
        ok, why = pair_ok(en, zh)
        # 译文里出现"原文没有的英文词" ⇒ 多半是他加的注释（Jingoism指…），不是纯翻译
        en_toks = set(en.split())
        extra = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", zh)} - en_toks
        if extra:
            flags.append("gloss-explain")
        raw = re.sub(r"\s+", " ", (r.get("en") or "").strip())
        rec = {"en": en, "en_raw": raw, "zh": zh, "bvid": r.get("bvid", ""),
               "t": float(r.get("t") or 0),
               "flags": flags, "ratio": zh_len(zh) / max(1, len(en.split())),
               "keep": ok, "why": why}
        key = (en, zh)
        if key in seen:
            continue
        seen.add(key)
        rows.append(rec)
    # 时间序（同一视频内按 t），跨视频按 bvid
    rows.sort(key=lambda r: (r["bvid"], r["t"]))
    return rows


DIRTY = {"watermark", "comment", "gloss-explain"}


def usable(rows):
    """进入 TM 的条目：比例/长度过关，且没有水印/吐槽/夹注释。"""
    return [r for r in rows if r["keep"] and not (DIRTY & set(r["flags"]))]


# ---------------------------------------------------------------- 索引与对齐
class TmIndex:
    """翻译记忆索引：单条 + 「相邻 2~3 条拼接」的候选，词级倒排加速。"""

    def __init__(self, items, combos=True, combo_gap=6.0, combo_max=3):
        self.items = list(items)          # [{'en','zh','bvid','t', ...}]
        n_single = len(self.items)
        if combos:
            byvid = collections.defaultdict(list)
            for i, it in enumerate(self.items):
                byvid[it["bvid"]].append(i)
            for ids in byvid.values():
                ids.sort(key=lambda i: self.items[i]["t"])
                for a in range(len(ids)):
                    for k in (2, combo_max):
                        win = ids[a:a + k]
                        if len(win) < 2 or k < 2:
                            continue
                        span = self.items[win[-1]]["t"] - self.items[win[0]]["t"]
                        if span > combo_gap:
                            continue
                        self.items.append({
                            "en": " ".join(self.items[i]["en"] for i in win),
                    "en_raw": " ".join(self.items[i].get("en_raw", self.items[i]["en"])
                                      for i in win),
                            "zh": "".join(self.items[i]["zh"] for i in win),
                            "bvid": self.items[win[0]]["bvid"],
                            "t": self.items[win[0]]["t"],
                            "combo": win, "flags": [], "ratio": 0.0,
                        })
        self.n_single = n_single
        self.inv = collections.defaultdict(list)
        for i, it in enumerate(self.items):
            for w in set(it["en"].split()):
                self.inv[w].append(i)

    def query(self, en_norm, topn=60):
        """-> [(sim, index)] 按相似度降序（只算共享词最多的 topn 个候选）。"""
        toks = en_norm.split()
        if not toks:
            return []
        cnt = collections.Counter()
        for w in set(toks):
            for i in self.inv.get(w, ()):
                cnt[i] += 1
        out = []
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(toks)
        for i, _ in cnt.most_common(topn):
            sm.set_seq1(self.items[i]["en"].split())
            out.append((sm.ratio(), i))
        out.sort(reverse=True)
        return out

    def best(self, en_norm, topn=60):
        q = self.query(en_norm, topn)
        return q[0] if q else (0.0, None)


def read_srt(path):
    """-> [{'idx','t0','t1','en'}]（只取文本，时间轴原样保留在调用方）。"""
    cues = []
    blocks = open(path, encoding="utf-8", errors="replace").read().split("\n\n")
    for blk in blocks:
        ls = blk.strip().split("\n")
        if len(ls) < 3 or "-->" not in ls[1]:
            continue
        a, b = [x.strip() for x in ls[1].split("-->")]
        cues.append({"idx": len(cues), "t0": a, "t1": b,
                     "en": " ".join(x.strip() for x in ls[2:])})
    return cues


def align_cues(cues, index, min_sim=0.85, short_min_sim=1.0, topn=60):
    """把 TM 指配给 cue：贪心 1:1（相似度高的先占位）。

    * 每条 cue 最多命中一个候选，每个候选最多喂一条 cue（避免同一句译文被复用）；
    * 少于 3 词的 cue 只接受**完全一致**（短句模糊匹配假阳性太高）；
    * 返回 {cue_idx: match}。
    """
    proposals = []
    for c in cues:
        en = norm_en(c["en"])
        if not en:
            continue
        need = short_min_sim if len(en.split()) < 3 else min_sim
        for sim, i in index.query(en, topn):
            if sim + 1e-9 < need:
                break
            proposals.append((sim, c["idx"], i))
    proposals.sort(reverse=True)
    used_c, used_i, out = set(), set(), {}
    for sim, ci, ti in proposals:
        if ci in used_c or ti in used_i:
            continue
        used_c.add(ci)
        used_i.add(ti)
        it = index.items[ti]
        out[ci] = {"cue": ci + 1, "sim": round(sim, 4),
                   "kind": "exact" if it["en"] == norm_en(cues[ci]["en"]) else
                           ("combo" if "combo" in it else "fuzzy"),
                   "en_corpus": it["en"], "zh": it["zh"],
                   "bvid": it["bvid"], "t": it["t"]}
    return out


# ---------------------------------------------------------------- 术语候选
def glossary_candidates(pairs, existing=(), min_count=3, top=400):
    """从对齐语料里抽专名候选（原文 + 出现次数 + 例句），译名留给人工/夜间模型。

    只做**确定性**的部分：抽大写专名（1~4 词，排除停用词、排除句首单词误报），
    统计出现次数，给两条例句（英文 + 他的中文），外加"现有术语表里是否已有"。
    猜译名这种事（"poor man's 对齐"实测不可靠）交给夜间模型读例句，人再确认。
    """
    have = {t["term"].lower() for t in existing}
    cnt = collections.Counter()
    samples = collections.defaultdict(list)
    tok = re.compile(r"[A-Za-z][A-Za-z'\-]*")
    for en, zh in pairs:
        words = list(tok.finditer(en))
        seen = set()
        for n in (1, 2, 3, 4):
            for i in range(len(words) - n + 1):
                seq = [w.group(0) for w in words[i:i + n]]
                if any(w.lower() in gloss.STOPWORDS for w in seq):
                    continue
                if not all(w[:1].isupper() for w in seq):
                    continue
                if i == 0 and n == 1:
                    continue        # 句首单词多半是普通词的大写
                if any("'" in w for w in seq):
                    continue
                phrase = " ".join(seq)
                if phrase in seen:
                    continue
                seen.add(phrase)
                cnt[phrase] += 1
                if len(samples[phrase]) < 2:
                    samples[phrase].append((en, zh))
    # 丢掉被"更长且几乎同样频繁"的短语覆盖的子串（City Gun 之于 Liberty City Gun）
    keep = []
    ranked = [p for p, c in cnt.most_common(top * 4) if c >= min_count]
    ranked.sort(key=lambda p: (-cnt[p], -len(p)))
    for phrase in ranked:
        c = cnt[phrase]
        if any(phrase != q and phrase in q and cnt[q] >= 0.9 * c for q in keep):
            continue
        keep.append(phrase)
        if len(keep) >= top:
            break
    table = {t["term"].lower(): t for t in existing}
    out = []
    for phrase in keep:
        t = table.get(phrase.lower())
        out.append({"term": phrase, "count": cnt[phrase],
                    "zh": t["zh"] if t else "", "kind": t["kind"] if t else "",
                    "examples": samples[phrase]})
    return out


def write_glossary_candidates(path, cands):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# b 站人工听译语料里的专名候选（tm_build.py 生成，勿手改；"
                "填好译名后并入 glossary.tsv）\n")
        f.write("# 原文\t译名(待填)\t类型\t出现次数\t例句(EN | 他的中文)\n")
        for c in cands:
            ex = " || ".join(f"{e[:90]} | {z[:45]}" for e, z in c["examples"])
            f.write(f"{c['term']}\t{c['zh']}\t{c['kind']}\t{c['count']}\t{ex}\n")


# ---------------------------------------------------------------- 报告
def report_rows(stats):
    lines = ["# 语料落地报告（tm_build.py）", "",
             "| SRT | cue | exact | combo | fuzzy | 合计 | 覆盖率 |",
             "|---|---|---|---|---|---|---|"]
    tot = collections.Counter()
    for s in stats:
        n = s["cues"]
        k = s["kinds"]
        hit = sum(k.values())
        lines.append(f"| {s['name']} | {n} | {k.get('exact',0)} | {k.get('combo',0)} | "
                     f"{k.get('fuzzy',0)} | {hit} | {hit/max(1,n):.1%} |")
        tot["cues"] += n
        tot["hit"] += hit
        for kk, vv in k.items():
            tot[kk] += vv
    lines.append(f"| **合计** | {tot['cues']} | {tot.get('exact',0)} | {tot.get('combo',0)} | "
                 f"{tot.get('fuzzy',0)} | {tot['hit']} | {tot['hit']/max(1,tot['cues']):.1%} |")
    return "\n".join(lines) + "\n"


def cmd_build(a):
    os.makedirs(a.out, exist_ok=True)
    rows = load_corpus(a.corpus)
    ok = usable(rows)
    print(f"语料 {len(rows)} 条（唯一，清洗后）→ 可用 {len(ok)} 条 "
          f"({len(ok)/max(1,len(rows)):.0%})", file=sys.stderr)
    why = collections.Counter(r["why"] for r in rows if not r["keep"])
    if why:
        print("  被筛掉的原因: " + ", ".join(f"{k}={v}" for k, v in why.most_common()),
              file=sys.stderr)
    fl = collections.Counter(f.split(":")[0] for r in ok for f in r["flags"])
    if fl:
        print("  可用条目的标记: " + ", ".join(f"{k}={v}" for k, v in fl.most_common()),
              file=sys.stderr)

    with open(os.path.join(a.out, "tm_all.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tm_path = os.path.join(a.out, "tm.tsv")
    with open(tm_path, "w", encoding="utf-8") as f:
        f.write("# 翻译记忆（b 站人工听译，tm_build.py 生成）："
                "en_norm \\t zh \\t bvid \\t t\n")
        for r in ok:
            f.write(f"{r['en']}\t{r['zh']}\t{r['bvid']}\t{r['t']:g}\n")
    print(f"TM -> {tm_path}（{len(ok)} 条）", file=sys.stderr)

    srt_files = []
    for pat in a.srt:
        srt_files.extend(sorted(glob.glob(pat)))
    stats = []
    if srt_files:
        index = TmIndex(ok)
        print(f"索引：单条 {index.n_single} + 拼接候选 {len(index.items)-index.n_single}",
              file=sys.stderr)
        adir = os.path.join(a.out, "align")
        os.makedirs(adir, exist_ok=True)
        for path in srt_files:
            cues = read_srt(path)
            m = align_cues(cues, index, a.min_sim)
            name = os.path.basename(path)
            with open(os.path.join(adir, name.replace(".en.srt", "") + ".tm.jsonl"),
                      "w", encoding="utf-8") as f:
                for ci in sorted(m):
                    f.write(json.dumps(m[ci], ensure_ascii=False) + "\n")
            kinds = collections.Counter(v["kind"] for v in m.values())
            stats.append({"name": name, "cues": len(cues), "kinds": dict(kinds)})
            print(f"  {name:34s} {len(cues):5d} cue → 命中 {len(m):4d} "
                  f"({len(m)/max(1,len(cues)):5.1%}) {dict(kinds)}", file=sys.stderr)
        with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
            f.write(report_rows(stats))
        print(f"报告 -> {os.path.join(a.out,'report.md')}", file=sys.stderr)

    existing = gloss.load(a.glossary)
    cands = glossary_candidates([(r.get("en_raw") or r["en"], r["zh"]) for r in ok], existing,
                                min_count=a.min_term_count)
    gp = os.path.join(a.out, "glossary_candidates.tsv")
    write_glossary_candidates(gp, cands)
    print(f"术语候选 {len(cands)} 条 -> {gp}", file=sys.stderr)
    return 0


def cmd_terms(a):
    """让模型从候选表的例句里"读出"译名（读人工译文，不是重新翻译）。

    夜间跑（GPUStack 规则见 AGENTS.md）；输出 `term\tzh\tkind\tconfidence`，
    **仍需人工过一遍**再并入 glossary.tsv（模型只做抽取，判断力有限）。
    """
    import translate_srt  # 复用 chat/system 里的 API 配置
    cands = []
    for line in open(a.candidates, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        p = line.rstrip("\n").split("\t")
        while len(p) < 5:
            p.append("")
        cands.append({"term": p[0], "zh": p[1], "count": int(p[3] or 0), "ex": p[4]})
    todo = [c for c in cands if not c["zh"] and c["count"] >= a.min_count]
    print(f"候选 {len(cands)} 条，待填 {len(todo)} 条（已有译名/低频的跳过）", file=sys.stderr)
    out = []
    for i in range(0, len(todo), a.batch):
        part = todo[i:i + a.batch]
        listing = "\n".join(f"{n+1}. {c['term']}  ‖  {c['ex']}" for n, c in enumerate(part))
        msg = ("下面每行是「英文专名 ‖ 例句(英文原句 | 人工中文译文)」。"
               "请只从人工中文译文里**读出**该专名的中文写法；若译文里没有对应的中文写法"
               "（例如保留英文缩写、或该专名不是真正的专名），就留空。\n"
               "输出格式：每行 `编号|中文译名|类型`，类型取 人名/地名/机构/节目/品牌/其他；"
               "没有中文写法的写 `编号||skip`。不要解释。\n\n" + listing)
        try:
            reply = translate_srt.chat([{"role": "system", "content": "你是字幕术语抽取器，只读不译。"},
                                        {"role": "user", "content": msg}])
        except Exception as e:  # noqa: BLE001
            print(f"  批次 {i} 失败: {e}", file=sys.stderr)
            continue
        got = {}
        for line in reply.splitlines():
            if "|" not in line:
                continue
            k, _, rest = line.partition("|")
            k = k.strip().strip("[].").strip()
            if k.isdigit():
                f = rest.split("|")
                got[int(k)] = (f[0].strip(), f[1].strip() if len(f) > 1 else "")
        for n, c in enumerate(part):
            zh, kind = got.get(n + 1, ("", ""))
            out.append({"term": c["term"], "zh": zh if zh != "skip" else "", "kind": kind,
                        "count": c["count"], "ex": c["ex"]})
        print(f"  {min(i+a.batch, len(todo))}/{len(todo)}", file=sys.stderr)
    kept = [c for c in cands if c["zh"]]
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("# 术语候选 + 模型建议译名（人工确认后再并入 glossary.tsv）\n")
        f.write("# 原文\t译名\t类型\t出现次数\t例句\n")
        for c in kept + out:
            f.write(f"{c['term']}\t{c['zh']}\t{c.get('kind','')}\t{c['count']}\t{c['ex']}\n")
    print(f"-> {a.out}（{sum(1 for c in out if c['zh'])} 条模型建议）", file=sys.stderr)
    return 0


def cmd_check(a):
    """TM 里已有术语的译法 vs glossary.tsv：看语料怎么说，标出不一致。"""
    terms = gloss.load(a.glossary)
    pairs = []
    for line in open(a.tm, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        p = line.rstrip("\n").split("\t")
        if len(p) >= 2:
            pairs.append((p[0], p[1]))
    bad = 0
    for t in terms:
        key = t["term"].lower()
        hit = [(en, zh) for en, zh in pairs if key in en]
        if not hit:
            continue
        agree = sum(1 for _, zh in hit if t["zh"].replace(" ", "") in zh.replace(" ", ""))
        mark = "OK" if agree / len(hit) >= 0.5 else "!!"
        if mark == "!!":
            bad += 1
        print(f"{mark} {t['term']:28s} 表:{t['zh']:12s} 语料 {len(hit):3d} 条中一致 "
              f"{agree:3d} ({agree/len(hit):.0%})")
    print(f"-- {bad} 条与人工译文不一致，值得人工看一眼", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description="b 站听译语料 → 翻译记忆 / 术语候选")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="清洗语料、建 TM、对齐 SRT、出术语候选与报告")
    b.add_argument("--corpus", required=True, help="pairs.jsonl（bili_cn_corpus.py 的产物）")
    b.add_argument("--out", required=True, help="输出目录")
    b.add_argument("--srt", nargs="*", default=[], help="要统计覆盖率的英文 SRT（支持通配）")
    b.add_argument("--min-sim", type=float, default=0.85, help="对齐阈值（词级相似度）")
    b.add_argument("--min-term-count", type=int, default=3, help="术语候选最少出现次数")
    b.add_argument("--glossary", default=gloss.DEFAULT)
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("terms", help="夜间：让模型从例句里读出术语译名（需人工确认）")
    t.add_argument("--candidates", required=True, help="glossary_candidates.tsv")
    t.add_argument("--out", required=True)
    t.add_argument("--min-count", type=int, default=3)
    t.add_argument("--batch", type=int, default=20)
    t.set_defaults(func=cmd_terms)

    c = sub.add_parser("check", help="拿 TM 复核 glossary.tsv 的译法一致性")
    c.add_argument("--tm", required=True)
    c.add_argument("--glossary", default=gloss.DEFAULT)
    c.set_defaults(func=cmd_check)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
