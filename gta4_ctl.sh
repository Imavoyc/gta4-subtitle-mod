#!/bin/bash
# GTA IV (Steam AppId 12210, Proton) control helpers for the subtitle mod.
#
#   gta4_ctl.sh status          # 进程 / 窗口状态
#   gta4_ctl.sh quit            # **从菜单正常退出**：暂停菜单 → 游戏 → 退出 → 是
#   gta4_ctl.sh stop [--force]  # 结束游戏：先试正常退出，失败（或 --force）才强杀
#   gta4_ctl.sh start           # steam -applaunch 12210，等到游戏窗口出现
#   gta4_ctl.sh restart         # stop（正常退出优先）+ start
#   gta4_ctl.sh shot NAME       # 截图到 ~/tmp/<date>/gta4-radio/NAME.png
#   gta4_ctl.sh key K [K...]    # 按住 350ms 的按键序列
#   gta4_ctl.sh wheel N [4|5]   # 滚轮 N 次（4=上 5=下）
#   gta4_ctl.sh wait-window S   # 等窗口出现，最多 S 秒
#
# 为什么要"正常退出"：反复强杀会让 RGL 卡在 "Connecting to Rockstar Games Services"
# （docs/gta4-env.md 坑 6b），之后连启动都起不来；正常退出还会让自动存档落盘。
#
# 键盘要点（都是实测踩出来的，见 README 的"用菜单正常退出"一节）：
#   * 按键必须按住 >=350ms，瞬发会被吞（0.12s 的连按一个都不认）；
#   * 页签只能用左右方向键切，鼠标点不动页签；
#   * 刚切过来的页签，列表还没拿焦点，上下方向键完全没反应 —— 要先按一下 Return
#     把焦点放进列表（只落在第一项，不执行）；
#   * 上下在列表里都会**绕回**（顶部再按上是最后一项）→ 脚本靠"橙色高亮行 y 突然
#     变大"来确认已经落到最后一项"退出"，不数行数、不认文案。
#
# 注意：不要用 pkill -f（会匹配到自己这条 bash -c，见仓库 AGENTS.md）。
set -u

APPID=12210
WINRE='^GTAIV$'
SHOTDIR="$HOME/tmp/$(date +%F)/gta4-radio"
export DISPLAY=:0
export XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
mkdir -p "$SHOTDIR"

pids() {  # pids <exe 名片段>  —— 用 ps+awk 过滤，避免自匹配
    ps -eo pid,args | awk -v pat="$1" 'index($0, pat) > 0 && $0 !~ /awk/ {print $1}'
}

wid() { xdotool search --name "$WINRE" 2>/dev/null | head -1; }

# ---------------------------------------------------------------- 菜单自动化
PROBE_PY="$(dirname "$0")/screen_probe.py"

