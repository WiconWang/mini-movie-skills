"""TTS Provider Protocol 与公共能力描述。"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Protocol

from ..types import RawSynthesis, TtsProfile, TtsSegment


@dataclass
class ProviderCapabilities:
    native_pause: bool
    tone_tags: bool
    emotion: bool
    pronunciation_dict: bool
    word_timestamps: bool


class TTSProvider(Protocol):
    """所有供应商都必须实现的统一边界。"""

    provider_id: str
    capabilities: ProviderCapabilities

    def compile_text(self, segments: list[TtsSegment]) -> tuple[str, list[int]]:
        """返回供应商文本和边界停顿；Edge 可返回零停顿。"""
        ...

    def synthesize(self, segments: list[TtsSegment], profile: TtsProfile,
                   output_dir: Path) -> RawSynthesis:
        """完整合成一次，并归一化时间轴。"""
        ...
