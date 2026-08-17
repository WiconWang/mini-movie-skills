"""阶段2：ASR + 台词对齐。

核心思路（设计文档 §4 阶段2）：
- ASR 只提供时间戳，文本以准确台词为准
- 字符级序列对齐（difflib），对上的行继承时间，夹缝中的短行插值
- 长段连续未匹配 = 视频未包含该段，跳过不插值
- 纯剧情实录、无口播；分支差异秒级，按普通未匹配处理
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field

# 超过此连续未匹配行数，判定为「视频未包含该段」，不插值
MAX_INTERPOLATE_GAP = 5
# 中文语音语速（字/秒），用于预估缺失行的应有语音时长
SPEECH_CHARS_PER_SEC = 4.5
# 插值放行条件：锚点时间差 ≥ 缺失行预估时长 × 此系数；
# 不满足说明音频里根本没有这些行的时间（未录制/被剪掉），标记 unmatched 不插值
MIN_TIME_FIT = 0.6


# 行内字符命中率低于此值，不采信 matched（防止相似行错配到错误区域）
MIN_MATCH_RATIO = 0.5


@dataclass
class AsrWord:
    text: str
    start: float
    end: float


@dataclass
class AlignedLine:
    id: int
    text: str
    speaker: str | None = None
    start: float | None = None
    end: float | None = None
    align: str = "unmatched"  # matched / interpolated / unmatched
    asr_text: str = ""        # 对齐上的 ASR 原文（调试用）


def _normalize(s: str) -> str:
    """对齐前的归一化：去标点空白，只留可比字符。"""
    return "".join(ch for ch in s if ch.isalnum())


def align(script_lines: list[dict], asr_words: list[AsrWord]) -> dict:
    """台词（script.jsonl 行）× ASR 词流 → 带时间戳的台词表 + 覆盖率报告。

    script_lines: [{"text": ..., "speaker": ...}, ...] 按剧情顺序
    asr_words: ASR 词级结果，按时间顺序
    """
    # 1. 把 ASR 词流按台词行数做粗分段锚定：用整体序列对齐
    asr_text = "".join(_normalize(w.text) for w in asr_words)
    # 每个字符属于哪个词（用于回溯时间戳）
    char_owner: list[int] = []
    for i, w in enumerate(asr_words):
        char_owner.extend([i] * len(_normalize(w.text)))

    script_text = "".join(_normalize(l["text"]) for l in script_lines)
    # 每个字符属于哪一行
    line_owner: list[int] = []
    for i, l in enumerate(script_lines):
        line_owner.extend([i] * len(_normalize(l["text"])))

    # 2. 字符级序列对齐
    sm = difflib.SequenceMatcher(a=script_text, b=asr_text, autojunk=False)
    lines: list[AlignedLine] = [
        AlignedLine(id=i + 1, text=l["text"], speaker=l.get("speaker"))
        for i, l in enumerate(script_lines)
    ]

    # 3. 匹配块 → 行级时间戳
    # 行内任一字符命中匹配块，即可用该块内本行字符的首尾定时间；
    # 但命中率低于 MIN_MATCH_RATIO 的行不采信（相似行错配防护）
    line_spans: dict[int, list[tuple[float, float]]] = {}
    line_hits: dict[int, int] = {}
    line_lens = [len(_normalize(l["text"])) for l in script_lines]

    for blk in sm.get_matching_blocks():
        a0, b0, size = blk.a, blk.b, blk.size
        if size == 0:
            continue
        for ci in range(a0, a0 + size):
            li = line_owner[ci]
            wi = char_owner[b0 + (ci - a0)]
            w = asr_words[wi]
            line_spans.setdefault(li, []).append((w.start, w.end))
            line_hits[li] = line_hits.get(li, 0) + 1

    for li, spans in line_spans.items():
        if line_hits[li] / max(line_lens[li], 1) < MIN_MATCH_RATIO:
            continue  # 命中率过低，留给插值/未匹配判定
        lines[li].start = min(s[0] for s in spans)
        lines[li].end = max(s[1] for s in spans)
        lines[li].align = "matched"

    # 4. 插值：夹在两个已匹配锚点之间的短 gap，且锚点时间差装得下这些行的语音
    matched_idx = [i for i, l in enumerate(lines) if l.align == "matched"]
    for a, b in zip(matched_idx, matched_idx[1:]):
        gap = b - a - 1
        if not (0 < gap <= MAX_INTERPOLATE_GAP):
            continue
        t0, t1 = lines[a].end, lines[b].start
        gap_chars = sum(len(_normalize(lines[i].text)) for i in range(a + 1, b))
        est_dur = gap_chars / SPEECH_CHARS_PER_SEC
        if (t1 - t0) < est_dur * MIN_TIME_FIT:
            continue  # 时间装不下 → 这些行不在音频里，保持 unmatched
        cursor = t0
        for i in range(a + 1, b):
            share = len(_normalize(lines[i].text)) / max(gap_chars, 1)
            lines[i].start = round(cursor, 2)
            lines[i].end = round(cursor + (t1 - t0) * share, 2)
            lines[i].align = "interpolated"
            cursor = lines[i].end

    matched = sum(1 for l in lines if l.align == "matched")
    interpolated = sum(1 for l in lines if l.align == "interpolated")
    total = len(lines)
    return {
        "lines": [
            {"id": l.id, "start": l.start, "end": l.end, "text": l.text,
             "speaker": l.speaker, "align": l.align}
            for l in lines
        ],
        "report": {
            "total": total,
            "matched": matched,
            "interpolated": interpolated,
            "unmatched": total - matched - interpolated,
            "coverage": round((matched + interpolated) / max(total, 1), 4),
        },
    }


def save(result: dict, path) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
