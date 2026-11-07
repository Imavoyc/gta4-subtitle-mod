/* IVAUD（GTA IV 电台段，IMA ADPCM）→ 原始 PCM（s16le，通道顺序拼接）。
 *
 * 为什么用 C：973 个段 / 约 5000 个通道，Python 版 `ivaud2wav.py` 逐 nibble
 * 解释执行太慢（做音频指纹搜索时不可用）。这个是同一算法的 C 版，快两个数量级。
 *
 * 用法：
 *   ivaud2pcm <段文件> <out.raw>          # 全部通道，按顺序拼接
 *   ivaud2pcm <段文件> -                    # 写到 stdout
 *   ivaud2pcm <段文件> <out.raw> --ch 3     # 只解一个通道
 *   ivaud2pcm <段文件> - --info             # 只打印头信息
 *
 * 容器（见 docs/gta4-radio.md）：
 *   u32 @0x08 通道数  u32 @0x0C 每通道字节数(509952)  u32 @0x14 采样率kHz  u32 @0x2C 数据偏移
 *   校验：offset + ch*bpc == filesize（对不上就报错退出，另有头部变体本工具不认）
 *
 * 编译： gcc -O2 -o ivaud2pcm ivaud2pcm.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static const int STEP[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487,
    12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};
static const int IDX[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};

static uint32_t rd32(const uint8_t* p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static int decode_channel(const uint8_t* data, long n, int16_t* out)
{
    if (n < 4) return 0;
    int pred = (int16_t)((uint16_t)data[0] | (uint16_t)data[1] << 8);
    int index = data[2];
    long k = 0;
    for (long i = 4; i < n; ++i)
    {
        uint8_t b = data[i];
        for (int half = 0; half < 2; ++half)
        {
            int nib = half ? (b & 0xF) : (b >> 4);
            int step = STEP[index];
            int diff = step >> 3;
            if (nib & 4) diff += step;
            if (nib & 2) diff += step >> 1;
            if (nib & 1) diff += step >> 2;
            pred += (nib & 8) ? -diff : diff;
            if (pred > 32767) pred = 32767;
            if (pred < -32768) pred = -32768;
            index += IDX[nib];
            if (index < 0) index = 0;
            if (index > 88) index = 88;
            out[k++] = (int16_t)pred;
        }
    }
    return (int)k;
}

int main(int argc, char** argv)
{
    if (argc < 3)
    {
        fprintf(stderr, "usage: %s <segment> <out.raw|-> [--ch N] [--info]\n", argv[0]);
        return 2;
    }
    int only_ch = -1, info = 0;
    for (int i = 3; i < argc; ++i)
    {
        if (!strcmp(argv[i], "--ch") && i + 1 < argc) only_ch = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--info")) info = 1;
    }
    FILE* f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 1; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size < 0x40) { fprintf(stderr, "too small\n"); return 1; }
    uint8_t* buf = malloc(size);
    if (fread(buf, 1, size, f) != (size_t)size) { fprintf(stderr, "read fail\n"); return 1; }
    fclose(f);

    uint32_t ch = rd32(buf + 8), bpc = rd32(buf + 0x0C), khz = rd32(buf + 0x14), off = rd32(buf + 0x2C);
    if (info)
    {
        printf("ch=%u bpc=%u khz=%u off=0x%x size=%ld check=%s\n", ch, bpc, khz, off, size,
               (off + (long)ch * bpc == size) ? "ok" : "MISMATCH");
        free(buf);
        return 0;
    }
    if (!ch || ch > 400 || bpc != 509952 || off + (long)ch * bpc != size)
    {
        fprintf(stderr, "unsupported container\n");
        return 3;
    }
    FILE* o = (strcmp(argv[2], "-") == 0) ? stdout : fopen(argv[2], "wb");
    if (!o) { perror(argv[2]); return 1; }
    int16_t* pcm = malloc((size_t)bpc * 2 * sizeof(int16_t));
    for (uint32_t c = 0; c < ch; ++c)
    {
        if (only_ch >= 0 && (uint32_t)only_ch != c) continue;
        int n = decode_channel(buf + off + (long)c * bpc, bpc, pcm);
        fwrite(pcm, 2, (size_t)n, o);
    }
    if (o != stdout) fclose(o);
    free(pcm);
    free(buf);
    return 0;
}
