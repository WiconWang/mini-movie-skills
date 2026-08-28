"""manifest 与供应商时间轴的统一对齐逻辑。"""

from __future__ import annotations

import re
import unicodedata

from .types import SegmentSpan, TtsSegment, WordTiming


def normalize_for_alignment(text: str) -> str:
    """去掉标点和空白，只保留会实际发音的字/字母/数字。"""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in normalized
        if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "S"))
    )


def align_segments(segments: list[TtsSegment],
                   timings: list[WordTiming]) -> list[SegmentSpan]:
    """按顺序消耗词级时间轴，返回每个语义片段的实际发声区间。

    这里刻意不使用固定字数估算：Edge 的中文边界可能是词，MiniMax 会返回标点。
    两个供应商都先归一化成“可发音字符流”，再按 manifest 顺序消费。
    """
    spans: list[SegmentSpan] = []
    cursor = 0
    for segment in segments:
        expected = normalize_for_alignment(segment.source_text)
        if not expected:
            raise ValueError(f"narration_id={segment.narration_id} 没有可发音文本")

        consumed = 0
        start_ms: float | None = None
        end_ms: float | None = None
        while cursor < len(timings) and consumed < len(expected):
            timing = timings[cursor]
            timing_text = normalize_for_alignment(timing.text)
            cursor += 1
            if not timing_text:
                continue

            remaining = expected[consumed:]
            if timing_text == remaining[:len(timing_text)]:
                consumed += len(timing_text)
            elif remaining.startswith(timing_text[:len(remaining)]):
                # 理论上不应跨语义片段；这里保底消费整词并让校验失败。
                consumed += len(remaining)
            else:
                raise ValueError(
                    "TTS 时间轴与解说文本不一致："
                    f"narration_id={segment.narration_id} 期望 {remaining[:12]!r}，"
                    f"实际 {timing_text[:12]!r}"
                )
            if start_ms is None:
                start_ms = timing.start_ms
            end_ms = timing.end_ms

        if consumed != len(expected):
            raise ValueError(
                f"TTS 时间轴提前结束：narration_id={segment.narration_id}，"
                f"匹配 {consumed}/{len(expected)} 个可发音字符"
            )
        if start_ms is None or end_ms is None or end_ms < start_ms:
            raise ValueError(f"narration_id={segment.narration_id} 时间区间无效")
        spans.append(SegmentSpan(
            index=segment.index,
            narration_id=segment.narration_id,
            start_ms=start_ms,
            end_ms=end_ms,
        ))

    for left, right in zip(spans, spans[1:]):
        if right.start_ms < left.start_ms:
            raise ValueError("TTS 时间轴顺序与 EDL 不一致")
    return spans


def is_mostly_punctuation(text: str) -> bool:
    return not normalize_for_alignment(text)


def clamp_pause_ms(value: object, maximum_ms: int = 2000) -> int:
    """限制 LLM 给出的停顿，避免异常输出放大成本和时长。"""
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, maximum_ms))


PUNCTUATION_RE = re.compile(r"\s+")
