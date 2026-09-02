"""统一 TTS 适配层、LLM 边界与时间轴对齐测试。"""

from __future__ import annotations

import sys
import unittest
import json
import tempfile
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmm.tts.align import align_segments, normalize_for_alignment
from mmm.tts.providers.minimax import MiniMaxTTSProvider
from mmm.tts.runtime import resolve_profile
from mmm.tts.types import PronunciationRule, TtsPerformance, TtsSegment


def make_segments() -> list[TtsSegment]:
    return [
        TtsSegment(0, 1, "城里的线索断了，", TtsPerformance(pause_after_ms=350)),
        TtsSegment(1, 2, "派蒙提议去找图书管理员丽莎。", TtsPerformance()),
    ]


class ResolveProfileTests(unittest.TestCase):
    def test_legacy_edge_config_maps_to_dry(self):
        profile = resolve_profile({"engine": "edge", "voice": "v", "speed": 1.1})
        self.assertEqual(profile.mode, "dry")
        self.assertEqual(profile.provider, "edge")
        self.assertEqual(profile.voice, "v")

    def test_new_prod_config_uses_minimax_defaults(self):
        profile = resolve_profile({
            "profile": "prod",
            "prod": {
                "provider": "minimax",
                "model": "speech-2.8-hd",
                "voice_id": "test-voice",
                "speed": 1.1,
                "emotion": "calm",
            },
        })
        self.assertEqual(profile.provider, "minimax")
        self.assertEqual(profile.model, "speech-2.8-hd")
        self.assertEqual(profile.voice, "test-voice")

    def test_dry_provider_cannot_be_replaced(self):
        with self.assertRaisesRegex(ValueError, "dry profile 固定使用 edge"):
            resolve_profile({"profile": "dry", "dry": {"provider": "minimax"}})


class LlmBoundaryTests(unittest.TestCase):
    def test_llm_segment_parser_rejects_text_change(self):
        from mmm.tts.runtime import _parse_llm_segments

        clips = [{"index": 0, "narration_id": 1, "text": "终稿原文。"}]
        raw = {"segments": [{
            "index": 0,
            "narration_id": 1,
            "source_text": "被修改的原文。",
            "performance": {},
            "pronunciations": [],
        }]}
        with self.assertRaisesRegex(ValueError, "试图修改"):
            _parse_llm_segments(raw, clips, resolve_profile({}), 2000)

    def test_llm_segment_parser_accepts_pronunciation_and_clamps_pause(self):
        from mmm.tts.runtime import _parse_llm_segments

        clips = [{
            "index": 0,
            "narration_id": 1,
            "text": "安柏在蒙德城。",
        }]
        raw = {"segments": [{
            "index": 0,
            "narration_id": 1,
            "source_text": "安柏在蒙德城。",
            "performance": {
                "pause_before_ms": 99999,
                "pause_after_ms": 350,
                "tone": "breath",
                "emotion": "sad",
                "speed_hint": 1.0,
            },
            "pronunciations": [
                {"term": "安柏", "pinyin": "an1 bo2", "note": "原神角色"}
            ],
        }]}
        segments = _parse_llm_segments(raw, clips, resolve_profile({}), 2000)
        self.assertEqual(segments[0].performance.pause_before_ms, 2000)
        self.assertEqual(segments[0].performance.pause_after_ms, 350)
        self.assertEqual(segments[0].pronunciations[0].pinyin, "an1 bo2")


class GlossaryTests(unittest.TestCase):
    def test_series_glossary_loaded_with_common_and_version_terms(self):
        from mmm.tts.glossary import load_series_pronunciations

        rules = load_series_pronunciations("原神", "1.4")
        by_term = {rule.term: rule for rule in rules}
        self.assertEqual(by_term["安柏"].pinyin, "an1 bo2")
        self.assertEqual(by_term["蒙德"].pinyin, "meng2 de2")
        self.assertEqual(by_term["丽莎"].pinyin, "li4 sha1")
        self.assertEqual(by_term["蒂玛乌斯"].pinyin, "di4 ma3 wu1 si1")
        self.assertNotIn("贝雅特丽奇", by_term)

    def test_apply_glossary_overrides_llm_by_glossary(self):
        from mmm.tts.runtime import _apply_glossary

        segment = TtsSegment(
            0, 1, "安柏和蒙德来了。", TtsPerformance(),
            [PronunciationRule("安柏", "an1 bo2")],
        )
        units = [{"index": 0, "narration_id": 1, "source_text": "安柏和蒙德来了。"}]
        glossary = [
            PronunciationRule("安柏", "an1 bo3"),
            PronunciationRule("蒙德", "meng2 de2"),
        ]
        result = _apply_glossary([segment], units, glossary)
        rules = {rule.term: rule for rule in result[0].pronunciations}
        self.assertEqual(rules["安柏"].pinyin, "an1 bo3")
        self.assertEqual(rules["蒙德"].pinyin, "meng2 de2")


