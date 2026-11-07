// Self-rendered subtitles: our own font atlas drawn through D3D9.
//
// Why: the native-text path depends on the 汉化 patch (its text-encode hook and
// its 3518-codepoint atlas). Drawing ourselves through D3D9 makes the mod
// standalone: any font, any charset, no interference with the game's text state.
//
// Hook chain: IAT-hook Direct3DCreate9 (same trick as the bink probe) ->
// capture IDirect3D9 -> vtable-hook CreateDevice -> capture IDirect3DDevice9 ->
// vtable-hook Present -> draw right before the frame is shown.
//
// Font atlas: baked offline by pipeline/bake_atlas.py (FreeType via PIL) from
// Noto Sans CJK — A8 bitmap + a flat metrics file, shipped next to the ASI.
#include <d3d9.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace selfrender
{

struct Glyph
{
    float u, v, w, h;   // atlas coords (pixels)
    float adv;          // advance in atlas px
    float xoff, yoff;   // offset from the ascender-top origin
};

struct Content
{
    bool visible = false;
    float x = 0.5f, y = 0.055f;   // screen fractions
    float scale = 1.0f;           // backbufferH / 720 reference
    float textScale = 1.0f;       // 额外字号倍率（电台占位行用它调小）
    float maxW = 0.80f;           // wrap width (fraction)
    float lineGap = 0.075f;       // fraction of height
    int boxAlpha = 0;             // 0 = off
    float boxPad = 0.014f;
    float boxH = 0.030f;
    bool centre = true;
    std::string text;             // may contain \n (pre-wrapped) — we re-wrap here
};

static Content g_content;         // written by the main thread, read by the render thread
static bool g_ready = false;      // device + texture + metrics loaded
static IDirect3DDevice9* g_dev = nullptr;
static IDirect3DTexture9* g_tex = nullptr;
static float g_atlasW = 1, g_atlasH = 1, g_px = 34, g_lineH = 50;
static std::vector<Glyph> g_glyphs;         // slot order
static std::vector<std::pair<uint32_t, uint32_t>> g_cpMap;  // sorted cp -> slot
static std::vector<uint32_t> g_cpList;      // charset in slot order
static std::string g_atlasPath;
// 字库数据的文件 I/O 全部在 tick 线程做（render 线程里 fopen/fprintf 会静默失效，
// 和 bink 钩子上的教训一样：非主线程只写变量）。
static std::vector<uint8_t> g_atlasA8;      // 位图字节（tick 线程填）
static volatile LONG g_atlasFile = 0;      // 0=未试 1=OK 2=指标文件 3=位图文件
static volatile DWORD g_atlasErr = 0;      // 最近一次失败的系统错误码

static HRESULT(WINAPI* g_origPresent)(IDirect3DDevice9*, const RECT*, const RECT*, HWND,
                                      RGNDATA*) = nullptr;
static void** g_presentSlot = nullptr;
// ---------------------------------------------------------------- diag counters
// 只在渲染线程里 InterlockedIncrement（不写日志：日志要回到 tick 线程写）
static volatile LONG g_cPresent = 0;      // 我们钩到的 Device9::Present 被调用次数
static volatile LONG g_cSwapPresent = 0;  // SwapChain9::Present
static volatile LONG g_cEndScene = 0;     // EndScene
static volatile LONG g_cDrawn = 0;        // 真的画了一帧字
static volatile LONG g_cReadyFail = 0;    // atlas/纹理初始化失败次数
static HRESULT(WINAPI* g_origSwapPresent)(IDirect3DSwapChain9*, const RECT*, const RECT*,
                                          HWND, RGNDATA*) = nullptr;
static void** g_swapSlot = nullptr;
static HRESULT(WINAPI* g_origEndScene)(IDirect3DDevice9*) = nullptr;
static void** g_endSceneSlot = nullptr;
// ---------------------------------------------------------------- metrics
static bool FindGlyph(uint32_t cp, Glyph& out)
{
    size_t lo = 0, hi = g_cpMap.size();
    while (lo < hi)
    {
        size_t mid = (lo + hi) / 2;
        if (g_cpMap[mid].first < cp)
            lo = mid + 1;
        else
            hi = mid;
    }
    if (lo >= g_cpMap.size() || g_cpMap[lo].first != cp)
        return false;
    out = g_glyphs[g_cpMap[lo].second];
    return out.w > 0 || out.adv > 0;
}

static void AppendUtf8(std::vector<uint32_t>& out, const std::string& s)
{
    for (size_t i = 0; i < s.size();)
    {
        unsigned char c = (unsigned char)s[i];
        uint32_t cp = c;
        size_t len = 1;
        if ((c & 0xE0) == 0xC0 && i + 1 < s.size())
        {
            cp = ((c & 0x1F) << 6) | (s[i + 1] & 0x3F);
            len = 2;
        }
        else if ((c & 0xF0) == 0xE0 && i + 2 < s.size())
        {
            cp = ((c & 0x0F) << 12) | ((s[i + 1] & 0x3F) << 6) | (s[i + 2] & 0x3F);
            len = 3;
        }
        else if ((c & 0xF8) == 0xF0 && i + 3 < s.size())
        {
            cp = ((c & 0x07) << 18) | ((s[i + 1] & 0x3F) << 12) | ((s[i + 2] & 0x3F) << 6) |
                 (s[i + 3] & 0x3F);
            len = 4;
        }
        out.push_back(cp);
        i += len;
    }
}

// 整个文件读进内存（Win32 API，不碰 CRT —— 在钩子线程里 fopen 不可靠）
static bool ReadWholeFile(const std::string& path, std::vector<uint8_t>& out, DWORD* err)
{
    *err = 0;
    HANDLE h = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE)
    {
        *err = GetLastError();
        return false;
    }
    LARGE_INTEGER sz;
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart <= 0)
    {
        *err = GetLastError();
        CloseHandle(h);
        return false;
    }
    out.resize((size_t)sz.QuadPart);
    DWORD got = 0;
    BOOL ok = ReadFile(h, out.data(), (DWORD)out.size(), &got, nullptr);
    CloseHandle(h);
    if (!ok || got != out.size())
    {
        *err = GetLastError();
        return false;
    }
    return true;
}