probe() {  # 截图 + 分析；设置 MENU(0/1 暂停菜单) TAB(页签 x) ROW(高亮行 y) MEAN(平均亮度)
    local w line f="$SHOTDIR/_probe.png"
    w=$(wid) || return 1
    [ -n "$w" ] || return 1
    import -window "$w" "$f" 2>/dev/null || return 1
    line=$(python3 "$PROBE_PY" "$f" 2>/dev/null) || return 1
    [ -n "$line" ] || return 1
    # line = "menu=1 tab=118 row=-1 mean=9 w=1280 h=720"
    read -r f1 f2 f3 f4 _ <<<"$line"
    MENU=${f1#menu=}; TAB=${f2#tab=}; ROW=${f3#row=}; MEAN=${f4#mean=}
    return 0
}

tap() {  # tap <xdotool 键名> [按键后的等待秒数]  —— 按住 350ms
    xdotool keydown "$1"; sleep 0.35; xdotool keyup "$1"; sleep "${2:-0.35}"
}

focus_game() {  # 激活窗口，并把鼠标挪到菜单列表之外（悬停会锁住方向键）
    local w WIDTH HEIGHT
    w=$(wid); [ -n "$w" ] || return 1
    xdotool windowactivate --sync "$w" 2>/dev/null
    read -r WIDTH HEIGHT <<<"$(xdotool getwindowgeometry --shell "$w" \
        | awk -F= '/^(WIDTH|HEIGHT)=/{printf "%s ", $2}')"
    [ -n "${WIDTH:-}" ] && xdotool mousemove --window "$w" $((WIDTH/2)) $((HEIGHT-8))
    sleep 0.2
    return 0
}

rgl_launcher_pids() {  # RGL 的 Launcher.exe（注意别误伤 at-spi-bus-launcher 之类）
    ps -eo pid,args | awk '/Rockstar/ && /Launcher\.exe/ && $0 !~ /awk/ {print $1}'
}

cleanup_after_exit() {  # 收尾：RGL 覆盖层 + RGL Launcher + Proton 包装链
    pids 'SocialClubHelper.exe' | xargs -r kill -9
    # RGL 的 Launcher.exe 在游戏退出后经常不自己退（和“Connecting to Rockstar
    # Games Services”卡死是同一个毛病）；它不退整条链就挂着，Steam 会以为游戏还在跑
    rgl_launcher_pids | xargs -r kill -9
    pids 'PlayGTAIV.exe' | xargs -r kill 2>/dev/null
    for i in $(seq 1 20); do
        [ -z "$(pids 'GTAIV.exe')" ] && break
        sleep 1
    done
    echo "clean: 链上还剩 [$(pids 'GTAIV.exe' | tr '\n' ' ')]"
}

reclaim_focus() {  # RGL 覆盖层会抢键盘/焦点（docs/gta4-env.md 坑 1）：杀掉后重新聚焦
    pids 'SocialClubHelper.exe' | xargs -r kill -9
    sleep "${1:-2}"
    focus_game
}

esc_toggle() {  # 按一次 Esc，并用“菜单开关状态真的翻转了”确认按键没被吞
    local before
    probe || return 1
    before=$MENU
    tap Escape 1.2; probe || return 1
    [ "$MENU" != "$before" ] && return 0
    # 没反应：几乎都是 RGL 覆盖层把焦点抢走了
    reclaim_focus 2 || true
    probe || return 1
    tap Escape 1.2; probe || return 1
    [ "$MENU" != "$before" ] && return 0
    return 1
}

walk_tabs_last() {  # 页签一路向右直到绕回最左 → 说明上一格就是最右的“游戏”
    local i prev=$TAB wrapped=0 stuck=0
    for i in $(seq 1 9); do
        tap Right 0.5; probe || return 1
        if [ "$TAB" = "$prev" ]; then
            stuck=$((stuck + 1))
            [ "$stuck" -ge 3 ] && break      # 不绕回的话就是已经在最后一个页签
            reclaim_focus 1 >/dev/null 2>&1 || true
            probe || return 1
            continue
        fi
        stuck=0
        if [ "$TAB" -lt $((prev - 20)) ]; then wrapped=1; break; fi
        prev=$TAB
    done
    if [ "$wrapped" = 1 ]; then tap Left 0.7; probe || return 1; fi
    echo "quit: 页签 tab=$TAB (绕回=$wrapped)"
    return 0
}

walk_last_item() {  # walk_last_item [allow_enter] —— 列表一路向上直到绕回底部 → 停在最后一项
    local i prev found=0 stuck=0 allow_enter=${1:-1}
    # 刚切过来的页签，列表还没拿到焦点（方向键完全没反应，只有橙色高亮能证明焦点在列表里）：
    # 这里按一下回车只是"把焦点放进列表"（落在第一项，不执行），否则方向键按不动。
    # 但主菜单（allow_enter=0）不能盲按回车 —— 那里第一项是"开始游戏"！
    if [ "$ROW" -lt 0 ] && [ "$allow_enter" = 1 ]; then
        tap Return 1.2; probe || return 1
        if [ "$ROW" -lt 0 ]; then
            echo "quit: 回车没能把焦点放进列表（row=-1）"
            return 1
        fi
        echo "quit: 列表焦点在 row=$ROW"
    fi
    prev=$ROW
    for i in $(seq 1 24); do
        tap Up 0.5; probe || return 1
        if [ "$ROW" = "$prev" ]; then
            stuck=$((stuck + 1))
            [ "$stuck" -ge 3 ] && break
            reclaim_focus 1 >/dev/null 2>&1 || true
            probe || return 1
            continue
        fi
        stuck=0
        if [ "$prev" -ge 0 ] && [ "$ROW" -ge 0 ] && [ "$ROW" -gt $((prev + 25)) ]; then
            found=1; break
        fi
        prev=$ROW
    done
    if [ "$found" != 1 ]; then
        echo "quit: 没走到列表底部（绕回检测失败，row=$ROW）"
        return 1
    fi
    # 自检：再按 Down 应该绕回顶部（row 变小），再 Up 回到最后一项
    prev=$ROW
    tap Down 0.6; probe || return 1
    if [ "$ROW" -ge "$prev" ]; then echo "quit: 自检失败（下移没绕回顶部）"; return 1; fi
    tap Up 0.6; probe || return 1
    [ "$ROW" -gt $((prev - 25)) ] || { echo "quit: 自检失败（没回到最后一项）"; return 1; }
    echo "quit: 高亮 = 最后一项/退出 (row=$ROW)"
    return 0
}

confirm_and_exit() {  # 回车 -> 确认框 -> 回车(是) -> 等游戏退出 -> 收尾
    local timeout=${1:-40} i n t0
    tap Return 1.4; probe || return 1
    if [ "$MENU" = 1 ] || [ "$MEAN" -gt 8 ]; then
        echo "quit: 确认框没出来（menu=$MENU mean=$MEAN）"
        return 1
    fi
    # 回车 = 是
    tap Return 1.0
    t0=$(date +%s)
    # 游戏本体退出的判据 = 窗口消失（Proton/RGL 的包装链会晚几秒到几十秒）
    for i in $(seq 1 "$timeout"); do
        [ -z "$(wid)" ] && break
        sleep 1
    done
    if [ -n "$(wid)" ]; then
        echo "quit: ${timeout}s 内游戏窗口没关（可能没点中\"是\"）"
        return 1
    fi
    echo "quit: 游戏已退出（$(( $(date +%s) - t0 ))s）"
    sleep 4
    if [ -z "$(pids 'GTAIV.exe')" ]; then
        echo "quit: 整条进程链自己清空了"
        pids 'SocialClubHelper.exe' | xargs -r kill -9
        return 0
    fi
    echo "quit: 包装链（Proton/RGL）还赖着，收尾"
    cleanup_after_exit
    n=$(pids 'GTAIV.exe' | wc -l)
    if [ "$n" != 0 ]; then
        echo "quit: 收尾后链上还留着 $n 个进程"
        return 1
    fi
    return 0
}

open_pause_menu() {  # 拿到「刚打开」的暂停菜单（页签条有焦点）：已开着就先关再开
    probe || return 1
    if [ "$MENU" = 1 ]; then
        esc_toggle || return 1   # 关掉，为的是让重新打开时焦点回到页签条
    fi
    esc_toggle || return 1       # 打开
    [ "$MENU" = 1 ]
}

graceful_quit() {  # graceful_quit [等退出的秒数] —— 从菜单退出，返回 0 = 干净退出
    local timeout=${1:-40} try
    if [ -z "$(pids 'GTAIV.exe')" ]; then echo "quit: 游戏没在跑"; return 0; fi
    if [ -z "$(wid)" ]; then
        echo "quit: 进程在但还没有窗口（正在启动/正在退出），不盲按 ESC"
        return 1
    fi
    focus_game || { echo "quit: 窗口激活失败"; return 1; }

    # 走菜单偶尔会被吞一拍（按键/焦点），整条流程重试一次比在中间猜更稳
    for try in 1 2; do
        if open_pause_menu; then
            if walk_tabs_last && walk_last_item; then
                confirm_and_exit "$timeout" && return 0
            fi
            echo "quit: 第 ${try} 次没走通，重开暂停菜单再试"
            sleep 1
        else
            # 拿不到暂停菜单：没存档时的主菜单 / 过场里 Esc 可能不起作用
            echo "quit: 拿不到暂停菜单，当作主菜单处理（列表最后一项也是退出）"
            if walk_last_item 0 && confirm_and_exit "$timeout"; then return 0; fi
            break
        fi
    done
    return 1
}

force_stop() {  # 强杀（老办法）：只在前面的正常退出失败时用
    pids 'GTAIV.exe' | xargs -r kill
    for i in $(seq 1 30); do
        [ -z "$(pids 'GTAIV.exe')" ] && break
        sleep 1
    done
    pids 'GTAIV.exe' | xargs -r kill -9
    cleanup_after_exit
    sleep 3
    echo "stopped: GTAIV.exe=[$(pids 'GTAIV.exe' | tr '\n' ' ')]"
}

case "${1:-status}" in
status)
    g=$(pids 'GTAIV.exe' | tr '\n' ' ')
    p=$(pids 'PlayGTAIV.exe' | tr '\n' ' ')
    echo "GTAIV.exe: ${g:-none}   PlayGTAIV.exe: ${p:-none}"
    w=$(wid); echo "window: ${w:-none}"
    ;;
