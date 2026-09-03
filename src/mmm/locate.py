"""素材台词定位器：把用户台词（允许不完全准确）在 asr.json 词级时间轴里模糊匹配成源视频本地秒区间。

依赖阶段2 ASR 产物 asr.json（workspace/{video_id}/asr.json，任务模式回退
tasks/{task_id}/workspace/{video_id}/asr.json）。输出区间与 keep_requirements 同基准
（源视频本地秒）。纯函数无网络，便于单元测试。
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from .db import PROJECT_ROOT

DEFAULT_THRESHOLD = 0.6
DEFAULT_PAD = 0.5


def normalize_text(text: str) -> str:
    """NFKC + 去空白 + 去标点/符号，保留汉字/字母/数字。"""
    n = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in n
        if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "S"))
    )


def _levenshtein(a: str, b: str) -> int:
    """字符级编辑距离，用于台词不完全准确时的兜底相似度。"""
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def asr_path(video_id: str, task_id: str = "") -> Path | None:
    """返回可用的 asr.json；共享 workspace 优先，任务级兜底。"""
    shared = PROJECT_ROOT / "workspace" / video_id / "asr.json"
    if shared.exists():
        return shared
    if task_id:
        task = PROJECT_ROOT / "tasks" / task_id / "workspace" / video_id / "asr.json"
        if task.exists():
            return task
    return None


def load_words(path: Path) -> list[dict]:
    """读取 asr.json 的词级时间轴。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("words") or []


def locate_quote(words: list[dict], quote: str, *,
                 threshold: float = DEFAULT_THRESHOLD,
                 pad: float = DEFAULT_PAD) -> dict | None:
    """在词级时间轴里模糊定位 quote，返回 {start,end,matched_text,score,word_range}。"""
    pieces: list[str] = []
    owner: list[int] = []
    for idx, w in enumerate(words):
        n = normalize_text(w.get("text", ""))
        if not n:
            continue
        pieces.append(n)
        owner.extend([idx] * len(n))
    full = "".join(pieces)
    q = normalize_text(quote)
    if not q or not full:
        return None

    match = None
    pos = full.find(q)
    if pos >= 0:
        i0, i1 = owner[pos], owner[pos + len(q) - 1]
        match = (i0, i1, 1.0)
    else:
        w = len(q)
        best = (-1, -1, 0.0)
        for i in range(max(0, len(full) - w + 1)):
            cand = full[i:i + w]
            dist = _levenshtein(q, cand)
            sim = 1 - dist / max(1, len(q), len(cand))
            if sim > best[2]:
                best = (i, i + w - 1, sim)
        if best[2] >= threshold:
            i0, i1 = owner[best[0]], owner[best[1]]
            match = (i0, i1, best[2])

    if not match:
        return None
    i0, i1, score = match
    w0, w1 = words[i0], words[i1]
    start, end = float(w0["start"]), float(w1["end"])
    if pad > 0:
        start = max(0.0, start - pad)
        end = end + pad
    matched_text = "".join(
        normalize_text(words[i].get("text", "")) for i in range(i0, i1 + 1)
    )
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "matched_text": matched_text,
        "score": round(score, 3),
        "word_range": [i0, i1],
    }


def snap_interval(words: list[dict], approx_start: float, approx_end: float,
                  *, slop: float = 1.5) -> dict | None:
    """把近似时间区间吸附到 ASR 台词语音边界，用于矫正手工秒数。

    优先取与原区间重叠的词；区间内无词时放宽 slop 秒找最近台词。返回
    {start,end,matched_text}；区间内确实无台词（纯画面）返回 None，保留原值。
    """
    start, end = float(approx_start), float(approx_end)
    overlap = [w for w in words if w["end"] > start and w["start"] < end]
    if not overlap and slop > 0:
        s2, e2 = start - slop, end + slop
        overlap = [w for w in words if w["end"] > s2 and w["start"] < e2]
    if not overlap:
        return None
    matched = "".join(normalize_text(w["text"]) for w in overlap)
    return {
        "start": round(overlap[0]["start"], 3),
        "end": round(overlap[-1]["end"], 3),
        "matched_text": matched,
    }
