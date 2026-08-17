"""分镜板构建器：把 EDL + 帧图注入固化模板，产出自包含单文件 HTML。

设计约束（见设计文档 §阶段6 闸口2）：
- 模板是固化工具，一次开发全部复用；禁止 LLM 每次生成 HTML
- 产物自包含（帧图 base64 内联），双击即开，无需起服务
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from .db import PROJECT_ROOT

TEMPLATE = (PROJECT_ROOT / ".claude/skills/mini-movie-maker/tools/reviewer/storyboard_template.html")


def _img_data_uri(path: Path, max_bytes: int = 400_000) -> str:
    """图片转 base64 data URI（超尺寸跳过失真也不影响评审，仅防御）。"""
    if not path.exists() or path.stat().st_size > max_bytes:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _resolve_frames(clip: dict, base: Path) -> dict:
    """把 clip 里的帧图路径转成 data URI。"""
    clip = dict(clip)
    clip["frames"] = [u for u in (_img_data_uri(base / f) if isinstance(f, str) and not f.startswith("data:") else f
                                  for f in clip.get("frames", [])) if u]
    for cand in clip.get("candidates", []):
        if "frame" in cand and not cand["frame"].startswith("data:"):
            cand["frame"] = _img_data_uri(base / cand["frame"])
    return clip


def build_storyboard(edl: dict, out_path: Path, *, task_id: str, title: str = "",
                     target_minutes: float | None = None, frames_base: Path | None = None) -> Path:
    """注入数据生成自包含分镜板。frames_base：帧图路径的基准目录。"""
    base = frames_base or PROJECT_ROOT
    edl = dict(edl)
    edl["clips"] = [_resolve_frames(c, base) for c in edl["clips"]]

    payload = json.dumps({"task_id": task_id, "title": title,
                          "target_minutes": target_minutes, "edl": edl},
                         ensure_ascii=False)
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
