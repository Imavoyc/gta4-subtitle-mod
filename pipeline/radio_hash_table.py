#!/usr/bin/env python3
"""电台音频段的「资源 hash → 段名」表（M4 同步的关键一环，2026-11-07 破解）。

## 结论（静态+动态双证据）

游戏给每个电台段一个 32 位 **资源 hash**，算法是 RAGE 的 joaat（one-at-a-time），
被哈希的字符串是 **`<rpf 基名>_<文件名>` 全小写**，例如

    radio_the_vibe_justbegoodtome
    radio_adverts_ad016_alcotine_patch
    radio_wktt_1_unjust_1

运行时它就在 `CRadioStation`（台对象）里：

    slot   = 台对象 + 0x1917(当前槽 0/1) * 0xbd0
    entry  = *(u32*)(slot + 0x960)      // 8 字节的记录 {0, hash}
    hash   = *(u32*)(entry + 4)         // ← 这个 hash
    ms     = *(u32*)(slot + 0x990)      // 段内已播毫秒
    len_ms = *(u32*)(slot + 0x994)      // 本段时长（引擎认为的）

证据链：解 `FUN_00947020 / FUN_00946030 / FUN_00945230`（槽位/记录索引）+
`FUN_00b76660`（写 +0x960/+0x990/+0x994）；hash 由「973 个段名 × 模板」暴力
反查命中 35/42 个运行时观测值（其余 7 个是大混音台的内部子段，见 docs）。

## 用法

    # 从 RPF 清单 json 建表（pyrpfiv 的产物，见 docs/gta4-radio.md）
    radio_hash_table.py build --listing ~/tmp/<日期>/gta4-radio-cat/rpf_listing.json \
                              --out radio_audio_hash.json

    # 拿运行时的 hash 反查段名
    radio_hash_table.py lookup 0x3498fe36 [--table radio_audio_hash.json]

    # 自检：表里每条 hash 都已经过 joaat 反算（幂等）
    radio_hash_table.py verify --table radio_audio_hash.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TABLE = os.path.join(HERE, "radio_audio_hash.json")


def joaat(s):
    """RAGE 的 joaat / one-at-a-time（输入按小写处理，Rockstar 惯例）。"""
    h = 0
    for c in s.encode("latin-1"):
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    return h


def key_for(rpf, filename):
    """被哈希的字符串：rpf 基名 + '_' + 文件名，全小写。"""
    base = rpf[:-4] if rpf.lower().endswith(".rpf") else rpf
    return f"{base}_{filename}".lower()


def build(listing_path, out_path):
    listing = json.load(open(listing_path, encoding="utf-8"))
    table = {}
    for rpf, info in sorted(listing.items()):
        for d in info.get("directories", []):
            for f in d.get("files", []):
                name = f["name"]
                if name.startswith("0x"):      # TOC 里名字不可解，只能按 hash 记
                    continue
                k = key_for(rpf, name)
                table[f"{joaat(k):08x}"] = {
                    "rpf": rpf,
                    "station": d["name"],
                    "file": name,
                    "size": f["size"],
                    "key": k,
                }
    json.dump(table, open(out_path, "w", encoding="utf-8"), indent=0, sort_keys=True)
    print(f"写入 {out_path}：{len(table)} 条")
    return table


def load(path=DEFAULT_TABLE):
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8"))


def lookup(h, table):
    return table.get(f"{h & 0xFFFFFFFF:08x}")


def add_channel_counts(table):
    """给每条补 nch / dur_ms（从 RPF 里的文件尺寸反算）。

    容器校验 `doff + nch*509952 == size` 很硬，所以只要试 doff 就能解出通道数；
    台呼类（GENERAL/INTRO/TIME/TO）是另一种容器变体，解不出来就留空。
    """
    BPC = 509952
    for e in table.values():
        if "nch" in e:
            continue
        size = e["size"]
        for doff in range(0x400, 0x20000, 0x100):
            if (size - doff) % BPC == 0 and (size - doff) // BPC >= 1:
                e["nch"] = (size - doff) // BPC
                e["dur_ms"] = round(e["nch"] * 21248.0)
                break
    return table


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--listing", required=True)
    b.add_argument("--out", default=DEFAULT_TABLE)
    lk = sub.add_parser("lookup")
    lk.add_argument("hash", nargs="+")
    lk.add_argument("--table", default=DEFAULT_TABLE)
    v = sub.add_parser("verify")
    v.add_argument("--table", default=DEFAULT_TABLE)
    ix = sub.add_parser("index")
    ix.add_argument("--table", default=DEFAULT_TABLE)
    ix.add_argument("--out", default=os.path.join(HERE, "radio_index.tsv"))
    a = ap.parse_args()

    if a.cmd == "build":
        build(a.listing, a.out)
    elif a.cmd == "lookup":
        t = load(a.table)
        for h in a.hash:
            h = int(h, 0)
            e = lookup(h, t)
            print(f"{h:#010x}  {e['station'] if e else '?':22s} "
                  f"{e['file'] if e else '???':34s} "
                  f"{e['size'] if e else ''}")
    elif a.cmd == "index":
        t = add_channel_counts(load(a.table))
        json.dump(t, open(a.table, "w", encoding="utf-8"), indent=0, sort_keys=True)
        rows = [f"{h}\t{e['nch']}\t{e['dur_ms']}\t{e['file']}"
                for h, e in sorted(t.items()) if "nch" in e]
        open(a.out, "w", encoding="utf-8").write("\n".join(rows) + "\n")
        print(f"写入 {a.out}：{len(rows)} 条（插件用它把引擎 ms 换算成文件时间）")
    elif a.cmd == "verify":
        t = load(a.table)
        bad = 0
        for h, e in t.items():
            if f"{joaat(e['key']):08x}" != h:
                bad += 1
                print(f"BAD {h} {e}")
        print(f"{len(t)} 条，{bad} 条不自洽")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
