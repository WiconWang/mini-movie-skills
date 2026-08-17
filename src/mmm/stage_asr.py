"""阶段2 集成：ASR 转录 → 台词对齐 → lines.json。

ASR 职责（设计文档）：只当钟用不当文本源——取词级时间戳，文本弃用。
实测选型：faster-whisper small（4.7 倍实时，本机 CPU int8）。
"""

from __future__ import annotations

import json
from pathlib import Path

ASR_MODEL_SIZE = "medium"    # 实测定型：medium+VAD 25倍实时无幻觉；small 4.7倍但幻觉严重


def transcribe_words(video: Path, model_size: str = ASR_MODEL_SIZE) -> list[dict]:
    """视频 → 词级时间戳列表 [{text, start, end}]。

    实测定型配方：vad_filter 过滤音乐/静音，condition_on_previous_text=False 防幻觉连锁。
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video), language="zh", word_timestamps=True,
                                   condition_on_previous_text=False, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"text": w.word, "start": round(w.start, 3), "end": round(w.end, 3)})
    return words


def load_script(script_path: Path) -> list[dict]:
    """读取物料规范的 script.jsonl。"""
    lines = []
    for i, raw in enumerate(script_path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"{script_path.name} 第{i}行 JSON 解析失败: {e}")
        if not obj.get("text"):
            raise ValueError(f"{script_path.name} 第{i}行 text 为空")
        lines.append({"text": obj["text"], "speaker": obj.get("speaker")})
    return lines


def run(video: Path, script: Path, out_dir: Path, model_size: str = ASR_MODEL_SIZE) -> dict:
    """执行阶段2：ASR → 对齐 → asr.json / lines.json，返回覆盖率报告。"""
    from .stage_align import AsrWord, align

    out_dir.mkdir(parents=True, exist_ok=True)

    words = transcribe_words(video, model_size)
    (out_dir / "asr.json").write_text(
        json.dumps({"video": video.name, "model": model_size, "words": words},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    script_lines = load_script(script)
    asr_words = [AsrWord(text=w["text"], start=w["start"], end=w["end"]) for w in words]
    result = align(script_lines, asr_words)

    (out_dir / "lines.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result["report"]
