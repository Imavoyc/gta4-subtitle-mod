#!/usr/bin/env python3
"""GTA IV 屏幕状态探针 —— 给 gta4_ctl.sh 用，避免脚本瞎按键盘。

用法：screen_probe.py <screenshot.png>
输出一行： `menu=<0|1> tab=<x|-1> row=<y|-1> mean=<0..255> w=.. h=..`

判据（都在 2026-11-06 的实拍截图上标定过，分辨率无关）：

* `menu`：FusionFix 的暂停菜单在画面上部有一条**满幅的细分隔线**（1280x720 时
  y=137/138；颜色随页签背景在 45~124 之间，但**整行几乎零方差**，上下两行纯黑）。
  判据 = 右半边某行 std<1.5、中位亮度>25，且上下相邻行中位亮度≈0。
  游戏画面（亮天空、暗走廊、TV 画面）同一区段 std 都在 4~19，不会误判。
* `tab`：暂停菜单顶部页签，**当前页签橙色**（≈RGB 222,142,13），未选灰色（≈120）。
  取左半区 y∈[0.08h,0.18h] 橙色像素中位 x。只在改页签时用。
* `row`：菜单列表区（左侧栏，y∈[0.20h,0.78h]）橙色像素中位 y = 当前高亮行。
  用来做"一直按 Up/Down 直到绕回"的翻页检测，不依赖文案、不依赖行数。
* `mean`：整图平均亮度。确认退出对话框接近全黑（实测 0），可作二次确认。
"""
import sys

import numpy as np
from PIL import Image


def main():
    if len(sys.argv) < 2:
        print("usage: screen_probe.py <png>", file=sys.stderr)
        return 2
    im = np.asarray(Image.open(sys.argv[1]).convert("RGB")).astype(int)
    h, w, _ = im.shape
    lum = im.mean(axis=2)

    # 1) 暂停菜单：一条满幅的**细分隔线**（1280x720 时 y=137/138）+ 上下纯黑。
    #
    #    不能只看"整行零方差"：我们自己的字幕覆盖层（顶部半透明黑底条）会把这条线
    #    压暗（45 -> 17）甚至盖住它的一部分，绝对亮度和 std 都会失效。所以改成
    #    「把右半边横切成 4 个窗口，只要有一个窗口是一对均匀亮行 + 上下纯黑」。
    #    覆盖层最宽也只到屏幕的 ~0.63（MaxWidth 上限 0.8 时可能到 0.9），
    #    总有一个窗口是干净的。
    x0, x1 = int(w * 0.50), int(w * 0.99)
    nwin = 4
    wins = [(x0 + (x1 - x0) * i // nwin, x0 + (x1 - x0) * (i + 1) // nwin)
            for i in range(nwin)]

    def line_row(y, a, b):
        seg = lum[y, a:b]
        med = float(np.median(seg))
        if med < 8:
            return None
        if float((np.abs(seg - med) <= 3).mean()) < 0.90:
            return None
        return med

    menu = 0
    for y in range(max(2, int(h * 0.14)), int(h * 0.26)):
        for a, b in wins:
            m0 = line_row(y, a, b)
            if m0 is None:
                continue
            m1 = line_row(y + 1, a, b)
            if m1 is None or abs(m1 - m0) > 3:
                continue
            if np.median(lum[y - 1, a:b]) > 3 or np.median(lum[y + 2, a:b]) > 3:
                continue
            menu = 1
            break
        if menu:
            break

    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    orange = (r >= 160) & (g >= 90) & (g <= 200) & (b <= 90) & ((r - b) >= 100)

    # 2) 当前页签（橙色文字）的水平位置；页签条在左半区
    ys, xs = np.nonzero(orange[int(h * 0.08):int(h * 0.18), : int(w * 0.60)])
    tab = int(np.median(xs)) if len(xs) > 20 else -1

    # 3) 当前高亮行的垂直位置；列表在左栏
    y0 = int(h * 0.20)
    ys, _ = np.nonzero(orange[y0:int(h * 0.78), : int(w * 0.45)])
    row = int(np.median(ys)) + y0 if len(ys) > 20 else -1

    print(
        f"menu={menu} tab={tab} row={row} "
        f"mean={int(round(lum.mean()))} w={w} h={h}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
