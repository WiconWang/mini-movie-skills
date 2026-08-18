"""阶段5：解说稿生成。

输入时间轴索引（单视频 timeline.json 或多视频 global_timeline.json），
由文本 LLM 生成解说稿。每句解说必须引用相关台词 ID，由管线补写时间区间——
LLM 不直接输出秒数，从结构上消灭伪造时间戳的故障（设计文档 §4 阶段5）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .llm import chat

NARRATE_MODEL = "deepseek-v4-flash"
# 中文口语约 250~300 字/分钟，取中值用于字数预算
CHARS_PER_MINUTE = 275


def _build_prompt(timeline: dict, target_minutes: float) -> str:
    """把索引压缩成 LLM 可消费的文本，避免让模型自己算秒数。"""
    lines = timeline.get("lines", [])
    shots = timeline.get("shots", [])
    stats = timeline.get("stats", {})

    # 只把有时间的台词行给模型；unmatched 也带上，让模型知道「这里有剧情但缺画面」
    line_texts = []
    for l in lines:
        tag = {
            "matched": "✓",
            "interpolated": "~",
            "unmatched": "?",
            "unvoiced": "-",
        }.get(l.get("align", "unmatched"), "?")
        time_mark = f"[{l.get('start'):.1f}-{l.get('end'):.1f}]" if l.get("start") is not None else "[无时间]"
        speaker = l.get("speaker") or "旁白"
        line_texts.append(
            f"行{l['id']:03d} {tag} {time_mark} {speaker}：{l['text']}"
        )

    shot_texts = []
    for s in shots:
        desc = s.get("description") or "（无描述）"
        cls = s.get("class", "A")
        motion = s.get("motion", "low")
        ui = "有UI" if s.get("has_ui") else "无UI"
        shot_texts.append(
            f"镜头{s['id']:03d}[{cls}] {s['start']:.1f}-{s['end']:.1f} "
            f"motion={motion} {ui}：{desc}"
        )

    target_chars = int(target_minutes * CHARS_PER_MINUTE)
    prompt = f"""你是一位剧情向短视频解说撰稿人。请根据下面的素材索引，撰写一段约 {target_minutes} 分钟（约 {target_chars} 字）的解说稿。

要求：
1. 只讲述素材中实际出现的情节、角色、对话，不添加主观点评、吐槽或脑补。
2. 解说人格活泼、有情绪温度，但事实是铁证；全程用第三人称叙述。
3. 每句解说必须标注它依据了哪些台词行（related_line_ids），可引用 1~5 个连续或分散的行 ID。
4. 不要输出具体时间秒数，只输出引用的台词行 ID；时间区间由后续程序自动补写。
5. 解说句数量控制在 15~30 句之间，覆盖故事的起承转合（起因、转折、结局）。
6. 忽略标为 "-"（unvoiced）的无配音行，除非它对理解剧情必不可少。

素材索引：

【台词表】标记说明：✓=ASR直接匹配, ~=插值估计, ?=未匹配/可能未录, -=无配音
{chr(10).join(line_texts[:400])}  {"……（后续台词省略）" if len(line_texts) > 400 else ""}

【镜头表】标记说明：E/D/C/B/A 为画面优先级（E最高，A最低）
{chr(10).join(shot_texts[:150])}  {"……（后续镜头省略）" if len(shot_texts) > 150 else ""}

整体统计：{stats.get('shots', 0)} 镜头，分级 {stats.get('by_class', {})}。

请严格按以下 JSON 格式输出，不要包含其他文字：
{{
  "narration": [
    {{
      "id": 1,
      "text": "第一句解说文案",
      "related_line_ids": [1, 2, 3]
    }}
  ]
}}
"""
    return prompt


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON（支持 ```json 代码块）。"""
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    return json.loads(text)


def _to_markdown(narration: list[dict], timeline: dict) -> str:
    """生成闸口1 人工审阅用 Markdown（解说句 + 引用台词 + 时间区间）。"""
    lines_by_id = {l["id"]: l for l in timeline.get("lines", [])}
    md = ["# 解说稿（闸口1 审阅）\n", f"> 来源：{timeline.get('_source', 'timeline.json')}\n"]
    for n in narration:
        md.append(f"\n## 句{n['id']}\n")
        md.append(f"{n['text']}\n")
        related = n.get("related_line_ids", [])
        if related:
            times = []
            quoted = []
            for rid in related:
                l = lines_by_id.get(rid)
                if l and l.get("start") is not None:
                    times.append((l["start"], l["end"]))
                    quoted.append(f"- [{rid}] {l.get('speaker') or '？'}：{l['text']}")
            if times:
                t0 = min(t[0] for t in times)
                t1 = max(t[1] for t in times)
                md.append(f"**时间区间**：{t0:.1f}s - {t1:.1f}s\n")
            md.append("**引用台词**：\n")
            md.extend(quoted)
            md.append("")
    return "\n".join(md)


def run(timeline_path: Path, output_dir: Path, *, target_minutes: float = 15.0) -> dict:
    """生成解说稿 → narration.json + narration.md。"""
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    timeline["_source"] = str(timeline_path)

    prompt = _build_prompt(timeline, target_minutes)
    raw = chat(NARRATE_MODEL, [{"role": "user", "content": prompt}], max_tokens=8192, temperature=0.6)
    data = _extract_json(raw)
    narration = data.get("narration", [])

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "narration.json").write_text(
        json.dumps({"model": NARRATE_MODEL, "target_minutes": target_minutes,
                    "narration": narration}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (output_dir / "narration.md").write_text(
        _to_markdown(narration, timeline), encoding="utf-8")
    return {"sentences": len(narration), "output_dir": str(output_dir)}
