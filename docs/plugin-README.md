# tools/gta4_subtitle — GTA IV 中文字幕 mod（M0 已通）

结论与踩坑见 `docs/gta4-subtitle.md`；这里是源码 + 自动化脚本。

目标：给 GTA IV（1.2.0.59 / Complete Edition）的**电台**和**电视台**加中文字幕。
环境与可行性分析见 `docs/gta4-env.md`。

## M0 现状：`src/subtitle_test.cpp`

最小 ASI，验证「我们自己的插件能不能把 UTF-8 中文画到屏幕上」：

1. **跨平台调用 native**：游戏用一个 hash → handler 的解析函数（`56 8B 35 ? ? ? ? 85 F6 75 06`，
   CE 里有两份，取第二份 —— 签名与取值方式抄自 FusionFix 的 `comvars.ixx`，CE 验证过）。
2. **绘制**：`SET_TEXT_*` + `DISPLAY_TEXT_WITH_LITERAL_STRING(x, y, "STRING", utf8)`，
   位置/缩放/背景都从 ini 读，2 秒内热生效。
3. **字形**：直接给引擎 UTF-8 中文。汉化包（`GTA4.CHS.asi`）钩的是引擎的文本编码阶段
   （缓冲区 `ctx+0x48`、游标 `ctx+0x448`），把 UTF-8 映射到 `fonts.wtd` 图集，
   所以引擎自己的文本通道应该能画出 CJK。

构建（32 位 DLL，被 Ultimate ASI Loader 当 `.asi` 加载）：

```sh
make                       # -> build/GTAIV.SubtitleTest.asi
cp build/GTAIV.SubtitleTest.asi  "<GTAIV>/plugins/"
cp GTAIV.SubtitleTest.ini        "<GTAIV>/plugins/"
```

ini 键：`Text`（UTF-8）、`X`/`Y`（0..1 屏幕坐标）、`Scale`、`Font`、`Centre`、`Background`。

## 字幕语言 / 文件布局（2 秒热生效）

同一频道的中英两版都放在 `plugins/` 下，用 ini 切换，**换语言不用重启游戏**：

```
plugins/weazel.zh.srt   weazel.en.srt      <- Lang=zh / en 决定读哪个
plugins/CNT.zh.srt      CNT.en.srt
```

```ini
Lang=zh          ; 中文（默认）；Lang=en 切英文
```

插件按 **Bink 总帧数**认频道（77182=weazel、93115=CNT），拼出
`<频道>.<Lang>.srt`；那份不存在就退回 `<频道>.srt`（只有一份时随便哪种语言）。

## 翻译 / 校对（2026-11-06 新增）

翻译不是"逐句机翻"，而是**AI 初稿 → 一致性复审 → 人工校对**，流程与调研结论见
`docs/gta4-translation-pipeline.md`。三条命令：

```sh
P=tools/gta4_subtitle/pipeline
python3 $P/translate_srt.py weazel.en.srt weazel.zh.srt --glossary $P/glossary.tsv   # 初译（夜间）
python3 $P/translate_srt.py weazel.en.srt weazel.zh.srt --review --glossary $P/glossary.tsv
python3 $P/qa.py weazel.en.srt weazel.zh.srt --json /tmp/qa.json                    # QA 报告
python3 tools/gta4_subtitle/review/serve.py --en a.en.srt --zh a.zh.srt            # 人工校对（Web）
```

- 术语表 `pipeline/glossary.tsv`（原文/译名/类型/备注）：翻译时注入，
  QA 与复审时用来抓「同词多译」；`glossary.py --suggest x.en.srt` 抽专名候选；
- QA 阈值按 **Netflix 简体中文 TTSG**：每行 ≤16 字（最多 18）、≤2 行、CPS ≤9（最多 11）、
  另查时间重叠/字库外字符/未翻译/术语违规；
- 校对工具是本地 Web（只用标准库）：逐条左右对照、红黄标记、`j/k` 翻页、`s` 保存、
  `g` 把选中词加进术语表、`AI 压到阅读速度 / 润色` 只改当前条（前后各 3 条只读上下文）。

