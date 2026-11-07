#!/usr/bin/env python3
"""字幕初译：带**上下文 + 术语表 + 风格**的批量翻译（不是逐句机翻）。

设计照抄社区现行方案（见 docs/gta4-translation-pipeline.md）：
  * 一批 10 条（失败自动减半重试）；
  * 带前 CONTEXT 条（原文 + 已定译文）当上下文，保证代词/语气/术语连贯；
  * 只发与本批文本**相关**的术语表条目（避免长术语表把提示词撑爆）；
  * 硬约束：单行 ≤18 汉字（尽量 ≤16）、最多 2 行、CPS ≤9、只用字库内字符、条数一致；
  * 温度 0.2，只输出 `编号|译文`。

用法：
    translate_srt.py in.en.srt out.zh.srt [--glossary g.tsv] [--style style.md]
                     [--batch 10] [--context 12] [--resume] [--review]

`--review` 只做一致性复审（术语表违规、同词多译），不发整片重译。
夜间批量规则见仓库 AGENTS.md（GPUStack 白天只允许单条测试）。
"""
import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://gpustack.rock-chips.com/v1/chat/completions"
MODEL = "deepseek-v4.1-flash"
METRICS = os.path.join(HERE, "subtitle_atlas_metrics.bin")
GLOSSARY = os.path.join(HERE, "glossary.tsv")
STYLE = os.path.join(HERE, "style.md")

sys.path.insert(0, HERE)
import glossary as gloss  # noqa: E402
import tm_build  # noqa: E402  （翻译记忆：清洗/索引/对齐都在那里）

MAX_LINE_CHARS = 18   # Netflix 简体中文 TTSG：16，必要时 18
MAX_LINES = 2
MAX_CPS = 9           # 成人节目 9 CPS（必要时 11）
META_VER = 2          # meta.json 里译文的"可信版本"；旧版本 resume 会拒绝


# ---------------------------------------------------------------- 基础设施
def load_charset():
    d = open(METRICS, "rb").read()
    magic, w, h, px, line_h, n = struct.unpack_from("<IIIffI", d, 0)
    assert magic == 0x54414253, "metrics magic mismatch"
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
            continue          # 允许"只有时间轴、译文为空"的块（否则回写会把这些 cue 丢掉）
        a, b = [x.strip() for x in ls[1].split("-->")]
        cues.append({"t0": a, "t1": b, "en": " ".join(x.strip() for x in ls[2:])})
    return cues


def write_srt(path, cues, key="zh"):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{c['t0']} --> {c['t1']}\n{c.get(key, '')}\n\n")


def ts_seconds(t):
    h, m, rest = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def cps(text, t0, t1):
    dur = max(0.1, ts_seconds(t1) - ts_seconds(t0))
    return len(re.sub(r"\s", "", text)) / dur


