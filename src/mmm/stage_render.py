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
import re
import subprocess
import tempfile
from pathlib import Path

from .media import ffmpeg_bin, ffprobe_bin, probe_duration

# 输出规格缺省值（无 task.json 的冒烟路径用）；任务模式从 task.json output 读取
DEFAULT_OUT_W, DEFAULT_OUT_H, DEFAULT_OUT_FPS = 1920, 1080, 30
TTS_VOICE = "Tingting"    # macOS say 占位音色
EDGE_VOICE = "zh-CN-XiaoyiNeural"   # edge-tts 默认音色（女声活泼，解说调性）


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


TTS_MIN_INTERVAL = 1.5   # edge-tts 白嫖接口限速：两次调用最小间隔（秒）
_last_tts_call = 0.0


def tts_say(text: str, out_wav: Path, voice: str = TTS_VOICE) -> float:
    """本机 say 合成 → wav，返回实际时长（秒）。"""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        aiff = Path(f.name)
    try:
        _run(["say", "-v", voice, "-o", str(aiff), text])
        _run([ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(aiff),
              "-ar", "48000", "-ac", "1", str(out_wav)])
        return probe_duration(out_wav)
    finally:
        aiff.unlink(missing_ok=True)


def tts_edge(text: str, out_wav: Path, voice: str = EDGE_VOICE, rate: str = "+0%") -> float:
    """edge-tts 合成 → wav（微软 Edge 朗读接口，免费无 key，需联网；仅验证用）。

    edge-tts 是免费白嫖接口，有两个坑：
    1. 偶发 NoAudioReceived（连接微软服务瞬时失败）→ 重试 3 次，间隔 3s/6s/9s；
    2. 连续快速请求会被限流（实测 5 句内必现）→ 全局最小间隔 1.5s/句。
    """
    import asyncio
    import time

    import edge_tts

    global _last_tts_call
    last_err: Exception | None = None
    for attempt in range(3):
        # 限速：与上一次 TTS 调用至少间隔 1.5s（白嫖接口被限流的实测对策）
        gap = time.time() - _last_tts_call
        if gap < TTS_MIN_INTERVAL:
            time.sleep(TTS_MIN_INTERVAL - gap)
        _last_tts_call = time.time()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3 = Path(f.name)
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(mp3)))
            _run([ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(mp3),
                  "-ar", "48000", "-ac", "1", str(out_wav)])
            return probe_duration(out_wav)
        except Exception as e:
            last_err = e
            time.sleep(3 * (attempt + 1))   # 3s → 6s → 9s
        finally:
            mp3.unlink(missing_ok=True)
    raise RuntimeError(f"edge-tts 重试 3 次仍失败: {last_err}")


def synthesize(text: str, out_wav: Path, tts_cfg: dict | None = None) -> float:
    """TTS 统一入口（接口隔离点）：(text, out_wav) -> duration。

    按系列/任务配置的 tts.engine 分发：
    - say  ：macOS 本地占位（零依赖，机器音）
    - edge ：edge-tts 在线免费（验证级音质，需联网）
    云 TTS（火山/豆包）选型后在此加分支，调用方零改动。
    """
    cfg = tts_cfg or {}
    engine = cfg.get("engine", "say")
    if engine == "edge":
        speed = float(cfg.get("speed", 1.0))
        rate = f"{(speed - 1) * 100:+.0f}%"
        return tts_edge(text, out_wav, voice=cfg.get("voice") or EDGE_VOICE, rate=rate)
    return tts_say(text, out_wav)


