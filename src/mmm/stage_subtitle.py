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


def _output_spans(clips: list[dict],
                  seg_durations: list[float] | None = None) -> dict[int, tuple[float, float]]:
    """EDL 顺序 → 成片时间轴上每个 narration_clip 的 (start, end)，按 narration_id 索引。

    字幕烧在 concat 后的成片上，必须用**成片时间轴**（各片段时长累计），
    不能用 clip 里的源视频本地时间。seg_durations 为渲染实测片段时长
    （含 TTS 超长冻结补齐），缺省退化为源区间时长（raw_insert 也占位）。
    """
    spans: dict[int, tuple[float, float]] = {}
    t = 0.0
    for i, c in enumerate(clips):
        dur = seg_durations[i] if seg_durations else c["end"] - c["start"]
        if c.get("type") == "narration_clip":
            spans[c["narration_id"]] = (t, t + dur)
        t += dur
    return spans


def build_subtitles(narration: list[dict], clips: list[dict],
                    mode: str = "overlay",
                    seg_durations: list[float] | None = None) -> str:
    """生成 ASS 字幕内容。

    narration 与 clips 按 narration_id 一一对应，每句字幕对齐到对应片段在
    **成片时间轴**上的起止时间。当前先实现 overlay 模式。
    """
    if mode != "overlay":
        raise NotImplementedError(f"字幕模式 {mode} 尚未实现")

    spans = _output_spans(clips, seg_durations)
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
        span = spans.get(nid)
        if not span:
            continue
        t0, t1 = span
        lines = _split_lines(n["text"])
        if not lines:
            continue
        # 每行按字数分配时长，但受 [1.0, 6.0]s 约束
        total_dur = t1 - t0
        per_line = max(LINE_DURATION_MIN, min(total_dur / len(lines), LINE_DURATION_MAX))
        for i, line in enumerate(lines):
            start = t0 + i * per_line
            end = min(t0 + (i + 1) * per_line, t1)
            if end <= start:
                continue
            events.append(
                f"Dialogue: 0,{_to_ass_time(start)},{_to_ass_time(end)},Default,,0,0,0,,{line}"
            )
    return ass_header + "\n".join(events) + "\n"


def build_srt(narration: list[dict], clips: list[dict],
              seg_durations: list[float] | None = None) -> str:
    """生成 SRT 软字幕（ffmpeg 无 libass 时的 fallback）。时间轴同 build_subtitles。"""
    spans = _output_spans(clips, seg_durations)
    entries = []
    idx = 1
    for n in narration:
        span = spans.get(n["id"])
        if not span:
            continue
        t0, t1 = span
        lines = _split_lines(n["text"])
        if not lines:
            continue
        total_dur = t1 - t0
        per_line = max(LINE_DURATION_MIN, min(total_dur / len(lines), LINE_DURATION_MAX))
        for i, line in enumerate(lines):
            start = t0 + i * per_line
            end = min(t0 + (i + 1) * per_line, t1)
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


def run(work_dir: Path, mode: str = "overlay",
        seg_durations: list[float] | None = None) -> dict:
    """从 narration.json + edl.json 生成字幕文件（ASS + SRT fallback）。

    seg_durations：渲染实测的各片段时长（含 TTS 冻结补齐），用于对齐成片时间轴；
    缺省按源区间时长累计（与成片可能有出入，仅在未渲染时使用）。
    """
    narration = json.loads((work_dir / "narration.json").read_text())["narration"]
    edl = json.loads((work_dir / "edl.json").read_text())
    ass = build_subtitles(narration, edl["clips"], mode=mode, seg_durations=seg_durations)
    srt = build_srt(narration, edl["clips"], seg_durations=seg_durations)
    (work_dir / "subtitles.ass").write_text(ass, encoding="utf-8")
    (work_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
    return {"ass": str(work_dir / "subtitles.ass"), "srt": str(work_dir / "subtitles.srt")}
