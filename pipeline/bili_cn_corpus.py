#!/usr/bin/env python3
"""把 bilibili 上「赤佬听译」的 GTA4 电台视频变成**英中对照语料**。

他的画面就是"英文原句 + 中文译句"两行（有时还有注解），所以：
    bvid -> 720p mp4 -> 按字幕变化抽帧 -> glm-ocr 逐行 -> 配对(EN, ZH) -> JSONL

用途（比机翻可靠得多的人工翻译）：
  1. 直接复用：我们的英文转写和他视频里的英文句子对上（≥0.85 相似）→ 用他的中文；
  2. 建术语表 / 翻译记忆（TM）：`术语 = 译名`、整句对；
  3. 校验我们 MT 的用词与语气。

⚠️ 只用于个人学习用途；不分发视频、不搬运字幕。

用法：
  bili_cn_corpus.py --out ~/tmp/<日期>/cn-corpus [--limit 3] [--bvid BV1xxx] [--fps 1]
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://gpustack.rock-chips.com/v1/chat/completions"
MODEL = "glm-ocr"
UA = "Mozilla/5.0"
META = "https://api.bilibili.com/x/web-interface/view?bvid="
PLAY = ("https://api.bilibili.com/x/player/playurl?bvid={bv}&cid={cid}"
        "&qn=64&fnval=1&fourk=0")
HIS_MID = "11909177"


def http_json(url, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def list_videos(keyword="末代赤佬", pages=6):
    """用搜索接口列他的视频（避开 space 接口的 wbi 签名）。"""
    out, seen = [], set()
    for p in range(1, pages + 1):
        url = ("https://api.bilibili.com/x/web-interface/wbi/search/type?"
               f"search_type=video&keyword={urllib.parse.quote(keyword)}&page={p}&order=pubdate")
        try:
            d = http_json(url)
        except Exception:  # noqa: BLE001
            continue
        for r in ((d.get("data") or {}).get("result") or []):
            title = re.sub("<[^>]+>", "", r.get("title", ""))
            if "赤佬" not in r.get("author", "") or r["bvid"] in seen:
                continue
            seen.add(r["bvid"])
            out.append({"bvid": r["bvid"], "title": title, "dur": r.get("duration", ""),
                        "pubdate": r.get("pubdate", 0)})
        time.sleep(1)
    return out


def download(bv, outdir):
    dst = os.path.join(outdir, bv + ".mp4")
    if os.path.exists(dst) and os.path.getsize(dst) > 100000:
        return dst
    d = http_json(META + bv)
    v = d.get("data") or {}
    pages = v.get("pages") or []
    if not pages:
        raise RuntimeError("no pages")
    cid = pages[0]["cid"]
    pu = http_json(PLAY.format(bv=bv, cid=cid), referer=f"https://www.bilibili.com/video/{bv}/")
    durl = ((pu.get("data") or {}).get("durl") or [])
    if not durl:
        raise RuntimeError("no durl (需要登录?)")
    url = durl[0]["url"]
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": f"https://www.bilibili.com/video/{bv}/"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return dst


def frames(mp4, fps=1.0, outdir="."):
    """按 fps 抽帧（jpg），返回 [(t, path)]"""
    base = os.path.join(outdir, os.path.basename(mp4) + ".frames")
    os.makedirs(base, exist_ok=True)
    pattern = os.path.join(base, "%06d.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-vf", f"fps={fps}",
                    "-q:v", "3", pattern], check=True)
    out = []
    for i, name in enumerate(sorted(os.listdir(base)), 1):
        out.append(((i - 1) / fps, os.path.join(base, name)))
    return out


def ocr(img_bytes, tries=3):
    img = base64.b64encode(img_bytes).decode()
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
        {"type": "text", "text": "把图中所有文字按行原样输出，不要翻译、不要解释、不要加序号。"}]}]}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["GPUSTACK_API_KEY"]})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            return (d["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


CJK = re.compile(r"[\u4e00-\u9fff]")
# UP 的水印/署名会混进 OCR 行里，先剔掉
WATERMARK = re.compile(r"(末代赤佬|末代亦佬|末代赤老|赤佬听译|@?\s*bilibili|哔哩哔哩)", re.I)
LAT = re.compile(r"[A-Za-z]")


def split_pair(lines):
    """从 OCR 行里拆出 (英文句, 中文句)。他的排版：英文在上、中文在下。"""
    en, zh, notes = [], [], []
    for ln in lines:
        s = WATERMARK.sub("", ln).strip(" \t-_|/·、")
        s = re.sub(r"\s{2,}", " ", s)
        if not s:
            continue
        if s.startswith(("@", "*", "#")) or ("听译" in s and "GTA" in s) or s.startswith("GTA4"):
            continue
        has_cjk, has_lat = bool(CJK.search(s)), bool(LAT.search(s))
        if has_cjk and not has_lat:
            (notes if s.startswith("*") else zh).append(s)
        elif has_lat and not has_cjk:
            en.append(s)
        elif has_cjk and has_lat:
            # 中英混排（人名/品牌）→ 当成中文行
            zh.append(s)
    return " ".join(en), "".join(zh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--bvid", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--list-file", default="", help="预先缓存的视频清单 JSON（避免反复搜）")
    ap.add_argument("--local-dir", default="",
                    help="只处理这个目录里已有的 mp4（不下载、不访问 b 站；Windows 下载后传过来用这个）")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    jsonl = os.path.join(a.out, "pairs.jsonl")

    if a.local_dir:
        vids = [{"bvid": f[:-4], "title": f[:-4]} for f in sorted(os.listdir(a.local_dir))
                if f.lower().endswith(".mp4")]
        a.bvid = ""
        print(f"本地目录 {len(vids)} 个 mp4", file=sys.stderr)
    elif a.bvid:
        vids = [{"bvid": a.bvid, "title": a.bvid}]
    elif a.list_file and os.path.exists(a.list_file):
        vids = [v for v in json.load(open(a.list_file, encoding="utf-8"))
                if "GTA4" in v["title"] or "GTA 4" in v["title"]]
        print(f"清单文件 {len(vids)} 个 GTA4 视频", file=sys.stderr)
    else:
        all_v = list_videos()
        vids = [v for v in all_v if "GTA4" in v["title"] or "GTA 4" in v["title"]]
        print(f"他共 {len(all_v)} 个视频，其中 GTA4 相关 {len(vids)} 个", file=sys.stderr)
        vids.sort(key=lambda v: v["bvid"])
    if a.limit:
        vids = vids[:a.limit]
    print(f"目标 {len(vids)} 个视频", file=sys.stderr)

    done = set()
    if os.path.exists(jsonl):
        for line in open(jsonl, encoding="utf-8"):
            try:
                done.add(json.loads(line)["bvid"])
            except Exception:  # noqa: BLE001
                pass

    fout = open(jsonl, "a", encoding="utf-8")
    for v in vids:
        bv = v["bvid"]
        if bv in done:
            continue
        try:
            mp4 = (os.path.join(a.local_dir, bv + ".mp4") if a.local_dir
                   else download(bv, a.out))
        except Exception as e:  # noqa: BLE001
            print(f"  {bv} 下载失败: {e}", file=sys.stderr)
            continue
        if not os.path.exists(mp4):
            print(f"  {bv}: 没有 {mp4}", file=sys.stderr)
            continue
        fr = frames(mp4, a.fps, a.out)
        # 只在画面变化时 OCR（相邻帧的哈希差异大才重跑）
        pick, last_hash = [], None
        for t, path in fr:
            h = hashlib.md5(open(path, "rb").read()).hexdigest()
            if h != last_hash:
                pick.append((t, path))
                last_hash = h
        print(f"  {bv}: {len(fr)} 帧 -> OCR {len(pick)} 帧", file=sys.stderr)

        def work(item):
            t, path = item
            try:
                return t, ocr(open(path, "rb").read())
            except Exception as e:  # noqa: BLE001
                print(f"    OCR 失败 t={t}: {e}", file=sys.stderr)
                return t, ""

        rows = []
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for t, text in ex.map(work, pick):
                en, zh = split_pair(text.splitlines())
                if en or zh:
                    rows.append({"bvid": bv, "t": round(t, 2), "en": en, "zh": zh})
        # 常驻角标/水印（比如广告品牌名）在多数帧里都出现 → 按频率剔掉
        from collections import Counter
        fr_en, fr_zh = Counter(), Counter()
        for r in rows:
            if r["en"]:
                fr_en[r["en"]] += 1
            if r["zh"]:
                fr_zh[r["zh"]] += 1
        lim = max(3, int(len(rows) * 0.5))
        boiler = {k for k, c in fr_en.items() if c > lim} | {k for k, c in fr_zh.items() if c > lim}
        for k in boiler:
            for r in rows:
                if r["en"] == k:
                    r["en"] = ""
                if r["zh"] == k:
                    r["zh"] = ""
        # 去掉连续重复（同一句字幕持续几秒）
        dedup = []
        for r in rows:
            if dedup and r["en"] == dedup[-1]["en"] and r["zh"] == dedup[-1]["zh"]:
                continue
            dedup.append(r)
        # 完全空/只剩注解的丢掉
        dedup = [r for r in dedup
                 if (len(r["en"].split()) >= 2 or len(r["zh"]) >= 2)]
        for r in dedup:
            fout.write(json.dumps({**r, "title": v["title"]}, ensure_ascii=False) + "\n")
        fout.flush()
        print(f"  {bv}: {len(dedup)} 条 (EN {sum(1 for r in dedup if r['en'])} / "
              f"ZH {sum(1 for r in dedup if r['zh'])})", file=sys.stderr)
        # 帧目录很大，及时清
        subprocess.run(["rm", "-rf", os.path.join(a.out, os.path.basename(mp4) + ".frames")])
    fout.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
