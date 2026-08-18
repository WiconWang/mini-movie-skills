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
    metas = json.loads(meta_path.read_text()) if meta_path.exists() else []

    timeline = build_timeline(shots, fades, lines, metas)
    (work_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return timeline["stats"]
