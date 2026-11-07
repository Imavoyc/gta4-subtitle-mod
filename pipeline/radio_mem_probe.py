#!/usr/bin/env python3
"""从 Linux 侧直接读 GTA IV 进程内存做电台研究（不用改 .asi、不用重启游戏）。

为什么要有这个工具：M4（电台逐句同步）的研究全是"找内存里哪个字段是段身份 /
段内已播毫秒"，放在插件里试一次就得重编 + 重启游戏（90 秒 + 你回到车里）。
改成**外部进程读内存**（Wine 的 PE 就在 32 位平坦地址空间里，`/proc/<pid>/mem`
直接可读，本机 `kernel.yama.ptrace_scope=0`），就能一条命令反复试。

## 2026-11-07 晚：CRadioStation 的布局已解开（静态 + 动态双证据）

    CRadioStation（大小 0x1934）里是**两个 0xbd0 字节的播放槽**（双缓冲/交叉淡出）：

        slot0 = base + 0x000，slot1 = base + 0xbd0
        slot + 0x960  u32  -> 指向 8 字节记录 {0, hash}（hash = 段的资源 hash）
        slot + 0x964  u32     正在播的音频流句柄（FUN_00b769a0 里当 out 参数）
        slot + 0x990  u32   **段内已播毫秒**（0.5 s 走 ~500，换段归零）
        slot + 0x994  u32     本段时长（引擎认为的毫秒数，可能 < 文件总长）
        slot + 0xbc0  u8      模式（2 = 正常播放段）
        slot + 0xbc4  u32     记录索引（槽内的第几条，换段才变）
        slot + 0xbc8  u8      状态（1 已开始 / 3 已结束 / 0 空闲）
        slot + 0xbca  u8      槽被占用
        base  + 0x1917 u8   **当前是哪个槽**（0/1）
        base  + 0x17d0 u32    指向"描述符"，+0x1d 名字长度 / +0x1e 名字（台名）
        base  + 0x17fc char[?] 台名（如 THE_VIBE）

    台对象清单：全局 `*(u32*)0x011D76AC` = 数组基址（23 个指针，之后是 0xcdcdcdcd）。

    段的身份：`hash = *(u32*)(*(u32*)(slot+0x960) + 4)`，用 `radio_hash_table.py`
    的 joaat 表反查成 `RADIO_WKTT/5_FIZZ_2` 这样的名字。

## 子命令

    registry                 打印台对象清单（opcode → 名字，带校验）
    state                    一次性打印所有台的两个槽（当前槽 / 段名 / ms / 时长）
    log --secs N             每 0.5 s 打一行（每台一行，只打"在播"的槽）
    list / hunt / peek / watch   早期研究用的老命令（scanner 版，仍保留）
    peek <station> <off>     读某台某偏移的连续 8 个 dword（含 float 解释）

用法：
    radio_mem_probe.py state
    radio_mem_probe.py log --secs 60 > ~/tmp/<日期>/radio-state.tsv
"""
import argparse
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import radio_hash_table as HT
except Exception:                                   # 表/模块缺失也别让探针挂掉
    HT = None

NAME_OFF = 0x17FC        # CRadioStation::m_sName（1.0.8.0 与 CE 一致，已验证）
DESC_OFF = 0x17D0        # 指向描述符（名字在 +0x1d/长度 +0x1e）
OBJ_SIZE = 0x1934        # CRadioStation 大小
SLOT_SIZE = 0xBD0        # 一个播放槽的大小（两个槽）
CUR_SLOT = 0x1917        # 当前槽索引（byte）
REGISTRY = 0x011D76AC    # 全局：台对象指针数组（FUN_00945dd0 里 push 进去的）
STATIONS = ["RADIO", "THE_VIBE", "WKTT", "PLR", "INTEGRITY", "JOURNEY", "K109",
            "FUSION", "LIBERTY_ROCK", "SAN_JUAN", "VLADIVOSTOK", "AFRO_BEAT",
            "HARDCORE", "BABYLON", "MEDITATION", "CLASSICAL", "BROKER", "DANCE",
            "VCFM", "BEAT_95", "NY_CLASSICS", "RAMJAM", "MASSIVE", "SELF",
            "BOBBY_KONDERS", "INDEPENDENT", "JAZZ_NATION"]

