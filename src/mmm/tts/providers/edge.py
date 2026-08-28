"""微软 Edge TTS 的 dry 供应商适配器。"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from ..types import RawSynthesis, TtsProfile, TtsSegment, WordTiming
from .base import ProviderCapabilities


class EdgeTTSProvider:
    provider_id = "edge"
    capabilities = ProviderCapabilities(
        native_pause=False,
        tone_tags=False,
        emotion=False,
        pronunciation_dict=False,
        word_timestamps=True,
    )

    def compile_text(self, segments: list[TtsSegment]) -> tuple[str, list[int]]:
        """Edge 不支持自定义停顿；停顿由统一 splitter 补齐。"""
        full_text = "\n".join(segment.source_text for segment in segments)
        return full_text, [0] * max(0, len(segments) - 1)

    def synthesize(self, segments: list[TtsSegment], profile: TtsProfile,
                   output_dir: Path) -> RawSynthesis:
        try:
            import edge_tts
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Edge TTS 依赖未安装；请安装项目可选依赖 tts-edge"
            ) from exc

        full_text, manual_pause = self.compile_text(segments)
        rate = f"{(profile.speed - 1.0) * 100:+.0f}%"
        output_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            audio = bytearray()
            timings: list[WordTiming] = []
            try:
                async def stream_once() -> None:
                    communicate = edge_tts.Communicate(
                        full_text,
                        voice=profile.voice,
                        rate=rate,
                        boundary="WordBoundary",
                    )
                    async for chunk in communicate.stream():
                        kind = str(chunk.get("Type") or chunk.get("type") or "").lower()
                        if kind == "audio":
                            data = chunk.get("Data") or chunk.get("data")
                            if data:
                                audio.extend(data)
                        elif "boundary" in kind:
                            text = str(chunk.get("text") or "")
                            offset = float(chunk.get("offset", 0))
                            duration = float(chunk.get("duration", 0))
                            timings.append(WordTiming(
                                text=text,
                                start_ms=offset / 10_000.0,
                                end_ms=(offset + duration) / 10_000.0,
                            ))

                asyncio.run(stream_once())
                if not audio:
                    raise RuntimeError("Edge TTS 未返回音频")
                if not timings:
                    raise RuntimeError("Edge TTS 未返回 WordBoundary")

                raw_path = output_dir / "edge_master.mp3"
                raw_path.write_bytes(audio)
                return RawSynthesis(
                    audio_path=raw_path,
                    timings=timings,
                    provider_text=full_text,
                    warnings=[
                        "Edge 不支持原生停顿、语气词、情绪和发音字典；相关意图已在切分层降级处理。"
                    ],
                    manual_pause_ms=manual_pause,
                    metadata={"attempt": attempt + 1, "raw_format": "mp3"},
                )
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Edge TTS 重试 3 次仍失败: {last_error}")
