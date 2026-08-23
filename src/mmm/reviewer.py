"""分镜板构建器：把 EDL + 帧图注入固化模板，产出本地可打开的 HTML。

设计约束（见设计文档 §阶段6 闸口2）：
- 模板是固化工具，一次开发全部复用；禁止 LLM 每次生成 HTML
- 帧图默认以相对路径引用（文件在 workspace/tasks 内，无需起服务）；
  embed_frames=True 时仍可内联为 base64（适合拷给他人单文件分享）
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path

from .db import PROJECT_ROOT

TEMPLATE = (PROJECT_ROOT / "skills/mini-movie-maker/tools/reviewer/storyboard_template.html")


def _img_data_uri(path: Path, max_bytes: int = 400_000) -> str:
    """图片转 base64 data URI（超尺寸跳过失真也不影响评审，仅防御）。"""
    if not path.exists() or path.stat().st_size > max_bytes:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _resolve_frames(clip: dict, base: Path, out_dir: Path,
                    embed_frames: bool = True) -> dict:
    """把 clip 里的帧图路径转成浏览器可用的 src。

    embed_frames=True 时内联 base64；False 时转成相对 storyboard.html 的路径，
    避免几十 MB 的单文件膨胀。
    """
    clip = dict(clip)

    def _to_uri(p) -> str:
        if isinstance(p, str) and p.startswith("data:"):
            return p
        path = Path(p) if Path(p).is_absolute() else base / p
        if embed_frames:
            return _img_data_uri(path)
        if not path.exists():
            return ""
        try:
            return os.path.relpath(path, start=out_dir).replace(os.sep, "/")
        except ValueError:
            return str(path)

    clip["frames"] = [u for u in (_to_uri(f) for f in clip.get("frames", [])) if u]
    for cand in clip.get("candidates", []):
        if "frame" in cand and not cand["frame"].startswith("data:"):
            cand["frame"] = _to_uri(cand["frame"])
    return clip


def build_storyboard(edl: dict, out_path: Path, *, task_id: str, title: str = "",
                     target_minutes: float | None = None, frames_base: Path | None = None,
                     chars_per_sec: float = 4.5, tts_speed: float = 1.0,
                     embed_frames: bool = True) -> Path:
    """注入数据生成分镜板。

    frames_base：帧图路径的基准目录；chars_per_sec/tts_speed 用于估算解说片段时长。
    embed_frames=False 时帧图用相对路径引用，HTML 保持轻量。
    """
    base = frames_base or PROJECT_ROOT
    edl = dict(edl)
    out_dir = out_path.parent
    edl["clips"] = [_resolve_frames(c, base, out_dir, embed_frames) for c in edl["clips"]]

    payload = json.dumps({"task_id": task_id, "title": title,
                          "target_minutes": target_minutes, "edl": edl,
                          "chars_per_sec": chars_per_sec, "tts_speed": tts_speed},
                         ensure_ascii=False)
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
