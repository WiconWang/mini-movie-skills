"""阶段6：选片段 + 生成 EDL + 分镜板。

对解说稿每句，按 E→A 画面优先级从 timeline 中挑选源区间，
输出 EDL（相对时间轴）和自包含分镜板 HTML（设计文档 §4 阶段6）。

MVP 约定：
- 单视频任务，video_id 直接取自调用参数；
- TTS 时长按中文字符估算（默认 4.5 字/秒），尚未接入真实 TTS；
- 不考虑 raw_insert（闸口2 人工插入）；
- footage_usage 写入暂以日志/JSON 记录，未接台账（TODO）。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import reviewer

CLASS_RANK = {"E": 0, "D": 1, "C": 2, "B": 3, "A": 4}
DEFAULT_CHARS_PER_SEC = 4.5


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _estimate_duration(text: str, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> float:
    """按字数估算 TTS 时长（含少量气口缓冲）。"""
    return max(len(text) / chars_per_sec, 1.0) + 0.3


def _collect_candidates(shots: list[dict], t0: float, t1: float) -> list[dict]:
    """收集与解说句时间区间重叠的镜头，并按 E→A 排序。"""
    cands = [s for s in shots if _overlaps(s["start"], s["end"], t0, t1)]
    cands.sort(key=lambda s: CLASS_RANK.get(s.get("class", "A"), 4))
    return cands


def _pick_clip(candidates: list[dict], t0: float, t1: float,
               target_dur: float) -> tuple[list[dict], list[dict]]:
    """从候选池按优先级挑镜头，直到凑够目标时长。

    返回 (selected_intervals, candidates_for_storyboard)。
    selected_intervals 每项：{shot_id, start, end}。
    """
    selected = []
    remain = target_dur
    # 第一优先级：台词区间内的高质量镜头
    for s in candidates:
        if remain <= 0:
            break
        cs = max(s["start"], t0)
        ce = min(s["end"], t1)
        if ce <= cs:
            continue
        dur = min(ce - cs, remain)
        selected.append({"shot_id": s["id"], "start": cs, "end": round(cs + dur, 2)})
        remain -= dur

    # 第二优先级：如果还不够，向相邻镜头扩展（取镜头头/尾，避免跳太远）
    if remain > 0 and candidates:
        # 简单策略：从候选中最后一个镜头的结束处向后取相邻镜头
        last = max(candidates, key=lambda s: s["end"])
        # 找与 last 相接的下一个镜头
        next_shots = [s for s in candidates if s["start"] >= last["end"]]
        next_shots.sort(key=lambda s: s["start"])
        for s in next_shots:
            if remain <= 0:
                break
            dur = min(s["end"] - s["start"], remain)
            selected.append({"shot_id": s["id"], "start": s["start"], "end": round(s["start"] + dur, 2)})
            remain -= dur

    return selected, candidates


def _frame_paths(shot_id: int, work_dir: Path) -> list[str]:
    """返回镜头已有的抽帧相对路径（用于分镜板内联）。"""
    frames_dir = work_dir / "frames" / f"shot_{shot_id:03d}"
    if not frames_dir.exists():
        return []
    return [str(f.relative_to(work_dir)) for f in sorted(frames_dir.glob("*.jpg"))]


def build_edl(timeline: dict, narration: list[dict], video_id: str,
              work_dir: Path, *, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> dict:
    """根据解说稿生成 EDL。"""
    lines_by_id = {l["id"]: l for l in timeline.get("lines", [])}
    shots = timeline.get("shots", [])
    clips = []
    usage = []

    for n in narration:
        related = [lines_by_id[rid] for rid in n.get("related_line_ids", [])
                   if rid in lines_by_id]
        timed = [l for l in related if l.get("start") is not None]
        if not timed:
            # 没有可用时间戳的句跳过（理论上不应发生）
            continue
        t0 = min(l["start"] for l in timed)
        t1 = max(l["end"] for l in timed)
        target_dur = _estimate_duration(n["text"], chars_per_sec)

        candidates = _collect_candidates(shots, t0, t1)
        selected, all_cands = _pick_clip(candidates, t0, t1, target_dur)

        if not selected:
            # 兜底：直接用台词区间
            selected = [{"shot_id": None, "start": t0, "end": t1}]

        clip_start = selected[0]["start"]
        clip_end = selected[-1]["end"]
        shot_ids = [s["shot_id"] for s in selected if s["shot_id"] is not None]

        clips.append({
            "type": "narration_clip",
            "narration_id": n["id"],
            "text": n["text"],
            "video_id": video_id,
            "start": round(clip_start, 2),
            "end": round(clip_end, 2),
            "keep_audio": False,
            "shot_ids": shot_ids,
            "frames": _frame_paths(shot_ids[0], work_dir) if shot_ids else [],
            "candidates": [
                {
                    "shot_id": c["id"],
                    "class": c.get("class", "A"),
                    "description": c.get("description") or "",
                    "motion": c.get("motion") or "low",
                    "has_ui": c.get("has_ui"),
                    "frame": _frame_paths(c["id"], work_dir)[0] if _frame_paths(c["id"], work_dir) else "",
                }
                for c in all_cands[:5]
            ],
        })

        for s in selected:
            if s["shot_id"] is not None:
                usage.append({"video_id": video_id, "shot_id": s["shot_id"]})

    return {
        "video_id": video_id,
        "clips": clips,
        "footage_usage": usage,
    }


def run(work_dir: Path, video_id: str, *, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> dict:
    """从 workspace 读取 narration.json + timeline.json，产出 edl.json + storyboard.html。"""
    narration_path = work_dir / "narration.json"
    timeline_path = work_dir / "timeline.json"
    narration = json.loads(narration_path.read_text())["narration"]
    timeline = json.loads(timeline_path.read_text())

    edl = build_edl(timeline, narration, video_id, work_dir, chars_per_sec=chars_per_sec)
    (work_dir / "edl.json").write_text(
        json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    storyboard_path = work_dir / "storyboard.html"
    reviewer.build_storyboard(
        edl, storyboard_path,
        task_id=video_id,
        title=f"{video_id} 分镜板",
        frames_base=work_dir,
    )

    return {
        "clips": len(edl["clips"]),
        "total_source_seconds": round(sum(c["end"] - c["start"] for c in edl["clips"]), 2),
        "edl": str(work_dir / "edl.json"),
        "storyboard": str(storyboard_path),
    }
