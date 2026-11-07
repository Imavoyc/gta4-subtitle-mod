// GTA IV (1.2.0.59 / Complete Edition) — subtitle overlay proof of concept (M0).
//
// Goal of M0: prove that our own ASI can push an arbitrary **UTF-8 Chinese**
// string into the engine's text pipeline so it is drawn on screen, at a
// position we choose, with a translucent background.
//
// How:
//   1. the game resolves script natives through a hash -> handler function; we
//      find that function by pattern (pattern taken from FusionFix's
//      comvars.ixx, which is CE-verified) and call natives ourselves;
//   2. natives SET_TEXT_* + DISPLAY_TEXT_WITH_LITERAL_STRING are then driven
//      from a worker thread every ~20 ms (same approach as IV-SDK .NET /
//      ScriptHook, which tick their scripts off the main thread too);
//   3. the string is read from GTAIV.SubtitleTest.ini and re-read every 2 s, so
//      text/position/scale can be iterated without recompiling.
//
// The Chinese glyphs come from the game's own font — the 汉化 (GTA4.CHS.asi)
// hooks the engine's text encoder and maps UTF-8 to its fonts.wtd atlas, so any
// UTF-8 we hand to the engine should come out as CJK.
#include <windows.h>

#include <cstdarg>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <algorithm>
#include <vector>
#include <utility>
#include <cmath>

// ---------------------------------------------------------------- logging
static char g_logPath[MAX_PATH];

