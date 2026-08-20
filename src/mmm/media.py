"""媒体探测工具（ffprobe 封装，跨阶段共享）。"""

from __future__ import annotations

import subprocess
from pathlib import Path


def probe_duration(path: Path | str) -> float:
    """返回媒体文件时长（秒）。"""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)
