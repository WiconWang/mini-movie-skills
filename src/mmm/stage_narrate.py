"""阶段5：解说稿生成。

输入时间轴索引（单视频 timeline.json 或多视频 global_timeline.json），
由文本 LLM 生成解说稿。每句解说必须引用相关台词 ID，由管线补写时间区间——
LLM 不直接输出秒数，从结构上消灭伪造时间戳的故障（设计文档 §4 阶段5）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import models
from .llm import chat

# 解说模型（.env 可覆盖：MMM_NARRATE_MODEL，未配置时回退默认）
NARRATE_MODEL = os.environ.get("MMM_NARRATE_MODEL", "deepseek-v4-flash")
# 中文口语约 250~300 字/分钟，取中值用于字数预算
CHARS_PER_MINUTE = 275

# 讲述人风格指令（分片与融合必须统一遵循，防止风格漂移）
_NARRATIVE_STYLE = """1. 【素材边界】只讲述素材中实际出现的情节、角色、对话和画面信息，不添加素材之外的事实。可以在素材基础上合理推断，但用叙述语气自然带过（如"看起来""多半是""难道说"），不要写成下结论式的报告腔。
2. 【玩家视角】站在游戏操作者的视角讲述，"我们"指代玩家扮演的主角（旅行者、漂泊者、管理员等），主角的调查、行动、发现、决定都从"我们"出发；其他角色用名字或称谓第三人称描述。不要每句都堆"我们"，在行动与转折处自然使用即可。
3. 【对话取舍】对话不逐句复述：过场性、程序性的对话压缩成事件与结果即可；而身份揭露、重大反转、能立住角色个性的台词，保留直接引语让角色自己开口。引用宜短不宜长，爆点台词单独成句，前面留足铺垫。
4. 【点名分层】一场戏里角色有主有次，名字只留给值得记住的人：本段的推动者、真相揭示者，以及反应本身就带着个人印记的角色。其余角色并入"众人""大家"等群体动作，不为凑数报名字；多人传递相同信息时，合并成一次调查或一个结论。
5. 【句首呼吸】句子开头优先用场景、事件、情绪带入，避免连续多句以人名开头；人名出现后，后续描述尽量用"她""他""这位"承接，让听感松弛下来。
6. 【推进与留白】每个叙事单元只推进一个核心事件，并交代它带来的结果、判断或新疑问。转折和悬念靠内容与铺垫本身制造，慎用"出乎意料的是""更奇怪的是"这类强调套话起手。
7. 【配音节奏】句子保持短促自然、适合中文口播，每个条目通常一到三句。允许偶尔用一个长句铺陈蓄力、再用短句收束落点；通篇碎短句反而没有节奏。
8. 【温度】解说人格冷静但有温度，情绪服务于剧情：在危险、误会、失去、身份揭露和反转处自然加强，平时收着。不使用网络热梗，也不用解说者自己的油滑吐槽盖过剧情；素材中角色本人的幽默与吐槽照实呈现，那是剧情的一部分。
9. 【合并与收束】多个连续场景承担相同功能时主动合并，概括结果并点出其对后续剧情的影响；但角色各自的差异化反应是剧情内容，不在此列。段落关键节点用一帧画面感的描写收束，不要以"某某说"结尾。
10. 【口播稿】这是给主播念的口播稿，不是文章。禁用破折号"——"引出同位语、解释、补充（如"想到丽莎——泡在图书馆的人"），改用逗号或拆成独立短句承接；冒号副标题、书名号套副标题等念出来会卡壳的句式同样避免。写完默念一遍，凡需要回气、断不开的，重写。"""

# 风格示例（只学习表达方式，不复用示例中的人物、情节和措辞）
_NARRATIVE_EXAMPLE = """“消息传开，营地里炸开了锅。有人收拾行李，有人骂骂咧咧，只有守夜的老猎人蹲在火堆旁，憋了半天冒出一句：‘我就说那晚的星星不对劲。’”
“我们循着歌声走到崖边，才发现整座小镇的人都聚在那里，没有人说话。原来他们早就知道了，只是一直在等我们自己走到这一步。”"""

# 模型适配层（config/models.yaml）：max_tokens、temperature、分片并发等按模型切换
_PROFILE = models.profile_for(NARRATE_MODEL)
NARRATE_MAX_TOKENS = _PROFILE.max_tokens_cap or 32768
SEGMENT_MAX_TOKENS = _PROFILE.narration_segment_max_tokens or 8192
FUSE_MAX_TOKENS = _PROFILE.narration_fuse_max_tokens or 16384
# 提示词指纹：风格指令或示例变更后视为新版本，旧分片不续用
PROMPT_FP = hashlib.sha256(
    (_NARRATIVE_STYLE + _NARRATIVE_EXAMPLE).encode("utf-8")).hexdigest()[:12]


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
    _ui_label = {"none": "无UI", "dialogue": "对话UI", "gameplay": "操作UI"}
    for s in shots:
        desc = s.get("description") or "（无描述）"
        cls = s.get("class", "A")
        motion = s.get("motion", "low")
        ui = _ui_label.get(s.get("ui_type"), "无UI")
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
    prompt = f"""你是一位剧情向短视频解说口播稿撰写人——稿子要被主播念出来，不是给人读的文章。{scope}请根据下面的素材索引，撰写一段约 {target_minutes} 分钟（约 {target_chars} 字）的沉浸式故事复盘稿。

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
                 + f" 引用台词:{n.get('related_line_ids')}"
                 for n in seg.get("narration", [])]
        blocks.append(f"【片段 {vid}】\n" + "\n".join(items))
    segments_block = "\n\n".join(blocks)

    return f"""你是短视频解说口播稿总编——产出要被主播念出来，不是给人读的文章。下面是同一故事的 {len(segments)} 个片段解说草稿（每个片段是时间上连续的一段剧情），请融合成一篇完整的沉浸式故事复盘稿（约 {target_minutes} 分钟，约 {target_chars} 字）。

**必须严格遵循统一的讲述人风格**（分片草稿已按此风格撰写，融合不得漂移）：
{_NARRATIVE_STYLE}

融合额外要求：
1. 统一口吻、术语、人称，删除跨片段重复内容。
2. 按时间顺序重排，补齐片段边界的衔接，让整篇故事连续流畅。
3. 控制 15~30 个叙事单元，覆盖起因、发展、主要转折、结局，不遗漏任何片段的核心情节。
4. 每句解说必须标注依据的台词行，related_line_ids 用 {{"video_id": "...", "line_id": N}} 结构——**line_id 填你依据的片段草稿句号（即草稿里"句N(id=N)"的 N）**，只许引用草稿中实际存在的句号，多个草稿句就写多个对象。
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


def _remap_line_refs(narration: list[dict], segments: list[dict]) -> list[dict]:
    """融合输出引用的是【草稿句号】→ 映射回台词行 id（草稿句的 related_line_ids）。

    融合 LLM 对"草稿句号"与"台词行 id"两套数字易混淆，让 LLM 只引用草稿句号，
    此处做确定性映射：seg 草稿第 N 句 → 该句的 related_line_ids（台词行 id 集）。
    映射不到的引用原样保留（select 端解析失败走兜底，不阻塞）。
    """
    seg_by_key: dict[tuple[str, int], list] = {}
    for seg in segments:
        vid = seg.get("video_id")
        for sn in seg.get("narration", []):
            seg_by_key[(vid, sn.get("id"))] = sn.get("related_line_ids", [])
    out = []
    for s in narration:
        refs = []
        for r in s.get("related_line_ids", []):
            if not isinstance(r, dict):
                refs.append(r)
                continue
            vid, lid = r.get("video_id"), r.get("line_id")
            if (vid, lid) in seg_by_key:
                refs.extend({"video_id": vid, "line_id": lid2}
                            for lid2 in seg_by_key[(vid, lid)])
            else:
                refs.append(r)
        out.append({**s, "related_line_ids": refs})
    return out


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
        segments: dict[str, dict] = {}
        per_minutes = target_minutes / max(len(videos), 1)
        target_vids = [v["video_id"] for v in videos if v["video_id"] in per]

        def _segment_for(vid: str) -> tuple[str, dict]:
            seg_path = seg_dir / f"{vid}.json"
            if seg_path.exists():
                try:
                    existing = json.loads(seg_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = None
                if existing and existing.get("_model") == NARRATE_MODEL and \
                        existing.get("_prompt_fp") == PROMPT_FP and existing.get("narration"):
                    return vid, existing
            seg = run_segment(per[vid], target_minutes=per_minutes)
            seg = {**seg, "_model": NARRATE_MODEL, "_prompt_fp": PROMPT_FP}
            seg_path.write_text(json.dumps(seg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            return vid, seg

        workers = _PROFILE.narration_segment_workers or 1
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_segment_for, vid): vid for vid in target_vids}
                for fut in as_completed(futures):
                    vid, seg = fut.result()
                    segments[vid] = seg
        else:
            for vid in target_vids:
                vid, seg = _segment_for(vid)
                segments[vid] = seg

        ordered_segments = [segments[vid] for vid in target_vids]
        narration = fuse(ordered_segments, target_minutes=target_minutes)
        # 融合输出引用的是草稿句号，映射回台词行 id（确定性，不依赖 LLM 记性）
        narration = _remap_line_refs(narration, ordered_segments)
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
