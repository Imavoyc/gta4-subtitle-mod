#!/usr/bin/env python3
"""用更准的 ASR（GPUStack `qwen3-asr-1.7b`）**重提取**已有 SRT 的英文文本，时间轴沿用原 SRT。

为什么：qwen3-asr 没有时间戳（`verbose_json` 不支持），而 whisper 有时间戳但文本较弱
（实测会丢短句/促销口播、某些词听错）。所以分工：
    whisper SRT  →  时间骨架（cue 的开始/结束时间）
    qwen3-asr    →  文本（按"停顿切块"整块转写，避免被切在句子中间导致模型补全）

流程：
  1. 按 SRT 里相邻 cue 的间隔找"停顿"，把若干条 cue 合成一个 8~25 秒的块；
  2. 每块切音频 → qwen3-asr → 得到整块文本；
  3. 按句子（.!?…）切开块文本，按 cue 时长比例分回各条 cue（句数一致时一一对应）；
  4. 输出新的 EN SRT（时间轴原样）。

用法：
    reasr_srt.py --wav weazel_full.wav --srt weazel.en.srt --out weazel.en2.srt [--max-chunk 25]
    # 之后重新翻译：translate_srt.py weazel.en2.srt weazel.zh.srt
夜间批量规则见 AGENTS.md（GPUStack 白天只允许单条测试）。
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

API = "https://gpustack.rock-chips.com/v1/audio/transcriptions"
MODEL = "qwen3-asr-1.7b"


def secs(t):
    h, m, rest = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def parse_srt(path):
    cues = []
    for blk in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        ls = blk.strip().split("\n")
        if len(ls) < 3 or "-->" not in ls[1]:
            continue
        a, b = [x.strip() for x in ls[1].split("-->")]
        cues.append({"t0": a, "t1": b, "s": secs(a), "e": secs(b),
                     "text": " ".join(x.strip() for x in ls[2:])})
    return cues


def write_srt(path, cues):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{c['t0']} --> {c['t1']}\n{c['text'].strip()}\n\n")


def build_chunks(cues, max_chunk=25.0, gap=0.35, min_chunk=8.0):
    """把连续 cue 合成块：块内相邻 cue 间隔 < gap，且总长不超过 max_chunk。"""
    chunks, cur = [], []
    for c in cues:
        if not cur:
            cur = [c]
            continue
        span = c["e"] - cur[0]["s"]
        prev_gap = c["s"] - cur[-1]["e"]
        if prev_gap > gap or span > max_chunk:
            chunks.append(cur)
            cur = [c]
        else:
            cur.append(c)
    if cur:
        chunks.append(cur)
    # 太短的块并到前一块（避免切在句子中间）
    merged = []
    for ch in chunks:
        if merged and (ch[-1]["e"] - ch[0]["s"]) < min_chunk:
            merged[-1].extend(ch)
        else:
            merged.append(ch)
    return merged


ASR_PROMPT = os.environ.get("REASR_PROMPT", "")


def asr(wav_slice):
    """POST /v1/audio/transcriptions（手工拼 multipart，避免额外依赖）。

    `REASR_PROMPT` 环境变量作为 prompt 一起发（放专名，帮助拼写）。
    """
    boundary = "----reasrboundary"
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
                     f"\r\n\r\n{value}\r\n".encode())

    field("model", MODEL)
    if ASR_PROMPT:
        field("prompt", ASR_PROMPT)
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  "filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode())
    parts.append(open(wav_slice, "rb").read())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        API, data=body,
        headers={"Authorization": "Bearer " + os.environ["GPUSTACK_API_KEY"],
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["text"].strip()


def cut(wav, start, end, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t",
                    f"{end - start:.3f}", "-i", wav, "-ac", "1", "-ar", "16000", out],
                   check=True)


def split_sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def assign(chunk_cues, text):
    """把整块文本分回块内各条 cue。

    句数刚好相等时一一对应；否则用 difflib 把每个新句子匹配到"最像的原 cue"
    （whisper 的 cue 边界本来就大致等于句子边界），并保证匹配单调递增 ——
    这样文本不会被推着往后串一条（第一次实现按比例切就串了）。
    """
    sents = split_sentences(text)
    n = len(chunk_cues)
    if not sents:
        return [c["text"] for c in chunk_cues]
    if len(sents) == n:
        return sents
    out = ["" for _ in range(n)]
    j = 0
    for sent in sents:
        best, best_score = j, -1.0
        for k in range(j, n):
            score = difflib.SequenceMatcher(None, sent.lower(),
                                            chunk_cues[k]["text"].lower()).ratio()
            if score > best_score:
                best, best_score = k, score
        if best_score < 0.10 and j < n - 1:
            best = j + 1          # 完全不像：顺延到下一条，避免全堆在一条上
        out[best] = (out[best] + " " + sent).strip()
        j = best
    for k in range(n):
        if not out[k]:
            out[k] = chunk_cues[k]["text"]        # 没分到就沿用原文，不清空
    return out


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def rebuild_from_words(cues, word_stream):
    """把对齐好的词流分回 cue，并把 cue 边界**吸附到词边界**上。

    第一版按"词中点落在原 cue 窗口里"分，实测边界词会串到下一条（差半秒就串）。
    现在做法：先用原来的 cue 边界找出"最近的词起点"，把边界吸附过去，
    再按词序号区间分文本 —— 每条 cue 的词是连续区间，时间也是这些词的真实时间。
    吸附后变空的 cue 直接丢掉（不产生空字幕）。
    """
    if not word_stream:
        return cues
    words = sorted(word_stream, key=lambda x: x[1])
    starts = [w[1] for w in words]
    n = len(cues)
    # 边界：cue 的 t0/t1 序列（相邻 cue 的 t1==t0 时取一个）
    bounds = []
    for c in cues:
        if not bounds or abs(bounds[-1] - c["s"]) > 1e-6:
            bounds.append(c["s"])
        if not bounds or abs(bounds[-1] - c["e"]) > 1e-6:
            bounds.append(c["e"])
    # 吸附到最近的词起点（单调）
    snapped, prev = [], 0
    for t in bounds:
        best, bestd = prev, 1e9
        for k in range(prev, len(starts)):
            d = abs(starts[k] - t)
            if d < bestd:
                best, bestd = k, d
            if starts[k] > t + 2.0:
                break
        snapped.append(best)
        prev = best
    out, k = [], 0
    for bi in range(len(snapped) - 1):
        a, b = snapped[bi], snapped[bi + 1]
        seg = words[a:b]
        txt = " ".join(w[0] for w in seg).strip()
        if not txt:
            continue
        t0 = words[a][1]
        t1 = words[b][1] if b < len(words) else words[-1][2]
        out.append({"t0": fmt_ts(t0), "t1": fmt_ts(t1), "text": txt})
    return out


def resegment(words, gap=0.35, max_sec=6.0, max_chars=48):
    """从"词流"重新切 cue：停顿 >= gap 或超过长度/时长上限就断句。

    比"把文本塞回原 cue"更干净 —— 原 cue 边界是 whisper 的段边界，跟 qwen 的
    词并不对齐，硬塞会出现"句首多半个词"。这里直接以词时间轴为准。
    """
    cues, cur = [], []
    for w, s0, e0 in words:
        if cur:
            last = cur[-1]
            if (s0 - last[2] >= gap or (e0 - cur[0][1]) > max_sec or
                    sum(len(x[0]) + 1 for x in cur) + len(w) > max_chars):
                cues.append(cur)
                cur = []
        cur.append((w, s0, e0))
    if cur:
        cues.append(cur)
    # 太短的 cue（<0.9s，一闪而过）并到下一句或上一句
    merged = []
    for c in cues:
        dur = c[-1][2] - c[0][1]
        if merged and dur < 0.9:
            merged[-1].extend(c)
        elif merged and (c[0][1] - merged[-1][-1][2]) < 0.25 and dur < 1.2:
            merged[-1].extend(c)
        else:
            merged.append(list(c))
    # 合并可能让某条变超长 → 再切一次：优先在标点处断（成句更像字幕）
    final = []
    for c in merged:
        cur = []
        for w in c:
            if cur:
                chars = sum(len(x[0]) + 1 for x in cur) + len(w[0])
                too_long = chars > max_chars or (w[2] - cur[0][1]) > max_sec
                if too_long:
                    # 从后往前找最近一个以标点结尾的词，在那里断
                    cut = None
                    for k in range(len(cur) - 1, max(0, len(cur) - 7) - 1, -1):
                        if re.search(r"[.,?!;:\u2014-]$", cur[k][0]):
                            cut = k + 1
                            break
                    if cut is None:
                        cut = len(cur)
                    final.append(cur[:cut])
                    cur = cur[cut:]
            cur.append(w)
        if cur:
            final.append(cur)
    out = []
    for c in final:
        txt = " ".join(x[0] for x in c).strip()
        if txt:
            out.append({"t0": fmt_ts(c[0][1]), "t1": fmt_ts(c[-1][2]), "text": txt})
    return out


def fmt_ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    sec, ms = int(t % 60), int(round((t - int(t)) * 1000))
    if ms >= 1000:
        sec, ms = sec + 1, ms - 1000
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def align_words(whisper_words, qwen_text):
    """把 qwen 的词按序列对齐到 whisper 词时间上。

    whisper_words: [(w, s, e)]
    返回 (word, start, end) 列表：匹配上的用 whisper 时间；
    插入/替换的词用前后最近的时间线性插值。
    """
    qw = qwen_text.split()
    if not whisper_words:
        return [(w, 0.0, 0.0) for w in qw]
    a = [norm(w) for w, _, _ in whisper_words]
    b = [norm(w) for w in qw]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = [(None, whisper_words[i][1], whisper_words[i][2]) for i in range(len(a))]
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for t in range(i2 - i1):
                w = qw[j1 + t] if j1 + t < len(qw) else whisper_words[i1 + t][0]
                out[i1 + t] = (w, whisper_words[i1 + t][1], whisper_words[i1 + t][2])
        elif tag == "replace":
            t0 = whisper_words[i1][1] if i1 < len(whisper_words) else 0.0
            t1 = whisper_words[i2 - 1][2] if i2 - 1 < len(whisper_words) else t0 + 0.3
            n = max(1, j2 - j1)
            for t in range(i2 - i1):
                idx = j1 + t
                w = qw[idx] if idx < j2 else None
                frac = (t + 0.5) / max(i2 - i1, n)
                out[i1 + t] = (w, t0 + (t1 - t0) * frac, t0 + (t1 - t0) * (frac + 1.0 / max(i2 - i1, n)))
        elif tag == "insert":
            pos = i1 - 1
            t0 = whisper_words[pos][2] if pos >= 0 else 0.0
            t1 = whisper_words[i1][1] if i1 < len(whisper_words) else t0 + 0.3
            n = max(1, j2 - j1)
            for t in range(j2 - j1):
                out.insert(i1 + t, (qw[j1 + t], t0 + (t1 - t0) * t / n,
                                    t0 + (t1 - t0) * (t + 1) / n))
        # delete：whisper 多出来的词直接丢掉（qwen 没听到）
    return [x for x in out if x[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--srt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-chunk", type=float, default=25.0)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 块（调试/白天单测用）")
    ap.add_argument("--words", help="whisper JSON（含词级时间戳）：词级对齐模式")
    ap.add_argument("--resegment", action="store_true",
                    help="用词流重新切句（推荐）：不沿用原 cue 边界")
    a = ap.parse_args()

    cues = parse_srt(a.srt)
    if a.words:
        return word_mode(a, cues)
    chunks = build_chunks(cues, a.max_chunk)
    print(f"{len(cues)} cues -> {len(chunks)} chunks", file=sys.stderr)
    tmp = tempfile.mkdtemp(prefix="reasr-")
    changed = 0
    for n, ch in enumerate(chunks):
        if a.limit and n >= a.limit:
            break
        w = os.path.join(tmp, f"c{n}.wav")
        cut(a.wav, ch[0]["s"], ch[-1]["e"], w)
        try:
            text = asr(w)
        except Exception as e:  # noqa: BLE001
            print(f"  chunk {n} ({ch[0]['s']:.1f}s): ASR 失败 {e}，保留原文本", file=sys.stderr)
            continue
        new = assign(ch, text)
        for c, t in zip(ch, new):
            if t and t != c["text"]:
                c["text"] = t
                changed += 1
        print(f"  [{n+1}/{len(chunks)}] {ch[0]['s']:7.1f}s {len(ch)} cues: {text[:70]}...",
              file=sys.stderr)
    write_srt(a.out, cues)
    print(f"改动 {changed} 条 -> {a.out}", file=sys.stderr)
    return 0


def word_mode(a, cues):
    """词级对齐模式：whisper 出时间、qwen 出文本，重建 cue 文本（时间轴沿用 SRT）。"""
    data = json.load(open(a.words, encoding="utf-8"))
    ww = []
    for seg in data["segments"]:
        for w in seg.get("words", []):
            ww.append((w["w"].strip(), w["s"], w["e"]))
    print(f"whisper 词 {len(ww)} 个，cue {len(cues)} 条", file=sys.stderr)
    chunks = build_chunks(cues, a.max_chunk)
    tmp = tempfile.mkdtemp(prefix="reasr-")
    stream = []
    for n, ch in enumerate(chunks):
        if a.limit and n >= a.limit:
            break
        w = os.path.join(tmp, f"c{n}.wav")
        cut(a.wav, ch[0]["s"], ch[-1]["e"], w)
        try:
            text = asr(w)
        except Exception as e:  # noqa: BLE001
            print(f"  chunk {n}: ASR 失败 {e}", file=sys.stderr)
            continue
        sub = [(x[0], x[1], x[2]) for x in ww if x[1] >= ch[0]["s"] - 0.2 and x[2] <= ch[-1]["e"] + 0.2]
        stream.extend(align_words(sub, text))
        print(f"  [{n+1}/{len(chunks)}] {ch[0]['s']:7.1f}s {len(ch)} cues: {text[:60]}...",
              file=sys.stderr)
    # 对齐后的词流可能因插值而局部乱序 —— 重切句前必须按时间排好
    stream.sort(key=lambda x: (x[1], x[2]))
    if a.resegment:
        new = resegment(stream)
    else:
        new = rebuild_from_words(cues, stream)
    write_srt(a.out, new)
    print(f"重建 {len(new)} 条（原 {len(cues)}）-> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
