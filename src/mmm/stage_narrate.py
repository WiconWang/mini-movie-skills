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
# deepseek-v4-flash 是思考型模型：reasoning 与正文共享 max_tokens 预算。
# 全量索引（2万+ token 输入）下推理可烧掉 8k+，预算不足会 finish_reason=length 且正文为空。
# 实测推理 20102 字符 + 正文约 3k token，给 32768 留足余量。
NARRATE_MAX_TOKENS = 32768
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
    # 全量索引进 prompt（deepseek-v4-flash 1M 上下文余量充足，不截断；
    # 真超长的极端场景（数小时×多视频）再考虑分段摘要，届时按场景切分而非行数截断）
    lines_block = chr(10).join(line_texts)
    shots_block = chr(10).join(shot_texts)
    prompt = f"""你是一位剧情向短视频解说撰稿人。请根据下面的素材索引，撰写一段约 {target_minutes} 分钟（约 {target_chars} 字）的沉浸式故事复盘稿。

你的任务不是逐句整理角色对话，而是用连续、清晰、有画面感的旁白，带观众经历故事的发展、调查、冲突和反转。直接进入故事内容，不要添加“这段剧情很精彩”之类的开场评价。

要求：
1. 只讲述素材中实际出现的情节、角色、对话和画面信息，不添加素材之外的事实。可以根据素材进行合理推断，但必须使用“可能”“初步判断”“看起来”“难道”“这意味着”等措辞明确推断性质。
2. 整体采用沉浸式剧情复盘风格：第三人称叙事为主，在调查、推理和关键转折处适度使用“我们”“旅行者一行”等表达，增强代入感，但不要每句都强行使用“我们”。
3. 普通对话不要逐句复述。优先把对话压缩成事件、行动和结果，例如“众人得知”“调查发现”“这让大家开始怀疑”。只有关键证据、身份揭露、重大反转、重要情绪表达或能体现角色个性的台词，才保留直接引语或明确标注发言者。
4. 禁止连续使用“角色名 + 说/表示/回答/解释/询问”等句式。连续两句中最多出现一次人物发言归属；一条解说中不要安排多个角色轮流发言。多个角色提供相同信息时，合并为一段调查过程。
5. 每个叙事单元只推进一个核心事件，并说明该事件带来的结果、判断或新疑问。优先使用“但”“然而”“于是”“结果”“出乎意料的是”“更奇怪的是”“也就是说”“难道”等因果、转折和悬念连接方式，但不要机械堆砌。
6. 句子保持短促、自然、适合中文配音。每个 narration 条目通常包含 1~3 个短句，每个短句尽量只表达一个动作或信息；避免一个条目塞入多个角色的连续发言和多个无关事件。
7. 解说人格克制、自然、有情绪温度，像一个冷静但投入的故事讲述者。情绪应服务于剧情，在危险、误会、失去、身份揭露和反转处自然加强，不要持续煽情、夸张吐槽或使用网络热梗。
8. 多个连续场景承担相同功能时要主动合并，避免“依次询问角色 A、角色 B、角色 C”的流水账；应概括调查结果，并突出这个结果对后续剧情的影响。
9. 解说句数量控制在 15~30 个叙事单元之间，覆盖故事的起因、发展、主要转折和结局，**完整覆盖全部台词段，不得遗漏后半段情节**。
10. 每句解说必须标注它依据了哪些台词行（related_line_ids），可引用 1~5 个连续或分散的行 ID。
11. 不要输出具体时间秒数，只输出引用的台词行 ID；时间区间由后续程序自动补写。
12. 忽略标为 "-"（unvoiced）的无配音行，除非它对理解剧情必不可少。

风格示例（只学习表达方式，不复用示例中的人物、情节和措辞）：
“他们接连调查了几个人，却没有一个人听说过这座岛。连地图上都找不到它，事情开始变得不对劲。”
“尸体没有伤口，也没有中毒迹象。既然没人进出过房间，那么凶手就只可能藏在我们之中。”

素材索引：

【台词表】标记说明：✓=ASR直接匹配, ~=插值估计, ?=未匹配/可能未录, -=无配音
{lines_block}

【镜头表】标记说明：E/D/C/B/A 为画面优先级（E最高，A最低）
{shots_block}

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
    raw = chat(NARRATE_MODEL, [{"role": "user", "content": prompt}],
               max_tokens=NARRATE_MAX_TOKENS, temperature=0.6)
    if not raw.strip():
        raise RuntimeError(
            f"{NARRATE_MODEL} 返回空内容（思考型模型推理耗尽 max_tokens 的典型症状，"
            f"当前预算 {NARRATE_MAX_TOKENS}）")
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
