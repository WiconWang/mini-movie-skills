"""阶段6'：B 模式选片（原声高光直拼）。

从 narrate_low 的 per-line quality（story_beats_v2 的 line_marks）取叙事高光台词，
叠加 shots 画面维度（class/ui_type）过滤，生成全 raw_insert 的 EDL。

与 stage_select（A 模式）的根本差异：
- 输入：global_timeline + narration_segments.line_marks（不是 narration.json）
- 选片依据：quality 叙事维度 + shots 画面维度（不是解说句逐句配画面）
- EDL：全 raw_insert（keep_audio:true），无 narration_clip
- LLM：仅 low（per-line 标注），不依赖 high 终稿
- footage_usage：B 模式豁免不记账（quality 叙事价值与画面新鲜感解耦）

quality 语义（纯叙事维度，非画面/UI）：
- great：该台词有独立叙事价值，提取后能串联成整体故事 → 默认入选
- good：推进剧情但有上下文依赖 → 备选（quality_levels 加 good 时扩池）
- skip：过程性应答 → 不选
画面维度由 select-raw 从 shots 查（class/ui_type），在 selector 层合并，不进 LLM。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import reviewer
from .stage_select import CLASS_RANK, _frame_paths, _overlaps

# 有时间戳的 align（可定位源视频帧）；unmatched/unvoiced 无 local_start/local_end
TIMED_ALIGNS = ("matched", "interpolated")

VALID_QUALITY = ("great", "good", "skip")


def _build_shot_index(shots: list[dict]) -> dict[int, dict]:
    """line_id → 所属 shot（一个 line 可被多个 shot 包含，取首个命中）。"""
    line_to_shot: dict[int, dict] = {}
    for shot in shots:
        for lid in shot.get("line_ids", []):
            if lid not in line_to_shot:
                line_to_shot[lid] = shot
    return line_to_shot


def _collect_quality_lines(segments_dir: Path,
                           quality_levels: list[str]) -> tuple[dict[int, dict], dict]:
    """从 narration_segments 的 line_marks 取 quality 达标的 {line_id: {quality, reason}}。

    同一 line_id 跨多个 beat 被标时，取最高档（great > good > skip）。
    """
    rank = {"great": 3, "good": 2, "skip": 1}
    wanted = set(quality_levels)
    collected: dict[int, dict] = {}
    stats = {
        "segments_scanned": 0,
        "beats_total": 0,
        "line_marks_total": 0,
        "by_quality": Counter(),
    }
    if not segments_dir.exists():
        return collected, stats
    for seg_file in sorted(segments_dir.glob("*.json")):
        stats["segments_scanned"] += 1
        seg = json.loads(seg_file.read_text(encoding="utf-8"))
        for beat in seg.get("beats", []):
            stats["beats_total"] += 1
            for mark in beat.get("line_marks", []):
                stats["line_marks_total"] += 1
                lid = mark.get("line_id")
                q = mark.get("quality")
                if lid is None or q not in VALID_QUALITY:
                    continue
                stats["by_quality"][q] += 1
                if q not in wanted:
                    continue
                prev = collected.get(lid)
                if prev is None or rank.get(q, 0) > rank.get(prev["quality"], 0):
                    collected[lid] = {"quality": q, "reason": mark.get("reason", "")}
    stats["by_quality"] = dict(stats["by_quality"])
    return collected, stats


def _make_pick(line: dict, video_dur: float, quality: str, reason: str,
               buffer_sec: float) -> dict | None:
    """根据 line 构造 raw_insert pick，加 buffer 并 clamp 到视频时长。

    无 local_start/local_end 返回 None（调用方应已过滤，此处兜底）。
    """
    ls = line.get("local_start")
    le = line.get("local_end")
    if ls is None or le is None:
        return None
    start = max(ls - buffer_sec, 0.0)
    end = min(le + buffer_sec, video_dur)
    if end - start < 0.3:
        return None
    return {
        "video_id": line["video_id"],
        "start": round(start, 3),
        "end": round(end, 3),
        "line_id": line["id"],
        "speaker": line.get("speaker", ""),
        "text": line.get("text", ""),
        "quality": quality,
        "reason": reason,
    }


def _dedup_and_sort(picks: list[dict]) -> list[dict]:
    """同 line_id 去重（保留 quality 高的），按 start 排序。

    line_id=None 的（keep_requirements 人工区间）按 (video_id, start) 作 key，
    不会与自动挑选互撞，也不互相覆盖。
    """
    rank = {"great": 3, "good": 2, "skip": 1}
    best: dict[tuple, dict] = {}
    for p in picks:
        lid = p["line_id"]
        key = ("noid", p["video_id"], p["start"]) if lid is None else lid
        prev = best.get(key)
        if prev is None or rank.get(p["quality"], 0) > rank.get(prev["quality"], 0):
            best[key] = p
    return sorted(best.values(), key=lambda p: (p["video_id"], p["start"]))


def _merge_keep_requirements(picks: list[dict],
                             keep_reqs: list[dict]) -> list[dict]:
    """把人工 keep_requirements 合入 picks：人工区间优先，重叠的自动挑选剔除。

    keep_requirements 是人工必保的原声区间（与自动挑选的 raw_insert 同质）；
    同视频时间重叠时人工赢（避免渲染重复内容），无重叠则两者并存。
    """
    extra: list[dict] = []
    for req in keep_reqs:
        rs, re = float(req["start"]), float(req["end"])
        vid = req["video_id"]
        # 剔除与人工区间重叠的自动 pick（该台词区间已由人工区间覆盖）
        picks = [
            p for p in picks
            if p["video_id"] != vid
            or not _overlaps(p["start"], p["end"], rs, re)
        ]
        extra.append({
            "video_id": vid,
            "start": round(rs, 3),
            "end": round(re, 3),
            "line_id": None,
            "speaker": "",
            "text": req.get("matched_text") or req.get("note", ""),
            "quality": "keep",
            "reason": f"人工保留: {req.get('note', '')}",
        })
    return picks + extra


def build_raw_edl(timeline: dict, picks: list[dict],
                  video_order: dict[str, int],
                  workspace_of) -> dict:
    """构造全 raw_insert EDL（与 stage_select.build_edl 输出结构对齐）。

    每个 clip 携带 line_id/speaker/text/quality/reason/frames，供分镜板展示。
    shot_ids=[] → footage_usage 豁免（register_usage 按 shot_ids 记账，空即不记）。
    """
    lines_by_id = {l["id"]: l for l in timeline.get("lines", [])}
    line_to_shot = _build_shot_index(timeline.get("shots", []))

    clips = []
    for p in picks:
        lid = p["line_id"]
        shot = line_to_shot.get(lid, {}) if lid is not None else {}
        clip = {
            "type": "raw_insert",
            "video_id": p["video_id"],
            "start": p["start"],
            "end": p["end"],
            "keep_audio": True,
            "line_id": lid,
            "speaker": p["speaker"],
            "text": p["text"],
            "quality": p["quality"],
            "reason": p["reason"],
            "class": shot.get("class", "A"),
            "shot_ids": [],   # B 模式豁免 footage_usage
            "frames": _frame_paths(shot["id"], workspace_of(p["video_id"]))
                if shot and "id" in shot else [],
            "candidates": [],
        }
        clips.append(clip)

    clips.sort(key=lambda c: (video_order.get(c["video_id"], 99), c["start"]))
    return {
        "video_id": (timeline.get("videos") or [{}])[0].get("video_id", ""),
        "clips": clips,
        "footage_usage": [],   # B 模式豁免
        "keep_requirements": [],
    }


def ensure_segments(segments_dir: Path, timeline_path: Path,
                    work_dir: Path, auto_low_extract: bool) -> dict:
    """确保 segments 覆盖 timeline 全部视频且含 v2 line_marks；缺失时兜底跑 low-only。

    - 以 timeline 涉及的 video_id 集合为基准校验覆盖度（不仅看现有文件是否合法）：
      第一次运行中途失败会留下部分落盘段，仅校验"现有文件合法"会漏掉未跑的视频
    - 指纹/段级校验交给 run_low_only 内部的 _read_valid_cache：指纹匹配的段命中
      缓存跳过，不匹配的重跑（旧 v1 segments 缺 line_marks 由此自动升级到 v2）
    - auto_low_extract=False 且不完整 → 抛错（让用户显式决策）
    返回兜底执行摘要；若已就绪返回 {"skipped": True}。
    """
    if _segments_complete(segments_dir, timeline_path):
        return {"skipped": True, "reason": "segments 覆盖全部视频且含 line_marks"}
    if not auto_low_extract:
        raise RuntimeError(
            "narration_segments 不完整（缺视频或缺 line_marks）且 auto_low_extract=False；"
            "请先跑 mmm run narrate --task <id>（A 模式）或设 auto_low_extract=true"
        )
    from . import stage_narrate_low

    return stage_narrate_low.run_low_only(timeline_path, work_dir)


def _segments_complete(segments_dir: Path, timeline_path: Path) -> bool:
    """timeline 涉及的每个 video_id 是否都有含 line_marks 的 segment 文件。

    完整性以 video 覆盖度为基准（timeline 的 videos + lines 所属视频）；
    不重新推导 chunk 切分（那需要 endpoint 配置，测试/离线场景拿不到），
    段级与指纹级校验由 run_low_only 逐段处理。
    """
    if not segments_dir.exists() or not any(segments_dir.glob("*.json")):
        return False
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    needed = {v["video_id"] for v in timeline.get("videos", [])
              if v.get("video_id")}
    if not needed:   # 单视频 timeline（无 videos 块）退化为首行 video_id
        lines = timeline.get("lines", [])
        if lines and lines[0].get("video_id"):
            needed = {lines[0]["video_id"]}
    covered: set[str] = set()
    for f in segments_dir.glob("*.json"):
        try:
            seg = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        beats = seg.get("beats", [])
        vid = seg.get("video_id", "")
        if vid and beats and all(b.get("line_marks") for b in beats):
            covered.add(vid)
    return needed.issubset(covered)


def run(work_dir: Path, video_id: str, *, timeline_name: str = "global_timeline.json",
        quality_levels: list[str] | None = None,
        buffer_sec: float = 0.0,
        min_shot_class: str = "B",
        prefer_ui_types: list[str] | None = None,
        keep_requirements: list[dict] | None = None,
        exclude_task: str = "") -> dict:
    """B 模式选片：quality 叙事维度 + shots 画面维度 → 全 raw_insert 的 edl.json。

    依赖阶段1-4 产物（global_timeline.json）+ 阶段5 的 line_marks（per-line quality）。
    不依赖 narration.json（A 模式解说终稿）。

    min_shot_class 一票否决：shot class 低于此值的台词剔除（画面底线优先，
    quality 再高也不救画面不达标的台词）。class 排序 E>D>C>B>A（越小越好）。
    """
    from .db import PROJECT_ROOT

    quality_levels = quality_levels or ["great"]
    prefer_ui_types = prefer_ui_types or ["dialogue"]
    timeline_path = work_dir / timeline_name
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    segments_dir = work_dir / "narration_segments"

    # 1. 确保 segments 含 line_marks（无则兜底跑 low-only）
    ensure_segments(segments_dir, timeline_path, work_dir, auto_low_extract=True)

    # 2. 叙事维度：从 line_marks 取 quality 达标的 line_id
    quality_lines, qstats = _collect_quality_lines(segments_dir, quality_levels)

    # 3. 画面维度 + 时间戳过滤
    lines_by_id = {l["id"]: l for l in timeline.get("lines", [])}
    line_to_shot = _build_shot_index(timeline.get("shots", []))
    video_durs = {v["video_id"]: v["duration"] for v in timeline.get("videos", [])}
    min_rank = CLASS_RANK.get(min_shot_class, CLASS_RANK["B"])

    picks: list[dict] = []
    skipped_no_ts = 0
    skipped_class = 0
    for lid, info in quality_lines.items():
        line = lines_by_id.get(lid)
        if line is None:
            continue
        if line.get("align") not in TIMED_ALIGNS:
            skipped_no_ts += 1
            continue
        shot = line_to_shot.get(lid, {})
        cls = shot.get("class", "A")
        # 画面维度一票否决：class 低于下限剔除（X 排在最末，必然被否决）
        if CLASS_RANK.get(cls, CLASS_RANK["A"]) > min_rank:
            skipped_class += 1
            continue
        pick = _make_pick(line, video_durs.get(line["video_id"], 1e9),
                          info["quality"], info["reason"], buffer_sec)
        if pick:
            picks.append(pick)

    # 4. 合并人工 keep_requirements + 去重 + 排序
    if keep_requirements:
        picks = _merge_keep_requirements(picks, keep_requirements)
    video_order = {v["video_id"]: i for i, v in enumerate(timeline.get("videos", []))}
    picks = _dedup_and_sort(picks)

    # 5. 构造全 raw_insert EDL
    def ws_of(vid: str) -> Path:
        return PROJECT_ROOT / "workspace" / vid

    edl = build_raw_edl(timeline, picks, video_order, ws_of)
    (work_dir / "edl.json").write_text(
        json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. 分镜板（复用 build_storyboard，模板已支持 raw_insert + frames）
    storyboard_path = work_dir / "storyboard.html"
    reviewer.build_storyboard(
        edl, storyboard_path,
        task_id=video_id,
        title=f"{video_id} 原声高光分镜板",
        frames_base=PROJECT_ROOT,
        chars_per_sec=4.5,
        tts_speed=1.0,
        embed_frames=False,
    )

    return {
        "clips": len(edl["clips"]),
        "needs_review": 0,
        "excluded_used_shots": 0,
        "total_source_seconds": round(sum(c["end"] - c["start"] for c in edl["clips"]), 2),
        "skipped_no_timestamp": skipped_no_ts,
        "skipped_low_class": skipped_class,
        "quality_stats": qstats,
        "edl": str(work_dir / "edl.json"),
        "storyboard": str(storyboard_path),
    }
