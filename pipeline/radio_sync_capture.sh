#!/bin/sh
# 同时抓「电台状态(内存)」+「实际播放的音频」，用来事后离线定出"当前段身份"字段。
#
#   ./radio_sync_capture.sh [秒数] [输出目录]
#
# 产出：<out>/state.tsv（每 0.5 s 一行：时刻/台/段内ms/段长/idx/当前段指针）
#       <out>/audio.wav（48k 立体声，抓 sink monitor）
# 之后用 radio_sync_match.py 把音频对上我们已知的 973 个段，得到 ground truth，
# 再和 state.tsv 逐行比对，就知道哪个字段才是"段身份"。
set -eu
SECS=${1:-180}
OUT=${2:-$HOME/tmp/$(date +%F)/radio-capture-$(date +%H%M%S)}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
SINK=$(pactl get-default-sink)
echo "抓 $SECS s → $OUT  (sink=$SINK)"
parec --device="$SINK.monitor" --format=s16le --rate=48000 --channels=2 \
      --file-format=wav "$OUT/audio.wav" &
PAREC=$!
python3 "$HERE/radio_state_log.py" --secs "$SECS" > "$OUT/state.tsv"
kill $PAREC 2>/dev/null || true
wait $PAREC 2>/dev/null || true
echo "完成：$OUT/state.tsv  $(wc -l < "$OUT/state.tsv") 行,  audio.wav $(du -h "$OUT/audio.wav" | cut -f1)"