static void Log(const char* fmt, ...)
{
    FILE* f = fopen(g_logPath, "a");
    if (!f)
        return;
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static void RawLog(const char* text)
{
    FILE* f = fopen(g_logPath, "a");
    if (!f)
        return;
    SYSTEMTIME st;
    GetLocalTime(&st);
    const char* p = text;
    while (*p)
    {
        const char* nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        if (len)
        {
            fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
            fwrite(p, 1, len, f);
            fputc('\n', f);
        }
        if (!nl)
            break;
        p = nl + 1;
        GetLocalTime(&st);
    }
    fclose(f);
}

static const char* g_phase = "startup";

// ring buffer of the last phases, so a crash log shows the run-up
static const char* g_phaseRing[16];
static volatile LONG g_phaseRingPos = 0;
static void Phase(const char* p)
{
    g_phase = p;
    LONG i = InterlockedIncrement(&g_phaseRingPos) & 15;
    g_phaseRing[i] = p;
}

// ---------------------------------------------------------------- crash log
static LONG WINAPI VectoredHandler(EXCEPTION_POINTERS* ep)
{
    DWORD code = ep->ExceptionRecord->ExceptionCode;
    // only report real faults, not C++/debug exceptions
    if (code != EXCEPTION_ACCESS_VIOLATION && code != EXCEPTION_STACK_OVERFLOW &&
        code != EXCEPTION_INT_DIVIDE_BY_ZERO && code != EXCEPTION_ILLEGAL_INSTRUCTION &&
        code != EXCEPTION_PRIV_INSTRUCTION && code != 0xC0000409)
        return EXCEPTION_CONTINUE_SEARCH;
    void* stack[24];
    USHORT n = CaptureStackBackTrace(0, 24, stack, nullptr);
    Log("!!! VEH code=%08X addr=%p access=%s%p phase='%s'", code,
        ep->ExceptionRecord->ExceptionAddress,
        (code == EXCEPTION_ACCESS_VIOLATION && ep->ExceptionRecord->NumberParameters >= 2 &&
         ep->ExceptionRecord->ExceptionInformation[0] == 0) ? "read " : "write",
        (void*)ep->ExceptionRecord->ExceptionInformation[1], g_phase);
    Log("    phases: %s | %s | %s | %s | %s",
        g_phaseRing[(g_phaseRingPos - 4) & 15], g_phaseRing[(g_phaseRingPos - 3) & 15],
        g_phaseRing[(g_phaseRingPos - 2) & 15], g_phaseRing[(g_phaseRingPos - 1) & 15],
        g_phaseRing[g_phaseRingPos & 15]);
    for (USHORT i = 0; i < n; ++i)
        Log("    #%02d %p", i, stack[i]);
    return EXCEPTION_CONTINUE_SEARCH;  // let the game do whatever it does
}

static LONG WINAPI CrashFilter(EXCEPTION_POINTERS* ep)
{
    void* stack[32];
    USHORT n = CaptureStackBackTrace(0, 32, stack, nullptr);
    Log("!!! CRASH code=%08X addr=%p phase='%s'", ep->ExceptionRecord->ExceptionCode,
        ep->ExceptionRecord->ExceptionAddress, g_phase);
    for (USHORT i = 0; i < n; ++i)
        Log("   #%02d %p", i, stack[i]);
    return EXCEPTION_EXECUTE_HANDLER;
}

// ---------------------------------------------------------------- pattern scan
namespace pat
{
static uint8_t* find(const uint8_t* base, size_t size, const char* pattern, int nth = 0)
{
    std::vector<uint8_t> bytes;
    std::vector<bool> wild;
    for (const char* s = pattern; *s;)
    {
        while (*s == ' ')
            ++s;
        if (!*s)
            break;
        if (s[0] == '?')
        {  // wildcard, written as "?" or "??"
            bytes.push_back(0);
            wild.push_back(true);
            while (*s && *s != ' ')
                ++s;
        }
        else
        {
            char b[3] = { s[0], s[1], 0 };
            bytes.push_back((uint8_t)strtol(b, nullptr, 16));
            wild.push_back(false);
            s += 2;
        }
    }
    if (bytes.empty() || size < bytes.size())
        return nullptr;

    const size_t n = bytes.size();
    int seen = 0;
    for (size_t i = 0; i + n <= size; ++i)
    {
        size_t j = 0;
        for (; j < n; ++j)
            if (!wild[j] && base[i + j] != bytes[j])
                break;
        if (j == n)
        {
            if (seen++ == nth)
                return (uint8_t*)(base + i);
        }
    }
    return nullptr;
}
}  // namespace pat

// ---------------------------------------------------------------- natives
typedef void*(__stdcall* tGetNativeAddress)(uint32_t);

static tGetNativeAddress g_getNativeAddress = nullptr;
static uint32_t* g_pTableSize = nullptr;   // &rage::scrEngine::ms_dwNativeTableSize
static uint32_t* g_pTableL1 = nullptr;     // value stored in the ms_pNatives global
static uint32_t* g_pTableL2 = nullptr;     // *(value stored in the global)  (if it is a pointer-to-pointer)

// mirrors rage::scrNativeCallContext (see FusionFix natives.ixx)
struct NativeContext
{
    void* m_pReturn;
    uint32_t m_nArgCount;
    void* m_pArgs;
    uint32_t m_nDataCount;
    void* m_pOriginalData[4];
    float m_TemporaryData[16];
};

struct NativeArgs
{
    intptr_t buf[32];
};

static void PushArg(NativeArgs& a, int& n, int32_t v)
{
    a.buf[n++] = v;
}
static void PushArg(NativeArgs& a, int& n, float v)
{
    intptr_t t = 0;
    memcpy(&t, &v, sizeof(float));
    a.buf[n++] = t;
}
static void PushArg(NativeArgs& a, int& n, const char* v)
{
    a.buf[n++] = (intptr_t)v;
}
static void PushArg(NativeArgs& a, int& n, void* v)      // out 参数（游戏往我们这块内存写）
{
    a.buf[n++] = (intptr_t)v;
}

static volatile uint32_t g_nativeCalls = 0;
static volatile uint32_t g_hookCalls = 0;

static void* LookupNativeHash(uint32_t hash)
{
    static uint32_t reported[32];
    static int nReported = 0;
    void* handler = nullptr;
    if (g_getNativeAddress)
        handler = g_getNativeAddress(hash);
    if (!handler && g_pTableSize && *g_pTableSize)
    {
        uint32_t size = *g_pTableSize;
        for (uint32_t* tbl : { g_pTableL1, g_pTableL2 })
        {
            if (!tbl || size > (1u << 20))
                continue;
            uint32_t idx = hash % size;
            uint32_t h = tbl[2 * idx];
            if (h != hash)
            {
                uint32_t tmp = hash;
                uint32_t guard = 0;
                while (h && guard++ < size)
                {
                    tmp = (tmp >> 1) + 1;
                    idx = (tmp + idx) % size;
                    h = tbl[2 * idx];
                    if (h == hash)
                        break;
                }
            }
            if (h == hash && tbl[2 * idx + 1])
            {
                handler = (void*)(uintptr_t)tbl[2 * idx + 1];
                break;
            }
        }
    }
    bool seen = false;
    for (int i = 0; i < nReported; ++i)
        if (reported[i] == hash)
            seen = true;
    if (!seen && nReported < 32)
    {
        reported[nReported++] = hash;
        Log("native %08X -> %p", hash, handler);
    }
    return handler;
}

template <class... A>
static void Native(uint32_t hash, A... args)
{
    ++g_nativeCalls;
    void* handler = LookupNativeHash(hash);
    if (!handler)
        return;
    NativeArgs a{};
    int n = 0;
    (PushArg(a, n, args), ...);
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    cxt.m_nArgCount = (uint32_t)n;
    cxt.m_nDataCount = 0;
    ((void (*)(NativeContext*))handler)(&cxt);
}

enum : uint32_t
{
    N_SET_TEXT_BACKGROUND = 0x768F5140,
    N_SET_TEXT_CENTRE = 0x204A6AA4,
    N_SET_TEXT_COLOUR = 0x19C967B5,
    N_SET_TEXT_DROPSHADOW = 0x58F5023F,
    N_SET_TEXT_EDGE = 0x2D7A725D,
    N_SET_TEXT_FONT = 0x75363BB5,
    N_SET_TEXT_PROPORTIONAL = 0x15585A65,
    N_SET_TEXT_SCALE = 0x02C069E5,
    N_SET_TEXT_WRAP = 0x19D006EB,
    N_SET_TEXT_RIGHT_JUSTIFY = 0x748B78B6,
    N_SET_TEXT_LINE_HEIGHT_MULT = 0x5BF53817,
    N_DISPLAY_TEXT_WITH_LITERAL_STRING = 0x661B239A,
    N_GET_WIDTH_OF_LITERAL_STRING = 0x164B7363,
    N_GET_LENGTH_OF_LITERAL_STRING = 0x02BE2D97,
    N_GET_WIDTH_OF_LITERAL_STRING_U = 0x164B7363,
    N_DRAW_RECT = 0x3B2526E3,
    N_GET_PLAYER_RADIO_MODE = 0x32795678,
    N_GET_PLAYER_RADIO_STATION_INDEX = 0x4E493AAF,
    N_GET_PLAYER_RADIO_STATION_NAME = 0x25136AC2,
    N_GET_RADIO_NAME = 0x7EC9580E,
    N_IS_RADIO_RETUNING = 0x45C344AA,
    N_GET_AUDIBLE_MUSIC_TRACK_TEXT_ID = 0x18246AC8,
    N_SKIP_RADIO_FORWARD = 0x12A86E89,
    N_RETUNE_RADIO_UP = 0x6B1C6027,
    N_PRINT_STRING_WITH_LITERAL_STRING_NOW = 0x0CA539D6,
    N_GET_PLAYER_ID = 0x62E319C6,               // 见 IV-SDK NativeHashes.h（1.0.8.0 与 CE 通用）
    N_CONVERT_INT_TO_PLAYERINDEX = 0x5996315E,
    N_GET_PLAYER_CHAR = 0x511454A9,             // (playerIndex, Ped* out)
    N_IS_CHAR_IN_ANY_CAR = 0x71184DA3,
};

// ---------------------------------------------------------------- settings
struct Settings
{
    std::string text = u8"字幕测试：GTA IV 中文字幕能不能显示？";
    float x = 0.5f;      // screen space 0..1
    float y = 0.08f;     // 0 = top
    float scale = 0.42f;
    int font = 0;
    int centre = 1;
    int background = 0;
    int enable = 1;
    int binkHook = 0;   // 1 = hook binkw32 (TV timeline probe)
    int frameHook = 1;  // 1 = our per-frame hook (needed for drawing)
    int driver = 0;     // 0 = frame hook, 1 = worker thread, 2 = both
    int debugFrame = 0; // 1 = subtitle shows bink frame/time instead of text
    int alwaysShow = 0; // 1 = show ini Text when no cue is active
    int fontTest = 0;   // 1 = draw one row per font id (0..3) for style comparison
    int radioTest = 0;  // 1 = SKIP_RADIO_FORWARD every 5 s (autonomous station sweep)
    int radioProbe = 0; // 1 = M4 research: scan for CRadioStation objects and diff their fields
    int radioSync = 1;  // 1 = 电台逐句同步（读内存里的段 hash + 段内毫秒，选 per-track SRT）
    int radioTimeMode = 0;  // 0 = 线性缩放（引擎口径）1 = 逐通道锯齿（备选模型，用来实测对比）
    int renderer = 0;   // 0 = engine text, 1 = our D3D9 atlas
    int showMode = 0;   // 0 = always show ini text, 1 = only while tv/radio plays
    float fakeTime = -1.0f;  // >=0: pretend the video is at this second (offline testing)
    int mode = 0;   // 0=DISPLAY_TEXT_WITH_LITERAL_STRING 1=PRINT_..._NOW 2=both
    float maxWidth = 0.86f;   // wrap width (screen units)
    int maxChars = 0;         // >0: also hard-limit characters per line
    float lineGap = 0.048f;   // distance between line baselines
    int barAlpha = 150;       // 0 = no bar; >0 alpha of our own bar
    float barPad = 0.014f;    // horizontal padding of the bar
    float barHeight = 0.034f; // bar height per line
    float radioY = -1.0f;      // 电台占位行的 Y（<0 = 与字幕相同）；电台 HUD 在顶部，往下挪才不挡
    float radioScale = 0.0f;   // 电台占位行的字号倍率（<=0 = 与字幕相同）
    std::string lang = "zh";   // 字幕语言：zh / en（决定读 <频道>.zh.srt 还是 .en.srt）
};

static Settings g_set;
static char g_iniPath[MAX_PATH];

static std::string Trim(const std::string& s)
{
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos)
        return {};
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

static CRITICAL_SECTION g_setLock;

// The frame thread and the tick thread both touch settings; g_set is only ever
// swapped under the lock, and the draw path works on its own copy (g_view).
static void LoadSettings()
{
    FILE* f = fopen(g_iniPath, "rb");
    if (!f)
        return;
    Settings tmp{};
    char line[1024];
    while (fgets(line, sizeof line, f))
    {
        std::string l = Trim(line);
        if (l.empty() || l[0] == ';' || l[0] == '#' || l[0] == '[')
            continue;
        size_t eq = l.find('=');
        if (eq == std::string::npos)
            continue;
        std::string k = Trim(l.substr(0, eq));
        std::string v = Trim(l.substr(eq + 1));
        if (k == "Text")
            tmp.text = v;
        else if (k == "X")
            tmp.x = (float)atof(v.c_str());
        else if (k == "Y")
            tmp.y = (float)atof(v.c_str());
        else if (k == "Scale")
            tmp.scale = (float)atof(v.c_str());
        else if (k == "Font")
            tmp.font = atoi(v.c_str());
        else if (k == "Centre")
            tmp.centre = atoi(v.c_str());
        else if (k == "Background")
            tmp.background = atoi(v.c_str());
        else if (k == "Enable")
            tmp.enable = atoi(v.c_str());
        else if (k == "FrameHook")
            tmp.frameHook = atoi(v.c_str());
        else if (k == "Driver")
            tmp.driver = atoi(v.c_str());
        else if (k == "DebugFrame")
            tmp.debugFrame = atoi(v.c_str());
        else if (k == "Renderer")
            tmp.renderer = atoi(v.c_str());
        else if (k == "ShowMode")
            tmp.showMode = atoi(v.c_str());
        else if (k == "RadioTest")
            g_set.radioTest = atoi(v.c_str());
        else if (k == "RadioProbe")
            tmp.radioProbe = atoi(v.c_str());
        else if (k == "RadioSync")
            tmp.radioSync = atoi(v.c_str());
        else if (k == "RadioTimeMode")
            tmp.radioTimeMode = atoi(v.c_str());
        else if (k == "RadioY")
            tmp.radioY = (float)atof(v.c_str());
        else if (k == "RadioScale")
            tmp.radioScale = (float)atof(v.c_str());
        else if (k == "FontTest")
            tmp.fontTest = atoi(v.c_str());
        else if (k == "FakeTime")
            tmp.fakeTime = (float)atof(v.c_str());
        else if (k == "AlwaysShow")
            tmp.alwaysShow = atoi(v.c_str());
        else if (k == "BinkHook")
            tmp.binkHook = atoi(v.c_str());
        else if (k == "Mode")
            tmp.mode = atoi(v.c_str());
        else if (k == "MaxChars")
            tmp.maxChars = atoi(v.c_str());
        else if (k == "MaxWidth")
            tmp.maxWidth = (float)atof(v.c_str());
        else if (k == "LineGap")
            tmp.lineGap = (float)atof(v.c_str());
        else if (k == "BarAlpha")
            tmp.barAlpha = atoi(v.c_str());
        else if (k == "BarPad")
            tmp.barPad = (float)atof(v.c_str());
        else if (k == "BarHeight")
            tmp.barHeight = (float)atof(v.c_str());
        else if (k == "Lang" || k == "SubLang")
        {
            std::string l = v;
            for (auto& c : l)
                c = (char)tolower((unsigned char)c);
            tmp.lang = (l == "en" || l == "eng" || l == "english") ? "en" : "zh";
        }
    }
    fclose(f);
    EnterCriticalSection(&g_setLock);
    g_set = tmp;
    LeaveCriticalSection(&g_setLock);
}

// ---------------------------------------------------------------- init
static HMODULE g_exe = nullptr;

static bool ResolveNatives()
{
    auto* base = (uint8_t*)g_exe;
    auto* dos = (IMAGE_DOS_HEADER*)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE)
        return false;
    auto* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);

    // "56 8B 35 ? ? ? ? 85 F6 75 06" — the native hash -> handler resolver
    // (pattern from FusionFix comvars.ixx, CE-verified; CE has two, take the 2nd).
    static const char* kPat = "56 8B 35 ? ? ? ? 85 F6 75 06";
    std::vector<uint8_t*> hits;
    for (int i = 0; i < nt->FileHeader.NumberOfSections; ++i)
    {
        uint8_t* p = base + sec[i].VirtualAddress;
        size_t sz = sec[i].Misc.VirtualSize;
        for (int k = 0;; ++k)
        {
            uint8_t* h = pat::find(p, sz, kPat, k);
            if (!h)
                break;
            hits.push_back(h);
        }
    }
    if (hits.empty())
        return false;
    uint8_t* hit = hits.size() > 1 ? hits[1] : hits[0];
    g_getNativeAddress = (tGetNativeAddress)hit;
    Log("resolver: base=%p hits=%zu chosen=%p (base+0x%zx)", (void*)base, hits.size(), (void*)hit,
        (size_t)(hit - base));

    // Native hash table (fallback path). Patterns from FusionFix comvars.ixx.
    // "8B 35 ? ? ? ? 85 F6 75 06 33 C0 5E C2 04 00 53 57 8B 7C 24 10" -> table size
    // "8B 1D ? ? ? ? 8B CF 8B 04 D3 3B C7 74 19 8D 64 24 00 85 C0" -> table pointer
    auto collect = [&](const char* pat) {
        std::vector<uint8_t*> v;
        for (int i = 0; i < nt->FileHeader.NumberOfSections; ++i)
        {
            uint8_t* p = base + sec[i].VirtualAddress;
            size_t sz = sec[i].Misc.VirtualSize;
            for (int k = 0;; ++k)
            {
                uint8_t* h = pat::find(p, sz, pat, k);
                if (!h)
                    break;
                v.push_back(h);
            }
        }
        return v;
    };
    auto tblSizeHits = collect("8B 35 ? ? ? ? 85 F6 75 06 33 C0 5E C2 04 00 53 57 8B 7C 24 10");
    auto tblHits = collect("8B 1D ? ? ? ? 8B CF 8B 04 D3 3B C7 74 19 8D 64 24 00 85 C0");
    if (!tblSizeHits.empty())
        g_pTableSize = *(uint32_t**)(tblSizeHits[0] + 2);
    if (!tblHits.empty())
    {
        uintptr_t glob = *(uint32_t*)(tblHits[0] + 2);
        g_pTableL1 = *(uint32_t**)glob;
        if (g_pTableL1)
        {
            uintptr_t v = (uintptr_t)g_pTableL1;
            if (v > 0x1000 && v < 0x7fffffff)
                g_pTableL2 = *(uint32_t**)v;
        }
    }
    uint32_t sz = (g_pTableSize ? *g_pTableSize : 0);
    Log("table: sizeHits=%zu pHits=%zu pTableSize=%p size=%u L1=%p L2=%p", tblSizeHits.size(),
        tblHits.size(), (void*)g_pTableSize, sz, (void*)g_pTableL1, (void*)g_pTableL2);
    for (uint32_t* t : { g_pTableL1, g_pTableL2 })
        if (t && sz && sz < (1u << 20) && sz > 4)
            Log("  tbl %p: [0]=%08X [1]=%08X [2]=%08X [3]=%08X", (void*)t, t[0], t[1], t[2], t[3]);
    return true;
}


