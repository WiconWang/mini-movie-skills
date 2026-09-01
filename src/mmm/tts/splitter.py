"""完整音频到片段级 WAV 的统一切分器。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..media import ffmpeg_bin, ffprobe_bin
from .types import RawSynthesis, SegmentSpan, TtsSegment, TtsArtifact


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"TTS 音频切分失败: {' '.join(cmd[:8])}...\n{detail}")


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [ffprobe_bin(), "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _cut_points(spans: list[SegmentSpan], audio_duration_ms: float) -> list[tuple[float, float]]:
    """句间静音优先归前一句，防止下一句 WAV 从静音开始导致字幕提前。"""
    points: list[tuple[float, float]] = []
    lead_ms = 30.0
    guard_ms = 12.0

    start = max(0.0, spans[0].start_ms - lead_ms)
    for index, span in enumerate(spans):
        if index == len(spans) - 1:
            end = max(audio_duration_ms, span.end_ms)
        else:
            next_start = spans[index + 1].start_ms
            end = max(span.end_ms + guard_ms, next_start - lead_ms)
            end = min(end, max(span.end_ms + guard_ms, next_start))
        end = max(end, start + 0.05)
        points.append((start, end))
        start = end
    return points


def split_master_audio(master: Path, raw: RawSynthesis,
                       segments: list[TtsSegment], spans: list[SegmentSpan],
                       output_dir: Path) -> list[TtsArtifact]:
    """把供应商完整音频切成现有渲染器可消费的 tts_XXX.wav。"""
    if len(segments) != len(spans):
        raise ValueError("TTS 片段数与时间轴数量不一致")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_duration_ms = _probe_duration(master) * 1000.0
    cuts = _cut_points(spans, audio_duration_ms)
    artifacts: list[TtsArtifact] = []

    for segment, span, (start_ms, end_ms) in zip(segments, spans, cuts):
        start_s = start_ms / 1000.0
        end_s = end_ms / 1000.0
        wav_path = output_dir / f"sent_{segment.index:03d}.wav"
        duration_s = end_s - start_s
        # Edge 没有原生停顿能力；由统一切分器补齐 manifest 声明的句间停顿。
        manual_pause_s = 0.0
        if segment.index < len(raw.manual_pause_ms):
            manual_pause_s = max(0.0, raw.manual_pause_ms[segment.index] / 1000.0)
        target_duration_s = duration_s + manual_pause_s
        afilters = [f"apad=whole_dur={target_duration_s:.3f}"]
        fade_out_start = max(0.0, duration_s - 0.018)
        afilters.append(f"afade=t=out:st={fade_out_start:.3f}:d=0.018")

        _run([
            ffmpeg_bin(), "-y", "-v", "error",
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
            "-i", str(master),
            "-af", ",".join(afilters),
            "-ar", "48000", "-ac", "1",
            "-c:a", "pcm_s16le",
            str(wav_path),
        ])
        artifacts.append(TtsArtifact(
            index=segment.index,
            narration_id=segment.narration_id,
            source_text=segment.source_text,
            wav_path=wav_path,
            duration_s=_probe_duration(wav_path),
            speech_start_ms=span.start_ms,
            speech_end_ms=span.end_ms,
        ))
    return artifacts
