"""字幕烧录。

支持两种系列级 subtitle_mode（设计文档 §4 阶段7）：
- overlay：白边描边字幕直接压在画面上（默认，实现最简单）
- letterbox：上下加黑边电影画幅，字幕烧在下黑边上（待实现）

输入：narration.json（解说稿）+ EDL 片段（用于时间轴对齐）
输出：ass 字幕文件（ffmpeg ass 滤镜烧录）
"""

from __future__ import annotations

import json
import re
from pathlib import Path


# 每屏字数与语速联动（设计文档参数基线）
MAX_CHARS_PER_SCREEN = 18
LINE_DURATION_MIN = 1.0
LINE_DURATION_MAX = 6.0


def _split_lines(text: str, max_chars: int = MAX_CHARS_PER_SCREEN) -> list[str]:
    """按语义断行：优先在标点处切断，不切断词语。"""
    if len(text) <= max_chars:
        return [text]
    # 先按强标点切分
    parts = re.split(r"([。！？；，、])", text)
    parts = [p for p in parts if p]
    # 合并相邻的标点和前文
    sentences = []
    i = 0
    while i < len(parts):
        if parts[i] in "。！？；，、":
            if sentences:
                sentences[-1] += parts[i]
            else:
                sentences.append(parts[i])
            i += 1
        else:
            sentences.append(parts[i])
            i += 1

    lines = []
    current = ""
    for s in sentences:
        if len(current) + len(s) <= max_chars:
            current += s
        else:
            if current:
                lines.append(current)
            # 如果单句本身就超长，按字数硬切
            if len(s) > max_chars:
                for j in range(0, len(s), max_chars):
                    chunk = s[j:j + max_chars]
                    lines.append(chunk)
                current = ""
            else:
                current = s
    if current:
        lines.append(current)
    return lines


def _to_ass_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_subtitles(narration: list[dict], clips: list[dict],
                    mode: str = "overlay") -> str:
    """生成 ASS 字幕内容。

    narration 与 clips 按 narration_id 一一对应，每句字幕对齐到对应片段的
    全局起止时间。当前先实现 overlay 模式。
    """
    if mode != "overlay":
        raise NotImplementedError(f"字幕模式 {mode} 尚未实现")

    clip_by_nid = {c["narration_id"]: c for c in clips if c.get("type") == "narration_clip"}
    ass_header = """[Script Info]
Title: mini-movie-maker subtitles
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,42,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2.5,0,2,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for n in narration:
        nid = n["id"]
        clip = clip_by_nid.get(nid)
        if not clip:
            continue
        t0 = clip["start"]
        lines = _split_lines(n["text"])
        if not lines:
            continue
        # 每行按字数分配时长，但受 [1.0, 6.0]s 约束
        total_dur = clip["end"] - clip["start"]
        per_line = max(LINE_DURATION_MIN, min(total_dur / len(lines), LINE_DURATION_MAX))
        for i, line in enumerate(lines):
            start = t0 + i * per_line
            end = min(t0 + (i + 1) * per_line, clip["end"])
            if end <= start:
                continue
            events.append(
                f"Dialogue: 0,{_to_ass_time(start)},{_to_ass_time(end)},Default,,0,0,0,,{line}"
            )
    return ass_header + "\n".join(events) + "\n"


def build_srt(narration: list[dict], clips: list[dict]) -> str:
    """生成 SRT 软字幕（ffmpeg 无 libass 时的 fallback）。"""
    clip_by_nid = {c["narration_id"]: c for c in clips if c.get("type") == "narration_clip"}
    entries = []
    idx = 1
    for n in narration:
        clip = clip_by_nid.get(n["id"])
        if not clip:
            continue
        t0 = clip["start"]
        lines = _split_lines(n["text"])
        if not lines:
            continue
        total_dur = clip["end"] - clip["start"]
        per_line = max(LINE_DURATION_MIN, min(total_dur / len(lines), LINE_DURATION_MAX))
        for i, line in enumerate(lines):
            start = t0 + i * per_line
            end = min(t0 + (i + 1) * per_line, clip["end"])
            if end <= start:
                continue
            entries.append(
                f"{idx}\n{_to_srt_time(start)} --> {_to_srt_time(end)}\n{line}\n"
            )
            idx += 1
    return "\n".join(entries) + "\n"


def _to_srt_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    ms = int((s % 1) * 1000)
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"


def run(work_dir: Path, mode: str = "overlay") -> dict:
    """从 narration.json + edl.json 生成字幕文件（ASS + SRT fallback）。"""
    narration = json.loads((work_dir / "narration.json").read_text())["narration"]
    edl = json.loads((work_dir / "edl.json").read_text())
    ass = build_subtitles(narration, edl["clips"], mode=mode)
    srt = build_srt(narration, edl["clips"])
    (work_dir / "subtitles.ass").write_text(ass, encoding="utf-8")
    (work_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
    return {"ass": str(work_dir / "subtitles.ass"), "srt": str(work_dir / "subtitles.srt")}
