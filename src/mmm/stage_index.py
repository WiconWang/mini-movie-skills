"""阶段4：时间轴索引构建。

合并阶段1~3 产出（shots/fades/lines/shots_meta），按规则融合出 A~E 画面分类，
形成全片唯一事实源 timeline.json（设计文档 §4 阶段4）。

分类判定规则（E 优先裁决）：
- E：起止处有黑/白屏区间 且 VLM 判定无 UI           —— 标准过场
- D：无台词覆盖 且 无UI 且 非纯静止                  —— 无台词运镜
- C：高动态（战斗/特效），不强制台词覆盖              —— 实战修正：战斗镜头常无台词
- B：有台词覆盖 且 有运镜/动作（medium）             —— 对话有镜头活动
- A：其余（静态对话、带UI的低动态实机画面）            —— 选片最末位
"""

from __future__ import annotations

import json
from pathlib import Path

MOTION_RANK = {"static": 0, "low": 1, "medium": 2, "high": 3}


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def classify(shot: dict, meta: dict | None, has_lines: bool, fades: list[dict]) -> str:
    """多信号融合分类（meta 为 None 时按保守默认处理）。"""
    has_ui = meta.get("has_ui", True) if meta else True
    motion = MOTION_RANK.get(meta.get("motion", "low"), 1) if meta else 1

    # E：镜头起止附近有黑/白屏（±1s 容差）且无 UI
    bounded = any(
        _overlaps(f["start"], f["end"], shot["start"] - 1.0, shot["start"] + 1.0) or
        _overlaps(f["start"], f["end"], shot["end"] - 1.0, shot["end"] + 1.0)
        for f in fades
    )
    if bounded and not has_ui:
        return "E"
    if not has_lines and not has_ui and motion >= 1:
        return "D"
    if motion >= 3:
        return "C"
    if has_lines and motion >= 2:
        return "B"
    return "A"


def build_timeline(shots: list[dict], fades: list[dict], lines: list[dict],
                   metas: list[dict]) -> dict:
    """融合四路信号 → timeline。lines 为对齐后的台词表（含 start/end）。"""
    meta_by_shot = {m["shot_id"]: m for m in metas}
    timed_lines = [l for l in lines
                   if l.get("align") in ("matched", "interpolated")
                   and l.get("start") is not None]

    out_shots = []
    for s in shots:
        meta = meta_by_shot.get(s["id"])
        covered = [l for l in timed_lines
                   if _overlaps(l["start"], l["end"], s["start"], s["end"])]
        cls = classify(s, meta, bool(covered), fades)
        out_shots.append({
            **s,
            "class": cls,
            "description": meta.get("description") if meta else None,
            "has_ui": meta.get("has_ui") if meta else None,
            "motion": meta.get("motion") if meta else None,
            "line_ids": [l["id"] for l in covered],
        })

    counts = {c: sum(1 for s in out_shots if s["class"] == c) for c in "EDCBA"}
    return {"shots": out_shots, "fades": fades, "lines": lines,
            "stats": {"shots": len(out_shots), "by_class": counts}}


def run(work_dir: Path) -> dict:
    """从 workspace 目录读取四路产物，写出 timeline.json。"""
    shots = json.loads((work_dir / "shots.json").read_text())["shots"]
    fades = json.loads((work_dir / "fades.json").read_text())["fades"]
    lines = json.loads((work_dir / "lines.json").read_text())["lines"]
    meta_path = work_dir / "shots_meta.json"
    metas_raw = json.loads(meta_path.read_text()) if meta_path.exists() else []
    # shots_meta.json 兼容两种形态：纯 list，或 {metas: [...]}（stage_vision.run 产出）
    metas = metas_raw["metas"] if isinstance(metas_raw, dict) else metas_raw

    timeline = build_timeline(shots, fades, lines, metas)
    (work_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return timeline["stats"]


def build_global(task_id: str) -> dict:
    """阶段4.5：多视频合流 → tasks/{task_id}/global_timeline.json。

    按 task_map.seq 拼接各视频 timeline，内部维护 offset 表；
    每条 shot/line/fade 保留 video_id 与 local_start/local_end，
    start/end 为任务全局时间（设计文档 §4 阶段4 多视频合流）。
    """
    from .catalog import task_videos
    from .db import PROJECT_ROOT

    videos = task_videos(task_id)
    if not videos:
        raise KeyError(f"任务无关联素材: {task_id}（先 mmm task-create）")

    m_shots: list[dict] = []
    m_lines: list[dict] = []
    m_fades: list[dict] = []
    videos_meta = []
    offset = 0.0
    for v in videos:
        vid = v["video_id"]
        tl_path = PROJECT_ROOT / "workspace" / vid / "timeline.json"
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
        dur = max((s["end"] for s in tl["shots"]), default=0.0)
        for s in tl["shots"]:
            m_shots.append({**s, "video_id": vid,
                            "local_start": s["start"], "local_end": s["end"],
                            "start": round(s["start"] + offset, 3),
                            "end": round(s["end"] + offset, 3)})
        for l in tl["lines"]:
            nl = {**l, "video_id": vid}
            if l.get("start") is not None:
                nl["local_start"], nl["local_end"] = l["start"], l["end"]
                nl["start"] = round(l["start"] + offset, 2)
                nl["end"] = round(l["end"] + offset, 2)
            m_lines.append(nl)
        for f in tl.get("fades", []):
            m_fades.append({**f, "video_id": vid,
                            "start": round(f["start"] + offset, 3),
                            "end": round(f["end"] + offset, 3)})
        videos_meta.append({"video_id": vid, "offset": round(offset, 3),
                            "duration": round(dur, 3)})
        offset += dur

    counts = {c: sum(1 for s in m_shots if s["class"] == c) for c in "EDCBA"}
    out = {"task_id": task_id, "videos": videos_meta,
           "shots": m_shots, "lines": m_lines, "fades": m_fades,
           "stats": {"shots": len(m_shots), "by_class": counts,
                     "duration": round(offset, 1)}}
    task_dir = PROJECT_ROOT / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "global_timeline.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out["stats"]
