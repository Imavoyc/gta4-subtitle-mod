#!/usr/bin/env python3
"""电台「歌曲」部分：用 netease 歌单/LRC 覆盖，不用 ASR 转写。

思路：电台里 531 首歌曲本体不需要转文本（有歌词），只有 DJ 串词需要 ASR（已做）。
这个脚本负责把 RPF 里的歌曲文件名对上 netease 的歌（歌单 + 搜索接口），
然后把 LRC（带时间轴优先）拉下来，最后统计**覆盖率**（按首数 和 按分钟）。

数据源：
  * `/mnt/data/netease-lib/pl_*.songs.tsv`（GTA IV 各台歌单，title/artist/时长）
  * `netease-cli find <lib> --q <kw>`（搜索接口，用于歌单里没有的曲目）
  * `netease-cli lyric <song-id> <lib>`（下载 LRC 到 <lib>/lrc/<id>.lrc）

用法：
  radio_lyrics_match.py --out ~/tmp/<日期>/gta4-lyrics [--search] [--limit N]
输出：<out>/lyrics-<station>.tsv、<out>/by-station/<station>/*.lrc、<out>/report.md
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys

CLI = os.path.expanduser("~/code/host-project/apps/netease-cli/build/netease-cli")
LIB = "/mnt/data/netease-lib"
LISTING = os.path.expanduser("~/tmp/2026-11-06/gta4-radio-cat/rpf_listing.json")
DJ_RE = re.compile(r"^(ID_|SOLO_|INTRO|TIME|TO|GENERAL)")
SKIP = {"radio_extratracks", "radio_beat_95", "radio_bobby_konders", "radio_dance_mix",
        "radio_ny_classics", "radio_ramjamfm"}
# RPF 电台目录 -> 维基（西语版，含全部曲目）里的台名
STATION_MAP = {
    "radio_the_vibe": "The Vibe 98.8",
    "radio_liberty_rock": "Liberty Rock Radio 97.8",
    "radio_k109_the_studio": "K109 The Studio",
    "radio_san_juan_sounds": "San Juan Sounds",
    "radio_vladivostok": "Vladivostok FM",
    "radio_hardcore": "Liberty City Hardcore (L.C.H.C.)",
    "radio_dance_rock": "Radio Broker",
    "radio_meditation": "Self-Actualization FM",
    "radio_vcfm": "Vice City FM",
    "radio_ramjamfm": "RamJam FM",
    "radio_classical_ambient": "The Classics 104.1",
    "radio_fusion_fm": "Fusion FM",
    "radio_afro_beat": "International Funk 99 (IF99)",
    "radio_jazz_nation": "Jazz Nation Radio 108.5 (JNR)",
    "radio_babylon": "The Journey",
    "radio_bobby_konders": "Massive B Soundsystem 96.9",
    "radio_beat_95": "The Beat 102.7",
    "radio_independence": "The Journey",
}
TRACKLIST = "/tmp/gta4_tracklist.json"

CLIP_SEC = 21.25          # 每个"通道"是一段 21.25 秒
BYTES_PER_CLIP = 509952


def norm(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def demangle(name):
    """JUSTBEGOODTOME 这种没法自动分词 → 返回 None（只能靠歌单/搜索兜）"""
    return re.sub(r"[_\-.]+", " ", name)


def load_dict():
    """歌单 -> {norm_title: [ {id,title,artist,dur} ]}"""
    idx = {}
    for f in glob.glob(os.path.join(LIB, "pl_*.songs.tsv")):
        for line in open(f, encoding="utf-8", errors="replace"):
            r = line.rstrip("\n").split("\t")
            if len(r) < 5:
                continue
            sid, title, artist, album, dur = r[0], r[1], r[2], r[3], r[4]
            try:
                dur = int(dur) / 1000.0
            except ValueError:
                dur = 0.0
            idx.setdefault(norm(title), []).append(
                {"id": sid, "title": title, "artist": artist, "dur": dur})
    return idx


def songs(listing, only=None, limit=0):
    d = json.load(open(listing, encoding="utf-8"))
    out = []
    for rpf, info in sorted(d.items()):
        st = rpf[:-4]
        if st in SKIP or (only and st not in only):
            continue
        for dirs in info["directories"]:
            for f in dirs["files"]:
                nm = f["name"]
                if DJ_RE.match(nm):
                    continue
                size = int(f["size"], 16) if isinstance(f["size"], str) else f["size"]
                ch = max(1, round(size / BYTES_PER_CLIP))
                out.append({"station": st, "file": nm, "size": size, "ch": ch,
                            "sec": ch * CLIP_SEC})
                if limit and len(out) >= limit:
                    return out
    return out


def has_stamp(path):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False, False
    stamped = bool(re.search(r"\[\d{1,2}:\d{2}[.:]\d{2,3}\]", txt))
    translated = "译" in txt or "translation" in txt.lower()
    return stamped, translated


def search_best(name, lib=LIB):
    q = demangle(name)
    try:
        p = subprocess.run([CLI, "find", lib, "--q", q, "--limit", "3"],
                           capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for line in p.stdout.splitlines():
        m = re.match(r"VERIFY: hit \d+ id=(\d+) (.*?) - (.*?) \[(\d+):(\d+)\]", line)
        if not m:
            continue
        sid, artist, title, mm, ss = m.groups()
        dur = int(mm) * 60 + int(ss)
        cand = {"id": sid, "title": title, "artist": artist, "dur": dur}
        score = difflib.SequenceMatcher(None, norm(name), norm(title)).ratio()
        if best is None or score > best[0]:
            best = (score, cand)
    if best and best[0] >= 0.75:
        return best[1]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--listing", default=LISTING)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--search", action="store_true", help="歌单里没有的用搜索接口兜")
    ap.add_argument("--tracklist", default=TRACKLIST, help="维基解析出的曲目表 JSON")
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "by-station"), exist_ok=True)
    idx = load_dict()
    tl = {}
    if a.tracklist and os.path.exists(a.tracklist):
        tl = {k: [(x[0], x[1]) for x in v] for k, v in
              json.load(open(a.tracklist, encoding="utf-8")).items()}
        print(f"维基曲目表 {len(tl)} 个台 / {sum(len(v) for v in tl.values())} 首", file=sys.stderr)
    print(f"歌单字典 {len(idx)} 个曲名", file=sys.stderr)
    only = set(x for x in a.only.split(",") if x) or None
    rows = []
    for s in songs(a.listing, only, a.limit):
        key = norm(s["file"])
        cand, how = None, ""
        # 1) 同台的维基曲目表（最可靠：台内唯一）
        wiki = tl.get(STATION_MAP.get(s["station"], ""), [])
        best_w, best_sc = None, 0.0
        for artist, title in wiki:
            k2 = norm(title)
            if not k2:
                continue
            sc = 1.0 if k2 == key else difflib.SequenceMatcher(None, key, k2).ratio()
            if sc > best_sc:
                best_sc, best_w = sc, (artist, title)
        if best_w and best_sc >= 0.8:
            artist, title = best_w
            hit = None
            for e in idx.get(norm(title), []):
                if norm(artist).split(",")[0][:8] in norm(e["artist"]) or True:
                    hit = e
                    break
            cand = hit or {"id": "", "title": title, "artist": artist, "dur": 0}
            how = f"wiki{best_sc:.2f}"
        elif key in idx:
            cand, how = idx[key][0], "playlist"
        else:
            best_score, best = 0.0, None
            for k, v in idx.items():
                if abs(len(k) - len(key)) > 12:
                    continue
                sc = difflib.SequenceMatcher(None, key, k).ratio()
                if sc > best_score:
                    best_score, best = sc, v[0]
            if best is not None and best_score >= 0.88:
                cand, how = best, f"fuzzy{best_score:.2f}"
        if cand is not None and not cand.get("id") and a.search:
            q = f"{cand['title']} {cand['artist'].split(',')[0].split('&')[0].strip()}"
            got = search_best(q, a.lib if hasattr(a, "lib") else LIB)
            if got:
                cand = got
                how += "+search"
        if cand is None and a.search:
            cand = search_best(s["file"])
            how = "search" if cand else ""
        lrc = os.path.join(LIB, "lrc", f"{cand['id']}.lrc") if cand else None
        if cand and not os.path.exists(lrc):
            subprocess.run([CLI, "lyric", cand["id"], LIB], capture_output=True, timeout=60)
        stamped, translated = has_stamp(lrc) if (lrc and os.path.exists(lrc)) else (False, False)
        rows.append({**s, "match": how, "song": cand["title"] if cand else "",
                     "artist": cand["artist"] if cand else "",
                     "sid": cand["id"] if cand else "",
                     "lrc": os.path.basename(lrc) if (lrc and os.path.exists(lrc)) else "",
                     "stamped": stamped, "translated": translated,
                     "netease_sec": cand["dur"] if cand else 0})
        print(f"{s['station']:22s} {s['file'][:34]:34s} -> "
              f"{(cand['title'][:30] if cand else '—'):30s} {how:10s} "
              f"lrc={'Y' if stamped else ('y' if rows[-1]['lrc'] else 'n')}", file=sys.stderr)

    # 导出 + 覆盖率
    by = {}
    for r in rows:
        st = r["station"]
        d = by.setdefault(st, {"n": 0, "sec": 0.0, "n_lrc": 0, "sec_lrc": 0.0,
                               "n_stamped": 0, "sec_stamped": 0.0, "n_tr": 0})
        d["n"] += 1
        d["sec"] += r["sec"]
        if r["lrc"]:
            d["n_lrc"] += 1
            d["sec_lrc"] += r["sec"]
        if r["stamped"]:
            d["n_stamped"] += 1
            d["sec_stamped"] += r["sec"]
            if r["translated"]:
                d["n_tr"] += 1
        os.makedirs(os.path.join(a.out, "by-station", st), exist_ok=True)
        if r["lrc"]:
            dst = os.path.join(a.out, "by-station", st, r["file"] + ".lrc")
            try:
                open(dst, "w", encoding="utf-8").write(
                    open(os.path.join(LIB, "lrc", r["lrc"]), encoding="utf-8",
                         errors="replace").read())
            except OSError:
                pass

    with open(os.path.join(a.out, "songs.tsv"), "w", encoding="utf-8") as f:
        f.write("station\tfile\tmin\tmatch\tsong\tartist\tsid\tlrc\tstamped\ttranslated\n")
        for r in rows:
            f.write(f"{r['station']}\t{r['file']}\t{r['sec']/60:.1f}\t{r['match']}\t"
                    f"{r['song']}\t{r['artist']}\t{r['sid']}\t{r['lrc']}\t"
                    f"{int(r['stamped'])}\t{int(r['translated'])}\n")

    lines = ["# 电台歌曲 × 歌词库 覆盖率", "",
             "| 电台 | 歌曲数 | 分钟 | 匹配到 | 有 LRC | 其中带时间轴 | 按分钟覆盖 |",
             "|---|---|---|---|---|---|---|"]
    tn = ts = tnl = tsl = tns = tss = 0
    for st, d in sorted(by.items()):
        lines.append(f"| {st} | {d['n']} | {d['sec']/60:.0f} | {d['n_lrc']} | "
                     f"{d['n_lrc']} | {d['n_stamped']} | "
                     f"{d['sec_stamped']/max(1e-9,d['sec'])*100:.0f}% |")
        tn += d["n"]; ts += d["sec"]; tnl += d["n_lrc"]; tsl += d["sec_lrc"]; tns += d["n_stamped"]; tss += d["sec_stamped"]
    lines += ["", f"**合计**：{tn} 首 / {ts/60:.0f} 分钟；匹配 {tnl} 首；"
                  f"带时间轴 LRC {tns} 首（按分钟覆盖 **{tss/max(1e-9,ts)*100:.0f}%**）。"]
    open(os.path.join(a.out, "report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
