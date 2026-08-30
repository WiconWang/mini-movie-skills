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

from .media import ffmpeg_bin, ffprobe_bin


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"composition 命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


def _normalize_video(video: Path, out: Path, target_w: int = 1920, target_h: int = 1080,
                     target_fps: int = 30, trim: tuple[float, float] | None = None) -> None:
    """把片头视频统一成目标分辨率、yuv420p、目标 fps、aac 音轨（无音频则补静音）。

    必须固定 fps：片头若 fps 与正片不同，concat 后容器 fps 标记被片头污染，
    导致成片标错帧率（ffprobe 实测 hd-p1_final 曾出现 60fps 污染 30fps）。
    """
    vf = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
          f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},format=yuv420p")
    input_args: list[str] = []
    if trim is not None:
        input_args += ["-ss", f"{trim[0]:.3f}", "-t", f"{trim[1] - trim[0]:.3f}"]
    if _has_audio(video):
        cmd = [
            ffmpeg_bin(), "-y", "-v", "quiet", *input_args,
            "-i", str(video),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(out),
        ]
    else:
        cmd = [
            ffmpeg_bin(), "-y", "-v", "quiet", *input_args,
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
            [ffprobe_bin(), "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
            check=True, capture_output=True, text=True)
        return "audio" in r.stdout
    except Exception:
        return False


def compose(intro_files: list[Path], body: Path, out: Path,
            out_w: int = 1920, out_h: int = 1080, out_fps: int = 30,
            outro_items: list[tuple[Path, tuple[float, float] | None]] | None = None) -> None:
    """把若干片头 + 正片按顺序拼接。所有输入统一重编码到目标规格后 concat copy。"""
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        normalized = []
        for i, f in enumerate(intro_files):
            norm = tmpdir / f"intro_{i:03d}.mp4"
            _normalize_video(f, norm, out_w, out_h, out_fps)
            normalized.append(norm)
        body_norm = tmpdir / "body.mp4"
        _normalize_video(body, body_norm, out_w, out_h, out_fps)
        normalized.append(body_norm)

        for i, (f, trim) in enumerate(outro_items or []):
            norm = tmpdir / f"outro_{i:03d}.mp4"
            _normalize_video(f, norm, out_w, out_h, out_fps, trim=trim)
            normalized.append(norm)

        list_file = tmpdir / "concat.txt"
        list_file.write_text("".join(f"file '{p}'\n" for p in normalized))
        _run([ffmpeg_bin(), "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", str(out)])


def from_task(task_id: str, body_path: Path) -> Path:
    """读取 task.json composition，生成最终成片。"""
    from .db import PROJECT_ROOT

    task_dir = PROJECT_ROOT / "tasks" / task_id
    cfg = json.loads((task_dir / "task.json").read_text())
    composition = cfg.get("composition", [])

    intro_files: list[Path] = []
    outro_items: list[tuple[Path, tuple[float, float] | None]] = []
    for item in composition:
        t = item.get("type")
        src = item.get("src")
        if t in ("intro_common", "intro_special") and src:
            p = Path(src) if Path(src).is_absolute() else PROJECT_ROOT / src
            if p.exists():
                intro_files.append(p)
            else:
                raise FileNotFoundError(f"片头素材不存在: {p}")
        elif t == "outro_special" and src:
            p = Path(src) if Path(src).is_absolute() else PROJECT_ROOT / src
            if not p.exists():
                raise FileNotFoundError(f"片尾素材不存在: {p}")
            trim = None
            if item.get("start") is not None and item.get("end") is not None:
                trim = (float(item["start"]), float(item["end"]))
            outro_items.append((p, trim))

    if intro_files or outro_items:
        out_dir = PROJECT_ROOT / "output" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # final 名继承正片 stem（含时间戳），避免历史版本互相覆盖
        out_path = out_dir / f"{body_path.stem}_final{body_path.suffix}"
        out_cfg = cfg.get("output") or {}
        compose(intro_files, body_path, out_path,
                out_w=int(out_cfg.get("width", 1920)),
                out_h=int(out_cfg.get("height", 1080)),
                out_fps=int(out_cfg.get("fps", 30)),
                outro_items=outro_items)
        return out_path
    # 无片头：正片即最终成片，直接返回避免大文件复制副本
    return body_path