static bool LoadAtlas()
{
    // metrics: <IIIffI> magic,w,h,px,lineH,n ; n*(II cp,slot) ; n*(I count) ; count*(7f)
    std::vector<uint8_t> buf;
    DWORD err = 0;
    if (!ReadWholeFile(g_atlasPath + "_metrics.bin", buf, &err))
    {
        g_atlasErr = err;
        return false;
    }
    const uint8_t* p = buf.data();
    size_t left = buf.size();
    auto take = [&](void* dst, size_t n) {
        if (left < n)
            return false;
        memcpy(dst, p, n);
        p += n;
        left -= n;
        return true;
    };
    uint32_t hdr[3] = {0, 0, 0};
    float px = 0, lineH = 0;
    uint32_t n = 0;
    if (!take(&hdr[0], 4) || hdr[0] != 0x54414253)
    {
        g_atlasErr = 0xBAD;
        return false;
    }
    if (!take(&hdr[1], 4) || !take(&hdr[2], 4) || !take(&px, 4) || !take(&lineH, 4) ||
        !take(&n, 4))
    {
        g_atlasErr = 0xBAD;
        return false;
    }
    g_atlasW = (float)hdr[1];
    g_atlasH = (float)hdr[2];
    g_px = px;
    g_lineH = lineH;
    g_cpMap.clear();
    g_cpList.clear();
    g_glyphs.clear();
    g_cpMap.reserve(n);
    g_cpList.reserve(n);
    for (uint32_t i = 0; i < n; ++i)
    {
        uint32_t pair[2] = {0, 0};
        if (!take(&pair[0], 4) || !take(&pair[1], 4))
            break;
        g_cpMap.push_back({pair[0], pair[1]});
        g_cpList.push_back(pair[0]);
    }
    uint32_t m = 0;
    if (!take(&m, 4))
    {
        g_atlasErr = 0xBAD;
        return false;
    }
    g_glyphs.reserve(m);
    for (uint32_t i = 0; i < m; ++i)
    {
        float v[7];
        if (!take(v, sizeof v))
            break;
        g_glyphs.push_back({v[0], v[1], v[2], v[3], v[4], v[5], v[6]});
    }
    return !g_glyphs.empty();
}

