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
# 分层生成（5-10 视频合一篇）：分片短稿用较小预算，融合保留大预算
SEGMENT_MAX_TOKENS = 8192
FUSE_MAX_TOKENS = 16384
# 中文口语约 250~300 字/分钟，取中值用于字数预算
CHARS_PER_MINUTE = 275

# 讲述人风格指令（分片与融合必须统一遵循，防止风格漂移）
_NARRATIVE_STYLE = """1. 只讲述素材中实际出现的情节、角色、对话和画面信息，不添加素材之外的事实。可以根据素材进行合理推断，但必须使用“可能”“初步判断”“看起来”“难道”“这意味着”等措辞明确推断性质。
2. 整体采用沉浸式剧情复盘风格：第三人称叙事为主，在调查、推理和关键转折处适度使用“我们”“旅行者一行”等表达，增强代入感，但不要每句都强行使用“我们”。
3. 普通对话不要逐句复述。优先把对话压缩成事件、行动和结果，例如“众人得知”“调查发现”“这让大家开始怀疑”。只有关键证据、身份揭露、重大反转、重要情绪表达或能体现角色个性的台词，才保留直接引语或明确标注发言者。
4. 禁止连续使用“角色名 + 说/表示/回答/解释/询问”等句式。连续两句中最多出现一次人物发言归属；一条解说中不要安排多个角色轮流发言。多个角色提供相同信息时，合并为一段调查过程。
5. 每个叙事单元只推进一个核心事件，并说明该事件带来的结果、判断或新疑问。优先使用“但”“然而”“于是”“结果”“出乎意料的是”“更奇怪的是”“也就是说”“难道”等因果、转折和悬念连接方式，但不要机械堆砌。
6. 句子保持短促、自然、适合中文配音。每个 narration 条目通常包含 1~3 个短句，每个短句尽量只表达一个动作或信息；避免一个条目塞入多个角色的连续发言和多个无关事件。
7. 解说人格克制、自然、有情绪温度，像一个冷静但投入的故事讲述者。情绪应服务于剧情，在危险、误会、失去、身份揭露和反转处自然加强，不要持续煽情、夸张吐槽或使用网络热梗。
8. 多个连续场景承担相同功能时要主动合并，避免“依次询问角色 A、角色 B、角色 C”的流水账；应概括调查结果，并突出这个结果对后续剧情的影响。"""

# 风格示例（只学习表达方式，不复用示例中的人物、情节和措辞）
_NARRATIVE_EXAMPLE = """“他们接连调查了几个人，却没有一个人听说过这座岛。连地图上都找不到它，事情开始变得不对劲。”
“尸体没有伤口，也没有中毒迹象。既然没人进出过房间，那么凶手就只可能藏在我们之中。”"""