quit)  # 只走正常退出；失败也不强杀（会提示，强杀请用 stop --force）
    if graceful_quit "${2:-40}"; then exit 0; fi
    echo "quit: 正常退出没成功，游戏还在跑（未强杀；需要强杀用 stop --force）"
    exit 1
    ;;
stop)
    if [ "${2:-}" != "--force" ] && graceful_quit 40; then exit 0; fi
    echo "stop: 走强杀（下次启动可能要重试，见 docs/gta4-env.md 坑 6b）"
    force_stop
    ;;
start)
    if [ -n "$(wid)" ]; then echo "already running"; exit 0; fi
    # Steam 在刚结束一局之后会静默忽略 applaunch，所以要重试 + 确认进程真的起来
    for attempt in 1 2 3 4 5 6; do
        steam -applaunch "$APPID" >"$SHOTDIR/steam-launch.log" 2>&1
        for i in $(seq 1 12); do
            [ -n "$(pids 'PlayGTAIV.exe')" ] && { echo "launched (attempt $attempt)"; return 0 2>/dev/null || exit 0; }
            sleep 1
        done
        echo "launch attempt $attempt did not start; retrying"
        sleep 8
    done
    echo "failed to launch"; exit 1
    ;;
rgl-dismiss)  # RGL 联网验证常报 Code 4：一路点 RETRY，不行点 OFFLINE MODE
    for i in $(seq 1 "${2:-40}"); do
        e=$(xdotool search --onlyvisible --name "^Error$" 2>/dev/null | head -1)
        if [ -n "$e" ]; then
            n=$(xdotool getwindowgeometry --shell "$e" | awk -F= '/WIDTH/{print $2}')
            if [ "$i" -le 3 ]; then
                xdotool mousemove --window "$e" $((n/2)) 264 click 1   # RETRY
                echo "rgl: RETRY (try $i)"
            else
                xdotool mousemove --window "$e" 658 264 click 1        # OFFLINE MODE
                echo "rgl: OFFLINE MODE"
            fi
            sleep 6
        fi
        if [ -n "$(wid)" ]; then echo "game window up after ${i}s"; exit 0; fi
        sleep 1
    done
    echo "rgl-dismiss timeout"; exit 1
    ;;