#include "selfrender.hxx"

// ---------------------------------------------------------------- bink probe
// The safehouse TV is a plain Bink video (movies/CNT.bik, weazel.bik, ...), so
// hooking binkw32's BinkOpen/BinkDoFrame gives us "which channel" + "which
// frame" for free. Hooks are installed by overwriting the game's IAT slots, so
// no trampoline is needed: our wrapper calls the real export directly.
struct BinkState
{
    char name[64] = {0};
    void* handle = nullptr;
    volatile uint32_t width = 0, height = 0, frames = 0, frameNum = 0, lastFrameNum = 0;
    volatile uint32_t frameRate = 0, frameRateDiv = 0;
    volatile uint32_t openCount = 0;
    volatile uint32_t doFrameCount = 0;
};

static BinkState g_bink;

// 电台状态：GET_PLAYER_RADIO_MODE 在 CE 上恒为 0（实测：车里开着 Vibe 也是 0），
// 真正能用的是**电台名**（不在车里/没电台时为空串）。探针每 500 ms 更新一次。
static volatile LONG g_radioActive = 0;
static char g_radioLabel[96] = {0};   // 形如 "THE_VIBE · 146"，给字幕/调试用
static volatile LONG g_mediaState = -1;  // 0=无媒体 1=TV 2=电台 3=两者（变化时打日志）
static void*(__stdcall* g_origBinkOpen)(const char*, uint32_t) = nullptr;
static uint32_t(__stdcall* g_origBinkDoFrame)(void*) = nullptr;

static volatile int g_binkOpenPending = 0;   // set by the hook, consumed (logged) by the frame hook
static volatile int g_binkFramePending = 0;

static uint32_t __stdcall HookBinkDoFrame(void* bink)
{
    Phase("bink_doframe");
    // Record only; no CRT calls and no logging from this thread (that is what
    // crashed the game: our Log()/strncpy ran on bink's thread).
    if (bink && (uintptr_t)bink > 0x10000)
    {
        auto* p = (uint32_t*)bink;
        g_bink.handle = bink;
        g_bink.width = p[0];
        g_bink.height = p[1];
        g_bink.frames = p[2];
        g_bink.frameNum = p[3];
        g_bink.frameRate = p[5];
        g_bink.frameRateDiv = p[6];
        g_bink.doFrameCount++;
        g_binkFramePending = 1;
    }
    Phase("in_bink_doframe");
    return g_origBinkDoFrame ? g_origBinkDoFrame(bink) : 0;
}

static void* __stdcall HookBinkOpen(const char* name, uint32_t flags)
{
    Phase("bink_open");
    void* h = g_origBinkOpen ? g_origBinkOpen(name, flags) : nullptr;
    // plain bounded byte copy, no strncpy/CRT
    int i = 0;
    if (name && (uintptr_t)name > 0x10000)
        for (; i < 63; ++i)
        {
            char c = name[i];
            g_bink.name[i] = c;
            if (!c)
                break;
        }
    g_bink.name[i] = 0;
    g_bink.handle = h;
    g_bink.openCount++;
    g_binkOpenPending = 1;
    Phase("bink_open_done");
    return h;
}

// imports may be decorated: "_BinkOpen@8"
static bool NameMatches(const char* imported, const char* want)
{
    if (imported[0] == '_')
        ++imported;
    size_t n = strlen(want);
    if (_strnicmp(imported, want, n) != 0)
        return false;
    return imported[n] == '\0' || imported[n] == '@';
}

struct IatSlot
{
    void** slot = nullptr;
    void* orig = nullptr;
    void* repl = nullptr;
};
static IatSlot g_iatSlots[8];
static int g_iatSlotCount = 0;

static void PatchIat(const char* dllName, const char* funcName, void* replacement, void** origOut)
{
    auto* base = (uint8_t*)g_exe;
    auto* dos = (IMAGE_DOS_HEADER*)base;
    auto* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress)
        return;
    auto* imp = (IMAGE_IMPORT_DESCRIPTOR*)(base + dir.VirtualAddress);
    for (; imp->Name; ++imp)
    {
        const char* dll = (const char*)(base + imp->Name);
        if (_stricmp(dll, dllName) != 0)
            continue;
        auto* thunk = (IMAGE_THUNK_DATA*)(base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk
                                                                        : imp->FirstThunk));
        auto** iat = (void**)(base + imp->FirstThunk);
        for (int i = 0; thunk[i].u1.AddressOfData; ++i)
        {
            if (thunk[i].u1.Ordinal & IMAGE_ORDINAL_FLAG)
                continue;
            auto* ibn = (IMAGE_IMPORT_BY_NAME*)(base + thunk[i].u1.AddressOfData);
            const char* nm = (const char*)ibn->Name;
            static int listed = 0;
            if (listed < 40)
                Log("  import[%d] %s!%s", listed++, dllName, nm);
            if (!NameMatches(nm, funcName))
                continue;
            if (!*origOut)
                *origOut = iat[i];
            if (g_iatSlotCount < 8)
                g_iatSlots[g_iatSlotCount++] = { &iat[i], *origOut, replacement };
            DWORD old = 0;
            if (VirtualProtect(&iat[i], sizeof(void*), PAGE_READWRITE, &old))
            {
                iat[i] = replacement;
                VirtualProtect(&iat[i], sizeof(void*), old, &old);
                Log("iat: %s!%s slot=%p orig=%p -> %p", dllName, funcName, (void*)&iat[i],
                    *origOut, replacement);
            }
        }
    }
    Log("iat: %s!%s NOT FOUND", dllName, funcName);
}

static bool g_binkHooksOn = false;

static void SetBinkHooks(bool on)
{
    if (on == g_binkHooksOn)
        return;
    for (int i = 0; i < g_iatSlotCount; ++i)
    {
        DWORD old = 0;
        if (VirtualProtect(g_iatSlots[i].slot, sizeof(void*), PAGE_READWRITE, &old))
        {
            *g_iatSlots[i].slot = on ? g_iatSlots[i].repl : g_iatSlots[i].orig;
            VirtualProtect(g_iatSlots[i].slot, sizeof(void*), old, &old);
        }
    }
    g_binkHooksOn = on;
    Log("bink hooks %s", on ? "ON" : "OFF");
}

static void InstallBinkHooks()
{
    // binkw32 is a static import, so it is mapped before our thread runs
    for (int i = 0; i < 100 && !GetModuleHandleA("binkw32.dll"); ++i)
        Sleep(50);
    void* orig = nullptr;
    PatchIat("binkw32.dll", "BinkOpen", (void*)&HookBinkOpen, &orig);
    g_origBinkOpen = (void*(__stdcall*)(const char*, uint32_t))orig;
    orig = nullptr;
    PatchIat("binkw32.dll", "BinkDoFrame", (void*)&HookBinkDoFrame, &orig);
    g_origBinkDoFrame = (uint32_t(__stdcall*)(void*))orig;
    // PatchIat leaves the hooks live; record that and let the tick thread apply
    // the ini setting (so "off" really means off).
    g_binkHooksOn = true;
    SetBinkHooks(g_set.binkHook != 0);
}


// ---------------------------------------------------------------- charset
// The 汉化 font atlas only contains a fixed set of codepoints (char_table.dat,
// 3518 entries). Anything outside it renders as nothing / breaks the line, so we
// sanitise the text against it before handing it to the engine.
static std::vector<uint32_t> g_atlas;   // sorted codepoints

static void LoadAtlas()
{
    char path[MAX_PATH];
    strncpy(path, g_iniPath, MAX_PATH);
    char* slash = strrchr(path, '\\');
    if (slash)
        slash[1] = 0;
    strncat(path, "GTA4.CHS\\char_table.dat", MAX_PATH - strlen(path) - 1);
    FILE* f = fopen(path, "rb");
    if (!f)
    {
        Log("atlas: cannot open %s", path);
        return;
    }
    std::vector<uint32_t> v;
    uint32_t cp;
    while (fread(&cp, 4, 1, f) == 1)
        v.push_back(cp);
    fclose(f);
    std::sort(v.begin(), v.end());
    v.erase(std::unique(v.begin(), v.end()), v.end());
    g_atlas.swap(v);
    Log("atlas: %zu codepoints", g_atlas.size());
}

static bool AtlasHas(uint32_t cp)
{
    if (cp < 0x80)
        return true;  // ASCII comes from the game's own font, not the CHS atlas
    if (g_atlas.empty())
        return true;  // unknown atlas -> pass through
    return std::binary_search(g_atlas.begin(), g_atlas.end(), cp);
}

