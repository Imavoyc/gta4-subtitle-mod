# pipeline — 电视台/电台中文字幕内容管线（M2）

> 用法与版本坑都写在这；插件侧见 `../README.md` 与 `docs/gta4-subtitle.md`。

## 流程

```
<game>/movies/<频道>.bik
  │ ffmpeg -ss <起点> -t <时长> -i xx.bik -vn -ac 1 -ar 16000 xx.wav
  ▼
transcribe.py   faster-whisper small/int8, 8 线程, CPU（~0.25× 实时）
  │  → segments.json  [{start,end,text}, ...]（相对切片起点）
  ▼
translate.py    GPUStack deepseek-v4.1-flash（10 条/请求，~2-9 s/批）
  │            （译已有的英文 SRT 用 zh_from_srt.py，见下）
  │  → <频道>.srt（时间轴 = 切片起点 offset + 段内时间）
  │  → 字符集校验：不在 char_table.dat 3518 码点内的字符
  │     · 已知映射（【】（）等全角 → 半角）自动替换
  │     · 其余丢弃，并写 <srt>.report.txt 逐条列出（翻译提示词应避开）
  ▼
plugins/<频道>.srt   ← 插件按 Bink 总帧数自动认频道（77182=weazel, 93115=CNT），
                        文件 mtime 变化 2 秒内热加载，游戏不用重启
```

### 已有英文 SRT → 中文 SRT（2026-11-06 新增）

整频道英文 SRT 部署之后，翻译不必从 whisper JSON 重来，直接对 SRT 逐条翻：

```sh
python3 zh_from_srt.py ~/tmp/<date>/gta4-zh/weazel.srt ~/tmp/<date>/gta4-zh/weazel.zh.srt 20
# 20 条/请求；时间轴原样保留；不在字库里的字丢弃并写 <out>.report.txt
cp ~/tmp/<date>/gta4-zh/weazel.zh.srt "$P/weazel.srt"     # 热加载，不用重启
```

- **字符集校验改成查我们自己的字库**（`subtitle_atlas_metrics.bin`，6882 字形 =
  ASCII + 中文标点 + GB2312 全部 6763 汉字），不再依赖汉化包的 `char_table.dat`；
- 实测速度：weazel 1087 条 ~7 min、CNT 1375 条 ~11 min（两路并行，daytime 可跑）；
- 一次整频道翻译里命中二级字的条目不少（`嗯/裆/痿/瑜/伽/侏/噜/崽` 等），
  所以字库已扩到 GB2312 全字集（见 `bake_atlas.py`）。

## 实测参数（debian1，2026-11-06）

- 3 分钟音频：转写 ~45 s（8 线程），翻译 4 批 ~10 s；
- **整频道**：weazel 42:52 → **~11 min**，CNT 51:44 → **~13 min**（两路并行各 8 线程）；
- 频道音频抽取：整部 bik 解音轨 ~10 s/频道。

## 环境坑（已验证的解法）

1. `faster-whisper 1.2.1` 要求 `huggingface_hub==0.25.2`（2.x 会
   `open() got an unexpected keyword argument 'metadata_errors'`）；
2. 新版 PyAV 也炸同一个调用，**干脆不用 PyAV**：输入先用 ffmpeg 转成
   16k mono s16 wav，`transcribe.py` 里用 `wave+numpy` 自己解码；
3. 模型下载走 `HF_ENDPOINT=https://hf-mirror.com`（不用代理，~460 MB）；
4. venv：`python3 -m venv ~/.local/venvs/whisper`（本机 python3.13，
   pip 走 `https://mirrors.aliyun.com/pypi/simple/`）。

## 命令

```sh
G=/home/ydb/.steam/steam/steamapps/common/Grand\ Theft\ Auto\ IV/GTAIV
P=$G/GTAIV/plugins

# 1. 抽音轨（整频道）
ffmpeg -v error -y -i "$G/movies/weazel.bik" -vn -ac 1 -ar 16000 weazel_full.wav
# 2. 转写
~/.local/venvs/whisper/bin/python transcribe.py weazel_full.wav small weazel_full.json
# 3. 翻译 + 字符集校验（offset=0 因为抽的是整频道）
~/.local/venvs/whisper/bin/python translate.py weazel_full.json 0 weazel.srt
# 4. 放进游戏（热加载）
cp weazel.srt "$P/weazel.srt"
```

试点切片：`ffmpeg -ss 1740 -t 180 -i "$G/movies/weazel.bik" ...`（offset=1740）。

## 夜间批量（**2026-11-06 用户决定：翻译/转写统一留晚上做**）

> TV 英文底稿也**要重提取**（whisper-small 会丢 3~5% 的短句，实测对比见
> `docs/gta4-translation-pipeline.md` 二点五）：顺序是
> `transcribe.py --words`（词级时间戳）→ `reasr_srt.py --words`（qwen3-asr 文本 + 词对齐）
> → `translate_srt.py`（带术语表重译）→ `qa.py` → 人工校对。

