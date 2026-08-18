"""阶段7 导出器A：ffmpeg 直出 MP4（MVP）。

按 EDL 逐片段：切源视频（重编码）+ 解说配音 → 片段级音画对齐 → concat 成片。

MVP 约定（与最终设计的差距，逐步补齐）：
- TTS 用 macOS 本地 `say`（Tingting）占位，零成本验证端到端；云 TTS（火山/豆包）后续接入
- 无片头（composition）、无 BGM、无字幕烧录、无 transform 裁 LOGO
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
                   out_path: Path) -> float:
    """渲染单个片段（视频重编码 + 音轨对齐到片段时长），返回片段时长。"""
    v_dur = clip["end"] - clip["start"]
    a_dur = _duration(tts_wav) if tts_wav else 0.0
    seg = max(v_dur, a_dur, 0.5)
    pad_v = max(seg - v_dur, 0.0)

    vfilter = f"[0:v]tpad=stop_mode=clone:stop={pad_v:.2f},{SCALE},fps={FPS},format=yuv420p,setsar=1[v]"
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


def run(work_dir: Path, video: Path, out_path: Path | None = None) -> dict:
    """按 edl.json 渲染成片。"""
    edl = json.loads((work_dir / "edl.json").read_text())
    clips = edl["clips"]
    out_path = out_path or work_dir / "render.mp4"
    seg_dir = work_dir / "render_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_files = []
    total = 0.0
    for i, clip in enumerate(clips):
        wav = None
        if not clip.get("keep_audio"):
            wav = seg_dir / f"tts_{i:03d}.wav"
            tts_say(clip["text"], wav)
        seg_path = seg_dir / f"seg_{i:03d}.mp4"
        total += render_segment(video, clip, wav, seg_path)
        seg_files.append(seg_path)

    # concat（各片段编码参数一致，可 -c copy 无损拼接；路径需绝对，避免相对基准歧义）
    list_file = seg_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_files))
    _run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
          "-i", str(list_file), "-c", "copy", str(out_path)])

    return {"clips": len(clips), "duration": round(total, 1), "output": str(out_path)}
