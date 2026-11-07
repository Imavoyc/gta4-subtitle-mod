#!/usr/bin/env python3
"""电台「歌曲」歌词补全：把 RPF 里的歌名对到网易云并下载带时间轴的 LRC。

和 `radio_lyrics_match.py` 的区别（那个是第一版，只对歌单，覆盖率 14%）：

  * 先**离线**榨干本地资源（维基曲目表 × 本地歌单字典，含艺人和模糊匹配），
    只有离线确实解决不了的才发网络请求；
  * 网络阶段**限速 + 可续跑**：每个搜索结果都落盘（`state.tsv`），
    中断重跑只补缺口；搜索间隔默认 4 s（网易云连打约 400 次会被限流）；
  * 命中判据两条：歌名相似度 ≥0.8（有艺人时再加艺人相似度 ≥0.5）、LRC 必须带时间轴；
  * 输出沿用旧格式（`songs.tsv` / `report.md` / `by-station/<台>/<文件>.lrc`），
    下游不用改。

用法：
    radio_lyrics_fill.py --out ~/tmp/<日期>/gta4-lyrics2 [--sleep 4] [--limit N]
                         [--only radio_vcfm,radio_babylon] [--offline] [--retry]
"""
import argparse
import difflib
import glob
import json
import os
import random
import re
import subprocess
import sys
import time

CLI = os.path.expanduser("~/code/host-project/apps/netease-cli/build/netease-cli")
LIB = "/mnt/data/netease-lib"
LISTING = os.path.expanduser("~/tmp/2026-11-06/gta4-radio-cat/rpf_listing.json")
TRACKLIST = "/tmp/gta4_tracklist.json"
DJ_RE = re.compile(r"^(ID_|SOLO_|INTRO|TIME|TO|GENERAL|NEWS_|WEATHER_|AD\d|PACEMAKER|FIZZ)")
# 这几个台是一整个文件（连播 mix）或不是"歌"，没法按段挂歌词
SKIP = {"radio_extratracks", "radio_beat_95", "radio_bobby_konders", "radio_dance_mix",
        "radio_ny_classics", "radio_ramjamfm", "radio_adverts", "radio_news",
        "radio_weather", "radio_wktt", "radio_plr", "radio_lazlow"}
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
    "radio_classical_ambient": "The Classics 104.1",
    "radio_fusion_fm": "Fusion FM",
    "radio_afro_beat": "International Funk 99 (IF99)",
    "radio_jazz_nation": "Jazz Nation Radio 108.5 (JNR)",
    "radio_babylon": "The Journey",
    "radio_independence": "The Journey",
    "radio_ramjamfm": "RamJam FM",
    "radio_bobby_konders": "Massive B Soundsystem 96.9",
    "radio_beat_95": "The Beat 102.7",
}
CLIP_SEC = 21.25
BYTES_PER_CLIP = 509952


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def demangle(name):
    return re.sub(r"[_\-.]+", " ", name).strip()