class SentenceSplitTests(unittest.TestCase):
    def test_split_sentences_keeps_punctuation_and_quotes(self):
        from mmm.tts.runtime import split_sentences

        text = "气氛不对了。她问，真的吗？他喊，太过分了！信上写着……“答案”。"
        self.assertEqual(split_sentences(text), [
            "气氛不对了。",
            "她问，真的吗？",
            "他喊，太过分了！",
            "信上写着……“答案”。",
        ])


class AlignmentTests(unittest.TestCase):
    def test_alignment_consumes_edge_like_words_without_punctuation(self):
        segments = make_segments()
        timings = [
            self.timing("城里", 100, 475),
            self.timing("的", 475, 562),
            self.timing("线索", 575, 925),
            self.timing("断了", 937, 1350),
            self.timing("派蒙", 1587, 1937),
            self.timing("提议", 1950, 2412),
            self.timing("去找", 2525, 2862),
            self.timing("图书", 2875, 3200),
            self.timing("管理员", 3212, 3662),
            self.timing("丽莎", 3675, 4087),
        ]
        spans = align_segments(segments, timings)
        self.assertEqual(spans[0].start_ms, 100)
        self.assertEqual(spans[0].end_ms, 1350)
        self.assertEqual(spans[1].start_ms, 1587)
        self.assertEqual(spans[1].end_ms, 4087)

    def test_alignment_rejects_mismatched_text(self):
        segments = make_segments()
        timings = [self.timing("错误", 100, 200)]
        with self.assertRaisesRegex(ValueError, "不一致"):
            align_segments(segments, timings)

    @staticmethod
    def timing(text: str, start: int, end: int):
        from mmm.tts.types import WordTiming

        return WordTiming(text=text, start_ms=start, end_ms=end)


class MiniMaxCompileTests(unittest.TestCase):
    def test_compile_inserts_native_pause_between_segments(self):
        provider = MiniMaxTTSProvider()
        full_text, manual_pause = provider.compile_text(make_segments())
        self.assertIn("，<#0.35#>派蒙", full_text)
        self.assertEqual(manual_pause, [0])

    def test_pronunciation_rules_are_converted_to_dictionary_format(self):
        provider = MiniMaxTTSProvider()
        segments = [TtsSegment(
            0, 1, "安柏来了。",
            TtsPerformance(),
            [PronunciationRule("安柏", "an1 bo2", "原神角色")],
        )]
        full_text, _ = provider.compile_text(segments)
        self.assertEqual(full_text, "安柏来了。")
        cost = provider.estimate_cost(segments, resolve_profile({
            "profile": "prod",
            "prod": {
                "provider": "minimax",
                "model": "speech-2.8-hd",
                "voice_id": "v",
            },
        }))
        self.assertGreater(cost["billing_characters"], 0)

    def test_subtitle_parser_skips_tone_tags_and_merges_pronunciation_chunks(self):
        subtitle = [{
            "text": "(gasps)安柏来了。",
            "timestamped_words": [
                {"word": "(gasps)", "word_begin": 0, "word_end": 7,
                 "time_begin": 10, "time_end": 20},
                {"word": "安柏", "word_begin": 7, "word_end": 9,
                 "pronounce_word": "ㄅ", "time_begin": 20, "time_end": 30},
                {"word": "安柏", "word_begin": 7, "word_end": 9,
                 "pronounce_word": "ㄛ", "time_begin": 30, "time_end": 40},
                {"word": "安柏", "word_begin": 7, "word_end": 9,
                 "pronounce_word": "ˋ", "time_begin": 40, "time_end": 50},
                {"word": "来了", "word_begin": 9, "word_end": 11,
                 "time_begin": 50, "time_end": 80},
                {"word": "。", "word_begin": 11, "word_end": 12,
                 "time_begin": 80, "time_end": 90},
            ],
        }]
        timings = MiniMaxTTSProvider._parse_subtitle_timings(subtitle)
        self.assertEqual([
            (item.text, item.start_ms, item.end_ms) for item in timings
        ], [
            ("安柏", 20, 50),
            ("来了", 50, 80),
            ("。", 80, 90),
        ])
        segments = [TtsSegment(
            0, 1, "安柏来了。", TtsPerformance()
        )]
        spans = align_segments(segments, timings)
        self.assertEqual(spans[0].start_ms, 20)
        self.assertEqual(spans[0].end_ms, 80)


