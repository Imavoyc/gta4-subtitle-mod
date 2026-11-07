#!/usr/bin/env python3
"""字幕校对工具（本地 Web，只用 Python 标准库）。

    python3 review/serve.py --en weazel.en.srt --zh weazel.zh.srt \
        --glossary pipeline/glossary.tsv [--port 8765]

打开 http://127.0.0.1:8765 ：

* 左原文 / 右译文（可编辑，两行以内），逐条翻页；
* 每条右侧标 QA 结果（红=错、黄=警：行长、CPS、术语、字库外字符…）；
* 键盘：j/k 上下条，s 保存，g 把选中/本条的专名加进术语表，a 让 AI 重写当前条；
* 保存写回 SRT（时间轴不动），并立即重跑 QA；
* AI 按钮是"单条请求"级别的辅助（符合 AGENTS.md：白天只允许单条测试）。
"""
import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.join(os.path.dirname(HERE), "pipeline")
sys.path.insert(0, PIPE)

import glossary as gloss   # noqa: E402
import qa                  # noqa: E402
import translate_srt as tr  # noqa: E402

STATE = {"en": None, "zh": None, "glossary": None, "style": None}


def load_rows():
    terms = gloss.load(STATE["glossary"])
    rows = qa.check(STATE["en"], STATE["zh"], terms)
    return {"cues": rows, "terms": terms}


def save_rows(payload):
    cues = tr.parse_srt(STATE["zh"])
    by_index = {int(c["i"]): c["zh"] for c in payload.get("cues", [])}
    for c in cues:
        pass
    # tr.parse_srt 不保留下标，按顺序改
    for i, c in enumerate(cues, 1):
        if i in by_index:
            c["zh"] = by_index[i]
    tr.write_srt(STATE["zh"], cues)
    return load_rows()


def add_term(payload):
    term = (payload.get("term") or "").strip()
    zh = (payload.get("zh") or "").strip()
    if not term or not zh:
        return {"ok": False, "error": "term/zh 不能为空"}
    path = STATE["glossary"]
    existing = {t["term"].lower() for t in gloss.load(path)}
    if term.lower() in existing:
        return {"ok": True, "skipped": True}
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{term}\t{zh}\t{payload.get('kind','') or '待分类'}\t待校对\n")
    return {"ok": True}


def ai_assist(payload):
    """单条 AI 辅助：只改当前条，前后各 3 条作为只读上下文。"""
    i = int(payload.get("i", 1))
    action = payload.get("action", "polish")
    ask = (payload.get("ask") or "").strip()
    en = tr.parse_srt(STATE["en"])
    zh = tr.parse_srt(STATE["zh"])
    idx = i - 1
    if not (0 <= idx < len(en)):
        return {"ok": False, "error": "index out of range"}
    ctx = []
    for j in range(max(0, idx - 3), min(len(en), idx + 4)):
        mark = ">>" if j == idx else "  "
        ctx.append(f"{mark} EN: {en[j]['en']}")
        if j < len(zh) and zh[j].get("zh"):
            ctx.append(f"{mark} ZH: {zh[j]['zh']}")
    acts = {
        "shorten": f"把当前条（>> 那行）改短，满足 行长≤{tr.MAX_LINE_CHARS} 字、"
                   f"CPS≤{tr.MAX_CPS}，意思不变，只输出译文。",
        "polish": "把当前条（>> 那行）润色成更自然的中文口语字幕，意思不变，只输出译文。",
        "literal": "把当前条（>> 那行）改得更贴近原文、别意译过多，只输出译文。",
        "ask": ask or "解释当前这条的原文含义，并给一版更好的中文译文（只输出译文）。",
    }
    terms = gloss.load(STATE["glossary"])
    rel = gloss.relevant_terms(terms, en[idx]["en"])
    system = ("你是 GTA IV 字幕校对助手。只处理 `>>` 那一条，其它行是只读上下文。"
              "翻译要口语化、保留粗口力度、只用常用简体汉字。")
    block = gloss.as_prompt_block(rel)
    if block:
        system += "\n\n" + block
    reply = tr.chat([{"role": "system", "content": system},
                     {"role": "user", "content": "\n".join(ctx) + "\n\n" + acts.get(action, acts["ask"])}],
                    temperature=0.3)
    return {"ok": True, "text": reply.strip().strip('"').strip()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def log_message(self, *a):  # 安静点
        pass

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"):
            body = open(os.path.join(HERE, "index.html"), "rb").read()
            return self._send(200, body, "text/html; charset=utf-8")
        if p == "/api/data":
            return self._json(load_rows())
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/save":
            return self._json(save_rows(payload))
        if p == "/api/term":
            return self._json(add_term(payload))
        if p == "/api/ai":
            try:
                return self._json(ai_assist(payload))
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--en", required=True)
    ap.add_argument("--zh", required=True)
    ap.add_argument("--glossary", default=gloss.DEFAULT)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    STATE.update(en=a.en, zh=a.zh, glossary=a.glossary)
    rows = load_rows()
    nerr = sum(1 for r in rows["cues"] for s, _ in r["issues"] if s == "err")
    print(f"{len(rows['cues'])} cues, {nerr} error(s), {len(rows['terms'])} terms")
    print(f"http://{a.host}:{a.port}")
    HTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
