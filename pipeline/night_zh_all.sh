#!/bin/sh
# 全量中文字幕：电视 6 套 + 电台 21 台（**夜间跑**，GPUStack 规则见仓库 AGENTS.md）。
#
#   ./night_zh_all.sh                 # 真跑（worker 池并行；每路 --resume，中断可续）
#   LANES=2 ./night_zh_all.sh         # 并行路数（默认 3；一个文件一路，文件内串行）
#   BATCH=20 ./night_zh_all.sh        # 每个请求几条（默认 10）
#   ./night_zh_all.sh --dry           # 只打印顺序、规模与将执行的命令
#   ./night_zh_all.sh --tv-only       # 只跑电视（可部署进游戏的那部分）
#   ./night_zh_all.sh --radio-only    # 只跑电台（M4 同步没做完前只在磁盘上）
#   ./night_zh_all.sh --post          # 每个文件跑完再跑 qa.py
#
# 顺序：**电视先、电台后**，组内按 cue 数从大到小（先干大文件，尾巴短）。
# 并行是一个 worker 池：哪个文件先完，下一个文件立刻顶上去（不按"批"等齐）。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DATE=${DATE:-$(date +%F)}
OUT=${OUT:-$HOME/tmp/$DATE/gta4-zh}
TM=${TM:-$HOME/tmp/2026-11-07/gta4-tm/out/tm.tsv}      # tm_build.py build 的产物
TV_EN=${TV_EN:-$HOME/tmp/2026-11-06/gta4-tv-asr}
RADIO_EN=${RADIO_EN:-$HOME/tmp/2026-11-06/radio-srt}
LANES=${LANES:-3}
BATCH=${BATCH:-10}
DRY=0
WHAT=all
POST=0
for a in "$@"; do
    case $a in
    --dry) DRY=1 ;;
    --tv-only) WHAT=tv ;;
    --radio-only) WHAT=radio ;;
    --post) POST=1 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
    esac
done

[ -f "$TM" ] || { echo "缺 TM: $TM（先跑 tm_build.py build）" >&2; exit 1; }
mkdir -p "$OUT/logs" "$OUT/qa"

# ---- 1) 收集待翻文件（电视在前），并按 cue 数从大到小排
: > "$OUT/jobs.tsv"
add() { [ -f "$1" ] || { echo "  跳过（缺）$1" >&2; return 0; }
        printf '%s\t%s\n' "$1" "$2" >> "$OUT/jobs.tsv"; }
if [ "$WHAT" != radio ]; then
    for n in weazel CNT weazelep1 CNTep1 weazelep2 CNTep2; do
        add "$TV_EN/$n.en.srt" "$OUT/$n.zh.srt"
    done
fi
if [ "$WHAT" != tv ]; then
    for f in "$RADIO_EN"/*.en.srt; do
        b=$(basename "$f" .en.srt)
        add "$f" "$OUT/$b.zh.srt"
    done
fi

sort -t"$(printf '\t')" -k1,1r "$OUT/jobs.tsv" > "$OUT/jobs.raw"   # 先按路径排，稳定
python3 - "$OUT/jobs.raw" "$OUT/plan.txt" "$LANES" "$BATCH" <<'PY'
import sys
jobs = []
for line in open(sys.argv[1], encoding="utf-8"):
    p = line.rstrip("\n").split("\t")
    if len(p) == 2:
        jobs.append((open(p[0], encoding="utf-8", errors="replace").read().count("-->"), p[0], p[1]))
tv = sorted([j for j in jobs if "/gta4-tv-asr/" in j[1]], reverse=True)
radio = sorted([j for j in jobs if "/gta4-tv-asr/" not in j[1]], reverse=True)
ordered = tv + radio
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for _, en, zh in ordered:
        f.write(f"{en}\t{zh}\n")
tot = sum(n for n, _, _ in ordered)
tl = sum(n for n, en, _ in ordered if "/gta4-tv-asr/" in en)
lanes, batch = int(sys.argv[3]), int(sys.argv[4])
print(f"共 {len(ordered)} 个文件 / {tot} cue（电视 {tl}，电台 {tot-tl}）")
print(f"顺序（每组内长文件优先）：" + " ".join(f"{en.split('/')[-1].replace('.en.srt','')}({n})" for n, en, _ in ordered[:6]) + " …")
print(f"≈ {tot//batch} 个请求；{lanes} 路并行，预计 {tot//batch*4//lanes//60}~{tot//batch*8//lanes//60} 分钟")
PY

if [ "$DRY" = 1 ]; then
    echo "--- worker 将执行 ---"
    echo "translate_srt.py <en> <zh> --tm $TM --glossary $HERE/glossary.tsv --batch $BATCH --resume --tm-min-sim 0.97 --tm-exemplars 4"
    echo "translate_srt.py <en> <zh> --fix --glossary $HERE/glossary.tsv"
    echo "qa.py <en> <zh>   # 带 --post 时"
    exit 0
fi

: "${GPUSTACK_API_KEY:?GPUSTACK_API_KEY 未设置}"

# ---- 2) 生成 worker（xargs 里跑，避免子 shell 看不到函数）
cat > "$OUT/worker.sh" <<EOF
#!/bin/sh
HERE='$HERE'; OUT='$OUT'; TM='$TM'; BATCH='$BATCH'; POST='$POST'
en=\$1; zh=\$2
name=\$(basename "\$zh" .srt)
log="\$OUT/logs/\$name.log"
{
    echo "=== \$(date +%H:%M:%S) \$en"
    python3 "\$HERE/translate_srt.py" "\$en" "\$zh" --tm "\$TM" \\
            --glossary "\$HERE/glossary.tsv" --batch "\$BATCH" --resume \\
            --tm-min-sim 0.97 --tm-exemplars 4 2>&1
    echo "=== \$(date +%H:%M:%S) --fix"
    python3 "\$HERE/translate_srt.py" "\$en" "\$zh" --fix \\
            --glossary "\$HERE/glossary.tsv" 2>&1
    [ "\$POST" = 1 ] && python3 "\$HERE/qa.py" "\$en" "\$zh" > "\$OUT/qa/\$name.qa.txt" 2>&1 || true
    echo "=== \$(date +%H:%M:%S) done"
} > "\$log" 2>&1
EOF
chmod +x "$OUT/worker.sh"

# ---- 3) worker 池：一行一个文件，-P 控制并行数
xargs -I LINE -P "$LANES" sh -c 'tab=$(printf "\t"); en=${1%%"$tab"*}; zh=${1#*"$tab"}; sh '"$OUT"'/worker.sh "$en" "$zh"' _ LINE < "$OUT/plan.txt"

echo "完成。产物在 $OUT；日志 $OUT/logs/*.log"
echo "电视可部署：cp $OUT/weazel.zh.srt $OUT/CNT.zh.srt \"\$GTA/plugins\"/"