boot)  # 一键：重启（正常退出优先）-> 处理 RGL -> 等进游戏
    "$0" stop
    "$0" start
    "$0" rgl-dismiss 240
    echo "--- waiting for gameplay (menu/load may take a while) ---"
    sleep 25
    "$0" shot boot-$(date +%H%M%S)
    ;;
wait-window)
    limit=${2:-180}
    for i in $(seq 1 "$limit"); do
        w=$(wid)
        if [ -n "$w" ]; then echo "window $w after ${i}s"; exit 0; fi
        sleep 1
    done
    echo "timeout waiting for window"; exit 1
    ;;
restart)
    "$0" stop
    "$0" start
    "$0" wait-window 240
    ;;
shot)
    w=$(wid) || { echo "no window"; exit 1; }
    [ -n "$w" ] || { echo "no window"; exit 1; }
    import -window "$w" "$SHOTDIR/${2:-shot}.png" && echo "$SHOTDIR/${2:-shot}.png"
    ;;
key)
    w=$(wid); [ -n "$w" ] || { echo "no window"; exit 1; }
    xdotool windowactivate --sync "$w"; sleep 0.15
    shift
    for k in "$@"; do xdotool keydown "$k"; sleep 0.35; xdotool keyup "$k"; sleep 0.4; done
    ;;
hold)   # hold <key> <seconds>
    w=$(wid); [ -n "$w" ] || { echo "no window"; exit 1; }
    xdotool windowactivate --sync "$w"; sleep 0.15
    xdotool keydown "$2"; sleep "${3:-1}"; xdotool keyup "$2"; sleep 0.3
    ;;
mouse)  # mouse <relx> <rely>  —— 相对移动鼠标（转视角）
    w=$(wid); [ -n "$w" ] || { echo "no window"; exit 1; }
    xdotool windowactivate --sync "$w"; sleep 0.15
    xdotool mousemove_relative -- "$2" "$3"
    ;;
wheel)
    w=$(wid); [ -n "$w" ] || { echo "no window"; exit 1; }
    xdotool windowactivate --sync "$w"; sleep 0.15
    n=${2:-1}; d=${3:-5}
    for i in $(seq 1 "$n"); do xdotool click "$d"; sleep 0.4; done
    ;;
*)
    sed -n '2,24p' "$0"; exit 1 ;;
esac
