#!/usr/bin/env python3
"""夜间批量：把电台的**谈话/DJ 片段**转成文本（只提取文本，不翻译）。

数据来源：`pc/audio/sfx/radio_*.rpf` 里的段（RAGE IVAUD 容器，IMA ADPCM）
  → `ivaud2wav.parse_header/decode_channel` 解 21.25 秒单声道片段
  → GPUStack `qwen3-asr-1.7b`（比本地 whisper 准很多，见 docs/gta4-radio.md）

范围（2026-11-06 盘点后确定）：
  * 谈话台全要：news / adverts / weather / wktt / plr / lazlow
  * 音乐台只要 DJ 串词：文件名以 ID_ / SOLO_ / INTRO / TIME / TO / GENERAL 开头
  * 跳过：5 个整文件连播台、extratracks（纯备用曲库）

省请求的三个过滤器：
  1. 静音片段（rms < 250）跳过；
  2. 相同音频去重（16k PCM 的 md5）—— 很多通道是同一段的不同版本；
  3. `--limit` 可只跑前 N 个片段做小样。

输出：JSONL（一行一个片段，含 station/file/channel/rms/text），可断点续跑：
    ~/tmp/<日期>/gta4-radio-asr/asr.jsonl

用法：
    night_radio_asr.py --outdir ~/tmp/2026-11-06/gta4-radio-asr [--workers 4] [--limit 0]
                       [--only radio_news,radio_adverts]
"""
import argparse
import hashlib
import io
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("ivaud2wav", os.path.join(HERE, "ivaud2wav.py"))
ivaud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ivaud)

G = "/home/ydb/.steam/debian-installation/steamapps/common/Grand Theft Auto IV/GTAIV"
SFX = os.path.join(G, "pc/audio/sfx")
EXE = os.path.join(G, "GTAIV.exe")
API = "https://gpustack.rock-chips.com/v1/audio/transcriptions"
MODEL = "qwen3-asr-1.7b"

TALK = ("radio_news", "radio_adverts", "radio_weather", "radio_wktt", "radio_plr", "radio_lazlow")
SKIP = ("radio_extratracks", "radio_beat_95", "radio_bobby_konders", "radio_dance_mix",
        "radio_ny_classics", "radio_ramjamfm")
DJ_RE = re.compile(r"^(ID_|SOLO_|INTRO|TIME|TO|GENERAL)")

STATION_HINT = {
    "radio_news": "Weazel News radio news bulletin, Liberty City",
    "radio_adverts": "Liberty City radio commercial",
    "radio_weather": "Liberty City radio weather report",
    "radio_wktt": "WKTT talk radio, Liberty City",
    "radio_plr": "Public Liberty Radio talk show, Liberty City",
    "radio_lazlow": "Integrity 2.0 with Lazlow, Liberty City",
}
NAME_HINT = ("Baz Ruddin, Jeremy St. Ives, Weazel News, Liberty City, Broker, Bohan, Dukes, "
             "Algonquin, Alderney")


def downsample(pcm48):
    """48k int16 -> 16k int16（先做 15 抽头低通，再 3:1 抽样）"""
    x = pcm48.astype(np.float32)
    taps = np.hanning(15)
    taps /= taps.sum()
    y = np.convolve(x, taps, mode="same")
    return y[::3].astype(np.int16)


def wav_bytes(pcm16, rate=16000):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm16.tobytes())
    w.close()
    return buf.getvalue()


def asr(wav, prompt, tries=3):
    boundary = "----radioasrboundary"
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
                     f"\r\n\r\n{value}\r\n".encode())

    field("model", MODEL)
    if prompt:
        field("prompt", prompt)
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  "filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode())
    parts.append(wav)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Authorization": "Bearer " + os.environ["GPUSTACK_API_KEY"],
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["text"].strip()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"    retry {i}: {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))