def _build_prompt(timeline: dict, target_minutes: float, *,
                  compact: bool = True, video_label: str = "") -> str:
    """把索引压缩成 LLM 可消费的文本，避免让模型自己算秒数。

    compact（层1 压缩，5-10 视频合一篇的前置）：
    - 删除 unvoiced（无配音）行
    - unmatched 行不逐条喂入，降级为末尾一句区间标记（剧情跳跃提示）
    - shots 只保留有 line_ids 的（无台词引用的空镜/UI 对叙事贡献低）
    video_label：分片模式给片段命名（如"片段 gs-16-p1"），供融合阶段区分。
    """
    lines = timeline.get("lines", [])
    shots = timeline.get("shots", [])
    stats = timeline.get("stats", {})

    if compact:
        # 删除无配音行
        lines = [l for l in lines if l.get("align") != "unvoiced"]
        # unmatched 行全部降级为区间标记，不逐条喂（无 start 的用"素材各处"表述）
        unmatched = [l for l in lines if l.get("align") == "unmatched"]
        lines = [l for l in lines if l.get("align") != "unmatched"]
        # shots 只留有台词引用的
        shots = [s for s in shots if s.get("line_ids")]

    line_texts = []
    for l in lines:
        tag = {
            "matched": "✓",
            "interpolated": "~",
            "unmatched": "?",
            "unvoiced": "-",
        }.get(l.get("align", "matched"), "✓")
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

    # unmatched 降级标记（提示此处剧情跳跃，不消耗逐行 token）
    unmatched_note = ""
    if compact and unmatched:
        timed_u = [l for l in unmatched if l.get("start") is not None]
        span = (f"{min(l['start'] for l in timed_u):.0f}s-"
                f"{max(l['end'] for l in timed_u):.0f}s" if timed_u else "素材各处")
        unmatched_note = (
            f"\n【未匹配台词区间】{span} 存在约 {len(unmatched)} 段"
            f"无画面匹配的剧情，此处情节有跳跃，叙述时可一笔带过或衔接。")

    target_chars = int(target_minutes * CHARS_PER_MINUTE)
    # 全量索引进 prompt（deepseek-v4-flash 1M 上下文余量充足，不截断；
    # 真超长的极端场景（数小时×多视频）再考虑分段摘要，届时按场景切分而非行数截断）
    lines_block = chr(10).join(line_texts)
    shots_block = chr(10).join(shot_texts)
    scope = f"【{video_label}】" if video_label else ""
    prompt = f"""你是一位剧情向短视频解说撰稿人。{scope}请根据下面的素材索引，撰写一段约 {target_minutes} 分钟（约 {target_chars} 字）的沉浸式故事复盘稿。

你的任务不是逐句整理角色对话，而是用连续、清晰、有画面感的旁白，带观众经历故事的发展、调查、冲突和反转。直接进入故事内容，不要添加“这段剧情很精彩”之类的开场评价。

讲述人风格（必须严格遵循）：
{_NARRATIVE_STYLE}

结构要求：
9. 解说句数量控制在 15~30 个叙事单元之间，覆盖故事的起因、发展、主要转折和结局，**完整覆盖全部台词段，不得遗漏后半段情节**。
10. 每句解说必须标注它依据了哪些台词行（related_line_ids），可引用 1~5 个连续或分散的行 ID。
11. 不要输出具体时间秒数，只输出引用的台词行 ID；时间区间由后续程序自动补写。
12. 忽略标为 "-"（unvoiced）的无配音行，除非它对理解剧情必不可少。

风格示例（只学习表达方式，不复用示例中的人物、情节和措辞）：
{_NARRATIVE_EXAMPLE}

素材索引：

【台词表】标记说明：✓=ASR直接匹配, ~=插值估计, -=无配音
{lines_block}
{unmatched_note}

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
    """生成闸口1 人工审阅用 Markdown（解说句 + 引用台词 + 时间区间）。

    related_line_ids 支持两种结构：
    - 纯 id（单视频 oneshot）：[1, 2, 3]
    - {video_id, line_id}（多视频融合）：[{"video_id": "...", "line_id": 12}]
    """
    lines = timeline.get("lines", [])
    lines_by_key = {(l.get("video_id"), l["id"]): l for l in lines}
    md = ["# 解说稿（闸口1 审阅）\n", f"> 来源：{timeline.get('_source', 'timeline.json')}\n"]
    for n in narration:
        md.append(f"\n## 句{n['id']}\n")
        md.append(f"{n['text']}\n")
        related = n.get("related_line_ids", [])
        if related:
            times = []
            quoted = []
            for rid in related:
                if isinstance(rid, dict):
                    l = lines_by_key.get((rid.get("video_id"), rid.get("line_id")))
                    label = f"{rid.get('video_id')}:{rid.get('line_id')}"
                else:
                    l = next((x for x in lines if x["id"] == rid), None)
                    label = rid
                if l and l.get("start") is not None:
                    times.append((l["start"], l["end"]))
                    quoted.append(f"- [{label}] {l.get('speaker') or '？'}：{l['text']}")
            if times:
                t0 = min(t[0] for t in times)
                t1 = max(t[1] for t in times)
                md.append(f"**时间区间**：{t0:.1f}s - {t1:.1f}s\n")
            md.append("**引用台词**：\n")
            md.extend(quoted)
            md.append("")
    return "\n".join(md)


def _split_timeline(timeline: dict) -> dict[str, dict]:
    """把 global_timeline 按 video_id 拆成 per-video 索引（保留全局时间用于回源）。"""
    per: dict[str, dict] = {}
    for l in timeline.get("lines", []):
        vid = l.get("video_id")
        per.setdefault(vid, {"video_id": vid, "lines": [], "shots": []})["lines"].append(l)
    for s in timeline.get("shots", []):
        vid = s.get("video_id")
        if vid in per:
            per[vid]["shots"].append(s)
    return per


def _chat_json(prompt: str, *, max_tokens: int, temperature: float,
               label: str) -> list[dict]:
    """LLM 调用 + JSON 提取 + 空内容防护，返回 narration 列表。"""
    raw = chat(NARRATE_MODEL, [{"role": "user", "content": prompt}],
               max_tokens=max_tokens, temperature=temperature)
    if not raw.strip():
        raise RuntimeError(
            f"{NARRATE_MODEL} {label} 返回空内容（思考型模型推理耗尽 max_tokens 的典型症状，"
            f"预算 {max_tokens}）")
    return _extract_json(raw).get("narration", [])


def run_segment(seg_timeline: dict, *, target_minutes: float) -> dict:
    """层2 分片草稿：单视频索引 → 本片段解说稿（related_line_ids 为片段内纯 id）。"""
    prompt = _build_prompt(seg_timeline, target_minutes,
                           compact=True, video_label=seg_timeline.get("video_id", ""))
    narration = _chat_json(prompt, max_tokens=SEGMENT_MAX_TOKENS,
                           temperature=0.6, label=f"分片 {seg_timeline.get('video_id')}")
    return {"video_id": seg_timeline.get("video_id"),
            "narration": narration}


def _build_fuse_prompt(segments: list[dict], target_minutes: float) -> str:
    """融合 prompt：各分片草稿（小体积）→ 统一口吻/去重/裁剪 → 终稿。

    输出 related_line_ids 用 {"video_id","line_id"} 结构，line_id 必须是草稿中出现过的。
    """
    target_chars = int(target_minutes * CHARS_PER_MINUTE)
    blocks = []
    for seg in segments:
        vid = seg.get("video_id")
        items = [f"句{n.get('id')}(id={n.get('id')}) {n.get('text')}"
                 for n in seg.get("narration", [])]
        blocks.append(f"【片段 {vid}】\n" + "\n".join(items))
    segments_block = "\n\n".join(blocks)

    return f"""你是短视频解说稿总编。下面是同一故事的 {len(segments)} 个片段解说草稿（每个片段是时间上连续的一段剧情），请融合成一篇完整的沉浸式故事复盘稿（约 {target_minutes} 分钟，约 {target_chars} 字）。

