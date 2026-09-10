"""成片组装（composition）：固定四段式 Cover → 片头 → 正片 body → 片尾。

按 task.json composition 列表（未声明的段缺省跳过）：
- cover: 封面图（intro-maker 产出 1920×1080 JPG），图转固定时长视频（默认 2s）+ 补静音
- intro_common / intro_special: 外部片头视频，整段拼接，可选 transform 缩放/位移
- body: 解说稿正片（EDL 渲染结果，隐式段，恒在）
- outro: 片尾图片（与 cover 同构），图转固定时长视频（默认 2s）
  （原 outro_special 视频片尾彩蛋已废弃——见 docs/adr/0001，原声结尾片段改以 raw_insert 纳入 EDL）
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .media import ffmpeg_bin, ffprobe_bin


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"composition 命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


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


def _build_video_filter(target_w: int, target_h: int, target_fps: int,
                        transform: dict | None = None) -> str:
    """构造视频归一化滤镜：transform 非空时缩放+偏移裁切，否则等比缩放 pad 居中。

    transform 语义对齐 stage_render.render_segment 的 overlay_transform：
    scale 放大后按 offset_x/offset_y 裁到目标尺寸（用于片头裁 LOGO / 位移）。
    """
    scale = float((transform or {}).get("scale", 1.0))
    offset_x = float((transform or {}).get("offset_x", 0))
    offset_y = float((transform or {}).get("offset_y", 0))
    if scale != 1.0 or offset_x or offset_y:
        geo = (f"scale=iw*{scale}:-2,setsar=1,"
               f"crop={target_w}:{target_h}:(iw-{target_w})/2+{offset_x}:(ih-{target_h})/2+{offset_y}")
    else:
        geo = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
               f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    return f"{geo},fps={target_fps},format=yuv420p"


def _normalize_video(video: Path, out: Path, target_w: int = 1920, target_h: int = 1080,
                     target_fps: int = 30, transform: dict | None = None) -> None:
    """把片头视频统一成目标分辨率、yuv420p、目标 fps、aac 音轨（无音频则补静音）。

    必须固定 fps：片头若 fps 与正片不同，concat 后容器 fps 标记被片头污染，
    导致成片标错帧率（ffprobe 实测 hd-p1_final 曾出现 60fps 污染 30fps）。
    """
    vf = _build_video_filter(target_w, target_h, target_fps, transform)
    if _has_audio(video):
        cmd = [
            ffmpeg_bin(), "-y", "-v", "quiet",
            "-i", str(video),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(out),
        ]
    else:
        cmd = [
            ffmpeg_bin(), "-y", "-v", "quiet",
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


def _normalize_image(image: Path, out: Path, target_w: int = 1920, target_h: int = 1080,
                     target_fps: int = 30, duration: float = 2.0) -> None:
    """静态图 → 固定时长视频 + 静音音轨（cover/outro 用，与 cover 同构）。

    -loop 1 -t <duration> 把图片变视频；补 anullsrc 静音轨保证 concat 后音轨连续。
    """
    vf = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
          f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},format=yuv420p")
    cmd = [
        ffmpeg_bin(), "-y", "-v", "quiet",
        "-loop", "1", "-t", f"{duration:.3f}",
        "-i", str(image),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest",
        "-t", f"{duration:.3f}",
        str(out),
    ]
    _run(cmd)


def compose(cover: Path | None, intro_files: list[Path], body: Path,
            outro: Path | None, out: Path,
            out_w: int = 1920, out_h: int = 1080, out_fps: int = 30,
            cover_duration: float = 2.0, outro_duration: float = 2.0) -> None:
    """固定四段式拼接：Cover → 片头们 → 正片 → 片尾。未提供段跳过。

    所有输入统一重编码到目标规格后 concat copy；cover/outro 为图片时转视频。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        normalized = []

        if cover is not None:
            norm = tmpdir / "cover.mp4"
            _normalize_image(cover, norm, out_w, out_h, out_fps, cover_duration)
            normalized.append(norm)

        for i, f in enumerate(intro_files):
            norm = tmpdir / f"intro_{i:03d}.mp4"
            _normalize_video(f, norm, out_w, out_h, out_fps)
            normalized.append(norm)

        body_norm = tmpdir / "body.mp4"
        _normalize_video(body, body_norm, out_w, out_h, out_fps)
        normalized.append(body_norm)

        if outro is not None:
            norm = tmpdir / "outro.mp4"
            _normalize_image(outro, norm, out_w, out_h, out_fps, outro_duration)
            normalized.append(norm)

        list_file = tmpdir / "concat.txt"
        list_file.write_text("".join(f"file '{p}'\n" for p in normalized))
        _run([ffmpeg_bin(), "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", str(out)])


def from_task(task_id: str, body_path: Path) -> Path:
    """读取 task.json composition，生成最终成片。

    固定四段顺序分流：cover → intro_common/intro_special → body → outro。
    composition 未声明的段缺省跳过；outro_special 类型不复存在（ADR-0001）。
    无外段时正片即最终成片，直接返回避免大文件复制副本。
    """
    from .paths import DATA_ROOT

    task_dir = DATA_ROOT / "tasks" / task_id
    cfg = json.loads((task_dir / "task.json").read_text())
    composition = cfg.get("composition", [])

    cover: Path | None = None
    intro_files: list[Path] = []
    outro: Path | None = None
    for item in composition:
        t = item.get("type")
        # composition 以 asset_id 引用版本级物料（0910 规范 §12.5）
        p = None
        if item.get("asset_id") is not None:
            from .catalog import asset_by_id

            a = asset_by_id(item["asset_id"])
            p = DATA_ROOT / a["path"]
        elif item.get("src"):
            src = item["src"]
            p = Path(src) if Path(src).is_absolute() else DATA_ROOT / src
        if t == "cover" and p is not None:
            if not p.exists():
                raise FileNotFoundError(f"Cover 图片不存在: {p}")
            cover = p
        elif t in ("intro", "intro_common", "intro_special") and p is not None:
            if not p.exists():
                raise FileNotFoundError(f"片头素材不存在: {p}")
            intro_files.append(p)
        elif t == "outro" and p is not None:
            if not p.exists():
                raise FileNotFoundError(f"片尾图片不存在: {p}")
            outro = p

    if cover is None and not intro_files and outro is None:
        return body_path

    out_dir = DATA_ROOT / "output" / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # final 名继承正片 stem（含时间戳），避免历史版本互相覆盖
    out_path = out_dir / f"{body_path.stem}_final{body_path.suffix}"
    out_cfg = cfg.get("output") or {}
    compose(cover, intro_files, body_path, outro, out_path,
            out_w=int(out_cfg.get("width", 1920)),
            out_h=int(out_cfg.get("height", 1080)),
            out_fps=int(out_cfg.get("fps", 30)))
    return out_path
