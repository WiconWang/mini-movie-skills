"""阶段5'：B 模式专用 narrate-low-only 入口（只跑 low，不跑 high）。

B 模式不做解说终稿，只需 narrate_low 的节拍 + per-line quality 标注
（story_beats_v2 的 line_marks）。复用 stage_narrate 的内部函数，
硬编码 high_requests=0，绝不触发 narrate_high 调用（HIGH 费用约为 LOW 的 20 倍）。

不暴露 CLI：仅作 stage_select_raw.ensure_segments 内部兜底 + 单测调用。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .stage_narrate import (
    SegmentPlan,
    _build_segments,
    _clean_stale_segments,
    _drop_invalid_cache,
    _read_valid_cache,
    _run_low,
)


def run_low_only(timeline_path: Path, output_dir: Path, *,
                 force: bool = False) -> dict:
    """只跑 narrate_low（抽节拍 + per-line 标 quality），不跑 high。

    timeline_path：任务级 global_timeline.json（或单视频 timeline.json）。
    output_dir：narration_segments/ 的落盘目录（任务目录）。
    force=True 时忽略缓存强制重跑。

    缓存指纹校验沿用 _read_valid_cache：schema/prompt/profile/model/timeline
    任一不匹配即失效重跑（旧 v1 segments 缺 line_marks 自动由此升级到 v2）。
    """
    from .llm import load_endpoint

    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    low_endpoint = load_endpoint("narrate_low")
    segments = _build_segments(timeline, low_endpoint)

    seg_dir = output_dir / "narration_segments"
    output_dir.mkdir(parents=True, exist_ok=True)
    planned_names = {f"{s.segment_id}.json" for s in segments}
    _clean_stale_segments(output_dir, planned_names)

    for segment in segments:
        segment.cache_path = seg_dir / f"{segment.segment_id}.json"
        _drop_invalid_cache(segment, low_endpoint)

    def load_or_run(segment: SegmentPlan) -> dict:
        if not force:
            cached = _read_valid_cache(segment, low_endpoint)
            if cached is not None:
                segment.cache_hit = True
                return cached
        return _run_low(segment, low_endpoint, output_dir)

    workers = low_endpoint.profile.narration_segment_workers
    if workers > 1 and len(segments) > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(load_or_run, segment): segment.segment_id
                for segment in segments
            }
            results = {futures[f]: f.result() for f in as_completed(futures)}
    else:
        results = {s.segment_id: load_or_run(s) for s in segments}

    return {
        "segments": len(segments),
        "cache_hits": sum(1 for s in segments if s.cache_hit),
        "low_requests": sum(0 if s.cache_hit else 1 for s in segments),
        "high_requests": 0,   # ← B 模式硬约束：绝不跑 high
        "schema": "story_beats_v2",
    }
