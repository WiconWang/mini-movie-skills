"""阶段6：选片段 + 生成 EDL + 分镜板。

对解说稿每句，按 E→A 画面优先级从 timeline 中挑选源区间，
输出 EDL（相对时间轴）和本地轻量分镜板 HTML（设计文档 §4 阶段6）。

MVP 约定：
- 单视频任务，video_id 直接取自调用参数；
- TTS 时长按中文字符估算（默认 4.5 字/秒），尚未接入真实 TTS；
- 不考虑 raw_insert（闸口2 人工插入）；
- footage_usage：选片时查台账排除已占用镜头，导出时（stage_render）按 EDL 登记。
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


# v1.0.4：操作界面镜头改用 vision 的 ui_type=gameplay 准入否决（替代旧 _is_ui_only 关键词匹配，
# 关键词匹配有漏检；详见 docs/2026/0821-v1.0.4-画面UI分类与选片准入方案.md）


def _avoid_keep_intervals(start: float, end: float, keep_reqs: list[dict],
                          video_id: str) -> tuple[float, float] | None:
    """解说片段避让保留区间：裁剪与 raw_insert 重叠的部分，取未被占用的较长段。

    保留区间不排解说句（设计文档铁律）。若片段完全被保留区间覆盖返回 None（丢弃）。
    参数为本地时间，与 keep_requirements 同基准。
    """
    for req in keep_reqs:
        if req.get("video_id") != video_id:
            continue
        rs, re = req["start"], req["end"]
        if not _overlaps(start, end, rs, re):
            continue
        front = rs - start    # 保留区间前可用的画面
        back = end - re       # 保留区间后可用的画面
        if front >= back:
            end = min(end, rs)
        else:
            start = max(start, re)
    if end <= start:
        return None
    return start, end


def _shot_overlaps_keep(shot: dict, keep_reqs: list[dict],
                        default_video_id: str = "") -> bool:
    """镜头是否与某保留区间（同视频、本地时间重叠）冲突。

    保留区间（raw_insert）不排解说句：解说候选镜头若落在保留区间内则剔除。
    镜头用 local_start/local_end（本地时间），与 keep_requirements 同基准。
    """
    vid = shot.get("video_id") or default_video_id
    for req in keep_reqs:
        if req.get("video_id") != vid:
            continue
        ls = shot.get("local_start", shot["start"])
        le = shot.get("local_end", shot["end"])
        if _overlaps(ls, le, req["start"], req["end"]):
            return True
    return False


def _collect_candidates(shots: list[dict], t0: float, t1: float,
                        used: set[tuple[str, int]] | None = None,
                        default_video_id: str = "",
                        keep_reqs: list[dict] | None = None) -> tuple[list[dict], bool]:
    """收集与解说句时间区间重叠的镜头，按 E→A 排序；剔除 footage_usage 已占用镜头。

    返回 (候选列表, 是否兜底)。兜底 = 排除占用后候选耗尽，放回全部候选并标记人工复核。
    """
    cands = [s for s in shots if _overlaps(s["start"], s["end"], t0, t1)]
    # v1.0.4 准入门槛：ui_type=gameplay（操作界面）一票否决不入选
    cands = [s for s in cands if s.get("ui_type") != "gameplay"]
    # 排除过短镜头（<2s）——切到成片里频繁跳切会让观众眩晕（v1.0.2 与 vision 阶段同步废弃）
    cands = [s for s in cands if (s["end"] - s["start"]) >= 2.0]
    # 排除落在保留区间（raw_insert）内的镜头——保留区间不排解说句
    if keep_reqs:
        cands = [s for s in cands if not _shot_overlaps_keep(s, keep_reqs, default_video_id)]
    exhausted = False
    if used:
        fresh = [s for s in cands
                 if (s.get("video_id") or default_video_id, s["id"]) not in used]
        if fresh:
            cands = fresh
        elif cands:
            exhausted = True   # 候选被占用耗尽 → 闸口2 人工复核（设计文档 §4 阶段6 冲突兜底）
    cands.sort(key=lambda s: CLASS_RANK.get(s.get("class", "A"), 4))
    return cands, exhausted


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


def _frame_paths(shot_id: int, ws: Path) -> list[str]:
    """返回镜头已有的抽帧相对路径（相对项目根，用于分镜板内联）。

    只取单帧 f_*.jpg（960x540），排除 vision 用的三联拼图 grid.jpg——
    拼图 2880x540 在 flex 布局里会被 stretch 纵向拉高变形，不适合展示。
    """
    frames_dir = ws / "frames" / f"shot_{shot_id:03d}"
    if not frames_dir.exists():
        return []
    from .db import PROJECT_ROOT
    return [str(f.resolve().relative_to(PROJECT_ROOT))
            for f in sorted(frames_dir.glob("f_*.jpg"))]


def build_edl(timeline: dict, narration: list[dict], default_video_id: str,
              workspace_of=None, *, used_shots: set[tuple[str, int]] | None = None,
              chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
              keep_requirements: list[dict] | None = None) -> dict:
    """根据解说稿生成 EDL。

    多视频全局时间轴：shot/line 自带 video_id 与 local_start/local_end，
    EDL 片段记录 (video_id, 源内本地区间) —— 相对时间轴铁律。
    workspace_of: video_id → 该视频的 workspace 目录（找抽帧用）。
    keep_requirements: 人工指定的保留区间 [{video_id, start, end, note}]，
      生成 raw_insert 片段（原声原画）并入 EDL，区间内不排解说句；
      按源时间顺序与解说片段合流（后续内容整体后移）。
    """
    from .db import PROJECT_ROOT

    workspace_of = workspace_of or (lambda vid: PROJECT_ROOT / "workspace" / vid)

    def ws_of(vid: str) -> Path:
        return workspace_of(vid)

    lines = timeline.get("lines", [])
    lines_by_key = {(l.get("video_id"), l["id"]): l for l in lines}
    shots = timeline.get("shots", [])
    keep_reqs = keep_requirements or []
    clips = []
    usage = []

    def _resolve_line(rid):
        """related_line_ids 解析：支持 {video_id,line_id}（多视频融合）与纯 id（单视频 oneshot）。"""
        if isinstance(rid, dict):
            return lines_by_key.get((rid.get("video_id"), rid.get("line_id")))
        return next((x for x in lines if x["id"] == rid), None)

    # 保留区间登记占用镜头（避免解说选片重复使用）
    for req in keep_reqs:
        for s in shots:
            if s.get("video_id") != req["video_id"]:
                continue
            ls = s.get("local_start", s["start"])
            le = s.get("local_end", s["end"])
            if _overlaps(ls, le, req["start"], req["end"]):
                usage.append({"video_id": req["video_id"], "shot_id": s["id"]})

    for n in narration:
        related = [l for l in (_resolve_line(rid) for rid in n.get("related_line_ids", []))
                   if l is not None]
        timed = [l for l in related if l.get("start") is not None]
        if not timed:
            # 没有可用时间戳的句跳过（理论上不应发生）
            continue
        t0 = min(l["start"] for l in timed)
        t1 = max(l["end"] for l in timed)
        target_dur = _estimate_duration(n["text"], chars_per_sec)

        candidates, exhausted = _collect_candidates(
            shots, t0, t1, used_shots, default_video_id, keep_reqs)
        selected, all_cands = _pick_clip(candidates, t0, t1, target_dur)

        if not selected:
            # 兜底：直接用台词区间
            selected = [{"shot_id": None, "start": t0, "end": t1}]

        # 区间边界取所有选中镜头的 min/max：selected 按优先级收集（非时间序），
        # 直接取首尾会把时间倒序的镜头拼出负区间（start>end），ffmpeg 渲染直接崩
        clip_start = min(s["start"] for s in selected)
        clip_end = max(s["end"] for s in selected)
        shot_ids = [s["shot_id"] for s in selected if s["shot_id"] is not None]

        # 解析本片段的源视频与本地时间区间（相对时间轴：EDL 不记全局秒数）
        first_shot = next((s for s in candidates if s["id"] == shot_ids[0]), None) \
            if shot_ids else None
        vid = (first_shot or timed[0]).get("video_id") or default_video_id
        off = 0.0
        if first_shot is not None and "local_start" in first_shot:
            off = first_shot["start"] - first_shot["local_start"]   # 全局→本地 offset
        elif timed and "local_start" in timed[0]:
            # 兜底（候选为空，无镜头可依）：用引用台词行的 offset 换算，
            # 否则会把全局时间当本地时间写进 EDL，seek 超源视频时长直接渲染失败
            off = timed[0]["start"] - timed[0]["local_start"]
        local_start = round(clip_start - off, 2)
        local_end = round(clip_end - off, 2)

        # 避让保留区间：解说片段与 raw_insert 重叠时裁剪（保留区间不排解说句）
        if keep_reqs:
            adj = _avoid_keep_intervals(local_start, local_end, keep_reqs, vid)
            if adj is None:
                continue   # 解说句完全被保留区间覆盖，丢弃（raw_insert 覆盖该段）
            local_start, local_end = adj

        main_cand = next((k for k in all_cands if k["id"] == (shot_ids[0] if shot_ids else None)), None) \
            or (all_cands[0] if all_cands else None)
        clip = {
            "type": "narration_clip",
            "narration_id": n["id"],
            "text": n["text"],
            "video_id": vid,
            "start": local_start,
            "end": local_end,
            "class": (main_cand or {}).get("class", "A"),
            "keep_audio": False,
            "shot_ids": shot_ids,
            "frames": _frame_paths(shot_ids[0], ws_of(vid)) if shot_ids else [],
            "candidates": [
                {
                    "shot_id": c["id"],
                    "video_id": c.get("video_id") or default_video_id,
                    "class": c.get("class", "A"),
                    "start": c.get("local_start", c["start"]),
                    "end": c.get("local_end", c["end"]),
                    "description": c.get("description") or "",
                    "motion": c.get("motion") or "low",
                    "ui_type": c.get("ui_type"),
                    "frame": (_frame_paths(c["id"], ws_of(c.get("video_id") or default_video_id)) or [""])[0],
                }
                for c in all_cands[:5]
            ],
        }
        if exhausted:
            clip["needs_review"] = True   # 候选被占用耗尽，闸口2 人工复核（可强制放行）
        clips.append(clip)

        for s in selected:
            if s["shot_id"] is not None:
                usage.append({"video_id": vid, "shot_id": s["shot_id"]})

    # 保留区间 → raw_insert 片段（原声原画，keep_audio=True；区间内无解说）
    raw_clips = [
        {
            "type": "raw_insert",
            "video_id": req["video_id"],
            "start": req["start"],
            "end": req["end"],
            "keep_audio": True,
            "note": req.get("note", ""),
            "shot_ids": [],
            "candidates": [],
        }
        for req in keep_reqs
    ]
    clips = clips + raw_clips

    # 按 (视频在任务中的顺序, 源内起始) 排序——raw_insert 与解说按源时间合流，
    # 插入处后续内容整体后移由相对时间轴自动保证（渲染时按 EDL 顺序累计）
    video_order = {v["video_id"]: i for i, v in enumerate(timeline.get("videos", []))}
    clips.sort(key=lambda c: (video_order.get(c["video_id"], 99), c["start"]))

    return {
        "video_id": default_video_id,
        "clips": clips,
        "footage_usage": usage,
        "keep_requirements": keep_reqs,
    }


def run(work_dir: Path, video_id: str, *, timeline_name: str = "timeline.json",
        workspace_of=None, exclude_task: str = "",
        chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> dict:
    """从目录读取 narration.json + 时间轴，产出 edl.json + storyboard.html。

    单视频冒烟：work_dir=workspace/{vid}，timeline_name=timeline.json；
    任务模式：work_dir=tasks/{task_id}，timeline_name=global_timeline.json。
    exclude_task：复用排除时豁免本任务（允许重跑选片不被自己的旧登记卡住）。
    """
    from .catalog import used_shots
    from .db import PROJECT_ROOT

    narration_path = work_dir / "narration.json"
    timeline_path = work_dir / timeline_name
    narration = json.loads(narration_path.read_text())["narration"]
    timeline = json.loads(timeline_path.read_text())

    used = used_shots(exclude_task=exclude_task)

    # 任务级保留要求（人工指定 raw_insert 区间）：读 tasks/{task}/task.json 的 keep_requirements
    keep_reqs = []
    cfg_path = work_dir / "task.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        keep_reqs = cfg.get("keep_requirements", [])

    edl = build_edl(timeline, narration, video_id, workspace_of,
                    used_shots=used, chars_per_sec=chars_per_sec,
                    keep_requirements=keep_reqs)
    (work_dir / "edl.json").write_text(
        json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    storyboard_path = work_dir / "storyboard.html"
    cfg = {}
    cfg_path = work_dir / "task.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
    tts_cfg = cfg.get("tts") or {}
    tts_speed = float(tts_cfg.get("speed", 1.0))
    reviewer.build_storyboard(
        edl, storyboard_path,
        task_id=video_id,
        title=f"{video_id} 分镜板",
        frames_base=PROJECT_ROOT,
        chars_per_sec=chars_per_sec,
        tts_speed=tts_speed,
        embed_frames=False,
    )

    return {
        "clips": len(edl["clips"]),
        "needs_review": sum(1 for c in edl["clips"] if c.get("needs_review")),
        "excluded_used_shots": len(used),
        "total_source_seconds": round(sum(c["end"] - c["start"] for c in edl["clips"]), 2),
        "edl": str(work_dir / "edl.json"),
        "storyboard": str(storyboard_path),
    }
