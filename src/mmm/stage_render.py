"""阶段7 导出器A：ffmpeg 直出 MP4（MVP）。

按 EDL 逐片段：切源视频（重编码）+ 解说配音 → 片段级音画对齐 → concat 成片。

任务模式使用统一 TTS 适配层（dry/prod 均完整合成后切分）；冒烟路径保留本地占位/Edge：
- 无片头（composition）、无 BGM、无字幕烧录
- overlay_transform 裁 LOGO：系列级 + per-clip 覆盖
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


def _run_with_env(cmd: list[str], env: dict[str, str] | None = None) -> None:
    """与 _run 相同，但可选注入额外环境变量（用于 fontconfig 命中项目字体）。"""
    import os

    full_env = dict(os.environ)
    full_env.update(env or {})
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       env=full_env)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


def _fontconfig_env(font_dir: Path | None = None) -> dict[str, str]:
    """构造让 libass 命中项目字体目录的 FONTCONFIG_FILE 环境变量。

    libass 的 fontsdir 只是让字体“可读”，但 family 匹配仍走宿主机 fontconfig；
    本机缓存未索引 assets/fonts 时会静默回退到系统黑体。这里注入一个临时
    fontconfig，把字体目录加进配置并继承系统默认，从而真正命中，而不注册系统字体。
    """
    from .media import PROJECT_ROOT

    font_dir = font_dir or PROJECT_ROOT / "assets" / "fonts"
    fc_root = Path(tempfile.mkdtemp(prefix="mmm-fonts-"))
    cache_dir = fc_root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    conf = fc_root / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        '<fontconfig>\n'
        f'  <dir>{font_dir}</dir>\n'
        f'  <cachedir>{cache_dir}</cachedir>\n'
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        '</fontconfig>\n',
        encoding="utf-8",
    )
    return {"FONTCONFIG_FILE": str(conf)}


def _render_overlay_mask_png(mask_cfg: dict, out_w: int, out_h: int,
                             out_path: Path) -> bool:
    """用纯 ffmpeg 生成一张静态羽化遮罩 PNG（不依赖 Pillow/numpy）。

    mask_cfg：subtitle.overlay_mask（x/y/blur_sigma/feather）。drawbox 画实心矩形，
    再用 gblur 做边缘羽化（四周渐弱），输出为 0/255 灰阶 alpha 图，作为遮罩层。
    生成失败返回 False（调用方应回退到无遮罩）。
    """
    if not mask_cfg or not mask_cfg.get("enabled"):
        return False
    x0, x1 = mask_cfg.get("x", [210, 1710])
    y0, y1 = mask_cfg.get("y", [860, 1080])
    blur_sigma = mask_cfg.get("blur_sigma", 20)
    feather = mask_cfg.get("feather_top", mask_cfg.get("feather_side", 34))
    w, h = int(x1 - x0), int(y1 - y0)
    if w <= 0 or h <= 0:
        return False
    vf = (f"drawbox=x={int(x0)}:y={int(y0)}:w={w}:h={h}:color=white:t=fill,"
          f"gblur=sigma={feather},format=gray")
    cmd = [
        ffmpeg_bin(), "-y", "-v", "quiet",
        "-f", "lavfi", "-i", f"color=black:{out_w}x{out_h}:r=1:d=1",
        "-frames:v", "1", "-vf", vf, str(out_path),
    ]
    if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE).returncode != 0:
        return False
    return out_path.exists()


def _overlay_mask_filter(blur_sigma: float = 20) -> str:
    """构造 overlay 底部模糊遮罩的 -filter_complex 链（mask PNG 作第二输入）。

    输入约定：0:v 为已成片视频；1:v 为 _render_overlay_mask_png 生成的静态灰阶遮罩。
    对整帧 gblur，再用遮罩 alpha（alphamerge）把模糊层以羽化方式盖回原画，
    最后以 [maskout] 输出。调用方需 `-loop 1 -shortest` 匹配视频时长，避免 color 无限流。
    """
    return (
        "[0:v]split=2[a][b];"
        f"[a]gblur=sigma={blur_sigma}[blur];[blur]format=rgba[blr];"
        "[1:v]format=gray[mask];[blr][mask]alphamerge[blu];"
        "[b][blu]overlay=format=auto[maskout]"
    )


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
    import os
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
            _proxy = os.environ.get("MMM_TTS_PROXY") or os.environ.get("https_proxy")
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate, proxy=_proxy or None).save(str(mp3)))
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
    """冒烟路径 TTS 入口（接口隔离点）：(text, out_wav) -> duration。

    按系列/任务配置的 tts.engine 分发：
    - say  ：macOS 本地占位（零依赖，机器音）
    - edge ：edge-tts 在线免费（验证级音质，需联网）

    规划说明（尚未接入正式实现）：
    - dry/smoke 阶段继续用现有 say/edge，避免正式云服务费用影响流程验证。
    - 正式阶段计划接 MiniMax speech-2.8-hd：POST /v1/t2a_v2。
      候选 voice_id 见 config/series/{series}.yaml 的 TTS 注释，待试听后确定。
    - MiniMax 不是 SSML：停顿用文本内 <#秒#>，语气词如 (laughs)/(breath)/(sighs)
      直接嵌入文本；emotion 放入 voice_setting.emotion，不由这些小括号标签控制。

    任务模式不得直接调用；请使用 src/mmm/tts/runtime.py 的统一闸口流程。
    """
    cfg = tts_cfg or {}
    engine = cfg.get("engine", "say")
    if engine == "edge":
        speed = float(cfg.get("speed", 1.0))
        rate = f"{(speed - 1) * 100:+.0f}%"
        return tts_edge(text, out_wav, voice=cfg.get("voice") or EDGE_VOICE, rate=rate)
    return tts_say(text, out_wav)


def render_segment(video: Path, clip: dict, tts_wav: Path | None,
                   out_path: Path, overlay_transform: dict | None = None,
                   out_w: int = DEFAULT_OUT_W, out_h: int = DEFAULT_OUT_H,
                   fps: int = DEFAULT_OUT_FPS, letterbox: bool = False) -> float:
    """渲染单个片段（视频重编码 + overlay 画面适配 + 音轨对齐到片段时长），返回片段时长。

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

    # overlay 画面适配链：放大 + 位移，系列级可被 per-clip 覆盖
    xf = dict(overlay_transform or {})
    xf.update(clip.get("overlay_transform") or {})
    if letterbox and not clip.get("overlay_transform"):
        # 黑边模式默认不做缩放：黑边已营造电影画幅，放大裁切会破坏画面；
        # 仅当 per-clip 显式指定 overlay_transform 时才应用
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