// ---------------------------------------------------------------- drawing
struct SVtx
{
    float x, y, z, rhw;
    D3DCOLOR c;
    float u, v;
};

static void DrawBox(IDirect3DDevice9* dev, float x, float y, float w, float h, D3DCOLOR c)
{
    SVtx v[6] = {
        {x, y, 0, 1, c, 0, 0},         {x + w, y, 0, 1, c, 0, 0},
        {x, y + h, 0, 1, c, 0, 0},     {x + w, y, 0, 1, c, 0, 0},
        {x + w, y + h, 0, 1, c, 0, 0}, {x, y + h, 0, 1, c, 0, 0},
    };
    dev->SetTexture(0, nullptr);
    dev->DrawPrimitiveUP(D3DPT_TRIANGLELIST, 2, v, sizeof(SVtx));
}

static void DrawGlyphs(IDirect3DDevice9* dev, const std::vector<SVtx>& v)
{
    if (v.empty())
        return;
    dev->SetTexture(0, g_tex);
    dev->DrawPrimitiveUP(D3DPT_TRIANGLELIST, (UINT)v.size() / 3, v.data(), sizeof(SVtx));
}

static void SetupStates(IDirect3DDevice9* dev)
{
    // 不依赖游戏留下的管线状态
    dev->SetVertexShader(nullptr);
    dev->SetPixelShader(nullptr);
    dev->SetRenderState(D3DRS_ALPHABLENDENABLE, TRUE);
    dev->SetRenderState(D3DRS_SRCBLEND, D3DBLEND_SRCALPHA);
    dev->SetRenderState(D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
    dev->SetRenderState(D3DRS_ALPHATESTENABLE, FALSE);
    dev->SetRenderState(D3DRS_CULLMODE, D3DCULL_NONE);
    dev->SetRenderState(D3DRS_ZENABLE, FALSE);
    dev->SetRenderState(D3DRS_ZWRITEENABLE, FALSE);
    dev->SetRenderState(D3DRS_LIGHTING, FALSE);
    dev->SetRenderState(D3DRS_SCISSORTESTENABLE, FALSE);
    dev->SetRenderState(D3DRS_STENCILENABLE, FALSE);
    dev->SetTextureStageState(0, D3DTSS_COLOROP, D3DTOP_MODULATE);
    dev->SetTextureStageState(0, D3DTSS_COLORARG1, D3DTA_TEXTURE);
    dev->SetTextureStageState(0, D3DTSS_COLORARG2, D3DTA_DIFFUSE);
    dev->SetTextureStageState(0, D3DTSS_ALPHAOP, D3DTOP_MODULATE);
    dev->SetTextureStageState(0, D3DTSS_ALPHAARG1, D3DTA_TEXTURE);
    dev->SetTextureStageState(0, D3DTSS_ALPHAARG2, D3DTA_DIFFUSE);
    dev->SetTextureStageState(0, D3DTSS_TEXCOORDINDEX, 0);
    for (DWORD st = 1; st < 3; ++st)   // 后面的 stage 关掉，免得被游戏残留的双层合成污染
    {
        dev->SetTexture(st, nullptr);
        dev->SetTextureStageState(st, D3DTSS_COLOROP, D3DTOP_DISABLE);
        dev->SetTextureStageState(st, D3DTSS_ALPHAOP, D3DTOP_DISABLE);
    }
    dev->SetFVF(D3DFVF_XYZRHW | D3DFVF_DIFFUSE | D3DFVF_TEX1);
}

static bool CreateTexture(IDirect3DDevice9* dev)
{
    // 位图字节已由 tick 线程读进 g_atlasA8
    if (g_atlasA8.size() != (size_t)g_atlasW * (size_t)g_atlasH)
        return false;

    if (FAILED(dev->CreateTexture((UINT)g_atlasW, (UINT)g_atlasH, 1, 0, D3DFMT_A8R8G8B8,
                                  D3DPOOL_MANAGED, &g_tex, nullptr)))
        return false;
    D3DLOCKED_RECT lr;
    if (FAILED(g_tex->LockRect(0, &lr, nullptr, 0)))
        return false;
    auto* dst = (uint8_t*)lr.pBits;
    const uint8_t* src = g_atlasA8.data();
    size_t n = g_atlasA8.size();
    for (size_t i = 0; i < n; ++i)
    {
        dst[i * 4 + 0] = 255;
        dst[i * 4 + 1] = 255;
        dst[i * 4 + 2] = 255;
        dst[i * 4 + 3] = src[i];
    }
    g_tex->UnlockRect(0);
    return true;
}

// ---------------------------------------------------------------- 断行 / 版面
// 中文断行规则：
//   * 按字宽累加，超过 maxW 就换行；
//   * **避头点**：，。、；：？！）》”’…—·% 之类不能出现在行首 → 挤在上一行；
//   * 行尾的 "（《“‘" 也不能留在行尾（避尾）→ 一起挪到下一行。
struct LayoutMetrics
{
    int lines = 1;
    float maxLineW = 0;   // px
    float lineH = 0;      // px
    float px = 0;         // px (font size at this resolution)
    // 实际墨迹范围（相对 y 原点，px）：英文字母的下伸部（y/g/p 的尾巴）比 em 框
    // 低，黑底条必须按这个算，否则尾巴会露在条外面（实测踩过）。
    float inkTop = 0;
    float inkBottom = 0;
};

static bool IsNoBreakHead(uint32_t cp)   // 不能出现在行首
{
    static const uint32_t k[] = {0xFF0C, 0x3002, 0x3001, 0xFF1B, 0xFF1A, 0xFF1F, 0xFF01,
                                 0xFF09, 0x300B, 0x3009, 0x201D, 0x2019, 0x2026, 0x2014,
                                 0x00B7, 0xFF05, 0x0025, 0x002C, 0x002E, 0x0021, 0x003F,
                                 0x003A, 0x003B, 0x0029};
    for (uint32_t v : k)
        if (cp == v)
            return true;
    return false;
}

static bool IsNoBreakTail(uint32_t cp)   // 不能留在行尾
{
    static const uint32_t k[] = {0xFF08, 0x300A, 0x3008, 0x201C, 0x2018, 0x0028};
    for (uint32_t v : k)
        if (cp == v)
            return true;
    return false;
}

static float LineWidth(const std::vector<uint32_t>& ln, float scale, float px)
{
    float w = 0;
    for (uint32_t cp : ln)
    {
        Glyph g;
        w += FindGlyph(cp, g) ? g.adv * scale : px;
    }
    return w;
}

static void BuildLines(const Content& ct, float outW, float outH,
                       std::vector<std::vector<uint32_t>>& lines, LayoutMetrics& m)
{
    float scale = (outH / 720.0f) * (ct.textScale > 0 ? ct.textScale : 1.0f);
    m.px = g_px * scale;
    m.lineH = g_lineH * scale;
    float maxPx = ct.maxW * outW;
    const float px = m.px;

    std::vector<uint32_t> cps;
    AppendUtf8(cps, ct.text);

    lines.assign(1, {});
    float lineW = 0;
    size_t lastSpace = 0;   // 当前行最后一个空格之后的位置（英文的合法断点）

    auto trimTail = [](std::vector<uint32_t>& ln) {
        while (!ln.empty() && ln.back() == ' ')
            ln.pop_back();
    };
    auto trimHead = [](std::vector<uint32_t>& ln) {
        while (!ln.empty() && ln.front() == ' ')
            ln.erase(ln.begin());
    };

    for (size_t i = 0; i < cps.size(); ++i)
    {
        uint32_t cp = cps[i];
        if (cp == '\n')
        {
            trimTail(lines.back());
            lines.emplace_back();
            lineW = 0;
            lastSpace = 0;
            continue;
        }
        Glyph g;
        float adv = FindGlyph(cp, g) ? g.adv * scale : px;
        bool overflow = (lineW + adv > maxPx) && !lines.back().empty();

        // 1) 英文优先在空格处断，绝不把一个单词从中间切开
        if (overflow && lastSpace > 0 && lastSpace <= lines.back().size())
        {
            std::vector<uint32_t> tail(lines.back().begin() + (long)lastSpace, lines.back().end());
            lines.back().resize(lastSpace);
            trimTail(lines.back());
            lines.emplace_back(tail.begin(), tail.end());
            trimHead(lines.back());
            lineW = LineWidth(lines.back(), scale, px);
            lastSpace = 0;
            overflow = (lineW + adv > maxPx) && !lines.back().empty();
        }
        // 2) 中文（没有空格）：避开行首标点，宁可这一行稍微超一点
        if (overflow && IsNoBreakHead(cp))
            overflow = false;
        if (overflow)
        {
            // 3) 避尾：把上一行末尾的左括号/左引号挪到下一行
            std::vector<uint32_t> carry;
            while (!lines.back().empty() && IsNoBreakTail(lines.back().back()))
            {
                carry.push_back(lines.back().back());
                lines.back().pop_back();
            }
            lines.emplace_back();
            for (auto it = carry.rbegin(); it != carry.rend(); ++it)
                lines.back().push_back(*it);
            lineW = LineWidth(lines.back(), scale, px);
            lastSpace = 0;
        }
        lines.back().push_back(cp);
        lineW += adv;
        if (cp == ' ')
            lastSpace = lines.back().size();
    }
    trimTail(lines.back());

    m.lines = (int)lines.size();
    m.maxLineW = 0;
    m.inkTop = 1e9f;
    m.inkBottom = -1e9f;
    for (auto& ln : lines)
    {
        float w = LineWidth(ln, scale, px);
        if (w > m.maxLineW)
            m.maxLineW = w;
        for (uint32_t cp : ln)
        {
            Glyph g;
            if (!FindGlyph(cp, g))
                continue;
            // 空格这类字形 bake 时写的是 [0,0,0,0,...]：它没有墨迹，**不能**
            // 参与 ink 上下边计算 —— 否则 (yoff=0) 会把“墨迹顶”拉到 ascender 顶，
            // 黑底条比文字高出整整一个 cap-height（2026-11-07 实测：高出 15px@720p）。
            if (g.w <= 0.0f || g.h <= 0.0f)
                continue;
            if (g.yoff * scale < m.inkTop)
                m.inkTop = g.yoff * scale;
            if ((g.yoff + g.h) * scale > m.inkBottom)
                m.inkBottom = (g.yoff + g.h) * scale;
        }
    }
    if (m.inkBottom <= m.inkTop + 0.5f)   // 没有任何可见字形（全空格之类）
    {
        m.inkTop = 0;
        m.inkBottom = m.px;
    }
}

static void EmitLines(const Content& ct, const std::vector<std::vector<uint32_t>>& lines,
                      float outW, float outH, const LayoutMetrics& m, D3DCOLOR color,
                      std::vector<SVtx>& out)
{
    // 注意：这里必须跟 BuildLines 用**同一个** scale（含 textScale），否则字形
    // 不跟着 RadioScale 缩小，只有换行宽度变了（踩过：改 RadioScale 看不出变化）。
    float scale = (outH / 720.0f) * (ct.textScale > 0 ? ct.textScale : 1.0f);
    float px = m.px;
    float cy = ct.y * outH;
    for (size_t k = 0; k < lines.size(); ++k)
    {
        float baseline = cy + (float)k * (ct.lineGap * outH) + px;
        float lw = 0;
        for (uint32_t cp : lines[k])
        {
            Glyph g;
            lw += FindGlyph(cp, g) ? g.adv * scale : px;
        }
        float cx = ct.x * outW;
        if (ct.centre)
            cx -= lw * 0.5f;
        for (uint32_t cp : lines[k])
        {
            Glyph g;
            if (!FindGlyph(cp, g))
            {
                cx += px * 0.5f;   // 字库没有的字：留半个字宽的空
                continue;
            }
            float gx = cx + g.xoff * scale;
            float gy = baseline - px + g.yoff * scale;
            float w = g.w * scale, h = g.h * scale;
            float u0 = g.u / g_atlasW, v0 = g.v / g_atlasH;
            float u1 = (g.u + g.w) / g_atlasW, v1 = (g.v + g.h) / g_atlasH;
            SVtx q[6] = {
                {gx, gy, 0, 1, color, u0, v0},
                {gx + w, gy, 0, 1, color, u1, v0},
                {gx, gy + h, 0, 1, color, u0, v1},
                {gx + w, gy, 0, 1, color, u1, v0},
                {gx + w, gy + h, 0, 1, color, u1, v1},
                {gx, gy + h, 0, 1, color, u0, v1},
            };
            out.insert(out.end(), q, q + 6);
            cx += g.adv * scale;
        }
    }
}

static void RenderFrame(IDirect3DDevice9* dev)
{
    if (!g_ready)
    {
        if (g_atlasFile != 1)
        {
            InterlockedIncrement(&g_cReadyFail);
            return;
        }
        if (!CreateTexture(dev))
        {
            InterlockedIncrement(&g_cReadyFail);
            return;
        }
        g_ready = true;
    }
    IDirect3DSurface9* bb = nullptr;
    if (FAILED(dev->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &bb)) || !bb)
        return;
    D3DSURFACE_DESC d;
    bb->GetDesc(&d);
    bb->Release();
    float outW = (float)d.Width, outH = (float)d.Height;

    Content ct = g_content;   // small copy; writer is the main thread
    if (!ct.visible || ct.text.empty())
        return;

    SetupStates(dev);

    std::vector<std::vector<uint32_t>> lines;
    LayoutMetrics m;
    BuildLines(ct, outW, outH, lines, m);

    if (ct.boxAlpha > 0)
    {
        // 黑底条：宽度按最长行（+ pad），高度按**真实墨迹**算（空间靠 pad 撑）。
        // pad 跟着 textScale 缩：电台行字号小，黑框内衬也该小。
        float ts = ct.textScale > 0 ? ct.textScale : 1.0f;
        float padX = ct.boxPad * outW * ts, padY = ct.boxPad * outH * ts;
        float w = m.maxLineW + 2 * padX;
        if (w > ct.maxW * outW + 2 * padX)
            w = ct.maxW * outW + 2 * padX;
        float shadow = 3.0f * (outH / 720.0f);   // 黑影也要包住
        float h = (m.inkBottom - m.inkTop) + (m.lines - 1) * (ct.lineGap * outH) +
                  2 * padY + shadow;
        float left = ct.centre ? ct.x * outW - w * 0.5f : ct.x * outW - padX;
        float top = ct.y * outH + m.inkTop - padY;
        DrawBox(dev, left, top, w, h, D3DCOLOR_ARGB(ct.boxAlpha, 0, 0, 0));
    }

    std::vector<SVtx> v;
    // drop shadow (2 px down at 720p) then the text itself
    Content ct2 = ct;
    ct2.y += 2.5f / outH;
    EmitLines(ct2, lines, outW, outH, m, D3DCOLOR_ARGB(220, 0, 0, 0), v);
    DrawGlyphs(dev, v);
    v.clear();
    EmitLines(ct, lines, outW, outH, m, D3DCOLOR_ARGB(255, 255, 255, 255), v);
    DrawGlyphs(dev, v);
    InterlockedIncrement(&g_cDrawn);
}