def render_segment(video: Path, clip: dict, tts_wav: Path | None,
                   out_path: Path, transform: dict | None = None,
                   out_w: int = DEFAULT_OUT_W, out_h: int = DEFAULT_OUT_H,
                   fps: int = DEFAULT_OUT_FPS, letterbox: bool = False) -> float:
    """渲染单个片段（视频重编码 + transform + 音轨对齐到片段时长），返回片段时长。

    时长规则（声音为准，画面剪切）：
    - narration_clip：片段时长 = TTS 声音时长。画面比声音长→裁画面前段；
      画面比声音短→冻结末帧补齐。保证解说句之间声音连续、无空白等待。
    - raw_insert（保留原声）：片段时长 = 画面时长（画面与原声一体）。

    out_w/out_h/fps：任务输出规格（task.json output，默认 1920x1080 30fps）。
    letterbox：电影画幅——内容缩放至 2.35:1 居中，上下 pad 黑边，
      字幕（底部位置不变）自然落在下黑边上，不挡画面。
    """
    v_dur = clip["end"] - clip["start"]
    a_dur = probe_duration(tts_wav) if tts_wav else 0.0
    if clip.get("keep_audio"):
        seg = max(v_dur, 0.5)
    else:
        seg = max(a_dur, 0.5)
    pad_v = max(seg - v_dur, 0.0)

    # transform 链：放大 + 位移，系列级可被 per-clip 覆盖
    xf = dict(transform or {})
    xf.update(clip.get("transform") or {})
    if letterbox and not clip.get("transform"):
        # 黑边模式默认不做缩放：黑边已营造电影画幅，放大裁切会破坏画面；
        # 仅当 per-clip 显式指定 transform 时才应用
        xf = {}
    scale = xf.get("scale", 1.0)
    offset_x = xf.get("offset_x", 0)
    offset_y = xf.get("offset_y", 0)
    if scale != 1.0 or offset_x or offset_y:
        # 先放大，再平移；必须让 LOGO 区出画，同时保留主体
        geo = (f"scale=iw*{scale}:-2,setsar=1,"
               f"crop={out_w}:{out_h}:(iw-{out_w})/2+{offset_x}:(ih-{out_h})/2+{offset_y}")
    else:
        geo = f"scale={out_w}:-2"

    if letterbox:
        # 电影画幅 2.35:1：画面满宽，上下 crop 出电影比例（左右无黑边），再 pad 上下黑边
        lb_h = round(out_h * 0.756)
        geo = (f"{geo},"
               f"crop={out_w}:{lb_h}:(iw-{out_w})/2:(ih-{lb_h})/2,"
               f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1")

    vfilter = f"[0:v]tpad=stop_mode=clone:stop={pad_v:.2f},{geo},fps={fps},format=yuv420p,setsar=1[v]"
    if clip.get("keep_audio"):
        # raw_insert：保留原声
        afilter = f"[0:a]apad=whole_dur={seg:.2f},aresample=48000[a]"
        amap_input = 0
    else:
        afilter = f"[1:a]apad=whole_dur={seg:.2f},aresample=48000[a]"
        amap_input = 1

    cmd = [ffmpeg_bin(), "-y", "-v", "quiet",
           "-ss", f"{clip['start']:.3f}", "-t", f"{seg:.3f}", "-i", str(video)]
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
        ffmpeg_bin(), "-y", "-v", "quiet",
        "-i", str(video), "-i", str(bgm),
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(out),
    ])


def _bgm_duck_regions(clips: list[dict],
                      seg_durations: list[float] | None = None
                      ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """从 EDL 提取 narration 与 raw_insert 在**成片时间轴**上的区间。

    与字幕同源：BGM ducking 发生在 concat 之后，必须用从 0 累计的成片时间，
    不能用 clip 的源视频本地 start/end。seg_durations 为渲染实测片段时长
    （含 TTS 超长冻结补齐），缺省退化为源区间时长。
    """
    narration, raw_insert = [], []
    t = 0.0
    for i, c in enumerate(clips):
        dur = seg_durations[i] if seg_durations else c["end"] - c["start"]
        if c.get("type") == "narration_clip":
            narration.append((t, t + dur))
        elif c.get("type") == "raw_insert":
            raw_insert.append((t, t + dur))
        t += dur
    return narration, raw_insert


def _has_ass_filter() -> bool:
    """检测本机 ffmpeg 是否支持 ass/subtitles 滤镜。"""
    try:
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"],
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
        ffmpeg_bin(), "-y", "-v", "quiet",
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
        ffmpeg_bin(), "-y", "-v", "quiet",
        "-i", str(video),
        "-vf", f"ass={escaped}",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        str(out),
    ])