// decode one UTF-8 codepoint at i, return byte length
static uint32_t DecodeCp(const std::string& s, size_t i, size_t& len)
{
    unsigned char c = (unsigned char)s[i];
    if (c < 0x80)
    {
        len = 1;
        return c;
    }
    if ((c & 0xE0) == 0xC0 && i + 1 < s.size())
    {
        len = 2;
        return ((c & 0x1F) << 6) | ((unsigned char)s[i + 1] & 0x3F);
    }
    if ((c & 0xF0) == 0xE0 && i + 2 < s.size())
    {
        len = 3;
        return ((c & 0x0F) << 12) | (((unsigned char)s[i + 1] & 0x3F) << 6) |
               ((unsigned char)s[i + 2] & 0x3F);
    }
    if ((c & 0xF8) == 0xF0 && i + 3 < s.size())
    {
        len = 4;
        return ((c & 0x07) << 18) | (((unsigned char)s[i + 1] & 0x3F) << 12) |
               (((unsigned char)s[i + 2] & 0x3F) << 6) | ((unsigned char)s[i + 3] & 0x3F);
    }
    len = 1;
    return c;
}

static void AppendUtf8(std::string& out, uint32_t cp)
{
    if (cp < 0x80)
        out += (char)cp;
    else if (cp < 0x800)
    {
        out += (char)(0xC0 | (cp >> 6));
        out += (char)(0x80 | (cp & 0x3F));
    }
    else if (cp < 0x10000)
    {
        out += (char)(0xE0 | (cp >> 12));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    }
    else
    {
        out += (char)(0xF0 | (cp >> 18));
        out += (char)(0x80 | ((cp >> 12) & 0x3F));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    }
}

// replace unsupported codepoints with '?' (logged once per string)
static std::string Sanitize(const std::string& in, int* dropped = nullptr)
{
    if (g_atlas.empty())
        return in;
    std::string out;
    size_t i = 0;
    int bad = 0;
    while (i < in.size())
    {
        size_t len = 0;
        uint32_t cp = DecodeCp(in, i, len);
        if (AtlasHas(cp))
            out.append(in, i, len);
        else
        {
            out += '?';
            ++bad;
        }
        i += len;
    }
    if (dropped)
        *dropped = bad;
    return out;
}

// ---------------------------------------------------------------- SRT
struct Cue
{
    double start = 0.0;
    double end = 0.0;
    std::string text;
};

static std::vector<Cue> g_cues;
static CRITICAL_SECTION g_cuesLock;
static char g_srtPath[MAX_PATH];
static FILETIME g_srtMtime{};

static double ParseSrtTime(const char* s)
{
    int h = 0, m = 0, sec = 0, ms = 0;
    if (sscanf(s, "%d:%d:%d,%d", &h, &m, &sec, &ms) != 4)
        if (sscanf(s, "%d:%d:%d.%d", &h, &m, &sec, &ms) != 4)
            return 0.0;
    return h * 3600.0 + m * 60.0 + sec + ms / 1000.0;
}

// very small SRT reader: index / "start --> end" / text... / blank
static void ParseSrtFile(const char* path, std::vector<Cue>& cues)
{
    FILE* f = fopen(path, "rb");
    if (!f)
        return;
    char line[2048];
    Cue cur;
    bool haveTime = false;
    while (fgets(line, sizeof line, f))
    {
        std::string l = line;
        while (!l.empty() && (l.back() == '\n' || l.back() == '\r'))
            l.pop_back();
        if (l.empty())
        {
            if (haveTime && !cur.text.empty())
                cues.push_back(cur);
            cur = Cue{};
            haveTime = false;
            continue;
        }
        if (l.find("-->") != std::string::npos)
        {
            size_t arrow = l.find("-->");
            cur.start = ParseSrtTime(l.c_str());
            cur.end = ParseSrtTime(l.c_str() + arrow + 3);
            haveTime = true;
            continue;
        }
        if (!haveTime)
            continue;  // cue index line
        if (!cur.text.empty())
            cur.text += "\n";
        cur.text += l;
    }
    if (haveTime && !cur.text.empty())
        cues.push_back(cur);
    fclose(f);
}

static void LoadSrtInto(const char* path)
{
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExA(path, GetFileExInfoStandard, &fad))
        return;
    bool pathChanged = _stricmp(path, g_srtPath) != 0;
    if (!pathChanged && CompareFileTime(&fad.ftLastWriteTime, &g_srtMtime) == 0)
        return;   // same file, unchanged
    g_srtMtime = fad.ftLastWriteTime;

    std::vector<Cue> cues;
    ParseSrtFile(path, cues);

    EnterCriticalSection(&g_cuesLock);
    g_cues.swap(cues);
    LeaveCriticalSection(&g_cuesLock);
    strncpy(g_srtPath, path, MAX_PATH);
    Log("srt: %s -> %zu cues", g_srtPath, g_cues.size());
}

// ---------------------------------------------------------------- 电台逐句同步
// 段身份 + 段内位置就在 CRadioStation 里（2026-11-07 破解，见 docs/gta4-radio.md）：
//   registry = *(u32*)0x011D76AC（台对象指针数组）
//   台对象 +0x1917 = 当前槽（0/1）；槽 = 台对象 + 槽号*0xBD0
//   槽 + 0x960 -> 8 字节记录 {id, hash}  （hash = 段的资源 hash）
//   槽 + 0x990 = 段内已播毫秒
// 于是插件只需要「hash → 段内 SRT（时间从 0 算起）」：`radio_split_tracks.py` 生成。
static const uintptr_t kRadioRegistryVA = 0x011D76AC;
static const uintptr_t kRadioNameOff = 0x17FC;
static const uintptr_t kRadioDescOff = 0x17D0;
static const uintptr_t kRadioCurSlotOff = 0x1917;
static const uintptr_t kRadioSlotSize = 0xBD0;
static const uintptr_t kRadioClipOff = 0x960;
static const uintptr_t kRadioMsOff = 0x990;
static const uintptr_t kRadioLenOff = 0x994;

static volatile LONG g_radioMs = 0;
static volatile LONG g_radioLen = 0;
static volatile LONG g_radioDurMs = 0;   // 本段音频文件的总时长（nch*21.248s，来自 radio/index.tsv）
static volatile LONG g_radioNch = 0;    // 本段文件的通道数

// 电台时间轴换算模式（ini RadioTimeMode）：
//   0 = 线性缩放（验证过：引擎把整段音频按 dur/len 的比例压到 len 里，见 docs）
//   1 = 逐通道锯齿（每通道按 W=len/nch 前进，通道内按自然速度）—— 备选模型，实测用
static std::vector<std::pair<uint32_t, uint32_t>> g_radioIdx;   // hash -> packed(nch<<20|dur_s?)
static CRITICAL_SECTION g_radioIdxLock;
static bool g_radioIdxLoaded = false;
static void* g_radioIdxHandle = nullptr;
static uint32_t g_radioHash = 0;
static char g_radioTrack[64] = {0};   // 调试日志用（hash 的 8 位十六进制）

static std::vector<Cue> g_radioCues;
static CRITICAL_SECTION g_radioCuesLock;
static char g_radioSrtPath[MAX_PATH] = {0};
static FILETIME g_radioSrtMtime{};
static char g_radioDir[MAX_PATH] = {0};   // <ini 所在目录>\radio

// 内存读取前先问一句“这段地址真的可读吗”，不靠 SEH（mingw 没有 __try）。
static bool MemReadable(const void* p, size_t n)
{
    if (!p)
        return false;
    MEMORY_BASIC_INFORMATION mbi;
    if (!VirtualQuery(p, &mbi, sizeof mbi))
        return false;
    if (mbi.State != MEM_COMMIT)
        return false;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))
        return false;
    uintptr_t end = (uintptr_t)p + n;
    return end <= (uintptr_t)mbi.BaseAddress + mbi.RegionSize;
}

// 在台对象注册表里按名字找台，再把“当前槽的段 hash + 段内毫秒”读出来。
// 返回 true 表示拿到了（*hash != 0）。名字没找到 / 当前没在播就是 false。
static bool ReadRadioSegment(const char* station, uint32_t* hash, uint32_t* ms, uint32_t* len)
{
    *hash = 0;
    *ms = 0;
    *len = 0;
    if (!station || !station[0])
        return false;
    if (!MemReadable((const void*)kRadioRegistryVA, 4))
        return false;
    uint32_t* reg = *(uint32_t**)kRadioRegistryVA;
    if ((uintptr_t)reg < 0x10000)
        return false;
    for (int i = 0; i < 64; ++i)
    {
        if (!MemReadable(reg + i, 4))
            return false;
        uintptr_t st = reg[i];
        if (!st || st == 0xCDCDCDCD)
            return false;
        if (!MemReadable((const void*)(st + kRadioNameOff), 64))
            continue;
        const char* nm = (const char*)(st + kRadioNameOff);
        if (strncmp(nm, station, 63) != 0)
            continue;
        // 只有名字相同还不够：用描述符名字再校一次（防止扫到内存里的字符串副本）
        if (!MemReadable((const void*)(st + kRadioDescOff), 8))
            return false;
        uintptr_t desc = *(uint32_t*)(st + kRadioDescOff);
        if (!MemReadable((const void*)(desc + 0x1D), 1 + 64))
            return false;
        unsigned dlen = *(uint8_t*)(desc + 0x1D);
        if (!dlen || dlen > 64 || strncmp((const char*)(desc + 0x1E), station, dlen) != 0)
            return false;

        if (!MemReadable((const void*)(st + kRadioCurSlotOff), 1))
            return false;
        unsigned cur = *(uint8_t*)(st + kRadioCurSlotOff);
        if (cur > 1)
            return false;
        uintptr_t slot = st + (uintptr_t)cur * kRadioSlotSize;
        if (!MemReadable((const void*)(slot + kRadioClipOff), 4 + 4 + 8))
            return false;
        uintptr_t clip = *(uint32_t*)(slot + kRadioClipOff);
        if (!clip || clip < 0x10000)
            return false;
        if (!MemReadable((const void*)(clip + 4), 4))
            return false;
        if (!MemReadable((const void*)(slot + kRadioMsOff), 12))
            return false;
        *hash = *(uint32_t*)(clip + 4);
        *ms = *(uint32_t*)(slot + kRadioMsOff);
        *len = *(uint32_t*)(slot + kRadioLenOff);
        return *hash != 0;
    }
    return false;
}

