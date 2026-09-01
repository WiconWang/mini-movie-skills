"""系列级 TTS 发音兜底词库。

词库只做 fallback：LLM 已给出的发音保持优先，词库仅在 LLM 未识别该词时补齐。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..db import PROJECT_ROOT
from .types import PronunciationRule

PINYIN_RE = re.compile(
    r"^(?:[a-zA-ZüÜvV]+[1-5])(?: (?:[a-zA-ZüÜvV]+[1-5]))*$"
)


def load_series_pronunciations(series: str, version: str = "") -> list[PronunciationRule]:
    """按系列和版本加载兜底发音；version 条目覆盖同词 common 条目。"""
    if not series:
        return []
    path = PROJECT_ROOT / "config" / "tts" / f"{series}.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[tuple[str, str]] = []
    for item in data.get("common") or []:
        entries.append((item.get("term"), item.get("pinyin")))
    version_items = (data.get("versions") or {}).get(str(version)) or []
    for item in version_items:
        entries.append((item.get("term"), item.get("pinyin")))

    merged: dict[str, PronunciationRule] = {}
    for term, pinyin in entries:
        term = str(term or "").strip()
        pinyin = str(pinyin or "").strip()
        if not term or not pinyin:
            continue
        if not PINYIN_RE.fullmatch(pinyin):
            raise ValueError(
                f"发音词库拼音格式错误: {term}={pinyin}，应使用 an1 bo2 形式"
            )
        merged[term] = PronunciationRule(term=term, pinyin=pinyin, note="")
    return list(merged.values())
