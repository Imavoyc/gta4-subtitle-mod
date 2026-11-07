# GTA IV Radio Subtitle Mod (WIP)

Real-time **subtitle overlay for GTA IV** (Steam Complete Edition / 1.2.0.59 under Wine+Proton):
it draws its own text over the game with D3D9, and it can follow the **radio** segment by
segment, because it reads the audio engine's internal state directly.

> **Status: work in progress.** Things work in-game today, but the radio timing model is
> still being verified and some data tables have to be regenerated on your own copy of the
> game (see below).

## What it does

* **TV (Bink videos)** — subtitles driven by the video frame counter (`BinkOpen`/`BinkDoFrame`).
* **Radio** — per-segment subtitles. The plugin reads the currently playing segment's
  *resource hash* and *elapsed milliseconds* out of `CRadioStation` in the game's memory,
  picks `plugins/radio/<hash>.<lang>.srt`, and scrolls it with the engine clock.
* **Self-rendered text** — its own glyph atlas (SDF-free, baked bitmap + metrics), drawn through
  `IDirect3DSwapChain9::Present`, so it does not depend on the game font / any translation patch.
  Handles CJK line breaking (avoid line-head/line-tail punctuation) and a background bar sized
  to the real ink box.
* Hot reload: `.ini` and `.srt` take effect in ~2 s; only the `.asi` needs a game restart.

## Repository layout

```
src/            the ASI plugin (C++17, mingw-w64 cross build)
pipeline/       offline toolchain: RPF audio → ASR → SRT → translation, plus RE probes
gta4_ctl.sh     game control helpers (launch / quit through menus / screenshots / key taps)
screen_probe.py pause-menu detection from a screenshot (used by the control script)
docs/           notes (reverse-engineering details, plugin readme)
```

## Build

```sh
sudo apt install g++-mingw-w64-i686
make                       # -> build/GTAIV.SubtitleTest.asi
cp build/GTAIV.SubtitleTest.asi "$GTAIV_DIR/plugins/"
```

Then put `GTAIV.SubtitleTest.ini` next to it (see `docs/plugin-README.md`) and start the game.
Replace the `.asi` only while the game is **not** running (the process mmaps it).

## Pipeline (bring your own game copy)

**No game assets are included in this repository** — no `.rpf`, no extracted audio, no subtitle
text, no hash tables. The tools expect a local installation and generate everything locally:

```sh
export GTAIV_DIR="$HOME/.steam/steam/steamapps/common/Grand Theft Auto IV/GTAIV"

# 1. turn the radio RPF containers into PCM
gcc -O2 -o ivaud2pcm pipeline/ivaud2pcm.c
./ivaud2pcm "$GTAIV_DIR/pc/audio/sfx/radio_wktt.rpf" - --info   # (segments, not the rpf itself)

# 2. speech-to-text: one request per 21.25 s channel, texts only (no timestamps)
python3 pipeline/night_radio_asr.py ...          # needs GPUSTACK_API_KEY, or point it at any
                                                # OpenAI-compatible /v1/audio/transcriptions

# 3. engine ↔ file mapping (resource hash = joaat("<rpf base>_<file>"), lower case)
python3 pipeline/radio_hash_table.py build --listing rpf_listing.json
python3 pipeline/radio_hash_table.py index         # hash / channels / file duration

# 4. split per-station SRTs into per-segment ones (times are already segment-relative)
python3 pipeline/radio_split_tracks.py --en-dir radio-srt --zh-dir zh --out "$GTAIV_DIR/plugins/radio"
```

`rpf_listing.json` is the output of [pyrpfiv](https://github.com/…/pyrpfiv) (or any RPF3
extractor): `{rpf: {directories: [{name, files: [{name, size, offset}]}]}}`.

## How the radio sync works (short version)

`CRadioStation` has two 0xbd0-byte playback slots. The current slot index is at `+0x1917`;
inside a slot, `+0x960` points to an 8-byte record `{id, hash}` and `+0x990` is the elapsed
milliseconds. `hash` is `joaat("<rpf base>_<file name>")`. The segment's audio file contains
`n` independent 21.248 s clips, and the engine squeezes the whole file into its own `len`
(so file time ≈ `ms × file_duration / len`). Details, evidence and dead ends:
`docs/RE-NOTES.md`.

## 中文简介

给 GTA IV 加**中文字幕**：电视台（Bink 视频）按帧号走，**电台按段（segment）走**——
插件直接从游戏内存里读到当前播的是哪一段、已播多少毫秒，再去找对应的
`plugins/radio/<hash>.<lang>.srt`。字幕是我们自己用 D3D9 画的（自带字库），不依赖汉化包。

**状态：开发中。** 电台时间轴（引擎把整段音频压进自己的 `len`，比例 1.35~1.44×）还有
一个"哪种模型更准"的实机判定没做完，见 `docs/RE-NOTES.md` 第 3 节。

仓库**只放代码**：游戏本体、RPF 音频、字幕文本、字库位图一律不入库，工具链会从你本地的
游戏副本现场生成（见上面 Pipeline 一节）。

## Legal / scope

Personal, non-commercial, educational reverse engineering. This repository ships **code only**;
all game content (audio, dialogue, textures, fonts baked from third-party fonts) stays on the
user's own machine. Do not redistribute extracted game assets.

## License

MIT for the code in this repository (see `LICENSE`).