**必须严格遵循统一的讲述人风格**（分片草稿已按此风格撰写，融合不得漂移）：
{_NARRATIVE_STYLE}

融合额外要求：
1. 统一口吻、术语、人称，删除跨片段重复内容。
2. 按时间顺序重排，补齐片段边界的衔接，让整篇故事连续流畅。
3. 控制 15~30 个叙事单元，覆盖起因、发展、主要转折、结局，不遗漏任何片段的核心情节。
4. 每句解说必须标注依据的台词行，related_line_ids 用 {{"video_id": "...", "line_id": N}} 结构——video_id 必须是该句所属片段的标识，line_id 必须是草稿中出现过的 id。
5. 不要输出具体时间秒数。

风格示例（只学习表达方式，不复用示例中的人物、情节和措辞）：
{_NARRATIVE_EXAMPLE}

片段草稿：
{segments_block}

严格按以下 JSON 格式输出，不要包含其他文字：
{{
  "narration": [
    {{
      "id": 1,
      "text": "第一句解说文案",
      "related_line_ids": [{{"video_id": "gs-16-p1", "line_id": 12}}]
    }}
  ]
}}
"""


def fuse(segments: list[dict], *, target_minutes: float) -> list[dict]:
    """层2 全局融合：多分片草稿 → 终稿 narration（related_line_ids 为 {video_id, line_id}）。"""
    prompt = _build_fuse_prompt(segments, target_minutes)
    return _chat_json(prompt, max_tokens=FUSE_MAX_TOKENS,
                      temperature=0.4, label="融合")


def run(timeline_path: Path, output_dir: Path, *, target_minutes: float = 15.0,
        mode: str = "auto") -> dict:
    """生成解说稿 → narration.json + narration.md。

    mode：
    - auto（默认）：多视频（videos>1）走分片+融合（层2），单视频走 oneshot
    - segment：强制分层（多视频合一篇）
    - oneshot：单次调用（单视频）
    """
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    timeline["_source"] = str(timeline_path)
    videos = timeline.get("videos", [])
    multi = mode == "segment" or (mode == "auto" and len(videos) > 1)

    output_dir.mkdir(parents=True, exist_ok=True)

    if multi:
        # 层2 分层：每视频分片草稿（compact、有界输入）→ 全局融合
        per = _split_timeline(timeline)
        seg_dir = output_dir / "narration_segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segments = []
        per_minutes = target_minutes / max(len(videos), 1)
        for vid in [v["video_id"] for v in videos]:
            if vid not in per:
                continue
            seg = run_segment(per[vid], target_minutes=per_minutes)
            (seg_dir / f"{vid}.json").write_text(
                json.dumps(seg, ensure_ascii=False, indent=2), encoding="utf-8")
            segments.append(seg)
        narration = fuse(segments, target_minutes=target_minutes)
        used_mode = "segment"
    else:
        # 层1 压缩的单次调用（单视频）
        narration = _chat_json(_build_prompt(timeline, target_minutes, compact=True),
                               max_tokens=NARRATE_MAX_TOKENS, temperature=0.6,
                               label="解说稿")
        used_mode = "oneshot"

    (output_dir / "narration.json").write_text(
        json.dumps({"model": NARRATE_MODEL, "target_minutes": target_minutes,
                    "mode": used_mode, "narration": narration},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    (output_dir / "narration.md").write_text(
        _to_markdown(narration, timeline), encoding="utf-8")
    return {"sentences": len(narration), "mode": used_mode,
            "output_dir": str(output_dir)}
