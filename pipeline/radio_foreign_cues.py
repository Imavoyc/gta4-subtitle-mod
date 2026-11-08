"""电台里**非英语 cue**（俄语/乌克兰语/西语的外语版广告、Vladivostok FM 的俄语口播）
以及漏译的空条目：用"明确源语言"的提示词重译成中文。

为什么要单独处理：这些 cue 的原文不是英文，走普通 `translate_srt.py` 时模型会按英文
瞎猜（实测 62 条里大多数被译成无关内容）。哪些 cue 是外语，由 `radio_segment_index.py`
的 `lang` 字段给出（RPF 文件名里带 RUSSIAN/SPANISH 的 wave）。

用法：
    radio_foreign_cues.py [--zh-dir DIR] [--index JSON] [--srt-dir DIR]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import translate_srt as T  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--zh-dir", default=os.path.expanduser("~/tmp/2026-11-07/gta4-zh"))
_ap.add_argument("--index",
                 default=os.path.expanduser("~/tmp/2026-11-07/gta4-radio-index/segment_index.json"))
_ap.add_argument("--srt-dir", default=os.path.expanduser("~/tmp/2026-11-06/radio-srt"))
_args = _ap.parse_args()
ZH, SRT = _args.zh_dir, _args.srt_dir
IDX = json.load(open(_args.index, encoding="utf-8"))
SYS = ("你在给 GTA IV 的电台字幕做简体中文字幕。原文是**俄语/乌克兰语/西班牙语**的口播"
       "（Vladivostok FM、外语版广告），请译成自然口语的简体中文；"
       "保留粗口与玩笑的力度，不要音译成英文。"
       "输出格式：每行 `编号|原文前四词|译文`，条数与输入一致，不要解释。")

def blocks(p):
    return [b.strip().split('\n') for b in open(p, encoding='utf-8').read().split('\n\n')
            if b.strip() and '-->' in b]

def write(p, bl):
    with open(p, 'w', encoding='utf-8') as f:
        for b in bl:
            f.write(f"{b[0]}\n{b[1]}\n" + ("\n".join(b[2:]) if len(b) > 2 else "") + "\n\n")

total = 0
for st in sorted({s['station'] for s in IDX}):
    path = f'{ZH}/{st}.zh.srt'
    if not os.path.exists(path):
        continue
    info = [s for s in IDX if s['station'] == st][0]
    langs = {}
    for w, d in info['waves'].items():
        for c in d['cues']:
            langs[c['cue']] = d['lang']
    bl = blocks(path)
    enp = f'{SRT}/{st}.en.srt'
    en = blocks(enp) if os.path.exists(enp) else []
    def src_text(i):
        b = en[i - 1] if i - 1 < len(en) else []
        return (b[2] if len(b) > 2 else "").strip()
    todo = []
    for i, b in enumerate(bl, 1):
        zh0 = (b[2] if len(b) > 2 else "").strip()
        if not src_text(i):
            continue
        if langs.get(i) in ('ru', 'es') or not zh0:
            todo.append(i)
    if not todo:
        continue
    print(f"{st}: {len(todo)} 条俄/西语或空译", file=sys.stderr)
    for k in range(0, len(todo), 10):
        part = todo[k:k + 10]
        listing = "\n".join(f"{n+1}|{src_text(i)}" for n, i in enumerate(part))
        got = T.parse_pairs(T.chat([{"role": "system", "content": SYS},
                                    {"role": "user", "content": "待译条目：\n" + listing}]))
        for n, i in enumerate(part):
            e, zh = got.get(n + 1, (None, ""))
            if not zh:
                print(f"  #{i} 没回来", file=sys.stderr)
                continue
            z = T.tidy_zh(zh)
            keep = "".join(ch for ch in z if ord(ch) < 0x80 or ord(ch) in T.load_charset())
            bl[i-1] = bl[i-1][:2] + [keep]
            total += 1
        write(path, bl)
print("共修", total, "条", file=sys.stderr)