> **规则（已写进仓库 `AGENTS.md`）**：GPUStack 只准夜间用；白天只允许单个请求级别
> 的小样测试，不许白天跑整频道/整批翻译。

白天只做验证；下面这些等夜间一起跑（本机能跑的就本机，重的丢 115）：

```sh
G=/home/ydb/.steam/debian-installation/steamapps/common/Grand\ Theft\ Auto\ IV/GTAIV
W=~/.local/venvs/whisper/bin/python
D=~/tmp/<日期>/gta4-night            # 输出目录

# A. DLC 电视台集数（现在只有主频道 weazel/CNT 有字幕）
#    TLAD: weazelep1 12:04 / CNTep1 16:04；TBoGT: weazelep2 10:14 / CNTep2 11:43
for f in TLAD/weazelep1 TLAD/CNTep1 TBoGT/weazelep2 TBoGT/CNTep2; do
  b=$(basename $f)
  ffmpeg -v error -y -i "$G/$f.bik" -vn -ac 1 -ar 16000 "$D/$b.wav"
  $W transcribe.py "$D/$b.wav" small "$D/$b.json"
  $W json2srt.py  "$D/$b.json" "$D/$b.en.srt"
  python3 zh_from_srt.py "$D/$b.en.srt" "$D/$b.zh.srt" 20
done
# 之后要给插件加帧数映射（PickChannelSrt 目前只认 77182 / 93115）：
#   21700=weazelep1 28873=CNTep1 18399=weazelep2 21079=CNTep2

# B. 电台字幕内容（M4 前置）：**格式已解开**，本地就能解码 + 转写
#    1) 用 ~/.local/venvs/rpf/bin/python + pyrpfiv 从 pc/audio/sfx/radio_*.rpf 抽段
#       （RPFParser(rpf, GTAIV.exe) -> build_file_list() -> extract_file(path, outdir)）
#    2) ivaud2wav.py <段> x.wav --auto   # IMA ADPCM；一个段里每个"通道"是一条 21.25s 片段
#    3) **夜间**转写：优先 qwen3-asr-1.7b（GPUStack /v1/audio/transcriptions，
#       比本地 whisper-small 强很多，见 docs/gta4-radio.md 的对比）；
#       要时间轴就用 whisper 出 timestamp + qwen3-asr 出文本按词对齐合并。
#       （谈话类 253 段；音乐台的 DJ 串词 ~250 段同理）
#    4) zh_from_srt.py 翻中文
#    913 段 × 多通道里有不少空片段，先按 RMS 过滤；全量解包 ~3 GB，夜间做，或丢 115。
```

优先级：**A 先做**（纯本机、~40 min、立刻能在游戏里看见），B 要看 M4 的段同步进度。

## 翻译记忆（b 站人工听译语料，2026-11-07）

`bili_cn_corpus.py` 从「赤佬听译」的视频 OCR 出英中双行语料，`tm_build.py` 把它清洗成
**翻译记忆**（TM），既可以直接填 cue，也给机翻当风格样例。全本地、不发请求，30 s 跑完。

```sh
# 语料 → tm.tsv + align/<频道>.tm.jsonl + glossary_candidates.tsv + report.md
python3 tm_build.py build --corpus ~/tmp/<date>/cn-corpus2/pairs.jsonl \
        --out ~/tmp/<date>/gta4-tm --srt ~/tmp/<date>/radio-srt/*.en.srt
# 夜间：让模型从例句里读出术语译名（人工确认后并入 glossary.tsv）
python3 tm_build.py terms --candidates ~/tmp/<date>/gta4-tm/glossary_candidates.tsv \
        --out ~/tmp/<date>/gta4-tm/terms.tsv
# 用他的译法复核现有术语表
python3 tm_build.py check --tm ~/tmp/<date>/gta4-tm/tm.tsv
```

翻译时挂上 TM（`--tm`）：≥`--tm-min-sim`（默认 0.90）直接填、不再发请求；
0.55~0.90 的对作风格样例（每批 `--tm-exemplars` 条，默认 4）塞进提示词；
`--tm-only` 只填 TM、完全不联网（白天可用）。

实测覆盖（2026-11-07，40 个视频 / 4002 条可用语料）：radio_adverts 18.9%、
radio_wktt 14.0%、radio_news 8.6%、weazel 6.7%，**全部 SRT 合计 4.9%**；
数字与噪声画像见 `docs/gta4-translation-pipeline.md` 三点五节。

## 规模预估（全套内容）

| 内容 | 音频量 | 本机转写+翻译 |
|---|---|---|
| 电视台 CNT + weazel | ~94 min | **~25 min**（并行） |
| 电台 1928 段（pc/audio/sfx/radio_*.rpf） | 待测（估计 8–15 h） | 本机不合适 → 115 夜间（256 核可并行几十路，~1 h 级） |
