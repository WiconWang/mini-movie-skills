"""B 模式（原声高光直拼）核心逻辑单测。

覆盖方案 §9 步骤 6 验证点：
- line_marks 校验降级三档（未知行→炸 / quality 非法→降 skip / 漏标→等价 skip）
- stage_narrate_low.run_low_only：high_requests=0、line_marks 落盘、缓存命中
- stage_select_raw.run：全 raw_insert EDL、quality 过滤、画面维度一票否决、
  无时间戳过滤、keep_requirements 合并、无 segments 兜底
- stage_compose：四段式分流、outro_special 不再被识别
- stage_render：pipeline_mode=raw 跳过 TTS 闸口
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmm import stage_narrate, stage_narrate_low, stage_select_raw, stage_compose


def make_endpoint(**profile_overrides):
    from mmm import llm

    env = {
        "MMM_NARRATE_LOW_PROFILE": "opencode_go",
        "MMM_NARRATE_LOW_MODEL": "test-model",
        "MMM_NARRATE_LOW_BASE_URL": "https://example.test/v1",
        "MMM_NARRATE_LOW_API_KEY": "test-key",
    }
    with mock.patch.dict(llm.os.environ, env):
        endpoint = llm.load_endpoint("narrate_low")
    profile = replace(
        endpoint.profile,
        min_interval_seconds=0,
        retry_backoff_seconds=0,
        narration_segment_workers=1,
        **profile_overrides,
    )
    return replace(endpoint, profile=profile)


def make_timeline() -> dict:
    """双视频全局时间轴：v1 两行台词 + v2 两行台词，各带一个 shot。"""
    return {
        "task_id": "t1",
        "videos": [
            {"video_id": "v1", "offset": 0.0, "duration": 100.0},
            {"video_id": "v2", "offset": 100.0, "duration": 100.0},
        ],
        "lines": [
            {"id": 1, "video_id": "v1", "speaker": "派蒙", "text": "高光台词一！",
             "align": "matched", "start": 10.0, "end": 13.0,
             "local_start": 10.0, "local_end": 13.0},
            {"id": 2, "video_id": "v1", "speaker": "旅行者", "text": "过程性应答。",
             "align": "matched", "start": 14.0, "end": 15.0,
             "local_start": 14.0, "local_end": 15.0},
            {"id": 3, "video_id": "v2", "speaker": "NPC", "text": "另一视频的高光。",
             "align": "interpolated", "start": 110.0, "end": 113.0,
             "local_start": 10.0, "local_end": 13.0},
            {"id": 4, "video_id": "v2", "speaker": "旁白", "text": "无时间戳台词。",
             "align": "unmatched"},
        ],
        "shots": [
            {"id": 1, "video_id": "v1", "class": "B", "ui_type": "dialogue",
             "start": 9.0, "end": 16.0, "local_start": 9.0, "local_end": 16.0,
             "line_ids": [1, 2]},
            {"id": 2, "video_id": "v1", "class": "A", "ui_type": "none",
             "start": 16.0, "end": 20.0, "local_start": 16.0, "local_end": 20.0,
             "line_ids": []},
            {"id": 3, "video_id": "v2", "class": "C", "ui_type": "none",
             "start": 109.0, "end": 114.0, "local_start": 9.0, "local_end": 14.0,
             "line_ids": [3, 4]},
        ],
        "stats": {"shots": 3, "by_class": {}},
    }


def make_segments(timeline: dict) -> list[dict]:
    """构造含 line_marks 的 v2 segments（模拟 narrate_low 产出）。"""
    return [
        {
            "video_id": "v1", "chunk_id": "chunk_001", "segment_id": "v1::chunk_001",
            "beats": [{
                "id": 1, "summary": "高光节拍", "characters": ["派蒙"],
                "cause": "", "effect": "",
                "key_quotes": [{"speaker": "派蒙", "text": "高光台词一！", "line_id": 1}],
                "line_marks": [
                    {"line_id": 1, "quality": "great", "reason": "情绪爆点"},
                    {"line_id": 2, "quality": "skip", "reason": "过程应答"},
                ],
                "related_line_ids": [1, 2],
                "importance": "core", "confidence": "high",
            }],
        },
        {
            "video_id": "v2", "chunk_id": "chunk_001", "segment_id": "v2::chunk_001",
            "beats": [{
                "id": 1, "summary": "节拍二", "characters": ["NPC"],
                "cause": "", "effect": "", "key_quotes": [],
                "line_marks": [
                    {"line_id": 3, "quality": "great", "reason": "点题"},
                    {"line_id": 4, "quality": "great", "reason": "但无时间戳"},
                ],
                "related_line_ids": [3, 4],
                "importance": "core", "confidence": "high",
            }],
        },
    ]


def _plan_for(timeline: dict) -> stage_narrate.SegmentPlan:
    return stage_narrate.SegmentPlan(
        video_id="v1", chunk_id="chunk_001", segment_id="v1::chunk_001",
        timeline={"video_id": "v1", "lines": timeline["lines"],
                  "shots": timeline["shots"]},
        prompt="",
    )


class LineMarksValidationTests(unittest.TestCase):
    """line_marks 校验降级三档（方案 §4.1.5）。"""

    def _data(self, marks, refs=None):
        return {
            "video_id": "v1", "chunk_id": "chunk_001", "segment_id": "v1::chunk_001",
            "beats": [{
                "id": 1, "summary": "s", "characters": [], "cause": "", "effect": "",
                "key_quotes": [],
                "related_line_ids": refs if refs is not None else [1, 2],
                "line_marks": marks,
                "importance": "core", "confidence": "high",
            }],
        }

    def test_unknown_line_dropped(self):
        """line_marks 越界引用 related_line_ids 之外的行 → 丢弃该条（不炸）。

        实测 LLM 幻觉率约 25%，raise 会让整 chunk LLM 调用作废；丢弃只导致漏选
        不导致误选（selector 的 lines_by_id 查不到该 line_id 自然落空）。
        """
        timeline = make_timeline()
        data = self._data([{"line_id": 999, "quality": "great", "reason": ""}])
        result = stage_narrate._validate_low_segment(data, _plan_for(timeline))
        self.assertEqual(result["beats"][0]["line_marks"], [])

    def test_invalid_quality_downgrades_to_skip(self):
        """quality 非法 → 降为 skip（格式容错，不丢数据）。"""
        timeline = make_timeline()
        data = self._data([{"line_id": 1, "quality": "BEST", "reason": ""}])
        result = stage_narrate._validate_low_segment(data, _plan_for(timeline))
        self.assertEqual(result["beats"][0]["line_marks"][0]["quality"], "skip")

    def test_missing_line_marks_tolerated(self):
        """line_marks 整体缺失 → 空列表（不炸，所有行等价 skip）。"""
        timeline = make_timeline()
        data = self._data(None)
        result = stage_narrate._validate_low_segment(data, _plan_for(timeline))
        self.assertEqual(result["beats"][0]["line_marks"], [])

    def test_partial_missing_no_fake(self):
        """漏标行不补造假：只保留已标行，漏标行落出候选池。"""
        timeline = make_timeline()
        data = self._data([{"line_id": 1, "quality": "great", "reason": ""}])
        result = stage_narrate._validate_low_segment(data, _plan_for(timeline))
        marks = result["beats"][0]["line_marks"]
        self.assertEqual([m["line_id"] for m in marks], [1])


class RunLowOnlyTests(unittest.TestCase):
    """stage_narrate_low.run_low_only：high_requests=0 + 缓存命中。"""

    def _fake_chat(self, endpoint, prompt, *, max_tokens, temperature, label, output_dir):
        seg_id = label
        vid = seg_id.split("::")[0]
        # 按 segment 的台词表返回首个 line_id（v1 → line 1；v2 → line 3）
        first_line = 1 if vid == "v1" else 3
        data = {
            "video_id": vid,
            "chunk_id": "chunk_001", "segment_id": seg_id,
            "beats": [{
                "id": 1, "summary": "s", "characters": [], "cause": "", "effect": "",
                "key_quotes": [], "related_line_ids": [first_line],
                "line_marks": [{"line_id": first_line, "quality": "great", "reason": "r"}],
                "importance": "core", "confidence": "high",
            }],
        }
        return data, "{}"

    def test_high_requests_always_zero(self):
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            tl_path = out_dir / "global_timeline.json"
            tl_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
            with mock.patch("mmm.llm.load_endpoint", return_value=make_endpoint()), \
                 mock.patch.object(stage_narrate, "_chat_json", side_effect=self._fake_chat):
                summary = stage_narrate_low.run_low_only(tl_path, out_dir)
            self.assertEqual(summary["high_requests"], 0)
            self.assertEqual(summary["schema"], "story_beats_v2")
            self.assertEqual(summary["segments"], 2)
            seg_files = list((out_dir / "narration_segments").glob("*.json"))
            self.assertEqual(len(seg_files), 2)
            for f in seg_files:
                seg = json.loads(f.read_text(encoding="utf-8"))
                self.assertTrue(seg["beats"][0].get("line_marks"))

    def test_cache_hit_zero_requests(self):
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            tl_path = out_dir / "global_timeline.json"
            tl_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
            with mock.patch("mmm.llm.load_endpoint", return_value=make_endpoint()), \
                 mock.patch.object(stage_narrate, "_chat_json", side_effect=self._fake_chat):
                first = stage_narrate_low.run_low_only(tl_path, out_dir)
                second = stage_narrate_low.run_low_only(tl_path, out_dir)
        self.assertEqual(first["low_requests"], 2)
        self.assertEqual(second["cache_hits"], 2)
        self.assertEqual(second["low_requests"], 0)
        self.assertEqual(second["high_requests"], 0)


class SelectRawTests(unittest.TestCase):
    """stage_select_raw.run：双维度选片 + EDL 构造。"""

    def _prepare(self, tmp, timeline, segments):
        out_dir = Path(tmp)
        (out_dir / "global_timeline.json").write_text(
            json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
        seg_dir = out_dir / "narration_segments"
        seg_dir.mkdir(exist_ok=True)
        for seg in segments:
            (seg_dir / f"{seg['segment_id']}.json").write_text(
                json.dumps(seg, ensure_ascii=False), encoding="utf-8")
        return out_dir

    def _run(self, out_dir, **kwargs):
        with mock.patch("mmm.stage_select_raw.reviewer.build_storyboard"), \
             mock.patch("mmm.db.PROJECT_ROOT", out_dir):
            return stage_select_raw.run(out_dir, "t1", **kwargs)

    def test_full_raw_insert_edl(self):
        """EDL 全 raw_insert、keep_audio:true、footage_usage 空（B 模式豁免）。"""
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            self._run(out_dir)
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
        self.assertTrue(edl["clips"])
        for c in edl["clips"]:
            self.assertEqual(c["type"], "raw_insert")
            self.assertTrue(c["keep_audio"])
        self.assertEqual(edl["footage_usage"], [])

    def test_quality_filter_excludes_skip(self):
        """quality_levels=["great"] 时 skip 行不入选（line 2 是 skip）。"""
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            self._run(out_dir)
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertIn(1, line_ids)
        self.assertIn(3, line_ids)
        self.assertNotIn(2, line_ids)

    def test_good_level_expands_pool(self):
        """quality_levels 加 good 扩池：good 行入选。"""
        timeline = make_timeline()
        segments = make_segments(timeline)
        segments[0]["beats"][0]["line_marks"][1]["quality"] = "good"
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, segments)
            self._run(out_dir, quality_levels=["great", "good"])
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertIn(2, line_ids)

    def test_min_shot_class_veto(self):
        """min_shot_class=B 一票否决：class=A（最差）的台词剔除，C/B 及以上保留。

        画面分级 E>D>C>B>A（E 最好，rank 越小越好）；min_shot_class=B 意为"至少 B 级"，
        rank ≤ CLASS_RANK[B]=3 通过，A(rank=4) 剔除。故把 line 1 所在 shot 设为 A 验证否决。
        """
        timeline = make_timeline()
        timeline["shots"][0]["class"] = "A"   # line 1,2 所在 shot → A（最差）
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            self._run(out_dir, min_shot_class="B")
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertNotIn(1, line_ids)   # shot class A < B 下限 → 剔除
        self.assertIn(3, line_ids)      # shot class C ≥ B → 保留

    def test_min_shot_class_c_relaxes(self):
        """min_shot_class=A（最宽松）：所有达标台词保留，无否决。"""
        timeline = make_timeline()
        timeline["shots"][0]["class"] = "A"
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            self._run(out_dir, min_shot_class="A")
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertIn(1, line_ids)
        self.assertIn(3, line_ids)

    def test_no_timestamp_lines_filtered(self):
        """无 local_start/local_end（unmatched）行不入选（line 4）。"""
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            self._run(out_dir)
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertNotIn(4, line_ids)

    def test_keep_requirements_merged(self):
        """keep_requirements 合并：人工区间保留、重叠自动挑选剔除。"""
        timeline = make_timeline()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = self._prepare(tmp, timeline, make_segments(timeline))
            keep = [{"video_id": "v1", "start": 10.5, "end": 12.0, "note": "人工"}]
            self._run(out_dir, keep_requirements=keep)
            edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
            by_quality = {c.get("quality") for c in edl["clips"]}
            line_ids = {c.get("line_id") for c in edl["clips"]}
        self.assertIn("keep", by_quality)
        self.assertNotIn(1, line_ids)   # line1(10-13) 与人工(10.5-12) 重叠 → 剔除

    def test_ensure_segments_fallback(self):
        """无 segments 时 ensure_segments 兜底跑 low-only。"""
        timeline = make_timeline()
        calls = {"n": 0}

        def fake_low_only(timeline_path, work_dir, *, force=False):
            calls["n"] += 1
            seg_dir = work_dir / "narration_segments"
            seg_dir.mkdir(exist_ok=True)
            for seg in make_segments(make_timeline()):
                (seg_dir / f"{seg['segment_id']}.json").write_text(
                    json.dumps(seg, ensure_ascii=False), encoding="utf-8")
            return {"segments": 2, "cache_hits": 0, "low_requests": 2,
                    "high_requests": 0, "schema": "story_beats_v2"}

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "global_timeline.json").write_text(
                json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
            with mock.patch("mmm.stage_narrate_low.run_low_only",
                            side_effect=fake_low_only), \
                 mock.patch("mmm.db.PROJECT_ROOT", out_dir), \
                 mock.patch("mmm.stage_select_raw.reviewer.build_storyboard"):
                stage_select_raw.run(out_dir, "t1")
            self.assertEqual(calls["n"], 1)
            self.assertTrue((out_dir / "edl.json").exists())


class ComposeFourSegmentTests(unittest.TestCase):
    """stage_compose 四段式分流（ADR-0001）。"""

    def test_outro_special_no_longer_recognized(self):
        """outro_special 类型不被 from_task 识别（已废弃）。"""
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "tasks" / "t1"
            task_dir.mkdir(parents=True)
            body = task_dir / "body.mp4"
            body.write_bytes(b"x")
            cfg = {"composition": [
                {"type": "outro_special", "src": "/nonexist.mp4", "start": 0, "end": 1}]}
            (task_dir / "task.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            with mock.patch("mmm.stage_compose.PROJECT_ROOT" if False else "mmm.db.PROJECT_ROOT",
                            Path(tmp)):
                # outro_special 被忽略 → 无 cover/intro/outro → 直接返回 body
                result = stage_compose.from_task("t1", body)
        self.assertEqual(result, body)

    def test_from_task_splits_four_buckets(self):
        """cover/intro/outro 四桶分流：from_task 读 composition 构造 compose 入参。"""
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "tasks" / "t1"
            task_dir.mkdir(parents=True)
            cover = Path(tmp) / "cover.jpg"
            cover.write_bytes(b"x")
            intro = Path(tmp) / "intro.mp4"
            intro.write_bytes(b"x")
            outro = Path(tmp) / "outro.jpg"
            outro.write_bytes(b"x")
            body = task_dir / "body.mp4"
            body.write_bytes(b"x")
            cfg = {"composition": [
                {"type": "cover", "src": str(cover)},
                {"type": "intro_special", "src": str(intro)},
                {"type": "outro", "src": str(outro)},
            ], "output": {"width": 1920, "height": 1080, "fps": 30}}
            (task_dir / "task.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            captured = {}

            def fake_compose(cover, intro_files, body, outro, out, **kw):
                captured["cover"] = cover
                captured["intro_files"] = intro_files
                captured["outro"] = outro
                return out

            with mock.patch("mmm.db.PROJECT_ROOT", Path(tmp)), \
                 mock.patch.object(stage_compose, "compose", side_effect=fake_compose):
                stage_compose.from_task("t1", body)
        self.assertEqual(captured["cover"], cover)
        self.assertEqual(captured["intro_files"], [intro])
        self.assertEqual(captured["outro"], outro)


class RenderSkipTtsTests(unittest.TestCase):
    """stage_render.run：pipeline_mode=raw 跳过 TTS 闸口。"""

    def test_raw_mode_skips_prepare_render_artifacts(self):
        """raw 模式有 task_id 也不调 prepare_render_artifacts（B 任务无 tts_plan）。"""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            video = Path(tmp) / "src.mp4"
            video.write_bytes(b"x")
            clips = [{"type": "raw_insert", "video_id": "v1",
                      "start": 0.0, "end": 1.0, "keep_audio": True, "shot_ids": []}]
            (work_dir / "edl.json").write_text(
                json.dumps({"clips": clips}, ensure_ascii=False), encoding="utf-8")
            tts_called = {"n": 0}

            def fake_prepare(*args, **kwargs):
                tts_called["n"] += 1
                raise AssertionError("raw 模式不应调 prepare_render_artifacts")

            def fake_run(cmd):
                # concat 命令的输出文件补个占位（render_segment 已 mock，无真产物）
                out = Path(cmd[-1])
                if str(out).endswith(".mp4"):
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(b"x")

            with mock.patch.object(stage_compose, "from_task",
                                   side_effect=lambda tid, body: body), \
                 mock.patch("mmm.catalog.register_usage", return_value=0), \
                 mock.patch("mmm.stage_render.render_segment", return_value=1.0), \
                 mock.patch("mmm.stage_render._run", side_effect=fake_run), \
                 mock.patch("mmm.stage_render.probe_duration", return_value=1.0), \
                 mock.patch("mmm.tts.runtime.prepare_render_artifacts",
                            side_effect=fake_prepare), \
                 mock.patch("mmm.db.PROJECT_ROOT", Path(tmp)):
                from mmm import stage_render

                stage_render.run(work_dir, {"v1": video},
                                 task_id="t1", pipeline_mode="raw",
                                 subtitle_mode="none")
        self.assertEqual(tts_called["n"], 0)


if __name__ == "__main__":
    unittest.main()