class RenderArtifactBoundaryTests(unittest.TestCase):
    def test_load_render_artifacts_does_not_synthesize_missing_wav(self):
        from mmm.tts import runtime as tts_runtime
        from mmm.tts.types import TtsProfile, sha256_object

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            profile = TtsProfile(
                "prod", "minimax", "speech-2.8-hd", "test-voice", 1.0, "calm", {}
            )
            plan = {
                "schema_version": "tts_plan_v1",
                "profile": "prod",
                "strategy": "full_then_split",
                "execution": {
                    "provider": profile.provider,
                    "model": profile.model,
                    "voice": profile.voice,
                    "speed": profile.speed,
                    "emotion": profile.emotion,
                    "options": profile.options,
                    "config_fingerprint": profile.fingerprint(),
                },
                "cost_estimate": {},
                "segments": [{
                    "index": 0,
                    "narration_id": 1,
                    "source_text": "终稿原文。",
                    "performance": {},
                    "pronunciations": [],
                }],
            }
            document = {
                "schema_version": "tts_plan_v1",
                "plan": plan,
                "plan_sha256": sha256_object(plan),
            }
            (work_dir / "edl.json").write_text(json.dumps({
                "clips": [{
                    "type": "narration_clip",
                    "narration_id": 1,
                    "text": "终稿原文。",
                }]
            }), encoding="utf-8")
            (work_dir / "tts_plan.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            (work_dir / "tts_plan.approved.json").write_text(
                json.dumps({"plan_sha256": document["plan_sha256"]}),
                encoding="utf-8",
            )
            wav_path = work_dir / "render_segments/tts_000.wav"
            wav_path.parent.mkdir()
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b"\0\0" * 4800)
            (work_dir / "render_segments/tts_artifacts.json").write_text(
                json.dumps({
                    "plan_sha256": document["plan_sha256"],
                    "config_fingerprint": profile.fingerprint(),
                    "artifacts": [{
                        "index": 0,
                        "narration_id": 1,
                        "source_text": "终稿原文。",
                        "wav_path": "render_segments/tts_000.wav",
                        "duration_s": 0.1,
                        "speech_start_ms": 0,
                        "speech_end_ms": 100,
                    }],
                }),
                encoding="utf-8",
            )
            tts_cfg = {"profile": "prod", "prod": {"voice_id": "test-voice"}}

            loaded = tts_runtime.load_render_artifacts(work_dir, tts_cfg)
            self.assertEqual(loaded[0], wav_path)

            wav_path.unlink()
            with mock.patch.object(
                tts_runtime, "synthesize_approved_plan"
            ) as synth:
                with self.assertRaises(tts_runtime.TtsArtifactsMissing):
                    tts_runtime.load_render_artifacts(work_dir, tts_cfg)
                synth.assert_not_called()

    def test_splitter_converts_millisecond_cut_points_to_seconds(self):
        from mmm.tts.splitter import split_master_audio
        from mmm.tts.types import RawSynthesis, SegmentSpan

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            master = out_dir / "master.wav"
            with wave.open(str(master), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b"\0\0" * 48000 * 2)
            segments = make_segments()
            spans = [
                SegmentSpan(0, 1, 0, 500),
                SegmentSpan(1, 2, 1000, 1500),
            ]
            raw = RawSynthesis(
                audio_path=master,
                timings=[],
                provider_text="",
                warnings=[],
                manual_pause_ms=[0],
                metadata={},
            )
            artifacts = split_master_audio(master, raw, segments, spans, out_dir)
            self.assertEqual(len(artifacts), 2)
            self.assertLess(artifacts[0].duration_s, 1.2)
            self.assertLess(artifacts[1].duration_s, 1.3)


class NormalizeTests(unittest.TestCase):
    def test_punctuation_is_removed(self):
        self.assertEqual(normalize_for_alignment("安柏，来了！"), "安柏来了")


if __name__ == "__main__":
    unittest.main()