// ---------------------------------------------------------------- hooks
static HRESULT WINAPI HookPresent(IDirect3DDevice9* self, const RECT* src, const RECT* dst,
                                  HWND hwnd, RGNDATA* data)
{
    InterlockedIncrement(&g_cPresent);
    // 有交换链钩子时只在那里画（这个游戏本来也不走 Device9::Present）
    if (self && !g_swapSlot)
        RenderFrame(self);
    return g_origPresent ? g_origPresent(self, src, dst, hwnd, data) : S_OK;
}

static HRESULT WINAPI HookSwapPresent(IDirect3DSwapChain9* self, const RECT* src,
                                      const RECT* dst, HWND hwnd, RGNDATA* data)
{
    InterlockedIncrement(&g_cSwapPresent);
    if (!g_dev && self)   // 从这里拿设备比 pattern 更可靠
    {
        IDirect3DDevice9* d = nullptr;
        if (SUCCEEDED(self->GetDevice(&d)))
            g_dev = d;
    }
    if (g_dev)
        RenderFrame(g_dev);
    return g_origSwapPresent ? g_origSwapPresent(self, src, dst, hwnd, data) : S_OK;
}

static HRESULT WINAPI HookEndScene(IDirect3DDevice9* self)
{
    InterlockedIncrement(&g_cEndScene);
    return g_origEndScene ? g_origEndScene(self) : S_OK;
}