// 读 radio\index.tsv（radio_split_tracks.py / radio_index 生成）：hash nch dur_ms name
// 作用：引擎的 ms 不是"文件内时间"，而是把整段音频压进 len 的结果，
// 换算比例 = dur_ms / len_ms（2026-11-07 实测：PACEMAKER 65 通道、THE_SEANCE 69 通道都对上）。
static void LoadRadioIndex()
{
    if (g_radioIdxLoaded)
        return;
    g_radioIdxLoaded = true;
    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\index.tsv", g_radioDir);
    FILE* f = fopen(path, "rb");
    if (!f)
    {
        Log("radio index: 没有 %s（时间轴按 1:1 处理）", path);
        return;
    }
    char line[512];
    std::vector<std::pair<uint32_t, uint32_t>> v;
    while (fgets(line, sizeof line, f))
    {
        unsigned h = 0, nch = 0, dur = 0;
        if (sscanf(line, "%x\t%u\t%u", &h, &nch, &dur) != 3)
            continue;
        if (nch > 255 || dur > 0xFFFFFF)
            continue;                       // 打包：(dur << 8) | nch
        v.push_back({h, (dur << 8) | nch});
    }
    fclose(f);
    std::sort(v.begin(), v.end());
    EnterCriticalSection(&g_radioIdxLock);
    g_radioIdx.swap(v);
    LeaveCriticalSection(&g_radioIdxLock);
    Log("radio index: %zu 条（%s）", g_radioIdx.size(), path);
}

static bool RadioFileInfo(uint32_t hash, uint32_t* nch, uint32_t* durMs)
{
    bool ok = false;
    EnterCriticalSection(&g_radioIdxLock);
    auto it = std::lower_bound(g_radioIdx.begin(), g_radioIdx.end(),
                               std::make_pair(hash, (uint32_t)0));
    if (it != g_radioIdx.end() && it->first == hash)
    {
        *nch = it->second & 0xFF;
        *durMs = it->second >> 8;
        ok = true;
    }
    LeaveCriticalSection(&g_radioIdxLock);
    return ok;
}

// hash/语言变了就换文件；同一文件内容改了（mtime）也热重载。
static void LoadRadioSrt(uint32_t hash, const char* lang)
{
    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\%08x.%s.srt", g_radioDir, hash, lang);
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExA(path, GetFileExInfoStandard, &fad))
    {
        snprintf(path, sizeof path, "%s\\%08x.srt", g_radioDir, hash);   // 只有一种语言
        if (!GetFileAttributesExA(path, GetFileExInfoStandard, &fad))
        {
            if (g_radioSrtPath[0])
            {
                EnterCriticalSection(&g_radioCuesLock);
                g_radioCues.clear();
                LeaveCriticalSection(&g_radioCuesLock);
                Log("radio srt: no file for hash %08x (lang=%s)", hash, lang);
            }
            g_radioSrtPath[0] = 0;
            return;
        }
    }
    bool samePath = _stricmp(path, g_radioSrtPath) == 0;
    if (samePath && CompareFileTime(&fad.ftLastWriteTime, &g_radioSrtMtime) == 0)
        return;
    g_radioSrtMtime = fad.ftLastWriteTime;

    std::vector<Cue> cues;
    ParseSrtFile(path, cues);
    EnterCriticalSection(&g_radioCuesLock);
    g_radioCues.swap(cues);
    LeaveCriticalSection(&g_radioCuesLock);
    strncpy(g_radioSrtPath, path, MAX_PATH);
    Log("radio srt: hash=%08x lang=%s -> %s (%zu cues)", hash, lang, g_radioSrtPath,
        g_radioCues.size());
}

static std::string RadioCue(double t)
{
    std::string out;
    EnterCriticalSection(&g_radioCuesLock);
    for (const Cue& c : g_radioCues)
        if (t >= c.start && t <= c.end)
        {
            out = c.text;
            break;
        }
    LeaveCriticalSection(&g_radioCuesLock);
    return out;
}

static std::string ActiveCue(double t)
{
    std::string out;
    EnterCriticalSection(&g_cuesLock);
    for (const Cue& c : g_cues)
        if (t >= c.start && t <= c.end)
        {
            out = c.text;
            break;
        }
    LeaveCriticalSection(&g_cuesLock);
    return out;
}

static double BinkSeconds()
{
    if (g_set.fakeTime >= 0.0f)
        return (double)g_set.fakeTime;
    uint32_t frames = g_bink.frames, num = g_bink.frameNum;
    uint32_t rate = g_bink.frameRate ? g_bink.frameRate : 30;
    uint32_t div = g_bink.frameRateDiv ? g_bink.frameRateDiv : 1;
    if (!frames || !num)
        return -1.0;
    return (double)num * (double)div / (double)rate;
}

// ---------------------------------------------------------------- drawing
static int32_t NativeGetLength(const char* str)
{
    void* h = LookupNativeHash(N_GET_LENGTH_OF_LITERAL_STRING);
    if (!h)
        return -1;
    NativeArgs a{};
    a.buf[0] = (intptr_t)str;
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    cxt.m_nArgCount = 1;
    ((void (*)(NativeContext*))h)(&cxt);
    return (int32_t)a.buf[0];
}

static bool g_hookInstalled;

static float NativeGetWidth(const char* str)
{
    void* h = LookupNativeHash(N_GET_WIDTH_OF_LITERAL_STRING_U);
    if (!h)
        return 0.0f;
    NativeArgs a{};
    a.buf[0] = (intptr_t)str;
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    cxt.m_nArgCount = 1;
    ((void (*)(NativeContext*))h)(&cxt);
    float w = 0.0f;
    memcpy(&w, &a.buf[0], sizeof(float));
    return w;
}

// The measure native consumes the text state (scale/colour/...), so the style
// must be re-applied before *every* call that touches the text system.
static Settings g_view;   // main-thread snapshot of g_set

static void ApplyTextStyle()
{
    Native(N_SET_TEXT_FONT, (int32_t)g_view.font);
    Native(N_SET_TEXT_SCALE, g_view.scale, g_view.scale);
    Native(N_SET_TEXT_COLOUR, (int32_t)255, (int32_t)255, (int32_t)255, (int32_t)255);
    Native(N_SET_TEXT_CENTRE, (int32_t)(g_view.centre ? 1 : 0));
    Native(N_SET_TEXT_PROPORTIONAL, (int32_t)1);
    Native(N_SET_TEXT_WRAP, 0.0f, 1.0f);
    Native(N_SET_TEXT_LINE_HEIGHT_MULT, 1.0f);
    Native(N_SET_TEXT_DROPSHADOW, (int32_t)1, (int32_t)0, (int32_t)0, (int32_t)0, (int32_t)200);
    Native(N_SET_TEXT_EDGE, (int32_t)1, (int32_t)0, (int32_t)0, (int32_t)0, (int32_t)200);
}

static const char* NativeGetString(uint32_t hash)
{
    void* h = LookupNativeHash(hash);
    if (!h)
        return "";
    NativeArgs a{};
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    ((void (*)(NativeContext*))h)(&cxt);
    const char* r = (const char*)a.buf[0];
    return r ? r : "";
}

static int32_t NativeCall1(uint32_t hash, int32_t a1)
{
    void* h = LookupNativeHash(hash);
    if (!h)
        return -1;
    NativeArgs a{};
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    a.buf[0] = a1;
    cxt.m_nArgCount = 1;
    ((void (*)(NativeContext*))h)(&cxt);
    return (int32_t)a.buf[0];
}

static int32_t NativeCall2(uint32_t hash, int32_t a1, int32_t a2)
{
    void* h = LookupNativeHash(hash);
    if (!h)
        return -1;
    NativeArgs a{};
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    a.buf[0] = a1;
    a.buf[1] = a2;
    cxt.m_nArgCount = 2;
    ((void (*)(NativeContext*))h)(&cxt);
    return (int32_t)a.buf[0];
}

static int32_t NativeGetInt(uint32_t hash)
{
    void* h = LookupNativeHash(hash);
    if (!h)
        return -1;
    NativeArgs a{};
    NativeContext cxt{};
    cxt.m_pArgs = a.buf;
    cxt.m_pReturn = a.buf;
    ((void (*)(NativeContext*))h)(&cxt);
    return (int32_t)a.buf[0];
}

static float MeasuredWidth(const char* s)
{
    ApplyTextStyle();
    return NativeGetWidth(s);
}

static size_t NextCp(const std::string& s, size_t i)
{
    if (i >= s.size())
        return s.size();
    unsigned char c = (unsigned char)s[i];
    size_t n = 1;
    if (c >= 0xF0)
        n = 4;
    else if (c >= 0xE0)
        n = 3;
    else if (c >= 0xC0)
        n = 2;
    return (i + n > s.size()) ? s.size() : i + n;
}

struct WrappedLine
{
    std::string text;
    float width = 0.0f;
};

// Greedy wrap by measured width; cached because each measure costs ~9 natives.
static std::vector<WrappedLine> g_lines;
static std::string g_linesKey;

static void WrapLines(const std::string& text, std::string& outKey)
{
    // cache key: FNV-1a over the text + the layout knobs (no truncation)
    uint32_t h = 2166136261u;
    for (unsigned char c : text)
    {
        h ^= c;
        h *= 16777619u;
    }
    char key[96];
    snprintf(key, sizeof key, "%08x|%.3f|%.3f|%d|%d", h, g_view.scale, g_view.maxWidth, g_view.font,
             g_view.maxChars);
    outKey = key;
    if (outKey == g_linesKey)
        return;

    g_lines.clear();
    std::string line;
    size_t i = 0;
    while (i < text.size())
    {
        size_t j = NextCp(text, i);
        std::string candidate = line + text.substr(i, j - i);
        bool tooManyChars = g_view.maxChars > 0 && (int)candidate.size() / 3 >= g_view.maxChars &&
                            candidate.size() > line.size();
        if (!line.empty() && (tooManyChars || MeasuredWidth(candidate.c_str()) > g_view.maxWidth))
        {
            g_lines.push_back({ line, MeasuredWidth(line.c_str()) });
            line.clear();
            continue;  // retry this codepoint on the new line
        }
        line = candidate;
        i = j;
    }
    if (!line.empty())
        g_lines.push_back({ line, MeasuredWidth(line.c_str()) });
    g_linesKey = outKey;
}

