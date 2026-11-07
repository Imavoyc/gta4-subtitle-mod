#!/usr/bin/env python3
"""GTA IV 电台/音效段（RAGE "IVAUD" 容器）→ WAV。

背景：`pc/audio/sfx/*.rpf` 里每个段是「2048 字节头 + 每通道一段 IMA ADPCM」，
不是 PCM（按 PCM16 解出来是噪声，踩过）。IMA ADPCM 这条是网上俄文论坛的说法，
这里用 `AD079_POKER_PROMO` 实听 + whisper 验证过：
    "Oh, my God! Oh, mercy! ... the Ventura's Poker Challenge!"

头（小端 u32）：
    0x08  通道数
    0x0C  每通道字节数（本作**固定 509952 = 21.25 秒**）
    0x14  采样率（kHz，48）
    0x2C  数据起始偏移（0x800 / 0x1000 / 0x6800 / 0xd000…）
校验：offset + channels*bytes == filesize（对不上就是别的容器，跳过）

★ 关键发现：**一个"段"里的每个通道其实是各自独立的一条 21.25 秒 mono 片段**
  （不是立体声左右声道，也不是分轨）—— 广告 4 通道 = 4 条不同的剪辑/版本，
  WKTT 35 通道、PLR 69 通道也是这个意思。有的通道是空的（rms≈0），
  转录时要按 RMS 挑出有人声的那些（`--auto`）。

每通道数据 = 4 字节 IMA 状态头（int16 初值, uint8 step index, uint8 保留）
            + nibble 流（低 4 bit 先）。

usage: ivaud2wav.py in out.wav [--channels 0,1] [--mono]
"""
import struct
import sys
import wave

STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
        50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
        253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
        1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
        3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487,
        12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767]
IDX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def decode_channel(data: bytes) -> list:
    """IMA ADPCM 单通道（大块连续流，第 4 字节起是 nibble）。"""
    if len(data) < 5:
        return []
    pred, idx = struct.unpack_from("<hB", data, 0)[0], data[2]
    idx = max(0, min(88, idx))
    step = STEP[idx]
    out = []
    ap = out.append
    for byte in data[4:]:
        for code in (byte & 0x0F, byte >> 4):
            diff = step >> 3
            if code & 4:
                diff += step
            if code & 2:
                diff += step >> 1
            if code & 1:
                diff += step >> 2
            pred = pred - diff if code & 8 else pred + diff
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            idx += IDX[code]
            idx = 0 if idx < 0 else (88 if idx > 88 else idx)
            step = STEP[idx]
            ap(pred)
    return out


def parse_header(d: bytes):
    if len(d) < 0x40:
        return None
    h = struct.unpack_from("<16I", d, 0)
    ch, bpc, khz, off = h[2], h[3], h[5], h[11]
    if not (1 <= ch <= 128) or bpc == 0 or not (8 <= khz <= 192) or off == 0:
        return None
    if off + ch * bpc != len(d):
        return None
    return ch, bpc, khz * 1000, off


def rms(pcm):
    if not pcm:
        return 0.0
    return (sum(x * x for x in pcm) / len(pcm)) ** 0.5


def main():
    src, dst = sys.argv[1], sys.argv[2]
    want = None
    mono = False
    auto = False
    for i, a in enumerate(sys.argv[3:]):
        if a == "--mono":
            mono = True
        elif a == "--auto":
            auto = True
        elif a == "--channels":
            want = [int(x) for x in sys.argv[i + 4].split(",")]

    d = open(src, "rb").read()
    hdr = parse_header(d)
    if not hdr:
        print(f"skip (header/size check failed): {src}", file=sys.stderr)
        return 2
    ch, bpc, rate, off = hdr
    if auto:
        best, best_rms = 0, -1.0
        for c in range(ch):
            r = rms(decode_channel(d[off + c * bpc: off + (c + 1) * bpc])[:48000 * 4])
            print(f"  ch{c} rms={r:.0f}", file=sys.stderr)
            if r > best_rms:
                best, best_rms = c, r
        print(f"  auto -> ch{best} (rms {best_rms:.0f})", file=sys.stderr)
        want = [best]
    chans = want if want is not None else ([0] if mono else list(range(ch)))
    chans = [c for c in chans if c < ch] or [0]
    pcm = []
    for c in chans:
        pcm.append(decode_channel(d[off + c * bpc: off + (c + 1) * bpc]))
    n = min(len(x) for x in pcm)
    frames = bytearray()
    if len(pcm) == 1:
        frames = struct.pack("<%dh" % n, *pcm[0][:n])
    else:
        inter = []
        for i in range(n):
            for c in range(len(pcm)):
                inter.append(pcm[c][i])
        frames = struct.pack("<%dh" % len(inter), *inter)
    w = wave.open(dst, "wb")
    w.setnchannels(len(pcm))
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(frames)
    w.close()
    print(f"{src}: {ch}ch total -> {len(pcm)}ch @{rate} {n / rate:.1f}s -> {dst}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