// 记录渲染钩子计数，由 tick 线程调（每 5 秒一次）
static void LogStats(const char* tag)
{
    Log("selfrender[%s]: present=%ld swappresent=%ld endscene=%ld drawn=%ld ready=%d "
        "fail=%ld atlasFile=%ld tex=%p",
        tag, (long)g_cPresent, (long)g_cSwapPresent, (long)g_cEndScene, (long)g_cDrawn,
        (int)g_ready, (long)g_cReadyFail, (long)g_atlasFile, (void*)g_tex);
}

// device discovery: the game keeps IDirect3DDevice9* in a global; FusionFix's
// CE-verified pattern locates the `cmp [g_device],0` instruction that references it.
static IDirect3DDevice9** g_ppDev = nullptr;

static void FindDeviceGlobal(uint8_t* base, const IMAGE_NT_HEADERS* nt)
{
    static const char* kPats[] = {
        "83 3D ? ? ? ? ? 74 17 8B 4D 14",
        "83 3D ? ? ? ? ? 74 15 8B 44 24 1C",
        "83 3D ? ? ? ? ? 74 EF",
    };
    auto* sec = IMAGE_FIRST_SECTION(nt);
    for (int pi = 0; pi < 3 && !g_ppDev; ++pi)
        for (int i = 0; i < nt->FileHeader.NumberOfSections && !g_ppDev; ++i)
        {
            uint8_t* p = base + sec[i].VirtualAddress;
            size_t sz = sec[i].Misc.VirtualSize;
            uint8_t* hit = pat::find(p, sz, kPats[pi], 0);
            if (hit)
                g_ppDev = *(IDirect3DDevice9***)(hit + 2);
        }
}

