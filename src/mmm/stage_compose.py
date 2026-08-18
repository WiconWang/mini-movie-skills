"""片头拼接（composition）。

按 task.json composition 列表：
- intro_common: 通用片头（如品牌 logo）
- intro_special: 本任务特殊片头（如章节标题）
- body: 解说稿正片（EDL 渲染结果）

输出：把通用片头 + 特殊片头 + 正片按顺序 concat 成最终成片。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"composition 命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


def _normalize_video(video: Path, out: Path, target_w: int = 1280, target_h: int = 720,
                     target_ar: float = 16 / 9) -> None:
    """把片头视频统一成 1280x720、yuv420p、aac 音轨（无音频则补静音）。"""
    vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    if _has_audio(video):
        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", str(video),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(out),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", str(video),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf,
            "-filter_complex", "[1:a]anull[a]",
            "-map", "0:v", "-map", "[a]",
            "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(out),
        ]
    _run(cmd)


def _has_audio(video: Path) -> bool:
    """检测视频是否含音轨。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
            check=True, capture_output=True, text=True)
        return "audio" in r.stdout
    except Exception:
        return False


def compose(intro_files: list[Path], body: Path, out: Path) -> None:
    """把若干片头 + 正片按顺序拼接。所有输入会被重编码为统一参数后 concat copy。"""
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        normalized = []
        for i, f in enumerate(intro_files):
            norm = tmpdir / f"intro_{i:03d}.mp4"
            _normalize_video(f, norm)
            normalized.append(norm)
        body_norm = tmpdir / "body.mp4"
        _normalize_video(body, body_norm)
        normalized.append(body_norm)

        list_file = tmpdir / "concat.txt"
        list_file.write_text("".join(f"file '{p}'\n" for p in normalized))
        _run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", str(out)])


def from_task(task_id: str, body_path: Path) -> Path:
    """读取 task.json composition，生成最终成片。"""
    from .db import PROJECT_ROOT

    task_dir = PROJECT_ROOT / "tasks" / task_id
    cfg = json.loads((task_dir / "task.json").read_text())
    composition = cfg.get("composition", [])

    intro_files = []
    for item in composition:
        t = item.get("type")
        src = item.get("src")
        if t in ("intro_common", "intro_special") and src:
            p = PROJECT_ROOT / src
            if p.exists():
                intro_files.append(p)
            else:
                raise FileNotFoundError(f"片头素材不存在: {p}")

    out_dir = PROJECT_ROOT / "output" / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{cfg.get('task_id', 'render')}_final.mp4"

    if intro_files:
        compose(intro_files, body_path, out_path)
    else:
        # 无片头：正片即最终成片
        _run(["ffmpeg", "-y", "-v", "quiet", "-i", str(body_path),
              "-c", "copy", str(out_path)])
    return out_path