def targets(only=None):
    from pyrpfiv import RPFParser
    out = []
    for rpf in sorted(os.listdir(SFX)):
        if not (rpf.startswith("radio_") and rpf.endswith(".rpf")):
            continue
        base = rpf[:-4]
        if base in SKIP:
            continue
        if only and base not in only:
            continue
        talk = base in TALK
        pr = RPFParser(os.path.join(SFX, rpf), EXE)
        pr.build_file_list()
        for p in pr.paths:
            name = os.path.basename(p["path"])
            if talk or DJ_RE.match(name):
                out.append((base, rpf, p["path"], name, p["size"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    jsonl = os.path.join(a.outdir, "asr.jsonl")
    done = set()
    if os.path.exists(jsonl):
        for line in open(jsonl, encoding="utf-8"):
            try:
                r = json.loads(line)
                done.add((r["station"], r["file"], r["ch"]))
            except Exception:  # noqa: BLE001
                pass
    print(f"已完成 {len(done)} 个片段（续跑）", file=sys.stderr)

    only = set(x for x in a.only.split(",") if x) or None
    items = targets(only)
    print(f"目标文件 {len(items)} 个", file=sys.stderr)

    # 生产者线程：边解码边入队（有界队列，避免把 1.6 GB PCM 全堆在内存里）
    q = queue.Queue(maxsize=64)
    stats = {"clip": 0, "quiet": 0, "dup": 0, "bad": 0}

    def producer():
        from pyrpfiv import RPFParser
        seen_hash = set()
        tmp = os.path.join(a.outdir, "_tmp")
        os.makedirs(tmp, exist_ok=True)
        for base, rpf, path, name, size in items:
            pr = RPFParser(os.path.join(SFX, rpf), EXE)
            pr.build_file_list()
            try:
                pr.extract_file(path, tmp)
            except Exception as e:  # noqa: BLE001
                print(f"  提取失败 {path}: {e}", file=sys.stderr)
                continue
            raw = os.path.join(tmp, name)
            data = open(raw, "rb").read()
            os.unlink(raw)
            hdr = ivaud.parse_header(data)
            if not hdr:
                stats["bad"] += 1
                print(f"  跳过（容器不识别）{name}", file=sys.stderr)
                continue
            ch, bpc, rate, off = hdr
            for c in range(ch):
                key = (base, name, c)
                if key in done:
                    continue
                pcm = ivaud.decode_channel(data[off + c * bpc: off + (c + 1) * bpc])
                if not pcm:
                    continue
                arr = np.asarray(pcm, dtype=np.int16)
                rms = float(np.sqrt((arr.astype(np.float64) ** 2).mean()))
                if rms < 250:
                    stats["quiet"] += 1
                    done.add(key)
                    continue
                p16 = downsample(arr)
                h = hashlib.md5(p16.tobytes()).hexdigest()
                if h in seen_hash:
                    stats["dup"] += 1
                    done.add(key)
                    continue
                seen_hash.add(h)
                if a.limit and stats["clip"] >= a.limit:
                    break
                stats["clip"] += 1
                q.put((key, p16, rms, ch))
            if a.limit and stats["clip"] >= a.limit:
                break
        q.put(None)   # 结束信号
        print(f"生产者完成：{stats['clip']} 个片段（静音 {stats['quiet']} / 重复 {stats['dup']} / "
              f"容器不识别 {stats['bad']}）", file=sys.stderr)

    prod = threading.Thread(target=producer, daemon=True)
    prod.start()

    lock = threading.Lock()
    fout = open(jsonl, "a", encoding="utf-8")
    counter = {"n": 0, "t0": time.time()}

    def worker():
        while True:
            item = q.get()
            if item is None:
                q.put(None)      # 让其它 worker 也退出
                return
            (key, p16, rms, nch) = item
            station, name, c = key
            prompt = STATION_HINT.get(station, "Liberty City radio") + ". " + NAME_HINT
            t = time.time()
            try:
                text = asr(wav_bytes(p16), prompt)
            except Exception as e:  # noqa: BLE001
                print(f"  ASR 失败 {station}/{name}#{c}: {e}", file=sys.stderr)
                continue
            rec = {"station": station, "file": name, "ch": c, "nch": nch,
                   "rms": round(rms), "sec": round(len(p16) / 16000, 2),
                   "text": text, "asr_sec": round(time.time() - t, 2),
                   "t": int(time.time())}
            with lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                counter["n"] += 1
                if counter["n"] % 25 == 0:
                    el = time.time() - counter["t0"]
                    rate_ = counter["n"] / el
                    left = (stats["clip"] - counter["n"]) / rate_ if rate_ else 0
                    print(f"  {counter['n']}/{stats['clip']} 条  {rate_:.2f}/s  "
                          f"~{left/60:.0f} min  | {name}#{c}: {text[:60]}", file=sys.stderr)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    fout.close()
    print(f"完成 {counter['n']} 条 -> {jsonl}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