def _burn_drawtext(video: Path, dt_filter: str, out: Path) -> None:
    """把 drawtext 滤镜链烧录进视频（硬字幕，无 libass 时的替代方案）。"""
    _run([
        ffmpeg_bin(), "-y", "-v", "quiet",
        "-i", str(video),
        "-vf", dt_filter,
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

    # 任务级 transform / TTS / 输出规格（系列继承）；edl 或 clip 可覆盖
    transform = edl.get("transform") or {}
    tts_cfg: dict = {}
    out_w, out_h, out_fps = DEFAULT_OUT_W, DEFAULT_OUT_H, DEFAULT_OUT_FPS
    if task_id:
        from .db import PROJECT_ROOT

        cfg_path = PROJECT_ROOT / "tasks" / task_id / "task.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            transform = cfg.get("transform") or transform
            tts_cfg = cfg.get("tts") or {}
            out_cfg = cfg.get("output") or {}
            out_w = int(out_cfg.get("width", DEFAULT_OUT_W))
            out_h = int(out_cfg.get("height", DEFAULT_OUT_H))
            out_fps = int(out_cfg.get("fps", DEFAULT_OUT_FPS))

    out_path = out_path or work_dir / "render.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seg_dir = work_dir / "render_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_files = []
    seg_durations: list[float] = []   # 实测片段时长（含 TTS 冻结补齐），供字幕对齐成片时间轴
    total = 0.0
    for i, clip in enumerate(clips):
        video = videos.get(clip["video_id"])
        if video is None:
            raise KeyError(f"EDL 片段引用未提供的 video_id: {clip['video_id']}")
        wav = None
        if not clip.get("keep_audio"):
            wav = seg_dir / f"tts_{i:03d}.wav"
            if not wav.exists():
                synthesize(clip["text"], wav, tts_cfg)
        seg_path = seg_dir / f"seg_{i:03d}.mp4"
        seg_dur = render_segment(video, clip, wav, seg_path, transform,
                                 out_w=out_w, out_h=out_h, fps=out_fps,
                                 letterbox=(subtitle_mode == "letterbox"))
        seg_durations.append(seg_dur)
        total += seg_dur
        seg_files.append(seg_path)

    # concat（各片段编码参数一致，可 -c copy 无损拼接；路径需绝对，避免相对基准歧义）
    list_file = seg_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_files))
    raw_path = out_path.with_suffix(".raw" + out_path.suffix) if out_path else work_dir / "render.raw.mp4"
    _run([ffmpeg_bin(), "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
          "-i", str(list_file), "-c", "copy", str(raw_path)])

    # 若有 BGM 配置，生成与成片等长的 BGM 轨并混音
    bgm_path: Path | None = None
    if bgm_playlist:
        from . import stage_bgm

        narration_regions, raw_regions = _bgm_duck_regions(clips, seg_durations)
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

    # 字幕烧录/封装（overlay/letterbox 硬字幕：优先 ASS（需 libass），否则 drawtext）
    # letterbox 的画面已在 render_segment 加上下黑边，字幕底部位置不变即落在下黑边上
    if subtitle_mode in ("overlay", "letterbox"):
        from . import stage_subtitle

        narration = json.loads((work_dir / "narration.json").read_text())["narration"]
        if _has_ass_filter():
            subs = stage_subtitle.run(work_dir, mode="overlay", seg_durations=seg_durations)
            tmp_out = out_path or work_dir / "render_sub.mp4"
            _burn_subtitles(raw_path, Path(subs["ass"]), tmp_out)
            raw_path.unlink(missing_ok=True)
            if out_path is None:
                out_path = tmp_out
        else:
            # 无 libass：drawtext 硬字幕（整片重编码烧录，中文用 macOS 内置字体）
            dt = stage_subtitle.build_drawtext(narration, clips, seg_durations)
            tmp_out = out_path or work_dir / "render_sub.mp4"
            _burn_drawtext(raw_path, dt, tmp_out)
            raw_path.unlink(missing_ok=True)
            if out_path is None:
                out_path = tmp_out
    # letterbox 已并入 overlay 分支（画面含黑边，字幕落黑边）
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