static void TryHookPresent()
{
    if (g_presentSlot || !g_ppDev)
        return;
    IDirect3DDevice9* dev = *g_ppDev;
    if (!dev)
        return;
    void** vt = *(void***)dev;
    g_origPresent = (HRESULT(WINAPI*)(IDirect3DDevice9*, const RECT*, const RECT*, HWND,
                                      RGNDATA*))vt[17];
    g_presentSlot = &vt[17];
    DWORD old = 0;
    if (VirtualProtect(g_presentSlot, sizeof(void*), PAGE_EXECUTE_READWRITE, &old))
    {
        *g_presentSlot = (void*)&HookPresent;
        VirtualProtect(g_presentSlot, sizeof(void*), old, &old);
        Log("selfrender: Present hooked (device=%p)", (void*)dev);
    }

    // 顺带把 SwapChain9::Present(3) 和 EndScene(**22**) 也钩上。
    // 实测：这个游戏**不走** Device9::Present(17)（present=0），而是直接
    // `swapChain->Present()`（每秒 60 次）；EndScene 是 vtable 22，不是 42
    // （42 是 SetClipStatus，之前钩错了也能“每秒 60 次”，很骗人）。
    if (VirtualProtect(vt, 43 * sizeof(void*), PAGE_EXECUTE_READWRITE, &old))
    {
        g_origEndScene = (HRESULT(WINAPI*)(IDirect3DDevice9*))vt[22];
        g_endSceneSlot = &vt[22];
        *g_endSceneSlot = (void*)&HookEndScene;
        VirtualProtect(vt, 43 * sizeof(void*), old, &old);
        Log("selfrender: EndScene hooked (orig=%p)", (void*)g_origEndScene);
    }
    g_dev = dev;
    IDirect3DSwapChain9* sc = nullptr;
    if (SUCCEEDED(dev->GetSwapChain(0, &sc)) && sc)
    {
        void** svt = *(void***)sc;
        if (VirtualProtect(&svt[3], sizeof(void*), PAGE_EXECUTE_READWRITE, &old))
        {
            g_origSwapPresent = (HRESULT(WINAPI*)(IDirect3DSwapChain9*, const RECT*,
                                                  const RECT*, HWND, RGNDATA*))svt[3];
            g_swapSlot = &svt[3];
            *g_swapSlot = (void*)&HookSwapPresent;
            VirtualProtect(&svt[3], sizeof(void*), old, &old);
            Log("selfrender: SwapChain Present hooked (orig=%p)", (void*)g_origSwapPresent);
        }
        sc->Release();
    }
    else
        Log("selfrender: GetSwapChain FAILED");
}

