#!/usr/bin/env python3
"""md2jsonl：BWIKI 采集的 markdown 剧情文案 → 物料规范 script.jsonl。

转换规则（对应 docs/2026/0817-物料规范.md §3）：
- 跳过头部元数据（**字段**、# / ## 标题、分隔线）
- ### ◆小节标题 → 记入 meta.json 的 sections（不打断台词流）
- （括号）开头的整行 → 视为无配音旁白/场景描述，剔除
- 「角色名 : 台词」（半角/全角冒号、带空格变体）→ {"speaker", "text"}
- 长句按句末标点拆分（。！？；… 含闭合引号）——规范 §3 第4条

用法：python3 tools/md2jsonl.py <输入.md> <输出.jsonl> [--meta <meta.json>] [--unvoiced-speakers 旅行者,旁白]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DIALOGUE_RE = re.compile(r"^([^\s#（【].{0,10}?)\s*[:：]\s*(.+)$")
SECTION_RE = re.compile(r"^###\s*◆?\s*(.+)$")
SENTENCE_SPLIT = re.compile(r"(?<=[。！？；…])[」』”’]?")


def split_sentences(text: str) -> list[str]:
    parts = [p for p in SENTENCE_SPLIT.split(text) if p and p.strip()]
    return [p.strip() for p in parts] if parts else [text.strip()]


def convert(md_path: Path, unvoiced_speakers: set[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """返回 (台词行, sections, 警告)。unvoiced_speakers：无配音角色（如主角选项行）。"""
    unvoiced_speakers = unvoiced_speakers or set()
    lines, sections, warnings = [], [], []
    for lineno, raw in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s == "---" or s.startswith("**"):
            continue
        if m := SECTION_RE.match(s):
            sections.append({"title": m.group(1), "start_line": len(lines) + 1})
            continue
        if s.startswith("#"):
            continue
        if s.startswith("（") or s.startswith("("):
            continue  # 无配音旁白
        if m := DIALOGUE_RE.match(s):
            speaker, text = m.group(1).strip(), m.group(2).strip()
            for sent in split_sentences(text):
                if len(sent) > 50:
                    warnings.append(f"第{lineno}行拆分后仍超50字: {sent[:20]}…")
                line = {"speaker": speaker, "text": sent}
                if speaker in unvoiced_speakers:
                    line["voiced"] = False
                lines.append(line)
        else:
            warnings.append(f"第{lineno}行无法识别，已跳过: {s[:30]}")
    return lines, sections, warnings


def main() -> None:
    md_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    meta_path = None
    unvoiced: set[str] = set()
    args = sys.argv[3:]
    if "--meta" in args:
        meta_path = Path(args[args.index("--meta") + 1])
    if "--unvoiced-speakers" in args:
        unvoiced = set(args[args.index("--unvoiced-speakers") + 1].split(","))

    lines, sections, warnings = convert(md_path, unvoiced)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    if meta_path:
        meta_path.write_text(json.dumps({"sections": sections}, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    n_unvoiced = sum(1 for l in lines if l.get("voiced") is False)
    print(f"✓ {md_path.name} → {len(lines)} 行台词（其中无配音 {n_unvoiced} 行）, {len(sections)} 个小节")
    for w in warnings:
        print(f"  ⚠ {w}")


if __name__ == "__main__":
    main()
