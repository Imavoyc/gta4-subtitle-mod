"""faster-whisper 转写。第 4 个参数 `--words` 时额外输出**词级时间戳**。

词级时间戳的用途：qwen3-asr 文本更准但没有时间轴，用 whisper 的词时间做骨架，
把 qwen 的词按序列对齐上去（见 reasr_srt.py --words）。

    transcribe.py in16k.wav small out.json [--words]
"""
import json
import sys
import wave

import numpy as np
from faster_whisper import WhisperModel

wav, model_name, out = sys.argv[1], sys.argv[2], sys.argv[3]
want_words = "--words" in sys.argv[4:]

# read 16k mono s16 wav ourselves -> float32 numpy (avoids PyAV entirely)
w = wave.open(wav, "rb")
assert w.getframerate() == 16000 and w.getnchannels() == 1, (w.getframerate(), w.getnchannels())
raw = w.readframes(w.getnframes())
audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
w.close()

model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=8)
segments, info = model.transcribe(audio, language=None, vad_filter=True, beam_size=5,
                                  word_timestamps=want_words)
segs = []
for s in segments:
    seg = {"start": s.start, "end": s.end, "text": s.text.strip()}
    if want_words:
        seg["words"] = [{"s": wd.start, "e": wd.end, "w": wd.word} for wd in (s.words or [])]
    segs.append(seg)
json.dump({"language": info.language, "duration": info.duration, "segments": segs},
          open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"lang={info.language} dur={info.duration:.1f}s segments={len(segs)}"
      f"{' words=yes' if want_words else ''}")