def sim(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ---------------------------------------------------------------- 本地资源
def load_lib_index():
    """本地歌单字典：norm(title) -> [{id,title,artist,dur}]"""
    idx = {}
    for f in glob.glob(os.path.join(LIB, "pl_*.songs.tsv")):
        for line in open(f, encoding="utf-8", errors="replace"):
            r = line.rstrip("\n").split("\t")
            if len(r) < 5:
                continue
            try:
                dur = int(r[4]) / 1000.0
            except ValueError:
                dur = 0.0
            idx.setdefault(norm(r[1]), []).append(
                {"id": r[0], "title": r[1], "artist": r[2], "dur": dur})
    return idx


def rpf_songs(only=None, limit=0):
    d = json.load(open(LISTING, encoding="utf-8"))
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
                out.append({"station": st, "file": nm, "sec": ch * CLIP_SEC, "ch": ch})
                if limit and len(out) >= limit:
                    return out
    return out


def wiki_for(station, tl):
    return tl.get(STATION_MAP.get(station, ""), [])


# ---------------------------------------------------------------- 网络（限速）
class Pacer:
    def __init__(self, base):
        self.base = base
        self.last = 0.0
        self.bo = 0          # 连续空结果次数（退避）
        self.calls = 0

    def wait(self, kind="search"):
        gap = self.base if kind == "search" else min(2.0, self.base * 0.4)
        gap += random.uniform(0, gap * 0.4)
        dt = time.time() - self.last
        if dt < gap:
            time.sleep(gap - dt)
        self.last = time.time()
        self.calls += 1

    def cooldown(self, secs):
        print(f"    !! 命中网易云限流，冷却 {int(secs)} s", file=sys.stderr)
        time.sleep(secs)
        self.last = time.time()


def search(query, pacer, limit=5):
    """-> (hits, status)；status: ok / empty / ratelimit / error

    网易云被限流时返回的是"没有搜到"（和"真的没有这首歌"长得一样），
    所以这里用**连续空结果**来判定限流：第一次空当正常，连着 2 次就冷却。
    """
    pacer.wait("search")
    try:
        p = subprocess.run([CLI, "find", LIB, "--q", query, "--limit", str(limit)],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"    search 失败: {e}", file=sys.stderr)
        return [], "error"
    if "没有搜到" in p.stdout:
        return [], "empty"
    hits = []
    for line in p.stdout.splitlines():
        m = re.match(r"VERIFY: hit \d+ id=(\d+) (.*?) - (.*?) \[(\d+):(\d+)\]", line)
        if m:
            sid, artist, title, mm, ss = m.groups()
            hits.append({"id": sid, "artist": artist, "title": title,
                         "dur": int(mm) * 60 + int(ss)})
    return hits, "ok"


def fetch_lyric(sid, pacer, offline=False):
    path = os.path.join(LIB, "lrc", f"{sid}.lrc")
    if os.path.exists(path):
        return path
    if offline:
        return None
    pacer.wait("lyric")
    try:
        subprocess.run([CLI, "lyric", sid, LIB], capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    return path if os.path.exists(path) else None


def has_stamp(path):
    if not path or not os.path.exists(path):
        return False
    txt = open(path, encoding="utf-8", errors="replace").read()
    return bool(re.search(r"\[\d{1,2}:\d{2}[.:]\d{1,3}\]", txt))


# ---------------------------------------------------------------- 主流程
def load_state(path):
    st = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            r = line.rstrip("\n").split("\t")
            if len(r) >= 9:
                st[(r[0], r[1])] = {"match": r[2], "song": r[3], "artist": r[4],
                                    "sid": r[5], "lrc": r[6],
                                    "stamped": int(r[7] or 0), "translated": int(r[8] or 0)}
    return st


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        for (stn, fil), v in sorted(state.items()):
            f.write(f"{stn}\t{fil}\t{v['match']}\t{v['song']}\t{v['artist']}\t{v['sid']}\t"
                    f"{v['lrc']}\t{int(v['stamped'])}\t{int(v['translated'])}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sleep", type=float, default=4.0, help="搜索间隔基数（秒）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 首（调试用）")
    ap.add_argument("--only", default="")
    ap.add_argument("--offline", action="store_true", help="只用本地资源，不发网络请求")
    ap.add_argument("--retry", action="store_true", help="重试之前失败/没 LRC 的")
    ap.add_argument("--cooldown", type=float, default=600.0,
                    help="疑似限流时的冷却秒数（默认 600）")
    ap.add_argument("--max-ratelimit", type=int, default=3,
                    help="连续这么多次限流就收工（默认 3），下次重跑会接着补")
    ap.add_argument("--max-items", type=int, default=0,
                    help="本次最多处理 N 首（0=不限）；分批慢跑用，比如 30")
    ap.add_argument("--probe-first", action="store_true",
                    help="开跑前先打一次搜索探针：被限流就直接退出（不浪费时间）")
    a = ap.parse_args()

    os.makedirs(os.path.join(a.out, "by-station"), exist_ok=True)
    state_path = os.path.join(a.out, "state.tsv")
    state = load_state(state_path)
    tl = {}
    if os.path.exists(TRACKLIST):
        tl = {k: [(x[0], x[1]) for x in v]
              for k, v in json.load(open(TRACKLIST, encoding="utf-8")).items()}
    idx = load_lib_index()
    only = set(x for x in a.only.split(",") if x) or None
    songs = rpf_songs(only, a.limit)
    pacer = Pacer(a.sleep)
    print(f"台/曲 {len(songs)} 首；本地字典 {len(idx)} 个曲名；状态已有 {len(state)} 条；"
          f"sleep={a.sleep}s offline={a.offline}", file=sys.stderr)

    memo = {}          # (norm(title),norm(artist)) -> cand，同一首歌在多个台出现时只搜一次
    empty_streak = rl_streak = 0
    if a.probe_first:
        hits, status = search("signal failure padded cell", pacer)
        if status != "ok" or not hits:
            print(f"搜索接口不可用（status={status}），本次不跑；稍后再试", file=sys.stderr)
            return 4

    done = 0
    processed = 0
    for s in songs:
        key = (s["station"], s["file"])
        prev = state.get(key)
        if prev and prev["stamped"] and not a.retry:
            done += 1
            continue
        artist = title = ""
        cand = None
        how = ""
        # 1) 维基曲目表（台内唯一，最可靠）
        best_w, best_sc = None, 0.0
        for wa, wt in wiki_for(s["station"], tl):
            sc = sim(demangle(s["file"]), wt)
            if sc > best_sc:
                best_sc, best_w = sc, (wa, wt)
        if best_w and best_sc >= 0.8:
            artist, title = best_w[0], best_w[1]
            how = f"wiki{best_sc:.2f}"
        else:
            title = demangle(s["file"])
            how = "filename"
        # 2) 本地歌单字典（零请求）
        for e in idx.get(norm(title), []):
            cand = e
            how += "+lib"
            break
        if cand is None:
            best, bs = None, 0.0
            nk = norm(title)
            for k, v in idx.items():
                if abs(len(k) - len(nk)) > 12:
                    continue
                sc = difflib.SequenceMatcher(None, nk, k).ratio()
                if sc > bs:
                    bs, best = sc, v[0]
            if best is not None and bs >= 0.90:
                cand, how = best, how + f"+libfuzzy{bs:.2f}"
        # 3) 网络搜索（离线模式跳过；同一首歌只搜一次）
        mk = (norm(title), norm(artist))
        if cand is None and mk in memo:
            cand, how = memo[mk], how + "+memo"
        if cand is None and not a.offline:
            q = f"{title} {artist}".strip()
            hits, status = search(q, pacer)
            if status == "empty":
                empty_streak += 1
                if empty_streak >= 2:          # 连着两次空 → 大概率被限流
                    rl_streak += 1
                    empty_streak = 0
                    if rl_streak >= a.max_ratelimit:
                        save_state(state_path, state)
                        print(f"连续 {rl_streak} 次限流，先收工；重跑本命令会从 state.tsv 续跑",
                              file=sys.stderr)
                        return 3
                    pacer.cooldown(a.cooldown * rl_streak)
                    continue
            else:
                if status == "ok":
                    rl_streak = 0
                empty_streak = 0
            if not hits and artist:            # 带上艺人搜不到 → 只按歌名再试一次
                hits, status = search(title, pacer)
            best, bs = None, 0.0
            for h in hits:
                sc = sim(title, h["title"])
                if artist and sim(artist, h["artist"]) < 0.5:
                    sc -= 0.15
                if sc > bs:
                    bs, best = sc, h
            if best is not None and bs >= 0.8:
                cand, how = best, how + f"+search{bs:.2f}"
                memo[mk] = best
        stamped = translated = False
        lrc = ""
        if cand:
            p = fetch_lyric(cand["id"], pacer, a.offline)
            stamped = has_stamp(p)
            if stamped:
                lrc = os.path.basename(p)
                translated = "译" in open(p, encoding="utf-8", errors="replace").read()[:4000]
                dst = os.path.join(a.out, "by-station", s["station"], s["file"] + ".lrc")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                open(dst, "w", encoding="utf-8").write(
                    open(p, encoding="utf-8", errors="replace").read())
        state[key] = {"match": how, "song": cand["title"] if cand else "",
                      "artist": cand["artist"] if cand else "", "sid": cand["id"] if cand else "",
                      "lrc": lrc, "stamped": stamped, "translated": translated}
        done += 1
        processed += 1
        if done % 5 == 0:
            save_state(state_path, state)
        print(f"[{done}/{len(songs)}] {s['station']:22s} {s['file'][:34]:34s} "
              f"{(cand['title'][:28] if cand else '—'):28s} {how:18s} "
              f"lrc={'Y' if stamped else 'n'}", file=sys.stderr)
        if a.max_items and processed >= a.max_items:
            save_state(state_path, state)
            print(f"本次处理 {processed} 首（--max-items），收工；重跑会接着补",
                  file=sys.stderr)
            break

    save_state(state_path, state)

    # ---- 覆盖率报告（沿用旧格式）
    by = {}
    rows = []
    for s in songs:
        v = state.get((s["station"], s["file"]))
        if not v:
            continue
        rows.append((s, v))
        d = by.setdefault(s["station"], {"n": 0, "sec": 0.0, "n_lrc": 0, "sec_lrc": 0.0,
                                         "n_stamped": 0, "sec_stamped": 0.0})
        d["n"] += 1
        d["sec"] += s["sec"]
        if v["lrc"]:
            d["n_lrc"] += 1
        if v["stamped"]:
            d["n_stamped"] += 1
            d["sec_stamped"] += s["sec"]
    with open(os.path.join(a.out, "songs.tsv"), "w", encoding="utf-8") as f:
        f.write("station\tfile\tmin\tmatch\tsong\tartist\tsid\tlrc\tstamped\ttranslated\n")
        for s, v in rows:
            f.write(f"{s['station']}\t{s['file']}\t{s['sec']/60:.1f}\t{v['match']}\t"
                    f"{v['song']}\t{v['artist']}\t{v['sid']}\t{v['lrc']}\t"
                    f"{int(v['stamped'])}\t{int(v['translated'])}\n")
    lines = ["# 电台歌曲 × 歌词库 覆盖率（radio_lyrics_fill.py）", "",
             "| 电台 | 歌曲数 | 分钟 | 有 LRC | 带时间轴 | 按分钟覆盖 |",
             "|---|---|---|---|---|---|"]
    tn = ts = tns = tss = 0
    for st, d in sorted(by.items()):
        lines.append(f"| {st} | {d['n']} | {d['sec']/60:.0f} | {d['n_lrc']} | "
                     f"{d['n_stamped']} | {d['sec_stamped']/max(1e-9, d['sec'])*100:.0f}% |")
        tn += d["n"]; ts += d["sec"]; tns += d["n_stamped"]; tss += d["sec_stamped"]
    lines += ["", f"**合计**：{tn} 首 / {ts/60:.0f} 分钟；带时间轴 LRC {tns} 首"
                  f"（按分钟覆盖 **{tss/max(1e-9, ts)*100:.0f}%**）；网络请求 {pacer.calls} 次。"]
    open(os.path.join(a.out, "report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