static void InitDeviceScan(uint8_t* exeBase)
{
    auto* dos = (IMAGE_DOS_HEADER*)exeBase;
    auto* nt = (IMAGE_NT_HEADERS*)(exeBase + dos->e_lfanew);
    FindDeviceGlobal(exeBase, nt);
    Log("selfrender: device global %p", (void*)g_ppDev);
}

static void PollDevice()   // call from the tick thread
{
    TryHookPresent();
}

// tick 线程：读字库文件（失败原因写进 g_atlasFile，由 ReportStats 打日志）
static void LoadAtlasOnTick()
{
    if (g_atlasFile != 0)
        return;
    if (!LoadAtlas())
    {
        g_atlasFile = 2;
        Log("selfrender: metrics FAILED err=%lu (%s_metrics.bin)", (unsigned long)g_atlasErr,
            g_atlasPath.c_str());
        return;
    }
    DWORD err = 0;
    if (!ReadWholeFile(g_atlasPath + ".bin", g_atlasA8, &err))
    {
        g_atlasFile = 3;
        g_atlasErr = err;
        Log("selfrender: bitmap FAILED err=%lu (%s.bin)", (unsigned long)err,
            g_atlasPath.c_str());
        return;
    }
    if (g_atlasA8.size() != (size_t)g_atlasW * (size_t)g_atlasH)
    {
        g_atlasFile = 3;
        Log("selfrender: bitmap size mismatch (%u vs %ux%u)", (unsigned)g_atlasA8.size(),
            (unsigned)g_atlasW, (unsigned)g_atlasH);
        return;
    }
    g_atlasFile = 1;
    Log("selfrender: atlas ok (%fx%f px=%f glyphs=%u bytes=%u)", g_atlasW, g_atlasH, g_px,
        (unsigned)g_glyphs.size(), (unsigned)g_atlasA8.size());
}

static void ReportStats()   // call from the tick thread（每 5 秒）
{
    LogStats("tick");
}

static void SetAtlasPath(const std::string& basePath)
{
    g_atlasPath = basePath;
}

static int g_lastLoggedLen = -1;

static void SetContent(const char* utf8, bool visible, float x, float y, float maxW,
                       float lineGap, int boxAlpha, float boxPad, float boxH, bool centre,
                       float textScale = 1.0f)
{
    if (g_lastLoggedLen != (int)strlen(utf8))
    {
        g_lastLoggedLen = (int)strlen(utf8);
        Log("selfrender: SetContent len=%d vis=%d", g_lastLoggedLen, (int)visible);
    }
    Content c;
    c.visible = visible;
    c.x = x;
    c.y = y;
    c.maxW = maxW;
    c.lineGap = lineGap;
    c.boxAlpha = boxAlpha;
    c.boxPad = boxPad;
    c.boxH = boxH;
    c.centre = centre;
    c.textScale = textScale > 0 ? textScale : 1.0f;
    c.text = utf8 ? utf8 : "";
    g_content = c;   // small struct + one string; render thread tolerates the race
}

}  // namespace selfrender