static void DrawSubtitle(bool fromHook)
{
    {   // snapshot settings once per frame (writer: tick thread)
        EnterCriticalSection(&g_setLock);
        g_view = g_set;
        LeaveCriticalSection(&g_setLock);
    }
    if (!g_view.enable)
        return;

    static DWORD lastBeat = 0;
    static uint32_t lastNativeCount = 0;
    static uint32_t lastHookCount = 0;
    DWORD now = GetTickCount();
    bool beat = (now / 5000) != (lastBeat / 5000);
    if (beat)
        lastBeat = now;

    Phase("draw");

    // is any media actually playing right now?
    static DWORD lastDoFrame = 0, lastDoTick = 0;
    bool tvPlaying = false;
    if (g_bink.doFrameCount != lastDoFrame)
    {
        lastDoFrame = g_bink.doFrameCount;
        lastDoTick = GetTickCount();
        tvPlaying = true;
    }
    else if (lastDoTick && GetTickCount() - lastDoTick < 600)
        tvPlaying = true;
    bool radioOn = (g_radioActive != 0);

    // decide what to show: debug line > active SRT cue (只有电视真在播时才算数)
    //                    > 电台标签 > ini Text (if AlwaysShow)
    static std::string shown;
    if (g_view.debugFrame)
    {
        double t = BinkSeconds();
        char buf[128];
        snprintf(buf, sizeof buf, "frame %u/%u  t=%.1fs", g_bink.frameNum, g_bink.frames, t < 0 ? 0.0 : t);
        shown = buf;
    }
    else
    {
        double t = BinkSeconds();
        shown.clear();
        // 电视停了以后 bink 时间会冻在最后一帧，cue 还会命中 —— 那会留下一句
        // "僵尸台词"（实测：上车开电台时屏幕上还挂着上一条电视字幕）。
        // 所以只在 tvPlaying 时才用 SRT 台词。
        if (tvPlaying && t >= 0)
            shown = ActiveCue(t);
        // 电台（M4）：段内 SRT 优先（时间是引擎给的段内毫秒），没有 cue 才回退到
        // “台名 · trackTextId”占位行。
        if (shown.empty() && radioOn && g_view.radioSync && g_radioHash)
        {
            // 引擎的 ms 是"段内时钟"，要换算成"文件内时间"才能查 SRT：
            //   mode 0：t_file = ms * dur/len（整段被线性压缩，实测口径）
            //   mode 1：t_file = 通道序号*21.248 + 通道内自然位置（备选模型）
            double ms = (double)g_radioMs;
            LONG dur = g_radioDurMs, len = g_radioLen, nch = g_radioNch;
            double tfile = ms;
            if (dur > 0 && len > 0)
            {
                if (g_view.radioTimeMode == 1 && nch > 0)
                {
                    double W = (double)len / (double)nch;
                    double k = floor(ms / W);
                    tfile = k * 21248.0 + (ms - k * W);
                }
                else
                {
                    tfile = ms * (double)dur / (double)len;
                }
            }
            shown = RadioCue(tfile / 1000.0);
        }
        if (shown.empty() && radioOn && g_radioLabel[0])
            shown = g_radioLabel;      // 电台占位行（未同步到段时）
        if (shown.empty() && g_view.alwaysShow)
            shown = g_view.text;
    }

    if (g_view.fontTest)
    {
        // one row per font id, same text/scale — pick the row that matches the
        // game's own subtitle style
        const char* t = g_view.text.c_str();
        for (int f = 0; f <= 3; ++f)
        {
            Native(N_SET_TEXT_FONT, (int32_t)f);
            Native(N_SET_TEXT_SCALE, g_view.scale, g_view.scale);
            Native(N_SET_TEXT_CENTRE, (int32_t)1);
            Native(N_SET_TEXT_PROPORTIONAL, (int32_t)1);
            Native(N_SET_TEXT_DROPSHADOW, (int32_t)1, (int32_t)0, (int32_t)0, (int32_t)0, (int32_t)200);
            Native(N_DISPLAY_TEXT_WITH_LITERAL_STRING, g_view.x, g_view.y + 0.07f * f,
                   (const char*)"STRING", t);
        }
        return;
    }

    bool mediaActive = tvPlaying || radioOn;
    {
        LONG st = (tvPlaying ? 1 : 0) | (radioOn ? 2 : 0);
        if (st != g_mediaState)
        {
            g_mediaState = st;
            Log("media: tv=%d radio=%d active=%d label='%s' seg=%s ms=%d", (int)tvPlaying,
                (int)radioOn, (int)mediaActive, g_radioLabel, g_radioTrack, (int)g_radioMs);
        }
    }

    // 电台行（同步字幕或 "台名 · 曲目id" 占位）：游戏自己的电台 HUD 在顶部，所以给它
    // 单独的 Y / 字号（ini 的 RadioY / RadioScale，2 秒热生效），默认跟字幕完全一样。
    const bool isRadioLabel = radioOn && !tvPlaying && !shown.empty();
    const float drawY = (isRadioLabel && g_view.radioY >= 0.0f) ? g_view.radioY : g_view.y;
    const float drawScale = (isRadioLabel && g_view.radioScale > 0.0f) ? g_view.radioScale : 1.0f;

    if (g_view.renderer == 1)
    {
        // self-rendered path: no charset sanitize needed, the atlas is ours
        bool vis = (g_view.showMode == 0) || mediaActive;
        if (!mediaActive && g_view.showMode == 1)
            shown.clear();
        selfrender::SetContent(shown.c_str(), vis && !shown.empty(), g_view.x, drawY,
                               g_view.maxWidth, g_view.lineGap, g_view.barAlpha, g_view.barPad,
                               g_view.barHeight, g_view.centre != 0, drawScale);
        return;
    }

    int dropped = 0;
    shown = Sanitize(shown, &dropped);
    static int loggedDropped = -1;
    if (dropped != loggedDropped)
    {
        loggedDropped = dropped;
        if (dropped)
            Log("sanitize: %d unsupported codepoint(s) replaced", dropped);
    }

    std::string key;
    WrapLines(shown, key);

    for (size_t k = 0; k < g_lines.size(); ++k)
    {
        float y = drawY + (float)k * g_view.lineGap;
        if (g_view.barAlpha > 0)
        {
            float w = g_lines[k].width;
            if (w > 0.01f && w < 1.5f)
            {
                float half = w * 0.5f + g_view.barPad;
                Native(N_DRAW_RECT, g_view.x - half, y - g_view.barHeight, g_view.x + half,
                       y + g_view.barHeight * 0.45f, (int32_t)0, (int32_t)0, (int32_t)0,
                       (int32_t)g_view.barAlpha);
            }
        }
        Phase("draw_text");
        ApplyTextStyle();
        if (g_view.background == 1)
            Native(N_SET_TEXT_BACKGROUND, (int32_t)1);
        Native(N_DISPLAY_TEXT_WITH_LITERAL_STRING, g_view.x, y, (const char*)"STRING",
               g_lines[k].text.c_str());
    }

    if (beat)
        Log("beat[src=%s hookInst=%d driver=%d]: lines=%zu hookHz=%u nativeHz=%u bink='%s' frame=%u/%u",
            fromHook ? "hook" : "thread", (int)g_hookInstalled, g_view.driver, g_lines.size(),
            (g_hookCalls - lastHookCount) / 5, (g_nativeCalls - lastNativeCount) / 5, g_bink.name,
            g_bink.frameNum, g_bink.frames);
        lastNativeCount = g_nativeCalls;
        lastHookCount = g_hookCalls;
}

// ---------------------------------------------------------------- frame hook
// CGame::Process's first CALL (same site FusionFix hooks). We chain: our code
// runs, then the previous target (the game's own function, or FF's hook).
static void (*g_prevTarget)() = nullptr;
static uint8_t* g_hookSite = nullptr;
static int32_t g_hookOrigRel = 0;

static uint32_t g_lastFrames = 0;
static std::string g_lastLang;

// <plugins dir>\<channel>.<lang>.srt，没有就退回 <channel>.srt
static void PickChannelSrt()
{
    if (!g_bink.frames)
        return;
    // 按 Bink 总帧数认频道（游戏打开 .bik 时就能拿到帧数）
    const char* ch = nullptr;
    switch (g_bink.frames)
    {
    case 77182: ch = "weazel"; break;        // movies/weazel.bik 42:52
    case 93115: ch = "CNT"; break;           // movies/CNT.bik 51:44
    case 21700: ch = "weazelep1"; break;     // TLAD/movies/weazelep1.bik 12:04
    case 28873: ch = "CNTep1"; break;        // TLAD/movies/CNTep1.bik 16:04
    case 18399: ch = "weazelep2"; break;     // TBoGT/movies/weazelep2.bik 10:14
    case 21079: ch = "CNTep2"; break;        // TBoGT/movies/CNTep2.bik 11:43
    default: break;
    }
    if (!ch)
        return;
    // 换了频道或换了语言都要重新挑文件（Lang 是 2 秒热生效的）
    if (g_bink.frames == g_lastFrames && g_view.lang == g_lastLang)
        return;
    g_lastFrames = g_bink.frames;
    g_lastLang = g_view.lang;

    char dir[MAX_PATH];
    strncpy(dir, g_iniPath, MAX_PATH);
    char* slash = strrchr(dir, '\\');
    if (!slash)
        return;
    *slash = 0;

    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\%s.%s.srt", dir, ch, g_view.lang.c_str());
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExA(path, GetFileExInfoStandard, &fad))
        snprintf(path, sizeof path, "%s\\%s.srt", dir, ch);   // 只有一份时随便哪种语言
    Log("channel: %s lang=%s (frames=%u) -> %s", ch, g_view.lang.c_str(), g_bink.frames, path);
    LoadSrtInto(path);
}


