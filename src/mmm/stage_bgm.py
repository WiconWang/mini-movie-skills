"""BGM 轨生成器。

按 task.json bgm_playlist 铺设背景音乐：顺序播放、单曲循环衔接处 2s crossfade，
解说存在段自动 ducking（默认 -14dB），raw_insert 段再压低（默认 -20dB）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .media import ffmpeg_bin, probe_duration

CROSSFADE_SEC = 2.0
MASTER_VOLUME = 0.5       # 全局 BGM 音量（用户要求整体压低 50%，约 -6dB）
DUCK_DB = -14.0           # 解说段 BGM 在基础音量上再压低
RAW_INSERT_DUCK_DB = -20.0   # raw_insert 段再压低（合计 -26dB，避免与素材原声重叠）
OUTRO_FADE_SEC = 3.0      # 结尾淡出时长：成片最后 N 秒 BGM 线性降到 0（片尾纯原声/收尾）
SAMPLE_RATE = 48000
CHANNELS = 2        # BGM 保持立体声，混音前再 downmix 由调用方决定


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"BGM 命令失败: {' '.join(cmd[:6])}...\n{r.stderr.decode()[-800:]}")


def _normalize(bgm: Path, out: Path) -> None:
    """统一采样率/声道，避免 concat 因参数不一致失败。"""
    _run([ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(bgm),
          "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-af", "loudnorm=I=-16:TP=-1.5",
          str(out)])


def build_bgm_track(
    playlist: list[str],
    total_duration: float,
    narration_regions: list[tuple[float, float]] = None,
    raw_insert_regions: list[tuple[float, float]] = None,
    out_path: Path | None = None,
) -> Path:
    """生成与成片等长的 BGM 轨。

    playlist: 文件路径列表（相对项目根或绝对路径）
    narration_regions: 解说段区间（全局时间），BGM 压低
    raw_insert_regions: raw_insert 段区间，BGM 进一步压低
    """
    from .db import PROJECT_ROOT

    narration_regions = narration_regions or []
    raw_insert_regions = raw_insert_regions or []

    files = [Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p for p in playlist]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        raise FileNotFoundError(f"BGM 文件不存在: {', '.join(missing)}")

    if not files:
        # 无 BGM 配置：输出静音轨
        out_path = out_path or (PROJECT_ROOT / "workspace" / "_bgm_silence.wav")
        _run([ffmpeg_bin(), "-y", "-v", "quiet", "-f", "lavfi", "-i",
              f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-t", str(total_duration),
              "-ac", str(CHANNELS), str(out_path)])
        return out_path

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        normalized = []
        for i, f in enumerate(files):
            norm = tmpdir / f"bgm_{i:03d}.wav"
            _normalize(f, norm)
            normalized.append(norm)

        # 1. 把每首 BGM 延长到 total_duration（循环）
        looped = []
        for i, norm in enumerate(normalized):
            dur = probe_duration(norm)
            loops = max(int(total_duration // dur) + 2, 2)
            concat_list = tmpdir / f"loop_{i}.txt"
            concat_list.write_text(
                "".join(f"file '{norm}'\n" for _ in range(loops)))
            looped_wav = tmpdir / f"looped_{i}.wav"
            _run([ffmpeg_bin(), "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
                  "-i", str(concat_list), "-t", str(total_duration),
                  "-c", "copy", str(looped_wav)])
            looped.append(looped_wav)

        # 2. 多首 BGM 之间 crossfade 衔接
        if len(looped) == 1:
            mixed = looped[0]
        else:
            # acrossfade 只支持两输入，多首需串行
            mixed = looped[0]
            seg_dur = total_duration / len(looped)
            for nxt in looped[1:]:
                out_mix = tmpdir / f"mix_{id(nxt)}.wav"
                _run([
                    ffmpeg_bin(), "-y", "-v", "quiet",
                    "-i", str(mixed), "-i", str(nxt),
                    "-filter_complex",
                    f"[0:a]atrim=end={seg_dur}[a0];"
                    f"[1:a]atrim=start=0[af];"
                    f"[a0][af]acrossfade=d={CROSSFADE_SEC}:c1=tri:c2=tri[a]",
                    "-map", "[a]", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                    str(out_mix),
                ])
                mixed = out_mix

        # 3. ducking：解说段 / raw_insert 段音量压低，末尾接结尾淡出
        duck_expr_parts = []
        for s, e in narration_regions:
            duck_expr_parts.append(f"between(t,{s:.3f},{e:.3f})")
        for s, e in raw_insert_regions:
            duck_expr_parts.append(f"between(t,{s:.3f},{e:.3f})")

        # 结尾淡出：成片最后 OUTRO_FADE_SEC 秒线性降到 0。
        # 片尾是 raw_insert 时得到纯原声；片尾是解说段时也是正常收尾，不突兀。
        # 成片短于淡出时长则从 0 开始淡（fade-out start 不得为负）。
        fade_start = max(total_duration - OUTRO_FADE_SEC, 0.0)
        outro_fade = f"afade=t=out:st={fade_start:.3f}:d={OUTRO_FADE_SEC:.3f}"

        if duck_expr_parts:
            enable = "+".join(duck_expr_parts)
            # 音量层级（volume 滤镜串联乘法）：
            #   全局基础 0.5（-6dB，用户要求整体压低 50%）
            #   解说段在基础之上再 -14dB（合计 -20dB）
            #   raw_insert 段再 -6dB（合计 -26dB，避免与素材原声重叠）
            # enable 区间外不匹配 → 自动恢复基础音量 0.5（raw_insert 结束后恢复）
            volume_filter = f"volume={MASTER_VOLUME:.4f}"
            volume_filter += f",volume={10**(DUCK_DB/20):.4f}:enable='{enable}'"
            raw_parts = []
            for s, e in raw_insert_regions:
                raw_parts.append(f"between(t,{s:.3f},{e:.3f})")
            if raw_parts:
                raw_enable = "+".join(raw_parts)
                extra_db = RAW_INSERT_DUCK_DB - DUCK_DB
                volume_filter += f",volume={10**(extra_db/20):.4f}:enable='{raw_enable}'"
            volume_filter += f",{outro_fade}"

            out_path = out_path or (PROJECT_ROOT / "workspace" / "_bgm_ducked.wav")
            _run([ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(mixed),
                  "-af", volume_filter, "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                  "-t", str(total_duration), str(out_path)])
        else:
            out_path = out_path or (PROJECT_ROOT / "workspace" / "_bgm.wav")
            _run([ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(mixed),
                  "-af", outro_fade, "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                  "-t", str(total_duration), str(out_path)])

    return out_path


def from_task(task_id: str, total_duration: float) -> Path:
    """读取 tasks/{task_id}/task.json 与 edl.json，生成 BGM 轨。"""
    from .db import PROJECT_ROOT

    task_dir = PROJECT_ROOT / "tasks" / task_id
    cfg = json.loads((task_dir / "task.json").read_text())
    edl = json.loads((task_dir / "edl.json").read_text())

    playlist = cfg.get("bgm_playlist", [])
    narration_regions = [(c["start"], c["end"]) for c in edl["clips"]
                         if c.get("type") == "narration_clip"]
    raw_regions = [(c["start"], c["end"]) for c in edl["clips"]
                   if c.get("type") == "raw_insert"]

    out_dir = PROJECT_ROOT / "output" / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return build_bgm_track(
        playlist, total_duration,
        narration_regions=narration_regions,
        raw_insert_regions=raw_regions,
        out_path=out_dir / "bgm.wav",
    )