# 槽内字段
S_CLIP = 0x960
S_STREAM = 0x964
S_MS = 0x990
S_LEN = 0x994
S_MODE = 0xBC0
S_IDX = 0xBC4
S_STATE = 0xBC8
S_BUSY = 0xBCA


def find_pid():
    out = subprocess.run(["pgrep", "-f", r"GTAIV\.exe"], capture_output=True, text=True).stdout
    pids = [int(x) for x in out.split()]
    # 只要真正映射了 GTAIV.exe 的那个（PlayGTAIV/包装脚本也会命中 pgrep）
    for pid in pids:
        try:
            for line in open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace"):
                if line.rstrip().endswith("/GTAIV.exe"):
                    return pid
        except OSError:
            continue
    return None


class Mem:
    def __init__(self, pid):
        self.pid = pid
        self.f = open(f"/proc/{pid}/mem", "rb", buffering=0)
        self.regions = []      # [(start, end, perms)]
        for line in open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace"):
            m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S+)", line)
            if m:
                self.regions.append((int(m.group(1), 16), int(m.group(2), 16), m.group(3)))

    def read(self, addr, size):
        try:
            self.f.seek(addr)
            return self.f.read(size)
        except (OSError, ValueError):
            return None

    def u32(self, addr):
        b = self.read(addr, 4)
        return struct.unpack("<I", b)[0] if b and len(b) == 4 else None

    def u8(self, addr):
        b = self.read(addr, 1)
        return b[0] if b else None

    def cstr(self, addr, maxlen=48):
        b = self.read(addr, maxlen)
        if not b:
            return ""
        z = b.split(b"\0")[0]
        try:
            s = z.decode("ascii")
        except UnicodeDecodeError:
            return ""
        return s if s.isprintable() else ""

    def mapped(self, addr):
        return any(s <= addr < e for s, e, _ in self.regions)

    def rw_regions(self, min_size=0x10000, skip_huge=512 * 1024 * 1024):
        for s, e, p in self.regions:
            if not p.startswith("rw") or e - s < min_size or e - s > skip_huge:
                continue
            yield s, e


# ---------------------------------------------------------------- 台对象发现
def _valid_station(mem, base):
    """真台对象：+0x17fc 是台名，+0x17d0 的描述符名字跟它一致。"""
    nm = mem.cstr(base + NAME_OFF, 48)
    if not nm or "\\" in nm:
        return None
    d = mem.u32(base + DESC_OFF)
    if not d or not mem.mapped(d):
        return None
    ln = mem.u8(d + 0x1D)
    if not ln or ln > 64:
        return None
    return nm if mem.cstr(d + 0x1E, ln) == nm else None


def registry(mem):
    """按全局数组拿台对象：[(name, base)]，顺序就是游戏的台顺序。"""
    arr = mem.u32(REGISTRY)
    out = []
    if not arr or not mem.mapped(arr):
        return out
    for i in range(64):
        p = mem.u32(arr + i * 4)
        if not p or p == 0xCDCDCDCD or not mem.mapped(p):
            break
        nm = _valid_station(mem, p)
        if nm:
            out.append((nm, p))
    return out


