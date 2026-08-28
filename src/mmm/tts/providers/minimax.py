"""MiniMax T2A v2 的 prod 供应商适配器。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from ..types import RawSynthesis, TtsProfile, TtsSegment, WordTiming
from .base import ProviderCapabilities


PRICING_PER_10000 = {
    "speech-2.8-hd": 3.5,
    "speech-2.8-turbo": 2.0,
}


def _env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    from ...llm import _load_env_file

    return _load_env_file().get(name, "").strip()


def _pinyin_to_pronunciation(value: str) -> str:
    """把 `an1 bo2` 转成 MiniMax 示例要求的 `(an1)(bo2)`。"""
    syllables = re.findall(r"[a-zA-ZüvÜ]+[1-5]", value)
    if syllables:
        return "".join(f"({item})" for item in syllables)
    return value


class MiniMaxTTSProvider:
    provider_id = "minimax"
    capabilities = ProviderCapabilities(
        native_pause=True,
        tone_tags=True,
        emotion=True,
        pronunciation_dict=True,
        word_timestamps=True,
    )

    def __init__(self, *, base_url: str = "https://api.minimaxi.com"):
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _tone_name(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip().strip("()").lower()
        return cleaned or None

    def compile_text(self, segments: list[TtsSegment]) -> tuple[str, list[int]]:
        pieces: list[str] = []
        pauses: list[int] = []
        for index, segment in enumerate(segments):
            piece = segment.source_text
            tone = self._tone_name(segment.performance.tone)
            if tone:
                piece = f"({tone}){piece}"
            pieces.append(piece)
            if index:
                previous = segments[index - 1].performance
                pauses.append(max(
                    previous.pause_after_ms,
                    segment.performance.pause_before_ms,
                ))

        full_text = pieces[0] if pieces else ""
        for index, pause_ms in enumerate(pauses, start=1):
            # MiniMax 要求停顿夹在两段可发音文本之间，因此只作为连接符插入。
            full_text += f"<#{pause_ms / 1000.0:.2f}#>{pieces[index]}"
        return full_text, [0] * max(0, len(segments) - 1)

    @staticmethod
    def billing_characters(full_text: str) -> int:
        total = 0
        for ch in full_text:
            total += 2 if "\u4e00" <= ch <= "\u9fff" else 1
        return total

    def estimate_cost(self, segments: list[TtsSegment], profile: TtsProfile) -> dict:
        full_text, _ = self.compile_text(segments)
        characters = self.billing_characters(full_text)
        unit = float(profile.options.get(
            "price_per_10000", PRICING_PER_10000.get(profile.model, 3.5)
        ))
        return {
            "currency": "CNY",
            "billing_characters": characters,
            "price_per_10000": unit,
            "amount": characters / 10000.0 * unit,
        }

    def synthesize(self, segments: list[TtsSegment], profile: TtsProfile,
                   output_dir: Path) -> RawSynthesis:
        api_key = _env_value("MMM_MINIMAX_TTS_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 MMM_MINIMAX_TTS_API_KEY（.env 或环境变量）")

        full_text, _ = self.compile_text(segments)
        pronunciations = []
        seen_terms: set[str] = set()
        for segment in segments:
            for rule in segment.pronunciations:
                if rule.term in seen_terms:
                    continue
                seen_terms.add(rule.term)
                pronunciations.append(
                    f"{rule.term}/{_pinyin_to_pronunciation(rule.pinyin)}"
                )

        payload: dict = {
            "model": profile.model,
            "text": full_text,
            "stream": False,
            "voice_setting": {
                "voice_id": profile.voice,
                "speed": profile.speed,
                "vol": 1.0,
                "pitch": int(profile.options.get("pitch", 0)),
            },
            "audio_setting": {
                "sample_rate": int(profile.options.get("sample_rate", 44100)),
                "bitrate": int(profile.options.get("bitrate", 128000)),
                "format": profile.options.get("format", "wav"),
                "channel": 1,
            },
            "subtitle_enable": True,
            "subtitle_type": "word",
            "output_format": "url",
        }
        if profile.emotion:
            payload["voice_setting"]["emotion"] = profile.emotion
        if pronunciations:
            payload["pronunciation_dict"] = {"tone": pronunciations}

        output_dir.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            f"{self.base_url}/v1/t2a_v2",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=float(profile.options.get("timeout_seconds", 300))
            ) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            detail = detail.replace(api_key, "***")
            raise RuntimeError(f"MiniMax TTS HTTP {exc.code}: {detail}") from exc

        base_resp = body.get("base_resp") or {}
        if base_resp.get("status_code") != 0:
            raise RuntimeError(f"MiniMax TTS 业务失败: {base_resp}")
        data = body.get("data") or {}
        audio_value = data.get("audio")
        subtitle_url = data.get("subtitle_file")
        if not audio_value or not subtitle_url:
            raise RuntimeError(f"MiniMax TTS 返回缺少音频或字幕: {body.get('trace_id', '')}")

        raw_path = output_dir / "minimax_master.wav"
        if re.match(r"^https?://", audio_value):
            with urllib.request.urlopen(
                audio_value,
                timeout=float(profile.options.get("download_timeout_seconds", 120)),
            ) as response:
                raw_path.write_bytes(response.read())
        else:
            raw_path.write_bytes(bytes.fromhex(audio_value))

        subtitle_path = output_dir / "minimax_subtitle.json"
        with urllib.request.urlopen(
            subtitle_url,
            timeout=float(profile.options.get("download_timeout_seconds", 120)),
        ) as response:
            subtitle_path.write_bytes(response.read())

        subtitle = json.loads(subtitle_path.read_text(encoding="utf-8"))
        timings = self._parse_subtitle_timings(subtitle)
        if not timings:
            raise RuntimeError("MiniMax TTS 字幕 JSON 中没有时间戳词")
        return RawSynthesis(
            audio_path=raw_path,
            timings=timings,
            provider_text=full_text,
            warnings=[],
            manual_pause_ms=[0] * max(0, len(segments) - 1),
            metadata={
                "trace_id": body.get("trace_id", ""),
                "extra_info": body.get("extra_info") or {},
                "subtitle_path": str(subtitle_path),
            },
        )

    @staticmethod
    def _parse_subtitle_timings(subtitle: object) -> list[WordTiming]:
        """解析词级时间轴，并修平 MiniMax 发音替换造成的重复词。

        语气词标签会作为 `(gasps)` 这类 word 返回，但它不是 source_text 的内容；
        发音替换可能把一个原文词拆成多条相同 word 的时间项。这里跳过前者、合并后者，
        让统一 aligner 始终面对干净的原文词流。
        """
        entries = subtitle if isinstance(subtitle, list) else subtitle.get("segments", [])
        merged: list[WordTiming] = []
        last_raw: dict | None = None
        for segment in entries:
            for raw_word in (segment.get("timestamped_words") or []):
                text = str(raw_word.get("word", ""))
                start_ms = float(raw_word.get("time_begin", 0))
                end_ms = float(raw_word.get("time_end", 0))
                if re.fullmatch(r"\([a-z][a-z-]*\)", text.strip().lower()):
                    continue
                if (
                    last_raw is not None
                    and last_raw.get("word") == raw_word.get("word")
                    and last_raw.get("word_begin") == raw_word.get("word_begin")
                    and last_raw.get("word_end") == raw_word.get("word_end")
                ):
                    previous = merged[-1]
                    merged[-1] = WordTiming(
                        text=text,
                        start_ms=previous.start_ms,
                        end_ms=max(previous.end_ms, end_ms),
                    )
                    last_raw = raw_word
                    continue
                timing = WordTiming(text=text, start_ms=start_ms, end_ms=end_ms)
                merged.append(timing)
                last_raw = raw_word
        return merged
