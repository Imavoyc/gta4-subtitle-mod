#!/usr/bin/env python3
"""用**词级时间戳**给已有电台字幕重新定时（不改译文，只改 cue 的起止时间）。

## 为什么要这一步

电台段是"21.248 s 一个通道"的容器，现有的 cue 时间是这样来的：
`通道号 × 21.25 + 通道内按**字数比例**切`。字数比例和真实语速不一致（有人一句话停半天），
所以段内会差 1~3 s —— 这就是"字幕跟得上、但对不准"的来源。

## 怎么做

1. 对每个有文本的通道，解出 PCM（`ivaud2pcm --ch k`）→ 16 kHz；
2. `faster-whisper` 跑 `word_timestamps=True` 拿到**词级时间**（模型只用来定时间，
   文本仍以 qwen3-asr 的为准 —— 后者的英文质量更好）；
3. 把已有 cue 的文本按词对齐到 whisper 的词流上（difflib，顺序约束），
   `start = 首个匹配词的起点`、`end = 末个匹配词的终点`；
   对不齐（匹配率 < 0.5）的 cue 保留原来的比例时间，并在报告里计数；
4. 输出仍然是 per-track SRT（`<hash>.<lang>.srt`，时间轴 = 文件内时间），
   插件按引擎 `ms × 文件时长/len` 换算后直接命中。

用法：
    radio_retime.py --asr asr.jsonl --seg-dir ~/tmp/<日期>/seg-all \
                    --en-dir radio-srt --zh-dir gta4-zh --out ~/tmp/<日期>/radio-tracks-v2 \
                    [--stations radio_plr,radio_wktt] [--limit-channels 20] [--threads 8]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CLIP = 21.248                     # 每通道音频时长（秒）
SR = 48000
IVAUD2PCM = os.environ.get("IVAUD2PCM", "/tmp/ivaud2pcm")


# ------------------------------------------------------------------ SRT 读写
TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _sec(m):
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse_srt(path):
    cues = []
    if not os.path.exists(path):
        return cues
    cur, have = None, False
    for line in open(path, encoding="utf-8", errors="replace"):
        l = line.rstrip("\r\n")
        if not l.strip():
            if have and cur and cur[2]:
                cues.append([cur[0], cur[1], cur[2].strip()])
            cur, have = None, False
            continue
        if "-->" in l:
            m = list(TS.finditer(l))
            if len(m) >= 2:
                cur = [_sec(m[0]), _sec(m[1]), ""]
                have = True
            continue
        if have and cur is not None:
            cur[2] += ("" if not cur[2] else "\n") + l
    if have and cur and cur[2]:
        cues.append([cur[0], cur[1], cur[2].strip()])
    return cues


def ts(sec):
    sec = max(0.0, sec)
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s, ms = s + 1, ms - 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def load_map(path):
    out = {}
    for i, line in enumerate(open(path, encoding="utf-8")):
        if i == 0:
            continue
        p = line.rstrip("\n").split("\t")
        if len(p) >= 2:
            f, _, rest = p[1].partition("#")
            ch = rest.split(".")[0] if rest else ""
            out[int(p[0])] = (f, ch)
    return out


# ------------------------------------------------------------------ 通道解码
def decode_channel(seg_path, ch, tmp_raw):
    """解一个通道 → 16 kHz 单声道 float32（[-1,1]），直接喂 faster-whisper。

    注意：不要写 wav 再让 faster-whisper 去 open —— 那个 venv 里的 PyAV 版本
    和它传的 `metadata_errors` 参数不兼容（TypeError），直接给 numpy 数组最稳。
    """
    r = subprocess.run([IVAUD2PCM, seg_path, tmp_raw, "--ch", str(ch)],
                       capture_output=True)
    if r.returncode != 0 or not os.path.exists(tmp_raw):
        return None
    pcm = np.fromfile(tmp_raw, dtype="<i2")
    os.unlink(tmp_raw)
    if len(pcm) < SR:
        return None
    x = pcm.astype(np.float32) / 32768.0
    taps = np.hanning(15)
    taps /= taps.sum()
    return np.convolve(x, taps, mode="same")[::3].astype(np.float32)


# ------------------------------------------------------------------ 对齐
WORD_RE = re.compile(r"[a-z0-9']+")


def words_of(text):
    return WORD_RE.findall(text.lower().replace("’", "'"))


def retime_cues(cues, words, ch_start):
    """把通道内的 cue 按词流重新定时。

    返回 (times, dups)：
      times = {原 cue 序号: (start, end)}   —— 只有重定时成功的 cue 才在里面
      dups  = [(原 cue 序号, 偏移秒)]        —— "同一段话连播两遍"时，第二遍要复制的条目

    做法：整通道一次全局对齐（拼成一条词流再和 whisper 词流做一次 SequenceMatcher），
    比逐条对齐稳；再按 token 匹配率 + 整通道覆盖率做两道把关；
    最后若检测到通道是"双循环"，把 cue 表复制一份到后半段。
    """
    import difflib

    if not words:
        return {}, []
    wtok = [w for _, _, w in words]
    flat = []
    for i, (_, _, txt) in enumerate(cues):
        for t in words_of(txt):
            flat.append((i, t))
    if not flat:
        return {}, []

    sm = difflib.SequenceMatcher(None, [t for _, t in flat], wtok, autojunk=False)
    hit = {}
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            hit.setdefault(flat[b.a + k][0], []).append(b.b + k)

    times = {}
    for i, (s0, e0, txt) in enumerate(cues):
        idxs = hit.get(i) or []
        toks = words_of(txt)
        if not idxs or (toks and len(idxs) / len(toks) < 0.55):
            continue
        a = ch_start + words[idxs[0]][0]
        b = ch_start + words[idxs[-1]][1] + 0.15
        old = max(e0 - s0, 0.5)
        if b <= a or (b - a) < 0.4 or not (0.25 * old <= (b - a) <= 4.0 * old):
            continue
        times[i] = (a, b)

    if not times:
        return {}, []

    # 先算把"双循环"补起来之后的有效覆盖，再拿它跟原来比 —— 顺序反了会把
    # 本来就该复制的通道当成"缩水"毙掉（踩过）
    old_span = sum(max(e0 - s0, 0.0) for s0, e0, _ in cues)
    base_span = sum(b - a for a, b in times.values())
    # 复读检测（不靠"词序列重复率"，那个不可靠）：如果**大半个词流没被对齐上**，
    # 说明这段音频把同一段话播了两遍（ASR 文本里被 dedupe 成一遍），
    # 那就把还没用到的词再对齐一次，产出第二份 cue（时间按真实词时间）。
    dups2 = []
    used = [max(v) for v in hit.values()] if hit else []
    last_used = max(used) if used else -1
    tail_from = last_used + 1
    if tail_from >= 8 and len(wtok) - tail_from >= 10:
        sub = wtok[tail_from:]
        flat2 = [(i, t) for i, (_, _, txt) in enumerate(cues) for t in words_of(txt)]
        if flat2:
            sm2 = difflib.SequenceMatcher(None, [t for _, t in flat2], sub, autojunk=False)
            hit2 = {}
            for b in sm2.get_matching_blocks():
                for k2 in range(b.size):
                    hit2.setdefault(flat2[b.a + k2][0], []).append(tail_from + b.b + k2)
            for i in list(times.keys()):
                idxs = hit2.get(i) or []
                toks = words_of(cues[i][2])
                if not idxs or (toks and len(idxs) / len(toks) < 0.55):
                    continue
                a2 = ch_start + words[idxs[0]][0]
                b2 = ch_start + words[idxs[-1]][1] + 0.15
                if b2 - a2 < 0.4:
                    continue
                dups2.append((i, a2, b2))
    dups = dups2
    eff_span = base_span + sum(b2 - a2 for _i, a2, b2 in dups)
    old_lo, old_hi = min(c[0] for c in cues), max(c[1] for c in cues)
    lo = min(a for a, _ in times.values())
    hi = max([b for _, b in times.values()] + [b2 for _i, _a2, b2 in dups])
    if old_span > 1.0 and not (0.6 * old_span <= eff_span <= 1.8 * old_span):
        return {}, []
    if (old_hi - old_lo) > 2.0 and (hi - lo) < 0.6 * (old_hi - old_lo):
        return {}, []

    return times, dups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", required=True, help="asr.jsonl（提供 station/file/ch 清单）")
    ap.add_argument("--seg-dir", required=True, help="抽出来的段文件根目录（<station>/<FILE>）")
    ap.add_argument("--en-dir", required=True, help="台级 EN SRT + .map.tsv 目录")
    ap.add_argument("--zh-dir", default="", help="台级 ZH SRT 目录")
    ap.add_argument("--out", required=True, help="输出 per-track SRT 目录")
    ap.add_argument("--stations", default="", help="逗号分隔，只处理这些台")
    ap.add_argument("--limit-channels", type=int, default=0)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--model", default="small")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device="cpu", compute_type="int8", cpu_threads=a.threads)

    import radio_hash_table as HT
    table = HT.load()
    # ⚠️ 必须按 (rpf, 文件名) 建索引：ID_01 / SOLO_02 这类名字每个台都有，
    # 只按文件名会互相覆盖（写出来的 SRT 数量会莫名少一半，插件还会显示别的台的词）
    key2hash = {(e["rpf"][:-4], e["file"].lower()): h for h, e in table.items()}

    # 需要处理的通道：asr.jsonl 里每个 (station, file, ch) 一条
    need = defaultdict(set)                      # (station, file) -> {ch}
    order = []
    only = set(s.strip() for s in a.stations.split(",") if s.strip())
    for line in open(a.asr, encoding="utf-8"):
        r = json.loads(line)
        if not r.get("text", "").strip():
            continue
        if only and r["station"] not in only:
            continue
        k = (r["station"], r["file"])
        if k not in need:
            order.append(k)
        need[k].add(int(r["ch"]))

    os.makedirs(a.out, exist_ok=True)
    stats = []
    nchan = 0
    tmpdir = tempfile.mkdtemp(prefix="retime-")
    hit_limit = False
    for station, fname in order:
        if hit_limit:
            break
        seg = os.path.join(a.seg_dir, station, fname)
        if not os.path.exists(seg):
            stats.append((station, fname, 0, 0, "no-seg"))
            continue
        en = parse_srt(os.path.join(a.en_dir, f"{station}.en.srt"))
        zh = parse_srt(os.path.join(a.zh_dir, f"{station}.zh.srt")) if a.zh_dir else []
        cmap = load_map(os.path.join(a.en_dir, f"{station}.map.tsv"))
        # cue 序号(1 起) → (file, ch)
        by_ch_en, by_ch_zh = defaultdict(list), defaultdict(list)
        for i, c in enumerate(en, 1):
            f, ch = cmap.get(i, ("", ""))
            if f.lower() != fname.lower() or not ch.isdigit():
                continue
            by_ch_en[int(ch)].append(list(c))
        for i, c in enumerate(zh, 1):
            f, ch = cmap.get(i, ("", ""))
            if f.lower() != fname.lower() or not ch.isdigit():
                continue
            by_ch_zh[int(ch)].append(list(c))
        if not by_ch_en:
            stats.append((station, fname, 0, 0, "no-cues"))
            continue
        fixed_en, fixed_zh, total, done = [], [], 0, 0
        for ch in sorted(by_ch_en):
            if a.limit_channels and nchan >= a.limit_channels:
                hit_limit = True
                break
            audio = decode_channel(seg, ch, os.path.join(tmpdir, "ch.raw"))
            if audio is None:
                fixed_en += by_ch_en[ch]
                fixed_zh += by_ch_zh.get(ch, [])
                total += len(by_ch_en[ch])
                continue
            # vad_filter + 关掉上下文继承：没有 VAD 时 whisper 在有音乐床的通道上
            # 会陷入重复解码（实测单通道能跑几分钟），VAD 顺手把纯音乐段切掉也更准。
            cache = os.path.join(a.out, "words", f"{station}__{fname}__{ch}.json")
            words_cache = None
            if os.path.exists(cache):
                try:
                    rows = json.load(open(cache, encoding="utf-8"))
                    words_cache = [(r[0], r[1], r[2]) for r in rows]
                except Exception:
                    words_cache = None

            def run(vad):
                t0 = time.time()
                segs, _ = model.transcribe(audio, language="en", word_timestamps=True,
                                           vad_filter=vad, beam_size=1,
                                           condition_on_previous_text=False)
                ws = []
                for s in segs:
                    for w in (s.words or []):
                        tok = w.word.strip().lower()
                        if tok:
                            ws.append((w.start, w.end, tok))
                return ws, time.time() - t0

            expect = sum(len(words_of(c[2])) for c in by_ch_en[ch])
            if words_cache is not None:
                words = words_cache
            else:
                # 先走 VAD 快路（纯音乐段会被切掉、也不会陷入重复解码）；
                # 若 VAD 把话也切没了（词数远少于 cue 文本），再退回无 VAD 慢路。
                words, _dt = run(True)
                if expect and len(words) < 0.4 * expect:
                    words2, _dt2 = run(False)
                    if len(words2) > len(words):
                        words = words2
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                with open(cache, "w", encoding="utf-8") as cf:
                    json.dump([[w[0], w[1], w[2]] for w in words], cf)
            nchan += 1
            ch_start = ch * CLIP
            times, dups = retime_cues(by_ch_en[ch], words, ch_start)
            # 中文 cue 不能自己对英文词流对齐；但中英逐条一一对应 → 按序号抄英文的时间，
            # 英文那边"循环复制"的条目，中文也照样复制。
            zh_ch = by_ch_zh.get(ch, [])
            if len(zh_ch) == len(by_ch_en[ch]):
                new_en = [[times[i][0], times[i][1], c[2]] if i in times else list(c)
                          for i, c in enumerate(by_ch_en[ch])]
                new_zh = [[times[i][0], times[i][1], zh_ch[i][2]] if i in times else list(zh_ch[i])
                          for i in range(len(zh_ch))]
                for i, a2, b2 in dups:
                    tt = new_en[i][2]           # 别用 a/b（`a` 是 argparse 的 namespace！）
                    new_en.append([a2, b2, tt])
                    tt = new_zh[i][2]
                    new_zh.append([a2, b2, tt])
            else:
                new_en = [[times[i][0], times[i][1], c[2]] if i in times else list(c)
                          for i, c in enumerate(by_ch_en[ch])]
                new_zh = list(zh_ch)
            ok = len(times)
            fixed_en += new_en
            fixed_zh += new_zh
            total += len(by_ch_en[ch])
            done += ok
        if not fixed_en:            # 被 --limit 截断时不要写空文件覆盖已有结果
            stats.append((station, fname, total, done, "skipped"))
            continue
        h = key2hash.get((station, fname.lower()))
        if h:
            for lang, cues in (("en", fixed_en), ("zh", fixed_zh)):
                if not cues:
                    continue
                p = os.path.join(a.out, f"{h}.{lang}.srt")
                with open(p, "w", encoding="utf-8") as f:
                    for k, (s, e, t) in enumerate(sorted(cues, key=lambda c: c[0]), 1):
                        f.write(f"{k}\n{ts(s)} --> {ts(e)}\n{t}\n\n")
        stats.append((station, fname, total, done, ""))
        print(f"{station:26s} {fname:28s} cues={total:3d} retimed={done:3d}", flush=True)

    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write("station\tfile\tcues\tretimed\tnote\n")
            for st, fn, tot, dn, note in stats:
                f.write(f"{st}\t{fn}\t{tot}\t{dn}\t{note}\n")
    tot = sum(s[2] for s in stats)
    dn = sum(s[3] for s in stats)
    print(f"\n合计 {tot} 条 cue，重定时 {dn} 条（{dn/max(tot,1)*100:.0f}%），处理 {nchan} 个通道")


if __name__ == "__main__":
    main()
