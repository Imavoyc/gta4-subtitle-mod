#!/usr/bin/env python3
"""每 0.5 s 记一次电台状态（外部读内存）→ TSV。

一行 = 一个台**当前槽**的快照：

    t  station  cur  hash  track  rpf  ms  len  idx  mode  state

* `track` 用 `radio_hash_table.py` 的 joaat 表反查（查不到写 `?`）；
* `ms` 是段内已播毫秒、`len` 是这个段的总时长（都是引擎自己给的）；
* 同一时刻所有台都在"广播"（GTA IV 的电台是连续时间线），所以每行都有值；
  **哪个台在你耳朵里** 要另外看（插件日志的 `radio:` 行 / 下面的 `--audible`）。

用法：
    radio_state_log.py --secs 60 > ~/tmp/<日期>/radio-state.tsv
    radio_state_log.py --secs 60 --audible JAZZ_NATION   # 只打这一个台
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import radio_mem_probe as R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=180)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--audible", default="", help="只记录这个台（默认全部）")
    ap.add_argument("--table", default=None)
    ap.add_argument("--wallms", action="store_true",
                    help="第一列改写成带毫秒的壁钟（epoch 秒），用于和音频流对齐")
    a = ap.parse_args()

    pid = R.find_pid()
    if not pid:
        sys.exit("GTAIV.exe 没在跑")
    mem = R.Mem(pid)
    table = R.load_table(a.table)
    st = R.registry(mem)
    if a.audible:
        st = [(n, b) for n, b in st if n == a.audible]
    print("t\tstation\tcur\thash\ttrack\trpf\tms\tlen\tidx\tmode\tstate", flush=True)
    end = time.time() + a.secs
    while time.time() < end:
        ts = f"{time.time():.3f}" if a.wallms else time.strftime("%H:%M:%S")
        for nm, base in st:
            s = R.station_state(mem, base, table)
            sl = s["slots"][s["cur"] if s["cur"] in (0, 1) else 0]
            if not sl["clip"]:
                continue
            print(f"{ts}\t{nm}\t{s['cur']}\t{sl['hash']:08x}\t{sl['track'] or '?'}\t"
                  f"{sl['rpf'] or '?'}\t{sl['ms']}\t{sl['len']}\t{sl['idx']}\t"
                  f"{sl['mode']}\t{sl['state']}", flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