## 自动化

```sh
./gta4_ctl.sh boot          # 起游戏：正常退出旧局 -> steam -applaunch（重试）-> 点 RGL OFFLINE -> 等窗口
./gta4_ctl.sh quit          # **从菜单正常退出**（暂停菜单 -> 游戏 -> 退出 -> 是），失败不强杀
./gta4_ctl.sh stop          # 结束游戏：先走 quit，失败才强杀；--force 直接强杀
./gta4_ctl.sh shot name     # 截图
./gta4_ctl.sh key Escape    # 按键（按住 350ms）
./gta4_ctl.sh wheel 1 4     # 滚轮切台
```

### 用菜单正常退出（`quit` / `stop`）

**为什么不再强杀**：一天里强杀十几次之后，RGL 会卡在 “Connecting to Rockstar Games
Services”（`docs/gta4-env.md` 坑 6b），之后连启动都起不来。走菜单退出还会让自动存档落盘。

流程（每一步都用截图判定，不靠盲按键）：

1. `Escape` 开暂停菜单；如果已经开着就先关再开 —— 让**页签条重新拿到焦点**
   （焦点在列表里时看到的是 `↑↓ 移动 / ←→ 调整`，在页签条上才是切页签）。
2. 页签一路 `Right` 到绕回最左 ⇒ 说明上一格就是最右的「游戏」页签（再 `Left` 回一格）。
3. 列表：刚切过来的页签**方向键完全没反应**，要先按一下 `Return` 把焦点放进列表
   （只落到第一项，不执行）；然后一路 `Up` 到绕回底部 ⇒ 停在最后一项「退出」。
4. `Return` → 「确定要退出游戏吗？」（画面几乎全黑、页签条消失）→ `Return` = 是。
5. 等窗口消失（游戏本体退出）→ 收尾：RGL 的 `Launcher.exe` / `SocialClubHelper.exe`
   经常不自己退，会拖着 Proton 链让 Steam 以为游戏还在跑 —— 由 `cleanup_after_exit` 杀掉。

截图判定在 `screen_probe.py`（输出 `menu= tab= row= mean=`），三个判据都是分辨率无关的：

- `menu`：暂停菜单有一条满幅、**零方差**的细分隔线（1280x720 时 y=137），上下两行纯黑；
  游戏画面同一行 std 在 4~19，不会误判。
- `tab` / `row`：选中项/选中页签是**橙色**（≈RGB 222,142,13），未选是灰（≈120）。
  `row` 只用来判「按 Up 是否绕回了底部」，不认文案、不数行数，所以 FF/汉化改文案也不影响。

### 踩出来的键盘规律

- 按键必须按住 **≥350ms**，瞬发会被吞（0.12s 的连按一个都不认）。
- 页签只能用**左右方向键**切，鼠标点不动页签（悬停在行上只出一个箭头光标，不选中）。
- **刚切过来的页签，上下方向键完全没反应**：列表没拿焦点，要按一下 `Return`
  把焦点放进去（只落在第一项，不执行）；之后上下都能走，并且上下都**绕回**。
- **RGL 覆盖层抢焦点**：`SocialClubHelper.exe` 被杀了会被 RGL 立刻重拉，重拉那几秒
  键盘全丢。所以脚本不是一上来就杀它，而是「按键没反应」时才杀 + 重新聚焦再试。
- 主菜单（标题画面）的「退出」也是列表最后一项，所以拿不到暂停菜单时脚本会直接按列表处理；
  但那里**不会盲按回车**（第一项是「开始游戏」），只走方向键，找不到绕回就报错退出。

## 已知坑

- 换 `.asi` 必须重启游戏（UAL 只在启动时扫描 `plugins/`）；ini 是 2 秒热生效。
- **native 只能从主线程每帧钩子里调**（worker 线程调用会被每帧清状态，静默不显示）。
- 中文长句不会自动换行（引擎按空格断词），要自己做断行。
- 本机启动项是 `WINEDLLOVERRIDES="dinput8=n,b" %command%`，`dinput8.dll` 就是 UAL。
