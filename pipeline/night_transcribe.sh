#!/bin/bash
# 夜间任务：两个电视台整频道 转写 -> 英文 SRT -> 部署进游戏（热加载）
# 用 tools/jobs.sh start 跑（见仓库 AGENTS.md「并行与后台」）；预计 ~25 min（并行）/ ~45 min（串行）
set -e
D=~/tmp/$(date +%F)/gta4-m2
V=~/.local/venvs/whisper/bin/python
J="$(cd "$(dirname "$0")" && pwd)"
G="/home/ydb/.steam/steam/steamapps/common/Grand Theft Auto IV/GTAIV"
P="$G/GTAIV/plugins"
mkdir -p "$D"; cd "$D"

# 1. 抽音轨（已抽过就跳过；wav 各 ~100 MB）
for f in weazel CNT; do
  [ -s ${f}_full.wav ] || ffmpeg -v error -y -i "$G/movies/$f.bik" -vn -ac 1 -ar 16000 -c:a pcm_s16le ${f}_full.wav
done

# 2. 转写（并行，各 8 线程；白天跑会很慢，夜间无碍）
for f in weazel CNT; do
  [ -s ${f}_full.json ] || nice -n 10 $V "$J/transcribe.py" ${f}_full.wav small ${f}_full.json > ${f}_full.log 2>&1 &
done
wait
echo "$(date +%H:%M:%S) transcription done"

# 3. 英文 SRT -> 部署（插件按帧总数认频道，mtime 变化热加载）
for f in weazel CNT; do
  nice -n 10 $V "$J/json2srt.py" ${f}_full.json 0 ${f}_en.srt
  cp ${f}_en.srt "$P/$(echo $f | tr a-z A-Z).srt"
  echo "deployed $P/$(echo $f | tr a-z A-Z).srt"
done
echo "$(date +%H:%M:%S) all done"