// ---------------------------------------------------------------- M4 研究探针
// 目的：找到 CRadioStation（1.0.8.0 大小 0x1934、台名在 +0x17FC、+0x1927 是 frozen），
// 每 500 ms 拍一份快照、只打印**变化了的 4 字节字段**，用来定出
//   * 段内已播毫秒（每秒 +~2000、到 21250 归零）
//   * 当前段 id/hash（平时不动、换段时变）
// 只读内存、只在 tick 线程跑、只扫一次；扫描期间会有 1~2 秒卡顿（探针模式可接受）。
static const char* kStationNames[] = {
    "THE_VIBE", "WKTT", "PLR", "INTEGRITY", "JOURNEY", "K109", "FUSION",
    "LIBERTY_ROCK", "SAN_JUAN", "VLADIVOSTOK", "AFRO_BEAT", "HARDCORE",
    "BABYLON", "MEDITATION", "CLASSICAL", "BROKER", "DANCE", "VCFM",
};
static const uintptr_t kNameOff = 0x17FC;    // CRadioStation::m_sName
static const SIZE_T kObjSize = 0x1934;       // CRadioStation 大小（1.0.8.0，CE 待核）
#define RP_MAXOBJ 48
static uintptr_t g_rpBase[RP_MAXOBJ] = {0};
static int g_rpCount = 0;
static int g_rpScanned = 0, g_rpTries = 0;
static uint32_t g_rpSnap[RP_MAXOBJ][kObjSize / 4];

static bool ReadableRange(uintptr_t base, SIZE_T size)
{
    MEMORY_BASIC_INFORMATION mbi;
    if (!VirtualQuery((LPCVOID)base, &mbi, sizeof mbi))
        return false;
    if (mbi.State != MEM_COMMIT)
        return false;
    if (!(mbi.Protect & (PAGE_READWRITE | PAGE_READONLY | PAGE_WRITECOPY | PAGE_EXECUTE_READ |
                         PAGE_EXECUTE_READWRITE)))
        return false;
    return (uintptr_t)mbi.BaseAddress + mbi.RegionSize >= base + size;
}

static bool PrintableName(const char* p, int maxLen)
{
    for (int i = 0; i < maxLen; ++i)
    {
        unsigned char c = (unsigned char)p[i];
        if (!c)
            return i >= 2;
        if (c < 0x20 || c > 0x7E)
            return false;
    }
    return false;
}

static bool PlausibleStation(uintptr_t base)
{
    if ((base & 3) || base < 0x10000 || !ReadableRange(base, kObjSize))
        return false;
    // 名字必须是 1..31 个可打印字符 + NUL
    const char* nm = (const char*)(base + kNameOff);
    int n = 0;
    for (; n < 32; ++n)
    {
        unsigned char c = (unsigned char)nm[n];
        if (c == 0)
            break;
        if (c < 0x20 || c > 0x7E)
            return false;
    }
    if (n < 2 || n >= 32)
        return false;
    unsigned char frozen = *(unsigned char*)(base + 0x1927);
    return frozen <= 1;
}

static void RadioProbeScan()
{
    ++g_rpTries;
    DWORD t0 = GetTickCount();
    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = 0x10000;
    int hits = 0;
    while (addr < 0x7FFF0000 && hits < RP_MAXOBJ && g_rpCount < RP_MAXOBJ)
    {
        if (!VirtualQuery((LPCVOID)addr, &mbi, sizeof mbi))
            break;
        uintptr_t regionBase = (uintptr_t)mbi.BaseAddress;
        SIZE_T size = mbi.RegionSize;
        uintptr_t next = regionBase + size;
        bool rw = (mbi.State == MEM_COMMIT) && (mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY |
                                                              PAGE_EXECUTE_READWRITE));
        if (!rw || size > (SIZE_T)512 * 1024 * 1024 || size < kNameOff + 64)
        {
            addr = next ? next : addr + 0x1000;
            continue;
        }
        const unsigned char* p = (const unsigned char*)regionBase;
        SIZE_T limit = size - kNameOff - 64;
        for (SIZE_T off = 0; off < limit && g_rpCount < RP_MAXOBJ; off += 4)
        {
            const char* cand = (const char*)(p + off);
            unsigned char c0 = (unsigned char)cand[0];
            if (c0 < 'A' || c0 > 'Z')
                continue;                     // 台名都是大写字母开头
            for (size_t k = 0; k < sizeof kStationNames / sizeof *kStationNames; ++k)
            {
                const char* want = kStationNames[k];
                if (cand[0] != want[0] || cand[1] != want[1])
                    continue;
                size_t len = strlen(want);
                // 整串匹配：memcmp 全等 + 后面必须是 NUL/非字母数字
                if (memcmp(cand, want, len) != 0)
                    continue;
                if (isalnum((unsigned char)cand[len]) || cand[len] == '_')
                    continue;
                uintptr_t base = regionBase + off - kNameOff;
                if (!PlausibleStation(base))
                    continue;
                bool dup = false;
                for (int i = 0; i < g_rpCount; ++i)
                    if (g_rpBase[i] == base)
                        dup = true;
                if (dup)
                    continue;
                g_rpBase[g_rpCount] = base;
                Log("radioprobe: station object #%d base=%p name='%s'",
                    g_rpCount, (void*)base, (const char*)(base + kNameOff));
                ++g_rpCount;
                if (g_rpCount >= RP_MAXOBJ)
                    break;
            }
        }
        addr = next ? next : addr + 0x1000;
    }
    g_rpScanned = 1;
    Log("radioprobe: scan done in %u ms, %d object(s)", (unsigned)(GetTickCount() - t0), g_rpCount);
    if (g_rpCount == 0 && g_rpTries < 3)
        g_rpScanned = 0;                        // 最多再试 2 次（每次 ~300 ms，别把游戏扫卡）
    for (int i = 0; i < g_rpCount; ++i)
        memcpy(g_rpSnap[i], (const void*)g_rpBase[i], kObjSize);
}

// 台对象里应该存着"当前曲目"的指针；曲目描述记录的 name（+0x17FC）形如
// "THE_VIBE\\INMYBED"，所以把台对象里每个像指针的 dword 都拿去试一次，能读出这种名字
// 的就是我们要的字段。一次就够（台对象地址稳定，字段偏移也不会变）。
static void RadioProbeHunt()
{
    int hits = 0;
    for (int i = 0; i < g_rpCount; ++i)
    {
        const char* nm = (const char*)(g_rpBase[i] + kNameOff);
        if (strchr(nm, '\\'))
            continue;                              // 这是曲目描述记录本身，跳过
        const uint32_t* w = (const uint32_t*)g_rpBase[i];
        for (SIZE_T k = 0; k * 4 + 4 <= kObjSize; ++k)
        {
            uint32_t v = w[k];
            if (v < 0x10000000u || v > 0x7FFFFFFFu)
                continue;
            if (!ReadableRange(v, kNameOff + 64))
                continue;
            const char* tn = (const char*)(v + kNameOff);
            if (!PrintableName(tn, 48) || !strchr(tn, '\\'))
                continue;
            Log("radioprobe: HUNT %s +0x%03X -> %p '%s'", nm, (unsigned)(k * 4), (void*)v, tn);
            ++hits;
        }
    }
    Log("radioprobe: hunt done, %d candidate pointer(s)", hits);
}

static void RadioProbeTick()
{
    static DWORD lastScan = 0;
    if (!g_rpScanned)
    {
        DWORD now = GetTickCount();
        if (lastScan && now - lastScan < 5000)
            return;                             // 没找到时 5 s 才重扫一次（扫描会卡 0.3~2 s）
        lastScan = now;
        RadioProbeScan();
        return;
    }
    if (g_rpCount == 0)
        return;
    static char buf[4096];
    int used = 0;
    buf[0] = 0;
    static int hunted = 0;
    if (!hunted && g_rpCount)
    {
        hunted = 1;
        RadioProbeHunt();
    }
    for (int i = 0; i < g_rpCount; ++i)
    {
        if (strchr((const char*)(g_rpBase[i] + kNameOff), '\\'))
            continue;                              // 曲目描述记录是静态的，不用 diff
        const uint32_t* cur = (const uint32_t*)g_rpBase[i];
        int shown = 0;
        for (SIZE_T w = 0; w < kObjSize / 4; ++w)
        {
            if (cur[w] == g_rpSnap[i][w])
                continue;
            uint32_t a = g_rpSnap[i][w], b = cur[w];
            long long d = (long long)(int)b - (long long)(int)a;
            int n = snprintf(buf + used, sizeof buf - used,
                             "radioprobe: %s +0x%03X %08X -> %08X (delta=%lld)%s\n",
                             (const char*)(g_rpBase[i] + kNameOff), (unsigned)(w * 4), a, b, d,
                             (d > 0 && d < 30000) ? "  <pos?>" : "");
            if (n <= 0 || (size_t)(used + n) >= sizeof buf)
                break;
            used += n;
            g_rpSnap[i][w] = b;
            if (++shown > 12)
                break;                          // 单对象单 tick 最多 12 个字段
        }
        if (used >= (int)sizeof buf - 128)
            break;
    }
    if (used)
        RawLog(buf);                            // 一次 fopen 写完，别每行开关文件
}