def scan_stations(mem, limit=600):
    """老路：扫内存找"名字在 +0x17fc"的对象。返回 [(base, name, is_track)]。

    注意：**结果里有大量假阳性**（名字池里的字符串被当成对象），只有
    `_valid_station()` 校验通过的才是真台对象 —— 新代码请用 `registry()`。
    """
    found = {}
    pats = [n.encode() for n in STATIONS]
    for s, e in mem.rw_regions():
        size = e - s
        chunk = 4 << 20
        off = 0
        while off < size:
            data = mem.read(s + off, min(chunk + 0x2000, size - off))
            if not data:
                break
            for pat in pats:
                i = data.find(pat)
                while i >= 0:
                    base = s + off + i - NAME_OFF
                    if base not in found:
                        nm = mem.cstr(base + NAME_OFF, 48)
                        if nm and (nm == pat.decode() or nm.startswith(pat.decode() + "\\")):
                            found[base] = nm
                            if len(found) >= limit:
                                return [(b, n, "\\" in n) for b, n in sorted(found.items())]
                    i = data.find(pat, i + 1)
            off += chunk
    return [(b, n, "\\" in n) for b, n in sorted(found.items())]


# ---------------------------------------------------------------- 槽位/身份
def slot_state(mem, base, slot):
    """读一个播放槽；返回 dict。"""
    s = base + slot * SLOT_SIZE
    clip = mem.u32(s + S_CLIP)
    h = mem.u32(clip + 4) if clip else 0
    return {
        "slot": slot,
        "clip": clip,
        "hash": h,
        "ms": mem.u32(s + S_MS),
        "len": mem.u32(s + S_LEN),
        "idx": mem.u32(s + S_IDX),
        "mode": mem.u8(s + S_MODE),
        "state": mem.u8(s + S_STATE),
        "busy": mem.u8(s + S_BUSY),
    }


def station_state(mem, base, table=None):
    cur = mem.u8(base + CUR_SLOT)
    out = {"cur": cur, "slots": [slot_state(mem, base, 0), slot_state(mem, base, 1)]}
    for sl in out["slots"]:
        e = HT.lookup(sl["hash"], table) if (table and sl["hash"]) else None
        sl["track"] = e["file"] if e else None
        sl["rpf"] = e["rpf"] if e else None
    return out


def load_table(path=None):
    if not HT:
        return {}
    return HT.load(path or HT.DEFAULT_TABLE)


def fmt_slot(sl):
    name = sl["track"] or ("?" if sl["hash"] else "-")
    return (f"slot{sl['slot']} hash={sl['hash']:08x} {'':1s}{name:30s} "
            f"ms={str(sl['ms']):>8} len={str(sl['len']):>8} idx={sl['idx']:>3} "
            f"mode={sl['mode']} st={sl['state']} busy={sl['busy']}")