def _burn_subtitles(video: Path, ass: Path, out: Path,
                    mask_png: Path | None = None,
                    blur_sigma: float = 20,
                    mask_cfg: dict | None = None,
                    out_w: int = DEFAULT_OUT_W, out_h: int = DEFAULT_OUT_H) -> None:
    """把 ASS 字幕烧录进视频（需要 ffmpeg 启用 libass）。

    mask_png：overlay 底部模糊遮罩静态图（_render_overlay_mask_png 生成），
       非空时作为第二输入，经 -loop 1 -shortest 全程覆盖，烧字幕在其上方；
    mask_cfg：保留兼容（若传入且 mask_png 为空，回退无遮罩）；
    out_w/out_h：输出分辨率，遮罩坐标按此归一。
    """
    env = {**_fontconfig_env()}
    ass_opt = f"ass={_escape_ass_path(str(ass))}"
    cmd = [ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(video)]
    if mask_png and mask_png.exists():
        cmd += ["-loop", "1", "-i", str(mask_png)]
        fc = f"{_overlay_mask_filter(blur_sigma)};[maskout]{ass_opt}[vout]"
        cmd += ["-filter_complex", fc, "-map", "[vout]"]
        cmd += ["-shortest"]
    else:
        cmd += ["-filter_complex", f"[0:v]{ass_opt}[vout]", "-map", "[vout]"]
    cmd += [
        "-map", "0:a?",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        str(out),
    ]
    _run_with_env(cmd, env)


def _escape_ass_path(p: str) -> str:
    """转义 ass 滤镜路径中的逗号/冒号/反斜杠。"""
    return p.replace("\\", "/").replace(":", "\\:").replace(",", "\\,")


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

    # 任务级 subtitle（含画面适配 overlay_transform / 遮罩 overlay_mask）/ TTS / 输出规格
    # （系列继承）；edl 或 clip 可覆盖
    subtitle_cfg = edl.get("subtitle") or {}
    tts_cfg: dict = {}
    out_w, out_h, out_fps = DEFAULT_OUT_W, DEFAULT_OUT_H, DEFAULT_OUT_FPS
    if task_id:
        from .db import PROJECT_ROOT

        cfg_path = PROJECT_ROOT / "tasks" / task_id / "task.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            subtitle_cfg = cfg.get("subtitle") or subtitle_cfg
            tts_cfg = cfg.get("tts") or {}
            out_cfg = cfg.get("output") or {}
            out_w = int(out_cfg.get("width", DEFAULT_OUT_W))
            out_h = int(out_cfg.get("height", DEFAULT_OUT_H))
            out_fps = int(out_cfg.get("fps", DEFAULT_OUT_FPS))
    # overlay 画面适配与遮罩（缺省回退空，即不放大不遮罩）
    overlay_transform = subtitle_cfg.get("overlay_transform") or {}
    overlay_mask = subtitle_cfg.get("overlay_mask") or {}

    out_path = out_path or work_dir / "render.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seg_dir = work_dir / "render_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    # 任务模式必须经过统一 TTS 计划闸口；有效片段缓存由 runtime 按指纹校验。
    prepared_tts: dict[int, Path] | None = None
    if task_id:
        from .tts import runtime as tts_runtime

        prepared_tts = tts_runtime.prepare_render_artifacts(work_dir, tts_cfg)
    else:
        # 冒烟路径没有任务级计划；清理旧产物防止跨版本 EDL/narration 误复用。
        for old in seg_dir.glob("tts_*.wav"):
            old.unlink()
    for old in seg_dir.glob("seg_*.mp4"):
        old.unlink()

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
            if prepared_tts is not None:
                wav = prepared_tts[i]
            elif not wav.exists():
                synthesize(clip["text"], wav, tts_cfg)
        seg_path = seg_dir / f"seg_{i:03d}.mp4"
        seg_dur = render_segment(video, clip, wav, seg_path, overlay_transform,
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
            subs = stage_subtitle.run(work_dir, mode="overlay",
                                      seg_durations=seg_durations,
                                      subtitle_cfg=subtitle_cfg)
            tmp_out = out_path or work_dir / "render_sub.mp4"
            # overlay 模式且启用遮罩时，先按分辨率生成静态羽化遮罩，再在烧字幕前叠加
            use_mask = (subtitle_mode == "overlay" and overlay_mask.get("enabled"))
            mask_png = None
            if use_mask:
                mask_png = work_dir / "overlay_mask.png"
                if not _render_overlay_mask_png(overlay_mask, out_w, out_h, mask_png):
                    mask_png = None   # 生成失败则回退无遮罩（不中断渲染）
            _burn_subtitles(raw_path, Path(subs["ass"]), tmp_out,
                            mask_png=mask_png,
                            blur_sigma=overlay_mask.get("blur_sigma", 20),
                            out_w=out_w, out_h=out_h)
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
