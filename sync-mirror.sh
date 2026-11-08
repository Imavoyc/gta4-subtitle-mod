#!/bin/bash
# 把本目录的**代码**同步到对外镜像仓（不含任何游戏派生数据），并提交推送。
#   ./sync-mirror.sh [-m "提交信息"]
#
# 镜像：https://github.com/Imavoyc/gta4-subtitle-mod
# ⚠️ 只用 exclude 白名单式的 rsync，**不要** --delete-excluded（那会把目标目录里
#    没被 include 的东西全删掉，包括 .git —— 踩过一次）。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DST=${MIRROR_DIR:-$HOME/code/gta4-subtitle-mod}
MSG="auto: sync from game-project"
[ "${1:-}" = "-m" ] && MSG=${2:-$MSG}

[ -d "$DST/.git" ] || { echo "镜像目录/仓库不存在: $DST" >&2; exit 1; }

rsync -a --prune-empty-dirs \
  --exclude '__pycache__/' --exclude 'build/' \
  --exclude '*.srt' --exclude '*.tsv' --exclude '*.bin' --exclude '*.json' \
  --exclude '*.wav' --exclude '*.raw' --exclude '*.mp4' --exclude '*.mkv' \
  "$HERE/README.md" "$HERE/Licence" "$HERE/Makefile" "$HERE/gta4_ctl.sh" \
  "$HERE/screen_probe.py" "$HERE/sync-mirror.sh" "$DST"/ 2>/dev/null || true
cp "$HERE/README.md" "$HERE/Makefile" "$HERE/gta4_ctl.sh" "$HERE/screen_probe.py" \
   "$HERE/sync-mirror.sh" "$DST"/ 2>/dev/null || true
for d in src pipeline review docs; do
  [ -d "$HERE/$d" ] || continue
  rsync -a --exclude '__pycache__/' --exclude '*.srt' --exclude '*.tsv' \
        --exclude '*.bin' --exclude '*.json' --exclude '*.wav' --exclude '*.raw' \
        "$HERE/$d" "$DST"/
done

cd "$DST"
git add -A
git diff --cached --quiet && { echo "镜像已是最新"; exit 0; }
git commit -q -m "$MSG"
git push -q github HEAD:main
echo "已同步并推送: $(git log --oneline -1)"
