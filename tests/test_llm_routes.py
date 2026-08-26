"""LLM 路由、重试与解说引用防伪的本地测试。"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmm import llm, models, stage_narrate


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/chat/completions",
        status,
        "error",
        {},
        io.BytesIO(b"{}"),
    )


def ok_response(content: str = "ok") -> FakeResponse:
    return FakeResponse({
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def make_endpoint(route: str, **profile_overrides) -> llm.LLMEndpoint:
    prefix = {
        "narrate_low": "MMM_NARRATE_LOW_",
        "narrate_high": "MMM_NARRATE_HIGH_",
        "vision": "MMM_VISION_",
    }[route]
    env = {
        f"{prefix}PROFILE": "opencode_go",
        f"{prefix}MODEL": "test-model",
        f"{prefix}BASE_URL": "https://example.test/v1",
        f"{prefix}API_KEY": "test-key",
    }
    with mock.patch.dict(llm.os.environ, env):
        endpoint = llm.load_endpoint(route)
    profile = replace(
        endpoint.profile,
        min_interval_seconds=0,
        retry_backoff_seconds=0,
        **profile_overrides,
    )
    return replace(endpoint, profile=profile)


class LLMRouteTests(unittest.TestCase):
    def test_route_profiles_parse_and_validate(self):
        low = models.route_profile("narrate_low", "opencode_go")
        high = models.route_profile("narrate_high", "opencode_go")
        vision = models.route_profile("vision", "opencode_go")
        self.assertEqual(low.max_retries, 1)
        self.assertEqual(high.max_retries, 0)
        self.assertEqual(vision.max_retries, 1)
        self.assertIn("image", vision.capabilities)

    def test_missing_env_names_exact_variable(self):
        env = {"MMM_NARRATE_HIGH_PROFILE": "opencode_go",
               "MMM_NARRATE_HIGH_MODEL": "test",
               "MMM_NARRATE_HIGH_BASE_URL": "https://example.test/v1"}
        with mock.patch.dict(llm.os.environ, env, clear=False), \
                mock.patch.dict(llm.os.environ, {"MMM_NARRATE_HIGH_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "MMM_NARRATE_HIGH_API_KEY"):
                llm.load_endpoint("narrate_high")

    def test_high_http_error_never_retries(self):
        calls = []

        def fake_urlopen(*args, **kwargs):
            calls.append(args)
            raise http_error(503)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm.urllib.request, "urlopen", fake_urlopen), \
                    mock.patch.object(llm, "LOG_PATH", Path(tmp) / "llm.jsonl"):
                target = make_endpoint("narrate_high", max_retries=0)
                with self.assertRaises(llm.LLMCallError):
                    llm.chat(target, [{"role": "user", "content": "x"}], max_tokens=16)
                self.assertEqual(len(calls), 1)
                record = json.loads((Path(tmp) / "llm.jsonl").read_text())
        self.assertEqual(record["route"], "narrate_high")
        self.assertEqual(record["attempt"], 1)
        self.assertFalse(record["will_retry"])

    def test_low_retryable_http_error_retries_once(self):
        calls = []

        def fake_urlopen(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise http_error(503)
            return ok_response()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm.urllib.request, "urlopen", fake_urlopen), \
                    mock.patch.object(llm, "LOG_PATH", Path(tmp) / "llm.jsonl"):
                target = make_endpoint(
                    "narrate_low",
                    max_retries=1,
                    retryable_status_codes=frozenset({503}),
                )
                result = llm.chat(
                    target, [{"role": "user", "content": "x"}], max_tokens=16
                )
                self.assertEqual(result.attempt, 2)
                self.assertEqual(len(calls), 2)
                lines = (Path(tmp) / "llm.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(json.loads(lines[0])["will_retry"])

    def test_empty_content_is_business_failure(self):
        calls = []

        def fake_urlopen(*args, **kwargs):
            calls.append(args)
            return ok_response("")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm.urllib.request, "urlopen", fake_urlopen), \
                    mock.patch.object(llm, "LOG_PATH", Path(tmp) / "llm.jsonl"):
                target = make_endpoint(
                    "narrate_low",
                    max_retries=1,
                    retryable_status_codes=frozenset({503}),
                )
                with self.assertRaisesRegex(llm.LLMCallError, "EmptyContent"):
                    llm.chat(
                        target, [{"role": "user", "content": "x"}], max_tokens=16
                    )
        self.assertEqual(len(calls), 1)


class NarrateReferenceTests(unittest.TestCase):
    def test_low_and_high_reference_validation(self):
        timeline = {
            "video_id": "v1",
            "lines": [
                {"id": 1, "text": "一", "align": "matched"},
                {"id": 2, "text": "二", "align": "unvoiced"},
            ],
            "shots": [],
        }
        plan = stage_narrate.SegmentPlan(
            video_id="v1",
            chunk_id="chunk_001",
            segment_id="v1::chunk_001",
            timeline=timeline,
            prompt="",
        )
        valid = {
            "video_id": "v1",
            "chunk_id": "chunk_001",
            "segment_id": "v1::chunk_001",
            "beats": [{
                "id": 1, "summary": "事件", "characters": ["A"],
                "cause": "", "effect": "",
                "key_quotes": [], "related_line_ids": [1],
                "importance": "core", "confidence": "high",
            }],
        }
        self.assertTrue(stage_narrate._validate_low_segment(valid, plan)["beats"])
        invalid = json.loads(json.dumps(valid))
        invalid["beats"][0]["related_line_ids"] = [2]
        with self.assertRaisesRegex(ValueError, "无配音"):
            stage_narrate._validate_low_segment(invalid, plan)

        high = {
            "narration": [{
                "id": 1,
                "text": "终稿",
                "related_beat_ids": [
                    {"segment_id": "v1::chunk_001", "beat_id": 1}
                ],
            }]
        }
        beat_index = {("v1::chunk_001", 1): {"related_line_ids": [1]}}
        output = stage_narrate._validate_high_output(
            high, valid_beats=True, direct_ids=set(), beat_index=beat_index
        )
        self.assertEqual(output[0]["related_beat_ids"][0]["beat_id"], 1)

    def test_high_fuse_evidence_omits_line_refs_and_extra_quotes(self):
        segments = [{
            "segment_id": "v1::chunk_001",
            "beats": [{
                "id": 1,
                "summary": "核心事件",
                "characters": ["A"],
                "cause": "原因",
                "effect": "结果",
                "key_quotes": [
                    {"speaker": "A", "text": "第一句", "line_id": 1},
                    {"speaker": "A", "text": "第二句", "line_id": 2},
                ],
                "related_line_ids": [1, 2],
                "importance": "core",
                "confidence": "high",
            }],
        }]

        prompt = stage_narrate._build_high_fuse_prompt(segments, target_minutes=1)

        self.assertIn("v1::chunk_001", prompt)
        self.assertNotIn("related_line_ids", prompt)
        self.assertIn("第一句", prompt)
        self.assertNotIn("第二句", prompt)
        legacy_material = (
            stage_narrate._HIGH_NARRATION_STYLE
            + stage_narrate._NARRATIVE_EXAMPLE
        )
        legacy_fingerprint = hashlib.sha256(legacy_material.encode("utf-8")).hexdigest()[:12]
        self.assertNotEqual(stage_narrate.HIGH_PROMPT_FP, legacy_fingerprint)


class NarratePipelineTests(unittest.TestCase):
    def test_human_edited_final_is_protected_without_force(self):
        timeline = {
            "videos": [{"video_id": "v1", "path": "v1.mp4"}],
            "lines": [{
                "id": 1,
                "video_id": "v1",
                "start": 0.0,
                "end": 1.0,
                "speaker": "角色",
                "text": "台词",
                "align": "matched",
            }],
            "shots": [],
        }

        def forbidden_chat(*args, **kwargs):
            raise AssertionError("人工编辑稿未强制覆盖时不得发起 LLM 请求")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timeline_path = root / "timeline.json"
            output_dir = root / "out"
            timeline_path.write_text(json.dumps(timeline, ensure_ascii=False), "utf-8")
            output_dir.mkdir()
            final = {"_human_edited": True, "narration": []}
            (output_dir / "narration.json").write_text(
                json.dumps(final, ensure_ascii=False), "utf-8"
            )
            (output_dir / "narration.md").write_text("人工稿", "utf-8")

            endpoint = make_endpoint("narrate_high")
            with mock.patch.object(
                stage_narrate, "load_endpoint", return_value=endpoint
            ), mock.patch.object(llm, "chat", side_effect=forbidden_chat):
                with self.assertRaisesRegex(RuntimeError, "--force"):
                    stage_narrate.run(timeline_path, output_dir)

            self.assertEqual(
                json.loads((output_dir / "narration.json").read_text(encoding="utf-8")),
                final,
            )
            self.assertEqual(
                (output_dir / "narration.md").read_text(encoding="utf-8"), "人工稿"
            )

    def test_multi_video_low_high_generation_and_reuse(self):
        low = make_endpoint("narrate_low")
        high = make_endpoint("narrate_high")
        endpoints = {"narrate_low": low, "narrate_high": high}

        line_specs = [
            ("v1", 101, 0.0, 2.0, "第一段台词"),
            ("v1", 102, 2.1, 4.0, "第一段结尾"),
            ("v2", 201, 0.0, 2.0, "第二段台词"),
            ("v2", 202, 2.1, 4.0, "第二段结尾"),
        ]
        timeline = {
            "videos": [
                {"video_id": "v1", "path": "v1.mp4"},
                {"video_id": "v2", "path": "v2.mp4"},
            ],
            "lines": [
                {
                    "id": line_id,
                    "video_id": video_id,
                    "start": start,
                    "end": end,
                    "speaker": f"角色{video_id}",
                    "text": text,
                    "align": "matched",
                }
                for video_id, line_id, start, end, text in line_specs
            ],
            "shots": [],
        }
        low_refs = {"v1::chunk_001": 101, "v2::chunk_001": 201}
        calls = []

        def fake_chat(endpoint, messages, *, max_tokens, temperature=None, label=None):
            prompt = messages[0]["content"]
            calls.append((endpoint.route, label))

            if endpoint.route == "narrate_low":
                payload = {
                    "video_id": label.split("::", 1)[0],
                    "chunk_id": label.split("::", 1)[1],
                    "segment_id": label,
                    "beats": [{
                        "id": 1,
                        "summary": f"{label} 的核心事件",
                        "characters": ["我们"],
                        "cause": "",
                        "effect": "",
                        "key_quotes": [],
                        "related_line_ids": [low_refs[label]],
                        "importance": "core",
                        "confidence": "high",
                    }],
                }
            else:
                self.assertIn("v1::chunk_001", prompt)
                self.assertIn("v2::chunk_001", prompt)
                self.assertNotIn("related_line_ids", prompt)
                payload = {
                    "narration": [{
                        "id": 1,
                        "text": "这是融合后的终稿。",
                        "related_beat_ids": [
                            {"segment_id": "v1::chunk_001", "beat_id": 1},
                            {"segment_id": "v2::chunk_001", "beat_id": 1},
                        ],
                    }]
                }

            content = json.dumps(payload, ensure_ascii=False)
            return llm.LLMCallResult(
                content=content,
                route=endpoint.route,
                profile_id=endpoint.profile_id,
                model=endpoint.model,
                attempt=1,
                duration_ms=1,
                http_status=200,
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                response_chars=len(content),
                response_preview=content[:80],
                response_hash="sha256:test",
                log_path="test-log",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timeline_path = root / "timeline.json"
            output_dir = root / "out"
            timeline_path.write_text(json.dumps(timeline, ensure_ascii=False), "utf-8")
            output_dir.mkdir()
            (output_dir / "narration.json").write_text(
                json.dumps({"legacy": True}, ensure_ascii=False), "utf-8"
            )
            (output_dir / "narration.md").write_text("旧终稿", "utf-8")
            segment_dir = output_dir / "narration_segments"
            segment_dir.mkdir()
            (segment_dir / "legacy.json").write_text("{}", "utf-8")
            (segment_dir / "v1::chunk_001.json").write_text("{}", "utf-8")
            (segment_dir / "v2::chunk_001.json").write_text("{}", "utf-8")

            with mock.patch.object(
                stage_narrate, "load_endpoint", side_effect=lambda route: endpoints[route]
            ), mock.patch.object(llm, "chat", side_effect=fake_chat):
                summary = stage_narrate.run(timeline_path, output_dir, target_minutes=1)

                self.assertEqual(summary["mode"], "segment")
                self.assertEqual(summary["low_segments"], 2)
                self.assertFalse(summary["high_reused"])
                self.assertEqual(
                    calls,
                    [
                        ("narrate_low", "v1::chunk_001"),
                        ("narrate_low", "v2::chunk_001"),
                        ("narrate_high", "fuse:out"),
                    ],
                )

                self.assertFalse((segment_dir / "legacy.json").exists())
                result = json.loads(
                    (output_dir / "narration.json").read_text(encoding="utf-8")
                )
                self.assertEqual(result["segment_order"], ["v1::chunk_001", "v2::chunk_001"])
                self.assertEqual(
                    result["narration"][0]["related_line_ids"],
                    [
                        {"video_id": "v1", "line_id": 101},
                        {"video_id": "v2", "line_id": 201},
                    ],
                )

                calls.clear()
                summary = stage_narrate.run(timeline_path, output_dir, target_minutes=1)
                self.assertTrue(summary["high_reused"])
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