static void OurFrameHook()
{
    ++g_hookCalls;
    PickChannelSrt();
    // radio probe: station name/index + current track text id, log on change
    {
        static DWORD lastProbe = 0;
        DWORD nowMs = GetTickCount();
        if (nowMs - lastProbe > 500)
        {
            lastProbe = nowMs;
            static char lastName[64] = {0};
            static int32_t lastIdx = -1, lastTrack = -1, lastMode = -1;
            int32_t mode = NativeGetInt(N_GET_PLAYER_RADIO_MODE);
            int32_t idx = NativeGetInt(N_GET_PLAYER_RADIO_STATION_INDEX);
            int32_t track = NativeGetInt(N_GET_AUDIBLE_MUSIC_TRACK_TEXT_ID);
            if (g_view.radioTest && (nowMs / 5000) != (lastProbe / 5000) && mode != 0)
                Native(N_SKIP_RADIO_FORWARD);
            const char* nm = NativeGetString(N_GET_PLAYER_RADIO_STATION_NAME);
            char name[64];
            strncpy(name, nm, sizeof name - 1);
            name[sizeof name - 1] = 0;
            // 「电台在响」= 台名非空 + 曲目文本 id != 0 + **玩家在车里**。
            // 只看台名是不够的：下车之后 GET_PLAYER_RADIO_STATION_NAME 仍然返回上次的台名
            // （实测 19:08 下车后 radio: 行一直刷 BEAT_95），屏幕上就留下一条"僵尸字幕"。
            int32_t playerId = NativeGetInt(N_GET_PLAYER_ID);
            int32_t playerIndex = NativeCall1(N_CONVERT_INT_TO_PLAYERINDEX, playerId);
            int32_t ped = 0;
            if (playerIndex >= 0)
                NativeCall2(N_GET_PLAYER_CHAR, playerIndex, (int32_t)(intptr_t)&ped);
            int32_t inCar = ped ? NativeCall1(N_IS_CHAR_IN_ANY_CAR, ped) : 0;
            const bool radioLive = name[0] && track != 0 && inCar;
            InterlockedExchange(&g_radioActive, radioLive ? 1 : 0);
            if (radioLive)
                snprintf(g_radioLabel, sizeof g_radioLabel, "%s · %d", name, (int)track);
            else
                g_radioLabel[0] = 0;

            // M4：段身份（资源 hash）+ 段内毫秒 —— 两者都从引擎内存里读
            static uint32_t lastHash = 0;
            if (radioLive && g_view.radioSync)
            {
                uint32_t hash = 0, segMs = 0, segLen = 0;
                if (ReadRadioSegment(name, &hash, &segMs, &segLen))
                {
                    g_radioHash = hash;
                    InterlockedExchange(&g_radioMs, (LONG)segMs);
                    InterlockedExchange(&g_radioLen, (LONG)segLen);
                    LoadRadioIndex();
                    uint32_t fnch = 0, fdur = 0;
                    if (RadioFileInfo(hash, &fnch, &fdur))
                    {
                        InterlockedExchange(&g_radioNch, (LONG)fnch);
                        InterlockedExchange(&g_radioDurMs, (LONG)fdur);
                    }
                    else
                    {
                        InterlockedExchange(&g_radioNch, 0);
                        InterlockedExchange(&g_radioDurMs, 0);
                    }
                    snprintf(g_radioTrack, sizeof g_radioTrack, "%08x", hash);
                    if (hash != lastHash)
                    {
                        lastHash = hash;
                        Log("radio seg: %s hash=%08x len=%u ms=%u", name, hash, segLen, segMs);
                    }
                    LoadRadioSrt(hash, g_view.lang.c_str());
                }
                else if (g_radioHash)
                {
                    g_radioHash = 0;
                    g_radioTrack[0] = 0;
                    InterlockedExchange(&g_radioMs, 0);
                }
            }
            else if (!radioLive)
            {
                lastHash = 0;
                g_radioHash = 0;
                g_radioTrack[0] = 0;
                InterlockedExchange(&g_radioMs, 0);
            }
            if (strcmp(name, lastName) || idx != lastIdx || track != lastTrack || mode != lastMode)
            {
                Log("radio: mode=%d idx=%u trackTextId=%d name='%s'", mode, idx, track, name);
                strncpy(lastName, name, sizeof lastName - 1);
                lastIdx = idx;
                lastTrack = track;
                lastMode = mode;
            }
        }
    }

    if (g_set.radioProbe && g_radioActive)
    {
        static DWORD lastProbe2 = 0;
        DWORD nowMs2 = GetTickCount();
        if (nowMs2 - lastProbe2 >= 500)
        {
            lastProbe2 = nowMs2;
            RadioProbeTick();
        }
    }

    if (g_binkOpenPending)
    {
        g_binkOpenPending = 0;
        Log("bink open #%u: '%s' handle=%p", g_bink.openCount, g_bink.name, g_bink.handle);
    }
    if (g_binkFramePending && (g_bink.doFrameCount % 150 == 0))
    {
        g_binkFramePending = 0;
        Log("bink: '%s' frame %u/%u rate %u/%u %ux%u", g_bink.name, g_bink.frameNum,
            g_bink.frames, g_bink.frameRate, g_bink.frameRateDiv, g_bink.width, g_bink.height);
    }
    if (g_set.driver == 0 || g_set.driver == 2)
        DrawSubtitle(true);
    if (g_prevTarget)
        g_prevTarget();
}

static bool InstallFrameHook()
{
    auto* base = (uint8_t*)g_exe;
    auto* dos = (IMAGE_DOS_HEADER*)base;
    auto* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    uint8_t* site = nullptr;
    for (int i = 0; i < nt->FileHeader.NumberOfSections && !site; ++i)
        site = pat::find(base + sec[i].VirtualAddress, sec[i].Misc.VirtualSize,
                         "E8 ? ? ? ? E8 ? ? ? ? E8 ? ? ? ? B9 ? ? ? ? E8 ? ? ? ? E8 ? ? ? ? E8 ? ? ? ? E8 ? ? ? ? B9");
    if (!site)
        return false;
    int32_t rel = 0;
    memcpy(&rel, site + 1, 4);
    g_hookOrigRel = rel;
    g_hookSite = site;
    g_prevTarget = (void (*)())(site + 5 + rel);
    int32_t newRel = (int32_t)((uint8_t*)&OurFrameHook - (site + 5));
    DWORD old = 0;
    if (!VirtualProtect(site, 5, PAGE_EXECUTE_READWRITE, &old))
        return false;
    memcpy(site + 1, &newRel, 4);
    VirtualProtect(site, 5, old, &old);
    Log("frame hook: site=%p prev=%p", (void*)site, (void*)g_prevTarget);
    return true;
}

static void SetFrameHook(bool on)
{
    if (!g_hookSite || (!on && !g_hookInstalled) || (on && g_hookInstalled))
        return;
    int32_t rel = on ? (int32_t)((uint8_t*)&OurFrameHook - (g_hookSite + 5)) : g_hookOrigRel;
    DWORD old = 0;
    if (VirtualProtect(g_hookSite, 5, PAGE_EXECUTE_READWRITE, &old))
    {
        memcpy(g_hookSite + 1, &rel, 4);
        VirtualProtect(g_hookSite, 5, old, &old);
    }
    g_hookInstalled = on;
    Log("frame hook %s (rel=%d)", on ? "ON" : "OFF", rel);
}

static DWORD WINAPI TickThread(LPVOID)
{
    while (!g_exe)
    {
        g_exe = GetModuleHandleA("GTAIV.exe");
        Sleep(100);
    }
    selfrender::InitDeviceScan((uint8_t*)g_exe);
    int waited = 0;
    while (!ResolveNatives())
    {
        Sleep(100);
        waited += 100;
        if (waited > 60000)
        {
            Log("gave up resolving natives");
            return 0;
        }
    }
    // natives are only meaningful once the script VM exists
    while (!(g_pTableSize && *g_pTableSize))
        Sleep(200);

    InstallBinkHooks();

    if (InstallFrameHook())
        g_hookInstalled = true;
    else
        Log("frame hook NOT installed (pattern miss)");

    DWORD lastReload = 0;
    DWORD lastTickLog = 0;
    for (;;)
    {
        DWORD now = GetTickCount();
        selfrender::PollDevice();
        if (now - lastReload > 2000)
        {
            LoadSettings();
            lastReload = now;
            SetBinkHooks(g_set.binkHook != 0);
            SetFrameHook(g_set.frameHook != 0);
            if (g_srtPath[0])
                LoadSrtInto(g_srtPath);   // picks up edits/new transcriptions live
        }
        // who drives the drawing?
        bool byThread = (g_set.driver == 1) || (g_set.driver == 2) || !g_hookInstalled;
        if (byThread && g_set.enable)
        {
            DrawSubtitle(false);
            Sleep(8);
        }
        if ((now / 5000) != (lastTickLog / 5000))
        {
            lastTickLog = now;
            Log("tick: hookCalls=%u nativeCalls=%u hookInst=%d driver=%d binkFrame=%u/%u",
                g_hookCalls, g_nativeCalls, (int)g_hookInstalled, g_set.driver, g_bink.frameNum,
                g_bink.frames);
            selfrender::LoadAtlasOnTick();
            selfrender::ReportStats();
        }
        Sleep(20);
    }
    return 0;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(hModule);
        GetModuleFileNameA(hModule, g_iniPath, MAX_PATH);
        strncpy(g_logPath, g_iniPath, MAX_PATH);
        char* dot = strrchr(g_iniPath, '.');
        if (dot)
            strcpy(dot, ".ini");
        char* dot2 = strrchr(g_logPath, '.');
        if (dot2)
            strcpy(dot2, ".log");
        g_exe = GetModuleHandleA("GTAIV.exe");
        Log("=== loaded, exe=%p ini=%s", (void*)g_exe, g_iniPath);
        strncpy(g_srtPath, g_iniPath, MAX_PATH);
        char* dot3 = strrchr(g_srtPath, '.');
        if (dot3)
            strcpy(dot3, ".srt");
        InitializeCriticalSection(&g_cuesLock);   // once, before any thread uses it
        InitializeCriticalSection(&g_radioCuesLock);
        InitializeCriticalSection(&g_radioIdxLock);
        InitializeCriticalSection(&g_setLock);
        {   // 电台 per-track SRT 目录：<ini 目录>\radio\<hash>.zh.srt
            char dir[MAX_PATH];
            strncpy(dir, g_iniPath, MAX_PATH);
            char* sl = strrchr(dir, '\\');
            if (sl)
                *sl = 0;
            snprintf(g_radioDir, sizeof g_radioDir, "%s\\radio", dir);
        }
        LoadAtlas();
        LoadSettings();   // uses g_setLock (already initialised above)
        LoadSrtInto(g_srtPath);
        Log("text='%s' x=%.3f y=%.3f scale=%.2f bg=%d", g_set.text.c_str(), g_set.x, g_set.y,
            g_set.scale, g_set.background);
        SetUnhandledExceptionFilter(CrashFilter);
        AddVectoredExceptionHandler(1, VectoredHandler);
        {
            char atlasDir[MAX_PATH];
            strncpy(atlasDir, g_logPath, MAX_PATH);
            char* sl = strrchr(atlasDir, '\\');
            if (sl)
                *sl = 0;
            std::string base = std::string(atlasDir) + "\\subtitle_atlas";
            selfrender::SetAtlasPath(base);
        }
        CreateThread(nullptr, 0, TickThread, nullptr, 0, nullptr);
    }
    return TRUE;
}
