"""媒体工具（ffmpeg 路径解析 + ffprobe 探测，跨阶段共享）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 带 libass 的 static build（temp/ffmpeg-static，gitignore）；存在则优先，否则回退 PATH
_FFMPEG_STATIC_DIR = PROJECT_ROOT / "temp" / "ffmpeg-static"


def ffmpeg_bin() -> str:
    """返回 ffmpeg 可执行路径：static build（含 libass）优先，缺省回退 PATH。"""
    p = _FFMPEG_STATIC_DIR / "ffmpeg"
    return str(p) if p.exists() else "ffmpeg"


def ffprobe_bin() -> str:
    """返回 ffprobe 可执行路径：static build 优先，缺省回退 PATH。"""
    p = _FFMPEG_STATIC_DIR / "ffprobe"
    return str(p) if p.exists() else "ffprobe"


def probe_duration(path: Path | str) -> float:
    """返回媒体文件时长（秒）。"""
    out = subprocess.run(
        [ffprobe_bin(), "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)