# ---------------------------------------------------------------- 子命令
def cmd_registry(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe（游戏没在跑？）", file=sys.stderr)
        return 1
    mem = Mem(pid)
    arr = mem.u32(REGISTRY)
    print(f"pid={pid} registry @ {REGISTRY:#010x} -> {arr:#010x}" if arr else f"pid={pid} 没拿到 registry")
    for i, (nm, base) in enumerate(registry(mem)):
        print(f"  [{i:2d}] {base:#010x}  {nm}")
    return 0


def cmd_state(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe", file=sys.stderr)
        return 1
    mem = Mem(pid)
    table = load_table(a.table)
    st = registry(mem)
    print(f"pid={pid}  {len(st)} 个台对象  表 {len(table)} 条")
    for nm, base in st:
        s = station_state(mem, base, table)
        print(f"{nm:18s} cur={s['cur']}")
        for sl in s["slots"]:
            if sl["clip"]:
                print("   " + fmt_slot(sl))
    return 0


def cmd_log(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe", file=sys.stderr)
        return 1
    mem = Mem(pid)
    table = load_table(a.table)
    st = registry(mem)
    print("t\tstation\tcur\tslot\thash\ttrack\trpf\tms\tlen\tidx\tmode\tstate", flush=True)
    end = time.time() + a.secs
    while time.time() < end:
        ts = time.strftime("%H:%M:%S")
        for nm, base in st:
            s = station_state(mem, base, table)
            cur = s["cur"]
            sl = s["slots"][cur if cur in (0, 1) else 0]
            if not sl["clip"]:
                continue
            print(f"{ts}\t{nm}\t{cur}\t{sl['slot']}\t{sl['hash']:08x}\t{sl['track'] or '?'}\t"
                  f"{sl['rpf'] or '?'}\t{sl['ms']}\t{sl['len']}\t{sl['idx']}\t{sl['mode']}\t{sl['state']}",
                  flush=True)
        time.sleep(a.interval)
    return 0


def cmd_list(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe（游戏没在跑？）", file=sys.stderr)
        return 1
    mem = Mem(pid)
    objs = registry(mem)
    print(f"pid={pid} registry 找到 {len(objs)} 个台对象")
    for nm, base in objs:
        print(f"  {base:#010x}  {nm}")
    print("（老 scanner 结果只作参考，含假阳性）")
    t0 = time.time()
    raw = scan_stations(mem)
    print(f"scanner: {len(raw)} 个名字命中，{time.time()-t0:.1f}s")
    return 0


def cmd_hunt(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe", file=sys.stderr)
        return 1
    mem = Mem(pid)
    stations = registry(mem)
    print(f"{len(stations)} 个台对象，找「当前段记录」指针…")
    for nm, base in stations:
        for slot in (0, 1):
            sl = slot_state(mem, base, slot)
            if sl["clip"]:
                print(f"  {nm} slot{slot} +0x960 -> {sl['clip']:#010x} hash={sl['hash']:08x}")
    return 0


def cmd_peek(a):
    pid = find_pid()
    if not pid:
        print("没找到 GTAIV.exe", file=sys.stderr)
        return 1
    mem = Mem(pid)
    target = [b for n, b in registry(mem) if n == a.station]
    if not target:
        print(f"没找到台 {a.station}（先跑 registry）", file=sys.stderr)
        return 1
    base = target[0]
    for off in range(a.off, a.off + 32, 4):
        v = mem.u32(base + off)
        f = struct.unpack("<f", struct.pack("<I", v))[0] if v is not None else 0
        print(f"  +0x{off:03X}  {v:#010x}  int={v:>12}  float={f:12.3f}")
    return 0


def cmd_watch(a):
    pid = find_pid()
    mem = Mem(pid)
    objs = registry(mem)
    if not objs:
        print("没找到台对象", file=sys.stderr)
        return 1
    print(f"监控 {len(objs)} 个台：{' '.join(n for n, _ in objs)}")
    snap = {b: mem.read(b, OBJ_SIZE) for _, b in objs}
    t_end = time.time() + a.secs
    while time.time() < t_end:
        time.sleep(0.5)
        for name, base in objs:
            cur = mem.read(base, OBJ_SIZE)
            old = snap[base]
            if not cur or not old:
                continue
            changed = []
            for off in range(0, OBJ_SIZE - 3, 4):
                a32 = struct.unpack_from("<I", old, off)[0]
                b32 = struct.unpack_from("<I", cur, off)[0]
                if a32 != b32:
                    changed.append((off, a32, b32))
            if changed:
                ts = time.strftime("%H:%M:%S")
                for off, a32, b32 in changed[:10]:
                    d = b32 - a32
                    tag = "  <pos?>" if 100 <= d <= 3000 else ""
                    print(f"[{ts}] {name} +0x{off:03X}  {a32:#010x} -> {b32:#010x} (delta={d}){tag}")
                snap[base] = cur
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("registry", cmd_registry), ("state", cmd_state), ("list", cmd_list),
                     ("hunt", cmd_hunt), ("peek", cmd_peek)]:
        p = sub.add_parser(name)
        if name in ("state",):
            p.add_argument("--table", default=None)
        if name == "peek":
            p.add_argument("station")
            p.add_argument("off", type=lambda x: int(x, 0))
        p.set_defaults(func=fn)
    p = sub.add_parser("log")
    p.add_argument("--secs", type=float, default=60)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--table", default=None)
    p.set_defaults(func=cmd_log)
    p = sub.add_parser("watch")
    p.add_argument("--secs", type=float, default=60)
    p.set_defaults(func=cmd_watch)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
