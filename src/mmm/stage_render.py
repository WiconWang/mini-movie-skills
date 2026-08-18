"""阶段7 导出器A：ffmpeg 直出 MP4（MVP）。

按 EDL 逐片段：切源视频（重编码）+ 解说配音 → 片段级音画对齐 → concat 成片。

MVP 约定（与最终设计的差距，逐步补齐）：
- TTS 用 macOS 本地 `say`（Tingting）占位，零成本验证端到端；云 TTS（火山/豆包）后续接入
- 无片头（composition）、无 BGM、无字幕烧录
- transform 裁 LOGO：系列级 + per-clip 覆盖
- 片段时长 = max(源区间时长, TTS 时长)：TTS 更长时冻结末帧补齐（tpad clone）
- keep_audio 的 raw_insert 段保留原声、不配解说
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

FPS = 30
SCALE = "scale=1280:-2"   # MVP 统一 720p 输出（concat 要求各片段参数一致）
TTS_VOICE = "Tingting"


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def tts_say(text: str, out_wav: Path, voice: str = TTS_VOICE) -> float:
    """本机 say 合成 → wav，返回实际时长（秒）。"""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        aiff = Path(f.name)
    try:
        _run(["say", "-v", voice, "-o", str(aiff), text])
        _run(["ffmpeg", "-y", "-v", "quiet", "-i", str(aiff),
              "-ar", "48000", "-ac", "1", str(out_wav)])
        return _duration(out_wav)
    finally:
        aiff.unlink(missing_ok=True)


def render_segment(video: Path, clip: dict, tts_wav: Path | None,
                   out_path: Path, transform: dict | None = None) -> float:
    """渲染单个片段（视频重编码 + transform + 音轨对齐到片段时长），返回片段时长。"""
    v_dur = clip["end"] - clip["start"]
    a_dur = _duration(tts_wav) if tts_wav else 0.0
    seg = max(v_dur, a_dur, 0.5)
    pad_v = max(seg - v_dur, 0.0)

    # transform 链：放大 + 位移，系列级可被 per-clip 覆盖
    xf = dict(transform or {})
    xf.update(clip.get("transform") or {})
    scale = xf.get("scale", 1.0)
    offset_x = xf.get("offset_x", 0)
    offset_y = xf.get("offset_y", 0)
    if scale != 1.0 or offset_x or offset_y:
        # 先放大，再平移；必须让 LOGO 区出画，同时保留主体
        geo = f"scale=iw*{scale}:-2,setsar=1,crop=1280:720:(iw-1280)/2+{offset_x}:(ih-720)/2+{offset_y}"
    else:
        geo = SCALE

    vfilter = f"[0:v]tpad=stop_mode=clone:stop={pad_v:.2f},{geo},fps={FPS},format=yuv420p,setsar=1[v]"
    if clip.get("keep_audio"):
        # raw_insert：保留原声
        afilter = f"[0:a]apad=whole_dur={seg:.2f},aresample=48000[a]"
        amap_input = 0
    else:
        afilter = f"[1:a]apad=whole_dur={seg:.2f},aresample=48000[a]"
        amap_input = 1

    cmd = ["ffmpeg", "-y", "-v", "quiet",
           "-ss", f"{clip['start']:.3f}", "-t", f"{v_dur:.3f}", "-i", str(video)]
    if amap_input == 1:
        cmd += ["-i", str(tts_wav)]
    cmd += ["-filter_complex", vfilter + ";" + afilter,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "48000", "-ac", "1",
            "-t", f"{seg:.3f}", str(out_path)]
    _run(cmd)
    return seg


def _mix_with_bgm(video: Path, bgm: Path, out: Path) -> None:
    """把视频轨与 BGM 轨混音（BGM 已 ducking 处理，直接 amix 即可）。"""
    _run([
        "ffmpeg", "-y", "-v", "quiet",
        "-i", str(video), "-i", str(bgm),
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(out),
    ])


def _bgm_duck_regions(clips: list[dict]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """从 EDL 提取 narration 与 raw_insert 区间（全局时间）。"""
    narration = [(c["start"], c["end"]) for c in clips if c.get("type") == "narration_clip"]
    raw_insert = [(c["start"], c["end"]) for c in clips if c.get("type") == "raw_insert"]
    return narration, raw_insert


def _has_ass_filter() -> bool:
    """检测本机 ffmpeg 是否支持 ass/subtitles 滤镜。"""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, check=True)
        lines = r.stdout.splitlines()
        # 精确匹配滤镜名（行首格式如 "... ass" 或 " TSC ass"）
        return any(re.search(r"\bass\b|\bsubtitles\b", line) and
                   ("->" in line or "Apply" in line)
                   for line in lines)
    except Exception:
        return False


def _mux_srt(video: Path, srt: Path, out: Path) -> None:
    """把 SRT 作为软字幕轨封装进 MP4。"""
    _run([
        "ffmpeg", "-y", "-v", "quiet",
        "-i", str(video),
        "-i", str(srt),
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=chi",
        str(out),
    ])


def _burn_subtitles(video: Path, ass: Path, out: Path) -> None:
    """把 ASS 字幕烧录进视频（需要 ffmpeg 启用 libass）。"""
    # ffmpeg ass 滤镜要求路径中的特殊字符（逗号、冒号）用反斜杠转义
    escaped = str(ass).replace("\\", "/").replace(":", "\\:").replace(",", "\\,")
    _run([
        "ffmpeg", "-y", "-v", "quiet",
        "-i", str(video),
        "-vf", f"ass={escaped}",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        str(out),
    ])


def run(work_dir: Path, videos: dict[str, Path], out_path: Path | None = None,
        task_id: str = "", bgm_playlist: list[str] | None = None,
        subtitle_mode: str = "overlay") -> dict:
    """按 edl.json 渲染成片。videos: video_id → 源视频路径（多视频任务各片段可来自不同源）。

    task_id 非空时：渲染成功后登记 footage_usage（以导出时 EDL 为准）
    并归档 edl.final.json 到输出目录（设计文档 §4 阶段7 单向数据流）。
    """
    edl = json.loads((work_dir / "edl.json").read_text())
    clips = edl["clips"]

    # 任务级 transform（系列配置）；edl 或 clip 可覆盖
    transform = edl.get("transform") or {}
    if task_id:
        from .db import PROJECT_ROOT

        cfg_path = PROJECT_ROOT / "tasks" / task_id / "task.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            transform = cfg.get("transform") or transform

    out_path = out_path or work_dir / "render.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seg_dir = work_dir / "render_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_files = []
    total = 0.0
    for i, clip in enumerate(clips):
        video = videos.get(clip["video_id"])
        if video is None:
            raise KeyError(f"EDL 片段引用未提供的 video_id: {clip['video_id']}")
        wav = None
        if not clip.get("keep_audio"):
            wav = seg_dir / f"tts_{i:03d}.wav"
            tts_say(clip["text"], wav)
        seg_path = seg_dir / f"seg_{i:03d}.mp4"
        total += render_segment(video, clip, wav, seg_path, transform)
        seg_files.append(seg_path)

    # concat（各片段编码参数一致，可 -c copy 无损拼接；路径需绝对，避免相对基准歧义）
    list_file = seg_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_files))
    raw_path = out_path.with_suffix(".raw" + out_path.suffix) if out_path else work_dir / "render.raw.mp4"
    _run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
          "-i", str(list_file), "-c", "copy", str(raw_path)])

    # 若有 BGM 配置，生成与成片等长的 BGM 轨并混音
    bgm_path: Path | None = None
    if bgm_playlist:
        from . import stage_bgm

        narration_regions, raw_regions = _bgm_duck_regions(clips)
        bgm_path = stage_bgm.build_bgm_track(
            bgm_playlist, total,
            narration_regions=narration_regions,
            raw_insert_regions=raw_regions,
            out_path=out_path.parent / "bgm.wav" if out_path else work_dir / "bgm.wav",
        )
        mixed_path = out_path.with_suffix(".mixed" + out_path.suffix) if out_path else work_dir / "render.mixed.mp4"
        _mix_with_bgm(raw_path, bgm_path, mixed_path)
        raw_path.unlink(missing_ok=True)
        raw_path = mixed_path

    # 字幕烧录/封装（overlay 模式：优先硬字幕，ffmpeg 不支持则回退软字幕 SRT）
    if subtitle_mode == "overlay":
        from . import stage_subtitle

        subs = stage_subtitle.run(work_dir, mode="overlay")
        if _has_ass_filter():
            tmp_out = out_path or work_dir / "render_sub.mp4"
            _burn_subtitles(raw_path, Path(subs["ass"]), tmp_out)
            raw_path.unlink(missing_ok=True)
            if out_path is None:
                out_path = tmp_out
        else:
            tmp_out = out_path or work_dir / "render_sub.mp4"
            _mux_srt(raw_path, Path(subs["srt"]), tmp_out)
            raw_path.unlink(missing_ok=True)
            if out_path is None:
                out_path = tmp_out
    elif subtitle_mode == "letterbox":
        raise NotImplementedError("letterbox 字幕模式尚未实现")
    else:
        # 无字幕模式：raw 直接就是成片
        if raw_path != out_path:
            if out_path is None:
                out_path = raw_path
            else:
                raw_path.rename(out_path)

    registered = 0
    final_output = out_path
    if task_id:
        from .catalog import register_usage

        registered = register_usage(task_id, clips)
        # 归档最终 EDL（复盘/二期素材库用；剪映手调不回流，以此为准）
        (out_path.parent / "edl.final.json").write_text(
            json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
        # 片头拼接
        from . import stage_compose

        final_output = stage_compose.from_task(task_id, out_path)

    return {"clips": len(clips), "duration": round(total, 1),
            "output": str(final_output), "footage_registered": registered,
            "bgm": str(bgm_path) if bgm_path else None}
