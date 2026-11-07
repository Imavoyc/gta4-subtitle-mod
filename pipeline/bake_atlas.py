#!/usr/bin/env python3
"""Bake a font atlas for the GTA IV subtitle self-renderer.

Charset: ASCII 0x20-0x7E + GB2312 level-1 hanzi (3755) + CJK punctuation.
Output (next to the ASI, loaded at runtime):
  subtitle_atlas.bin        raw A8 bitmap (atlas_w * atlas_h)
  subtitle_atlas_metrics.bin  header + cp->slot map + per-glyph float metrics
  subtitle_atlas.json       human-readable metrics (same content)

usage: bake_atlas.py [ttf_path] [font_index] [px] [outdir]
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

DEFAULT_TTF = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
SC_INDEX = 2  # NotoSansCJK ttc order: JP, KR, SC, TC, HK


def charset():
    chars = [chr(c) for c in range(0x20, 0x7F)]
    # CJK punctuation we need
    chars += "，。、；：？！“”‘’（）《》〈〉【】·—…％￥"
    # GB2312 全部汉字：level-1 3755（0xB0A1-0xD7FE，常用）+ level-2 3008
    # （0xD8A1-0xF7FE，次常用 —— 嗯/裆/痿/瑜/伽/侏 这些都在二级，字幕里躲不掉）
    row = bytearray()
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            row += bytes([hi, lo])
    chars.append(row.decode("gb2312", errors="ignore"))
    seen, out = set(), []
    for c in "".join(chars):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    ttf = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TTF
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else SC_INDEX
    px = int(sys.argv[3]) if len(sys.argv) > 3 else 34
    outdir = sys.argv[4] if len(sys.argv) > 4 else os.path.dirname(os.path.abspath(__file__))
    chars = charset()
    font = ImageFont.truetype(ttf, px, index=idx)
    ascent, descent = font.getmetrics()

    # render each glyph into a scratch tile, crop, shelf-pack
    AW = 2048
    PAD = 6
    T = px * 2 + 2 * PAD
    scratch = Image.new("L", (T, T), 0)
    draw = ImageDraw.Draw(scratch)
    glyphs = {}
    x = y = 1
    rowh = 0
    tiles = []
    for ch in chars:
        draw.rectangle([0, 0, T, T], fill=0)
        draw.text((PAD, PAD), ch, font=font, fill=255)
        bb = scratch.getbbox()
        if bb is None:  # space & friends: keep metrics only
            glyphs[ch] = [0, 0, 0, 0, round(font.getlength(ch), 2), 0, 0]
            continue
        crop = scratch.crop(bb)
        w, h = crop.size
        adv = font.getlength(ch)
        xoff = bb[0] - PAD          # relative to the ascender-top origin
        yoff = bb[1] - PAD
        if x + w + 1 >= AW:
            x = 1
            y += rowh + 1
            rowh = 0
        tiles.append((ch, x, y, crop))
        glyphs[ch] = [x, y, w, h, round(adv, 2), xoff, yoff]
        x += w + 1
        rowh = max(rowh, h)
    AH = 1
    while AH < y + rowh + 2:
        AH *= 2
    img = Image.new("L", (AW, AH), 0)
    for ch, gx, gy, crop in tiles:
        img.paste(crop, (gx, gy))

    meta = {"w": AW, "h": AH, "px": px, "ascent": ascent,
            "line_h": ascent + descent, "glyphs": glyphs}

    # binary metrics: flat, in charset order, with a sorted cp->slot map
    import struct
    order = [c for c in chars if c in glyphs]  # chars iterated (deduped)
    m = struct.pack("<IIIffI", 0x54414253, AW, AH, px, ascent + descent, len(order))
    pairs = sorted((ord(c), i) for i, c in enumerate(order))
    for cp, i in pairs:
        m += struct.pack("<II", cp, i)
    m += struct.pack("<I", len(order))
    for c in order:
        g = glyphs[c]
        m += struct.pack("<7f", *g)
    open(os.path.join(outdir, "subtitle_atlas_metrics.bin"), "wb").write(m)
    open(os.path.join(outdir, "subtitle_atlas.bin"), "wb").write(img.tobytes())
    json.dump(meta, open(os.path.join(outdir, "subtitle_atlas.json"), "w"),
              ensure_ascii=False)
    n_cjk = sum(1 for c in glyphs if ord(c) > 0x7F)
    print(f"atlas {AW}x{AH}, {len(glyphs)} glyphs ({n_cjk} cjk), "
          f"line_h={ascent+descent}")


if __name__ == "__main__":
    main()
