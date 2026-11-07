# Reverse-engineering notes (GTA IV 1.2.0.59 / Complete Edition)

Everything here was found by static analysis (Ghidra) plus live memory probing on a running
process. Addresses are **runtime** addresses (the PE is loaded at `0x800000`, i.e. file VAs are
`−0x400000`; an external probe on Linux can read `/proc/<pid>/mem` directly).

## 1. Drawing over the game

* The game does **not** call `IDirect3DDevice9::Present`; it calls
  `IDirect3DSwapChain9::Present` (vtable slot 3). `EndScene` is vtable **22** — slot 42 is
  `SetClipStatus` and also ticks at 60 Hz, which is an easy trap.
* Hooking the swapchain `Present` gives a per-frame callback with a valid device (via
  `GetDevice`) plus a back buffer to draw into. Text is rendered as one big glyph atlas
  (baked bitmap + per-glyph metrics) with `DrawPrimitiveUP`.
* Do not `fopen`/`fprintf` from the render thread — that silently fails there. File I/O for
  hot reloading belongs to the game's own frame thread.

## 2. TV subtitles

`binkw32`'s `BinkOpen` / `BinkDoFrame` are reached through the game's IAT; wrapping them gives
the video name (→ which channel) and the frame counter (→ the timeline). Installing hooks by
patching IAT slots avoids needing a trampoline.

Caveat: after a video stops, the bink frame counter freezes, so cues keep matching — gate the
cue lookup on "the video is really playing right now".

## 3. Radio: identity + position

`CRadioStation` is `0x1934` bytes with the station name inline at `+0x17FC`, and the whole set
of stations is reachable from a global pointer array at **`0x011D76AC`** (23 entries).

Each station owns **two playback slots** of `0xBD0` bytes (`+0x000` and `+0xBD0`), i.e. a
double buffer for crossfading. The current slot is a byte at `+0x1917`. Inside a slot:

| offset | meaning |
|---|---|
| `+0x960` | pointer to an 8-byte record `{id, hash}` — `hash` is the segment's resource hash |
| `+0x964` | audio stream handle (filled by the engine) |
| `+0x990` | **elapsed milliseconds inside the segment** (resets on segment change) |
| `+0x994` | segment length in ms (**not** the audio file's length) |
| `+0xBC0` | mode byte (2 = normal segment) |
| `+0xBC4` | record index inside the node (changes on segment change) |
| `+0xBC8` | state (0 idle / 1 started / 3 finished) |
| `+0xBCA` | slot busy |
| `+0xBBC` | subtracted from `len` when `+0xBCC != 0` |

Guards worth keeping: the `+0x17FC` name is **not** unique — the same strings exist in a name
pool in the heap, so validate a candidate object by also reading its descriptor pointer
(`+0x17D0` → name length at `+0x1D`, name at `+0x1E`) and requiring the two names to match.

### resource hash

`hash = joaat("<rpf base name>_<file name>", lower case)`, e.g.
`joaat("radio_wktt_1_unjust_1")`. Verified by brute-forcing the string template against ~40
values observed live (35 hits, zero false positives; the rest are the `0x........`-named TOC
entries of the megamix stations).

### time axis (the subtle part)

A segment's audio file is a container of `n` **independent 21.248 s mono clips** (IMA ADPCM,
4-byte state header per clip). The engine squeezes the whole file into its own `len`:

```
file_time_ms ≈ ms × (n × 21248) / len_ms        # scale was 1.35 … 1.44× on tested segments
```

Verified twice by transcribing live audio and locating unique sentences in the per-clip ASR
transcripts (the observed clip index matched `ms·n/len` to within a few percent). Using `ms`
as file time directly makes subtitles drift by tens of seconds on long shows — that was the
main "radio subtitles don't match" bug.

There is a second candidate model (each clip consumed for `len/n` ms at natural speed), kept
behind `RadioTimeMode=1` in the ini; which one is correct is still being judged by ear.

## 4. Things that do NOT work (don't waste time)

* **Cross-correlating the played audio with the decoded clips.** The engine's radio path
  applies EQ/compression (and probably a car-interior reverb), so waveform correlation sits at
  noise level (0.006–0.06) even though the speech is perfectly intelligible. Verify by ASR +
  text matching instead.
* `GET_PLAYER_RADIO_MODE` is always 0; `GET_PLAYER_RADIO_STATION_NAME` stays non-empty after
  leaving the car (gate on `IS_CHAR_IN_ANY_CAR`).
* Scanning memory for "objects whose name is at `+0x17FC`" produces mostly false positives from
  the name pool — use the registry at `0x011D76AC`.
* `parec` (monitor capture) does not finalise the WAV header when killed: patch the RIFF/data
  sizes before reading, and never run two instances writing the same file.

## 5. Tooling notes

* Radio "segments" are RPF files; `pipeline/ivaud2pcm.c` decodes them ~100× faster than the
  Python version (useful when fingerprinting thousands of clips).
* `pipeline/radio_mem_probe.py` reads all of the above from outside the process, so
  experiments do not need a rebuild + restart of the game.