def chat(messages, temperature=0.2, tries=4):
    body = json.dumps({"model": MODEL, "messages": messages,
                       "temperature": temperature,
                       "reasoning_effort": "low"}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + os.environ["GPUSTACK_API_KEY"]})
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.load(r)
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            return text.strip()
        except Exception as e:  # noqa: BLE001
            print(f"    api try{i}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise RuntimeError("api failed")


def parse_pairs(reply):
    """解析 `编号|原文前四词|译文`。

    返回 {编号: (回显原文, 译文)}；没回显的给 None（会被"回显校验"卡住）。
    """
    got = {}
    for line in reply.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|")
        k = parts[0].strip().strip("[].")
        if not k.isdigit():
            continue
        if len(parts) >= 3:
            got[int(k)] = (parts[1].strip(), "|".join(parts[2:]).strip())
        else:
            got[int(k)] = (None, parts[1].strip())
    return got


def echo_ok(echo, en):
    """回显的"原文前四词"是否确实是这条的原文（防模型合并/拆分/错位）。"""
    if not echo:
        return False
    a = tm_build.norm_en(echo).split()
    b = tm_build.norm_en(en).split()
    if not a or not b:
        return False
    n = min(len(a), 4)
    a = a[:n]
    if b[:len(a)] == a:                      # 原文确实以回显开头
        return True
    import difflib as _d
    return _d.SequenceMatcher(None, a, b[:len(a)], autojunk=False).ratio() >= 0.75


def tidy_zh(text):
    """整理模型回复：去掉引号、把字面量 `\\n` 还原成换行、最多两行。"""
    t = (text or "").strip().strip('"').strip()
    t = t.replace("\\n", "\n").replace("\\N", "\n")
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    return "\n".join(lines[:MAX_LINES])


def style_text(path):
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    return ""


def system_prompt(style, terms, charset_count):
    parts = [
        "你是专业的游戏字幕译者，正在给 GTA IV 的电视节目做简体中文字幕。",
        "翻译原则：像人写的中文字幕，不要逐词直译；口语化、符合节目语气；"
        "保留粗口和玩笑的力度，不要净化；数字/单位按中文习惯写。",
        f"硬约束：一条最多两行，每行不超过 {MAX_LINE_CHARS} 个汉字；"
        f"阅读速度不超过 {MAX_CPS} 字/秒（必要时压缩台词而不是加字）；"
        f"只用常用简体汉字和中文标点（可用字 {charset_count} 个），不要夹杂英文原词"
        "（人名/品牌按术语表处理）。",
        "输入里的原文常是被切开的半句（上一条与下一条接得上）。**逐条独立翻译**，"
        "不许合并、不许拆分、不许补全上下条，条数和编号必须与输入一一对应。",
        "输出格式：每行 `编号|原文前四词|译文`（原文前四词要原样抄输入里的英文开头），"
        "不要解释、不要空行。",
    ]
    if style:
        parts.append("节目风格与背景：\n" + style)
    block = gloss.as_prompt_block(terms)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 初译
# ---------------------------------------------------------------- 翻译记忆

def group_pending(pending, batch, max_gap=12):
    """把待翻下标切成「每组 ≤ batch 条」的批次。

    TM 填掉的 cue 会在序列里留洞；只要洞不超过 max_gap（保持就近上下文），
    就继续往同一批里塞，避免被拆成一堆 batch=1 的小请求（实测 251 次 → 130 次）。
    """
    groups, run = [], []
    for i in pending:
        if run and (len(run) >= batch or i - run[-1] > max_gap):
            groups.append(run)
            run = []
        run.append(i)
    if run:
        groups.append(run)
    return groups


def tm_exemplars(index, part, k, skip_sim):
    """给本批找 k 条「同场景的人工听译」当风格样例（比 min_sim 高的已被直接填掉）。"""
    if not index or k <= 0:
        return []
    seen, seen_en, seen_zh, cand = set(), set(), set(), []
    for c in part:
        for sim, ti in index.query(tm_build.norm_en(c["en"]), 24):
            if sim < 0.55:
                break
            if sim >= skip_sim or ti in seen:
                continue
            it = index.items[ti]
            key = tm_build.norm_en(it.get("en_raw") or it["en"])
            if key in seen_en or it["zh"] in seen_zh:
                continue                # 同一句人工译文有多份 OCR 变体，只留一份
            seen.add(ti)
            seen_en.add(key)
            seen_zh.add(it["zh"])
            cand.append((sim, ti))
    cand.sort(reverse=True)
    return [index.items[ti] for _, ti in cand[:k]]


def fill_from_tm(cues, index, min_sim, charset):
    """TM 直接填（1:1 贪心）。返回填了多少条。"""
    m = tm_build.align_cues(cues, index, min_sim)
    for ci, hit in m.items():
        zh, bad = [], []
        for ch in hit["zh"]:
            if ord(ch) < 0x80 or ord(ch) in charset:
                zh.append(ch)
            else:
                bad.append(ch)
        if bad:
            print(f"  cue {ci+1}: TM 译文有字库外字符 {''.join(bad)}（已丢）", file=sys.stderr)
        c = cues[ci]
        c["zh"] = "".join(zh)
        c["src"] = "tm"
        c["tm"] = hit
    return len(m)


def translate(cues, args, terms, charset):
    out = [dict(c) for c in cues]
    # ver=2：2026-11-07 起带"回显校验"的译文；更早的 meta 不可信（可能是错位版写的）
    meta = {"ver": META_VER, "model": MODEL, "batch": args.batch, "context": args.context,
            "glossary": args.glossary, "cues": []}
    done = 0
    if args.resume and os.path.exists(args.out + ".meta.json"):
        prev = json.load(open(args.out + ".meta.json"))
        if int(prev.get("ver", 1)) < META_VER:
            print(f"resume: meta 是旧版本(v{prev.get('ver',1)})写的，可能含错位译文 → 忽略它重译",
                  file=sys.stderr)
            prev = {}
        for m in prev.get("cues", []):
            # 必须按 meta 里记录的 cue 号（i）回填，不能按列表位置 —— meta 里可能只记了
            # 部分 cue（有的条目当初失败/为空），按位置回填会整体错位（2026-11-07 踩过）
            i = int(m.get("i", 0)) - 1
            if 0 <= i < len(out) and m.get("zh") and not out[i].get("zh"):
                out[i]["zh"] = m["zh"]
                done += 1
        print(f"resume: 已有 {done} 条译文", file=sys.stderr)

    for i, c in enumerate(out):
        if c.get("zh") and c.get("src") == "tm":
            meta["cues"].append({"i": i + 1, "zh": c["zh"], "src": "tm",
                                 "t": int(time.time())})
    pending = [i for i, c in enumerate(out) if not c.get("zh")]
    if len(pending) < len(out):
        print(f"TM 已填 {len(out) - len(pending)} 条，待机翻 {len(pending)} 条", file=sys.stderr)

    batch = args.batch
    groups = group_pending(pending, batch)
    gi = 0
    while gi < len(groups):
        g = groups[gi]
        part = [out[i] for i in g]
        ctx = out[max(0, g[0] - args.context):g[0]]

        def _dur(c):
            try:
                return max(0.5, ts_seconds(c["t1"]) - ts_seconds(c["t0"]))
            except Exception:  # noqa: BLE001
                return 0.0
        listing = "\n".join(f"{n+1}|[{_dur(c):.0f}s] {c['en']}" for n, c in enumerate(part))
        user = []
        if ctx:
            user.append("上文（已定稿，仅供理解语境，不要翻译）：")
            for c in ctx:
                user.append(f"  EN: {c['en']}")
                if c.get("zh"):
                    user.append(f"  ZH: {c['zh']}")
            user.append("")
        ex = tm_exemplars(args.tm_index, part, args.tm_exemplars, args.tm_min_sim)
        if ex:
            user.append("参考（同一场景的人工听译稿，只借语气/术语，句子不同就不要照抄）：")
            for e in ex:
                user.append(f"  EN: {e.get('en_raw') or e['en']}")
                user.append(f"  ZH: {e['zh']}")
            user.append("")
        user.append("本批需要翻译的条目：")
        user.append(listing)
        text = "\n".join(user)

        if os.environ.get("GTA4_DEBUG_PROMPT"):
            with open(os.environ["GTA4_DEBUG_PROMPT"], "a", encoding="utf-8") as f:
                f.write(f"=== batch cues {[i+1 for i in g]}\n--- USER ---\n{text}\n")
        bterms = gloss.relevant_terms(terms, " ".join(c["en"] for c in part))
        bad = []
        try:
            reply = chat([{"role": "system", "content": system_prompt(style_text(args.style), bterms, len(charset))},
                          {"role": "user", "content": text}])
            got = parse_pairs(reply)
            bad = [n + 1 for n, c in enumerate(part)
                   if not echo_ok((got.get(n + 1) or (None,))[0], c["en"])]
            if os.environ.get("GTA4_DEBUG_PROMPT"):
                with open(os.environ["GTA4_DEBUG_PROMPT"], "a", encoding="utf-8") as f:
                    f.write(f"--- REPLY (keys={sorted(got)}, want={len(part)}) ---\n{reply}\n")
        except Exception as e:  # noqa: BLE001
            print(f"  batch {g[0]}: 失败 {e}", file=sys.stderr)
            got = {}

        if (len(got) < len(part) or bad) and batch > 2:
            batch = max(2, batch // 2)     # 失败减半重试（Subtitle Edit 同款策略）
            print(f"  batch {g[0]}: 回来 {len(got)}/{len(part)} 条，回显对不上 "
                  f"{len(bad)} 条（{bad[:6]}）→ 批大小降到 {batch}", file=sys.stderr)
            groups[gi:gi + 1] = group_pending(g, batch)
            continue
        if bad:
            # 已经减到最小批次还错位：宁可留空，也不要错位的字幕
            print(f"  batch {g[0]}: 仍有 {len(bad)} 条回显对不上（{bad}），这几条留空待重跑",
                  file=sys.stderr)

        for n, c in enumerate(part):
            zh = tidy_zh((got.get(n + 1) or (None, ""))[1])
            if (n + 1) in bad:
                continue
            keep, dropped = [], []
            for ch in zh:
                cp = ord(ch)
                if cp < 0x80 or cp in charset:
                    keep.append(ch)
                else:
                    dropped.append(f"{ch} U+{cp:04X}")
            if dropped:
                print(f"  cue {g[n]+1}: 字库外字符被丢 {', '.join(dropped)}", file=sys.stderr)
            c["zh"] = "".join(keep)
            c["src"] = "mt"
            meta["cues"].append({"i": g[n] + 1, "zh": c["zh"], "src": "mt",
                                 "t": int(time.time())})
        gi += 1
        if batch < args.batch:                  # 顺利一批后把批大小涨回去
            batch = min(args.batch, batch * 2)
        done = sum(1 for c in out if c.get("zh"))
        print(f"  {done}/{len(out)}", file=sys.stderr)
        write_srt(args.out, out)
        json.dump(meta, open(args.out + ".meta.json", "w"), ensure_ascii=False)
    return out


# ---------------------------------------------------------------- 一致性复审
def review(args, cues_zh, terms):
    """只挑出问题句：术语表违规 / 同词多译。"""
    issues = []
    en = parse_srt(args.src)
    for i, (e, z) in enumerate(zip(en, cues_zh), 1):
        for t in gloss.relevant_terms(terms, e["en"]):
            if t["zh"] not in cue_zh(z):
                issues.append((i, f"术语 '{t['term']}' 应为 '{t['zh']}'，实际: {cue_zh(z)}"))
    # 同词多译：术语表里的中文译名，扫译文里是否出现同一原文的不同写法（粗判：
    # 原文英文词在译文里以英文出现时提示人工确认）
    for i, (e, z) in enumerate(zip(en, cues_zh), 1):
        for w in re.findall(r"[A-Za-z]{3,}", cue_zh(z)):
            issues.append((i, f"译文中出现英文词 '{w}'，确认是否是术语表外的专名"))
    return issues


def latin_allowed(terms):
    """术语表里"允许保留拉丁字母"的词（Weazel/CNT/WKTT/Sprunk…）。"""
    out = set()
    for t in terms:
        for w in re.findall(r"[A-Za-z][A-Za-z'&.\-]{1,}", t["term"] + " " + t["zh"]):
            out.add(w.lower())
    return out


def cue_zh(c):
    """现有译文：parse_srt 把文本塞在 'en' 里，写完再存 'zh'（两个都要认）。"""
    return (c.get("zh") or c.get("en") or "").strip()


def fix_todo(en_cues, zh_cues, terms, allowed):
    """挑出真正需要修的条目。

    只认**可判定的缺陷**：译文里夹了术语表之外的英文（ASR 稿把专名拼错很正常，
    但中文译文里出现 `Jingoism`/`bili` 这类残留一定是缺陷）。
    **不做**"原文有术语 → 译文必须出现该译名"：我们的 cue 常是被切开的半句，
    合法译文本来就会省略这个词，强制会制造大量无意义改写（实测 1402 条误报 46 条）。
    术语一致性留给 `--review` 报告 + `qa.py` 警告，由人裁。
    """
    todo = []
    for i, (e, z) in enumerate(zip(en_cues, zh_cues), 1):
        text = cue_zh(z)
        residue = [w for w in re.findall(r"[A-Za-z]{3,}", text) if w.lower() not in allowed]
        if residue:
            todo.append((i, e, z, ["把夹在译文里的英文 " + "/".join(residue[:3]) +
                                   " 改成中文（术语表允许的专名除外）"]))
    return todo


def fix_pass(a, terms, charset):
    """一致性修正（**批量**）：只重译术语违规 / 夹英文的条目，10 条一请求。"""
    en_cues = parse_srt(a.src)
    zh_cues = parse_srt(a.out)
    for c in zh_cues:                      # 统一成 'zh'，避免 write_srt 写出空行
        c["zh"] = cue_zh(c)
    todo = fix_todo(en_cues, zh_cues, terms, latin_allowed(terms))
    print(f"待修正 {len(todo)} 条 / {len(en_cues)}", file=sys.stderr)
    changed, batch = 0, max(2, a.batch)
    i = 0
    while i < len(todo):
        part = todo[i:i + batch]
        lines = []
        for n, (ci, e, z, need) in enumerate(part, 1):
            lines.append(f"{n}|EN: {e['en']}")
            lines.append(f"  现译: {cue_zh(z)}")
            lines.append(f"  要求: {'；'.join(need)}")
        msg = ("下面每条是一条字幕的英文与现有中文译文，以及必须满足的修改要求。"
               "请只依据要求小改现有译文（不要重写整句、不要改变意思、保持长度不超过原文语速），"
               "输出 `编号|原文前四词|修改后的译文`，条数与输入一致，不要解释。\n\n"
               + "\n".join(lines))
        bterms = gloss.relevant_terms(terms, " ".join(e["en"] for _, e, _, _ in part))
        got, bad = {}, []
        try:
            reply = chat([{"role": "system", "content": system_prompt(
                style_text(a.style), bterms, len(charset))},
                {"role": "user", "content": msg}])
            got = parse_pairs(reply)
            bad = [n + 1 for n, (_, e, _, _) in enumerate(part)
                   if not echo_ok((got.get(n + 1) or (None,))[0], e["en"])]
        except Exception as ex:  # noqa: BLE001
            print(f"  修正批 {i}: 失败 {ex}", file=sys.stderr)
        if (len(got) < len(part) or bad) and batch > 2:
            batch = max(2, batch // 2)
            print(f"  修正批 {i}: 回来 {len(got)}/{len(part)}，回显不符 {len(bad)} → 批降到 {batch}",
                  file=sys.stderr)
            continue
        for n, (ci, e, z, need) in enumerate(part, 1):
            if (n + 1) in bad or n + 1 not in got:
                continue
            text = tidy_zh(got[n + 1][1])
            keep = "".join(ch for ch in text if ord(ch) < 0x80 or ord(ch) in charset)
            if keep and keep != cue_zh(z):
                z["zh"] = keep
                changed += 1
        i += len(part)
        if batch < a.batch:
            batch = min(a.batch, batch * 2)
        print(f"  {min(i, len(todo))}/{len(todo)}", file=sys.stderr)
        write_srt(a.out, zh_cues)
    print(f"修正 {changed} 条 -> {a.out}", file=sys.stderr)
    return 0


def take_lock(path):
    """同一个 .srt 只允许一个进程写（2026-11-07 踩过：两条 lane 撞同一文件，成品被写坏）。"""
    import fcntl
    fh = open(path + ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"另一个进程正在写 {path}（{path}.lock 被占用），本进程退出", file=sys.stderr)
        sys.exit(5)
    return fh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--glossary", default=GLOSSARY)
    ap.add_argument("--style", default=STYLE)
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--context", type=int, default=12)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--fix", action="store_true",
                    help="只重译被复审标出的条目（术语违规/夹英文），其余保持不动")
    ap.add_argument("--tm", help="翻译记忆 tm.tsv（b 站人工听译，tm_build.py 生成）")
    ap.add_argument("--tm-min-sim", type=float, default=0.97,
                    help="TM 直接填的相似度下限（低于它只当风格样例）")
    ap.add_argument("--tm-exemplars", type=int, default=4,
                    help="每批给模型的风格样例条数（0=关）")
    ap.add_argument("--tm-only", action="store_true",
                    help="只用 TM 填，不发任何 API 请求（白天可用）")
    a = ap.parse_args()
    _lock = take_lock(os.path.abspath(a.out))   # noqa: F841  持有到进程结束

    terms = gloss.load(a.glossary)
    charset = load_charset()
    a.tm_index = None
    if a.tm:
        items = [{"en": p[0], "en_raw": p[0], "zh": p[1], "bvid": p[2] if len(p) > 2 else "",
                  "t": float(p[3]) if len(p) > 3 else 0.0}
                 for p in (l.rstrip("\n").split("\t") for l in open(a.tm, encoding="utf-8")
                           if not l.startswith("#") and l.strip())]
        a.tm_index = tm_build.TmIndex(items)   # 单条 + 相邻拼接候选（他一行 = 我们多行）
        print(f"TM: {len(items)} 条", file=sys.stderr)
    print(f"{a.src}: 术语 {len(terms)} 条，字库 {len(charset)} 字", file=sys.stderr)

    if a.review:
        zh = parse_srt(a.out)
        issues = review(a, zh, terms)
        for i, msg in issues:
            print(f"cue {i}: {msg}")
        print(f"-- {len(issues)} 处待确认", file=sys.stderr)
        return 0

    if a.fix:
        return fix_pass(a, terms, charset)

    cues = parse_srt(a.src)
    for i, c in enumerate(cues):
        c["idx"] = i
    print(f"{len(cues)} 条待翻", file=sys.stderr)
    hit = 0
    if a.tm_index:
        hit = fill_from_tm(cues, a.tm_index, a.tm_min_sim, charset)
        print(f"TM 命中 {hit} 条（{hit/max(1,len(cues)):.1%}）", file=sys.stderr)
        if a.tm_only:
            write_srt(a.out, cues)
            print(f"只填 TM -> {a.out}（{hit} 条，其余留空）", file=sys.stderr)
            return 0
    out = translate(cues, a, terms, charset)   # 边翻边落盘（中断可 --resume）
    write_srt(a.out, out)
    print(f"完成 -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
