"""TTS 适配层的统一数据契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PronunciationRule:
    """供应商无关的发音修正；字段名沿用常见拼音标注习惯。"""

    term: str
    pinyin: str
    note: str = ""


@dataclass(frozen=True)
class TtsPerformance:
    """语音表演意图；不包含任何供应商专属语法。"""

    pause_before_ms: int = 0
    pause_after_ms: int = 0
    tone: str | None = None
    emotion: str | None = None
    speed_hint: float = 1.0


@dataclass(frozen=True)
class TtsSegment:
    """EDL 中一个 narration_clip 对应的待合成片段。"""

    index: int
    narration_id: int
    source_text: str
    performance: TtsPerformance
    pronunciations: list[PronunciationRule] = field(default_factory=list)


@dataclass(frozen=True)
class TtsProfile:
    """解析后的执行配置；主流程只认识这个对象。"""

    mode: str
    provider: str
    model: str
    voice: str
    speed: float = 1.0
    emotion: str | None = None
    options: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        raw = json.dumps({
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "speed": self.speed,
            "emotion": self.emotion,
            "options": self.options,
        }, ensure_ascii=False, sort_keys=True)
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WordTiming:
    """供应商原始时间边界归一化后的最小单位。"""

    text: str
    start_ms: float
    end_ms: float


@dataclass
class RawSynthesis:
    """供应商返回的未切分音频和归一化时间轴。"""

    audio_path: Path
    timings: list[WordTiming]
    provider_text: str
    warnings: list[str]
    manual_pause_ms: list[int]
    metadata: dict


@dataclass(frozen=True)
class SegmentSpan:
    """一个 TtsSegment 在完整音频中的实际发声区间。"""

    index: int
    narration_id: int
    start_ms: float
    end_ms: float


@dataclass
class TtsArtifact:
    """切分后交给渲染器和剪映导出器的统一产物。"""

    index: int
    narration_id: int
    source_text: str
    wav_path: Path
    duration_s: float
    speech_start_ms: float
    speech_end_ms: float


def canonical_json(value: object) -> str:
    """生成稳定 JSON，用于指纹和落盘。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_object(value: object) -> str:
    raw = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def dataclasses_to_dict(value: object) -> object:
    """递归转换 dataclass，便于产物 JSON 稳定落盘。"""
    if hasattr(value, "__dataclass_fields__"):
        return {key: dataclasses_to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [dataclasses_to_dict(item) for item in value]
    if isinstance(value, tuple):
        return [dataclasses_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: dataclasses_to_dict(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value
