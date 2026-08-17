"""阶段1：镜头切分（逻辑切分，不动原视频）。

- 场景检测：ffmpeg select='gt(scene,阈值)' → 镜头时间戳列表
- 黑/白屏检测：blackdetect（白屏 = 反相后再 blackdetect）
- 后处理：过短镜头并入相邻，超长镜头二次细分

产出：shots.json / fades.json（见设计文档 §4 阶段1）
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SCENE_THRESHOLD = 0.3       # 场景突变判定阈值（可调）
MIN_SHOT_SEC = 0.5          # 短于此并入相邻镜头
MAX_SHOT_SEC = 60.0         # 长于此二次细分
FADE_MIN_DURATION = 0.3     # 黑/白屏最短持续（秒）
FADE_PIX_TH = 0.10          # 黑屏像素阈值


def _duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_scene_cuts(video: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """返回所有场景切换点的时间戳（秒，升序）。"""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(video),
         "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    cuts = []
    for line in proc.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            cuts.append(float(m.group(1)))
    return sorted(cuts)


def _detect_black(video: Path, negate: bool) -> list[dict]:
    """黑屏检测；negate=True 时检测白屏（反相后白即黑）。"""
    vf = f"{'negate,' if negate else ''}blackdetect=d={FADE_MIN_DURATION}:pix_th={FADE_PIX_TH}"
    proc = subprocess.run(
        ["ffmpeg", "-i", str(video), "-vf", vf, "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    fades = []
    for line in proc.stderr.splitlines():
        m = re.search(r"black_start:([\d.]+)\s+black_end:([\d.]+)", line)
        if m:
            fades.append({"start": float(m.group(1)), "end": float(m.group(2))})
    return fades


def detect_fades(video: Path) -> list[dict]:
    """黑/白屏区间列表（E 类画面的高置信信号）。"""
    fades = [{"type": "black", **f} for f in _detect_black(video, negate=False)]
    fades += [{"type": "white", **f} for f in _detect_black(video, negate=True)]
    return sorted(fades, key=lambda f: f["start"])


def build_shots(cuts: list[float], duration: float) -> list[dict]:
    """切点 → 镜头区间；过短并入相邻，超长二次细分。"""
    bounds = [0.0] + cuts + [duration]
    shots = [{"start": bounds[i], "end": bounds[i + 1]}
             for i in range(len(bounds) - 1) if bounds[i + 1] - bounds[i] > 0.01]

    # 过短镜头并入前一个（首个则并入后一个）
    merged: list[dict] = []
    for s in shots:
        if s["end"] - s["start"] < MIN_SHOT_SEC and merged:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))

    # 超长镜头二次细分
    final: list[dict] = []
    for s in merged:
        span = s["end"] - s["start"]
        if span > MAX_SHOT_SEC:
            n = int(span // MAX_SHOT_SEC) + 1
            step = span / n
            for i in range(n):
                final.append({"start": s["start"] + i * step,
                              "end": s["start"] + (i + 1) * step})
        else:
            final.append(s)

    return [{"id": i + 1, "start": round(s["start"], 3), "end": round(s["end"], 3)}
            for i, s in enumerate(final)]


def run(video: Path, out_dir: Path, threshold: float = SCENE_THRESHOLD) -> dict:
    """执行阶段1，写出 shots.json / fades.json，返回汇总。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = _duration(video)
    cuts = detect_scene_cuts(video, threshold)
    shots = build_shots(cuts, duration)
    fades = detect_fades(video)

    (out_dir / "shots.json").write_text(
        json.dumps({"video": video.name, "duration": round(duration, 3),
                    "threshold": threshold, "shots": shots},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "fades.json").write_text(
        json.dumps({"fades": fades}, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"duration": round(duration, 3), "cuts": len(cuts),
            "shots": len(shots), "fades": len(fades)}
