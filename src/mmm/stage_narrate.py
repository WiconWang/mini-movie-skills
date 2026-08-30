"""阶段5：证据抽取、终稿写作与本地引用防伪。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .llm import LLMEndpoint, estimate_tokens, load_endpoint

CHARS_PER_MINUTE = 275
SCHEMA = "story_beats_v1"

_LOW_EXTRACTION_INSTRUCTIONS = """1. 【素材边界】只能使用素材索引中出现的信息，不虚构事实。可以在证据支持时标注推测，但必须说明置信度。
2. 【证据完整】按时间顺序抽取剧情节拍，完整覆盖起因、发展、转折、冲突和结局。不要为了摘要漂亮而删除潜在反转、身份揭露或后果。
3. 【因果人物】保留人物规范名、身份变化、动机、因果链和事件影响；过程性对话可以合并，但关键结论不能丢。
4. 【关键台词】身份揭露、重大反转、强烈情绪和能立住人物个性的台词保留原文；一般过程性对话可以转述。
5. 【引用防伪】每个 beat 必须给出 related_line_ids，且只能引用素材索引中真实存在、非 unvoiced 的台词行 ID。key_quotes 的 line_id 也必须真实存在。
6. 【不要写稿】不要优化口播节奏、文笔、人称或成稿句式。summary 使用中性、完整、可核查的事件描述。"""

_HIGH_NARRATION_STYLE = """1. 只讲述证据中明确呈现的信息（对话、过场、文本、环境），不添加证据之外的事实。可以在证据基础上做合理推断，推断必须服务于解释角色动机或填补逻辑空隙，不能用于制造额外情节。推断用叙述语气自然带过（如“看起来”“多半是”“难道说”），不要写成报告腔。
2. 【玩家视角】站在游戏操作者视角讲述，“我们”指代玩家扮演的主角；主角的调查、行动、发现、决定都从“我们”出发，其他角色用名字或称谓第三人称描述。
3. 【对话取舍】对话不逐句复述：过程性对话压缩为事件与结果；身份揭露、重大反转、能立住角色个性的台词保留直接引语。引用宜短不宜长，爆点台词单独成句。若对话本身包含关键信息但较长，可用“她简要说明了……”概括，并将其中最扎心的一句单独拎出。
4. 【点名分层】名字留给推动者、真相揭示者和反应有个人印记的角色；其余并入“众人”“大家”等群体动作，多人传递相同信息时合并。若连续多次提及同一名角色，第二次起可用代词或称呼（如“这位商人”“老猎户”）替换，以减轻听觉疲劳。
5. 【句首呼吸】句首优先用场景、事件、情绪带入，避免连续多句以人名开头；句首可优先使用环境音效、动作、情绪词（如“雨声刚停”“一推门”“接过名册时”）；人名出现后用代词或称谓承接。
6. 【推进留白】每个叙事单元（通常对应一个任务目标或地点转换）推进一个核心事件，并交代结果、判断或新疑问；转折靠内容与铺垫，不用套话强调。关键证据或反转应在叙述中逐步披露，避免一次性平铺；
7. 【口播节奏】句子短促自然、适合中文口播，每个条目通常一到三句；每段建议3~5句，但根据情绪灵活调整。可用长句（20字内）蓄力、短句收束，避免通篇碎短句；在情绪高潮处允许连续短句（每句≤10字），在铺垫处可用一个中等长度句蓄力。
8. 【温度】解说人格冷静但有温度，情绪在危险、误会、失去、身份揭露和反转处自然加强；不使用网络热梗，不覆盖角色本人的幽默。在遭遇危险或揭露真相时，可加入主播自身的轻微反应词（如“这就有点意外了”“我们当时愣住了”），但必须与角色情绪区分开，避免抢戏。不在任何情况下使用感叹号堆砌（!!）。
9. 【合并收束】相同功能的连续事件主动合并，概括结果并点出影响；关键节点用画面感描写收束，不以“某某说”结尾。
10. 【口播稿】这是给主播念的稿子。禁用破折号引出解释或同位语；冒号副标题、书名号套副标题等影响换气的句式避免。禁止使用括号进行补充说明，所有内容必须融入主句；禁止使用“——”破折号，若需递进可用“，也就是”或“，即”。
11. """

_HIGH_EVIDENCE_CONTRACT = (
    "beat_evidence_v2:omit_related_line_ids;keep_first_key_quote"
)

_NARRATIVE_EXAMPLE = """“消息传开，营地里炸开了锅。有人收拾行李，有人骂骂咧咧，只有守夜的老猎人蹲在火堆旁，憋了半天冒出一句：‘我就说那晚的星星不对劲。’”
“我们循着歌声走到崖边，才发现整座小镇的人都聚在那里，没有人说话。原来他们早就知道了，只是一直在等我们自己走到这一步。”"""

_LOW_OUTPUT_SCHEMA_HINT = """{
  "beats": [
    {
      "id": 1,
      "summary": "中性事件摘要",
      "characters": ["角色名"],
      "cause": "直接原因，未知则为空字符串",
      "effect": "事件影响，未知则为空字符串",
      "key_quotes": [{"speaker": "角色名", "text": "关键台词原文", "line_id": 1}],
      "related_line_ids": [1, 2],
      "importance": "core/supporting/background",
      "confidence": "high/medium/low"
    }
  ]
}"""

LOW_PROMPT_FP = hashlib.sha256(
    (_LOW_EXTRACTION_INSTRUCTIONS + _LOW_OUTPUT_SCHEMA_HINT).encode("utf-8")
).hexdigest()[:12]
HIGH_PROMPT_FP = hashlib.sha256(
    (_HIGH_NARRATION_STYLE + _NARRATIVE_EXAMPLE + _HIGH_EVIDENCE_CONTRACT).encode("utf-8")
).hexdigest()[:12]


@dataclass
class SegmentPlan:
    video_id: str
    chunk_id: str
    segment_id: str
    timeline: dict
    prompt: str
    cache_path: Path | None = None
    cache_hit: bool = False

    def timeline_fingerprint(self) -> str:
        payload = {
            "video_id": self.video_id,
            "chunk_id": self.chunk_id,
            "lines": self.timeline.get("lines", []),
            "shots": self.timeline.get("shots", []),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class NarratePlan:
    mode: str
    segments: list[SegmentPlan]
    high_prompt: str | None
    high_cache_reusable: bool
    high_cache_reason: str
    high_endpoint: LLMEndpoint
    low_endpoint: LLMEndpoint | None
    low_requests: int
    high_requests: int
    estimated_high_prompt_tokens: int | None
    needs_beat_remap: bool
    force: bool = False


def _usable_lines(timeline: dict) -> list[dict]:
    return [
        line for line in timeline.get("lines", [])
        if line.get("align") != "unvoiced"
    ]


def _line_label(line: dict) -> str:
    tag = {
        "matched": "✓", "interpolated": "~", "unmatched": "?", "unvoiced": "-",
    }.get(line.get("align", "matched"), "✓")
    start = line.get("start")
    end = line.get("end")
    time_mark = f"[{start:.1f}-{end:.1f}]" if start is not None and end is not None else "[无时间]"
    return f"行{line['id']:03d} {tag} {time_mark} {line.get('speaker') or '旁白'}：{line.get('text', '')}"


def _shot_label(shot: dict) -> str:
    ui_labels = {"none": "无UI", "dialogue": "对话UI", "gameplay": "操作UI"}
    return (
        f"镜头{shot['id']:03d}[{shot.get('class', 'A')}] "
        f"{shot.get('start', 0):.1f}-{shot.get('end', 0):.1f} "
        f"motion={shot.get('motion') or 'low'} "
        f"{ui_labels.get(shot.get('ui_type'), '无UI')}：{shot.get('description') or '（无描述）'}"
    )


def _timeline_blocks(timeline: dict) -> tuple[str, str]:
    lines = _usable_lines(timeline)
    referenced_shot_ids: set[int] = set()
    for shot in timeline.get("shots", []):
        for line_id in shot.get("line_ids", []):
            referenced_shot_ids.add(shot["id"])
    shots = [s for s in timeline.get("shots", []) if s["id"] in referenced_shot_ids]
    return (
        "\n".join(_line_label(line) for line in lines),
        "\n".join(_shot_label(shot) for shot in shots),
    )


def _build_low_prompt(timeline: dict) -> str:
    lines_block, shots_block = _timeline_blocks(timeline)
    return f"""你是剧情证据整理器。请从下面的素材索引中抽取完整剧情节拍，供后续总编写作使用。不要写解说稿。

抽取要求：
{_LOW_EXTRACTION_INSTRUCTIONS}

素材索引：
【台词表】✓=直接匹配, ~=插值估计, ?=未匹配；- 为无配音行，已过滤，不得引用。
{lines_block}

【镜头表】E/D/C/B/A 为画面优先级，X 为不可用操作画面。
{shots_block}

严格输出以下 JSON，不要输出其他文字：
{_LOW_OUTPUT_SCHEMA_HINT}"""


def _narrate_notes(output_dir: Path) -> str:
    """读取任务配置中的 narration_notes，注入解说稿 prompt。"""
    cfg_path = output_dir / "task.json"
    if not cfg_path.exists():
        return ""
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    raw = cfg.get("narration_notes") or ""
    if isinstance(raw, list):
        raw = "\n".join(str(item) for item in raw if str(item).strip())
    return str(raw).strip()


def _build_high_direct_prompt(timeline: dict, target_minutes: float, notes: str = "") -> str:
    lines_block, shots_block = _timeline_blocks(timeline)
    target_chars = int(target_minutes * CHARS_PER_MINUTE)
    return f"""你是剧情向短视频解说撰稿人。请把素材索引写成约 {target_minutes} 分钟（约 {target_chars} 字）的沉浸式故事复盘口播稿。

讲述人风格：
{_HIGH_NARRATION_STYLE}

结构要求：
1. 输出 15~30 个叙事单元，覆盖起因、发展、转折和结局，不得遗漏后半段。
2. 每句必须给出 related_line_ids，只能引用素材中真实存在且非 unvoiced 的台词行 ID。
3. 不输出时间秒数；时间区间由程序补写。

风格示例只学习表达方式，不复用人物、情节和措辞：
{_NARRATIVE_EXAMPLE}

补充说明（用于理解剧情，不得编造证据外的台词或情节）：
{notes}

素材索引：
【台词表】
{lines_block}

【镜头表】
{shots_block}

严格输出 JSON：
{{"narration":[{{"id":1,"text":"解说句","related_line_ids":[1,2]}}]}}"""


def _high_evidence_beat(beat: dict) -> dict:
    """HIGH 只选节拍；台词行引用由程序从 LOW 分片缓存回填。"""
    evidence = {
        key: value for key, value in beat.items()
        if key != "related_line_ids"
    }
    if "key_quotes" in evidence:
        evidence["key_quotes"] = (evidence.get("key_quotes") or [])[:1]
    return evidence


def _build_high_fuse_prompt(segments: list[dict], target_minutes: float, notes: str = "") -> str:
    target_chars = int(target_minutes * CHARS_PER_MINUTE)
    blocks = []
    for segment in segments:
        beats = []
        for beat in segment.get("beats", []):
            beats.append(json.dumps(
                _high_evidence_beat(beat), ensure_ascii=False, sort_keys=True
            ))
        blocks.append(f"【segment_id={segment['segment_id']}】\n" + "\n".join(beats))
    evidence = "\n\n".join(blocks)
    return f"""你是短视频解说口播稿总编。下面是同一故事按时间顺序整理的剧情节拍证据，请融合成约 {target_minutes} 分钟（约 {target_chars} 字）的完整口播稿。

讲述人风格：
{_HIGH_NARRATION_STYLE}

写作要求：
1. 只能使用证据中的事实，不得补充证据外剧情。
2. 合并重复节拍，统一口吻、术语和玩家视角，按时间顺序组织故事。
3. 输出 15~30 个叙事单元，覆盖所有视频的核心情节和结局。
4. 每句必须给出 related_beat_ids，只能引用证据中实际存在的 segment_id 与 beat id；引用不能为空。
5. 不输出时间秒数。

风格示例只学习表达方式：
{_NARRATIVE_EXAMPLE}

补充说明（用于理解剧情，不得编造证据外的台词或情节）：
{notes}

剧情节拍证据：
{evidence}

严格输出 JSON：
{{"narration":[{{"id":1,"text":"解说句","related_beat_ids":[{{"segment_id":"video::chunk_001","beat_id":1}}]}}]}}"""


def _split_per_video(timeline: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for line in timeline.get("lines", []):
        video_id = line.get("video_id") or timeline.get("video_id") or ""
        result.setdefault(video_id, {"video_id": video_id, "lines": [], "shots": []})["lines"].append(line)
    for shot in timeline.get("shots", []):
        video_id = shot.get("video_id") or timeline.get("video_id") or ""
        if video_id in result:
            result[video_id]["shots"].append(shot)
    return result


def _scene_paragraphs(lines: list[dict]) -> list[list[dict]]:
    timed = [line for line in lines if line.get("start") is not None]
    untimed = [line for line in lines if line.get("start") is None]
    paragraphs: list[list[dict]] = []
    current: list[dict] = []
    for line in sorted(timed, key=lambda x: (x["start"], x["end"], x["id"])):
        if current and line["start"] - current[-1].get("end", current[-1]["start"]) > 5.0:
            paragraphs.append(current)
            current = []
        current.append(line)
    if current:
        paragraphs.append(current)
    if untimed:
        paragraphs.append(sorted(untimed, key=lambda x: x["id"]))
    return paragraphs or [[]]


def _segment_for_lines(video_id: str, chunk_id: str, lines: list[dict], shots: list[dict]) -> SegmentPlan:
    starts = [line["start"] for line in lines if line.get("start") is not None]
    ends = [line["end"] for line in lines if line.get("start") is not None]
    if starts:
        start, end = min(starts), max(ends)
        selected = [
            shot for shot in shots
            if shot.get("start") is not None and shot["end"] > start - 0.1 and shot["start"] < end + 0.1
        ]
    else:
        selected = shots
    timeline = {"video_id": video_id, "lines": lines, "shots": selected}
    segment_id = f"{video_id}::{chunk_id}"
    prompt = _build_low_prompt(timeline)
    return SegmentPlan(
        video_id=video_id,
        chunk_id=chunk_id,
        segment_id=segment_id,
        timeline=timeline,
        prompt=prompt,
    )


def _split_video_by_context(video_id: str, timeline: dict, available_tokens: int) -> list[SegmentPlan]:
    lines = _usable_lines(timeline)
    shots = timeline.get("shots", [])
    paragraphs = _scene_paragraphs(lines)
    groups: list[list[dict]] = []
    for paragraph in paragraphs:
        if groups:
            candidate = groups[-1] + paragraph
            probe = _segment_for_lines(video_id, "probe", candidate, shots)
            if estimate_tokens(_build_low_prompt(probe.timeline)) <= available_tokens:
                groups[-1] = candidate
                continue
        groups.append(list(paragraph))

    overflow_groups: list[list[dict]] = []

    def append_fitting(group: list[dict]) -> None:
        if not group:
            return
        probe = _segment_for_lines(video_id, "probe", group, shots)
        if estimate_tokens(probe.prompt) <= available_tokens or len(group) == 1:
            overflow_groups.append(group)
            return
        middle = math.ceil(len(group) / 2)
        append_fitting(group[:middle])
        append_fitting(group[middle:])

    for group in groups:
        append_fitting(group)

    return [
        _segment_for_lines(video_id, f"chunk_{index:03d}", group, shots)
        for index, group in enumerate(overflow_groups, start=1)
    ]


def _context_available(endpoint: LLMEndpoint) -> int:
    return endpoint.profile.available_input_tokens()


def _build_segments(
    timeline: dict,
    low_endpoint: LLMEndpoint,
) -> list[SegmentPlan]:
    segments: list[SegmentPlan] = []
    for video_id, video_timeline in _split_per_video(timeline).items():
        segments.extend(
            _split_video_by_context(video_id, video_timeline, _context_available(low_endpoint))
        )
    return segments


def _extract_json(text: str) -> dict:
    source = text.strip()
    if "```" in source:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", source, re.DOTALL)
        if match:
            source = match.group(1)
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        value = json.loads(_repair_json(source))
    if not isinstance(value, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")
    return value


def _repair_json(source: str) -> str:
    """修复 LLM 常见 JSON 语法错误：漏引号的属性名、单引号、尾逗号。"""
    text = source
    # 属性名缺少左引号：, key" → , "key"
    text = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*("?\s*:)', r'\1"\2\3', text)
    # 属性名完全无引号：, key: → , "key":
    text = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', text)
    # 单引号替换为双引号
    text = text.replace("'", '"')
    # 尾逗号：,} 或 ,] → } 或 ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _failure_path(output_dir: Path, route: str, label: str) -> Path:
    from datetime import datetime
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = output_dir / "llm_failures"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{timestamp}_{route}_{safe_label}.txt"


def _save_raw_failure(output_dir: Path, route: str, label: str, raw: str) -> Path:
    path = _failure_path(output_dir, route, label)
    path.write_text(raw, encoding="utf-8")
    return path


def _chat_json(endpoint: LLMEndpoint, prompt: str, *, max_tokens: int,
               temperature: float | None, label: str,
               output_dir: Path) -> tuple[dict, str]:
    result = endpoint_profile_chat(endpoint, prompt, max_tokens, temperature, label)
    raw = result.content
    if not raw.strip():
        raise RuntimeError(
            f"{endpoint.route}/{endpoint.model} {label} 返回空内容；调用日志: {result.log_path}"
        )
    try:
        value = _extract_json(raw)
    except Exception as exc:
        path = _failure_path(output_dir, endpoint.route, label)
        path.write_text(raw, encoding="utf-8")
        raise RuntimeError(
            f"{endpoint.route} {label} JSON 解析失败: {exc}；"
            f"失败响应: {path}；调用日志: {result.log_path}"
        ) from exc
    return value, raw


def endpoint_profile_chat(endpoint: LLMEndpoint, prompt: str, max_tokens: int,
                           temperature: float | None, label: str):
    from .llm import chat
    return chat(
        endpoint,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
        label=label,
    )


def _timeline_line_ids(timeline: dict) -> set[int]:
    return {
        line["id"] for line in timeline.get("lines", [])
        if line.get("align") != "unvoiced" and line.get("id") is not None
    }


def _validate_low_segment(data: dict, plan: SegmentPlan) -> dict:
    required = {
        "video_id": plan.video_id,
        "chunk_id": plan.chunk_id,
        "segment_id": plan.segment_id,
    }
    for key, value in required.items():
        if data.get(key) != value:
            raise ValueError(f"LOW 分片标识不匹配: {key}={data.get(key)!r}, 期望 {value!r}")
    beats = data.get("beats")
    if not isinstance(beats, list) or not beats:
        raise ValueError("LOW 输出 beats 为空")
    valid_ids = _timeline_line_ids(plan.timeline)
    beat_ids: set[int] = set()
    for beat in beats:
        beat_id = beat.get("id")
        if beat_id in beat_ids:
            raise ValueError(f"LOW beat id 重复: {beat_id}")
        beat_ids.add(beat_id)
        refs = beat.get("related_line_ids")
        if not isinstance(refs, list) or not refs:
            raise ValueError(f"beat {beat_id} 的 related_line_ids 为空")
        unknown = set(refs) - valid_ids
        if unknown:
            raise ValueError(f"beat {beat_id} 引用未知或无配音台词行: {sorted(unknown)}")
        for quote in beat.get("key_quotes", []):
            if quote.get("line_id") not in valid_ids:
                raise ValueError(f"beat {beat_id} key_quotes 引用未知台词行: {quote.get('line_id')}")
        if not str(beat.get("summary", "")).strip():
            raise ValueError(f"beat {beat_id} summary 为空")
        if beat.get("importance") not in {"core", "supporting", "background"}:
            raise ValueError(f"beat {beat_id} importance 非法: {beat.get('importance')!r}")
        if beat.get("confidence") not in {"high", "medium", "low"}:
            raise ValueError(f"beat {beat_id} confidence 非法: {beat.get('confidence')!r}")
    return data


def _validate_high_output(data: dict, *, valid_beats: bool, direct_ids: set[int],
                          beat_index: dict[tuple[str, int], dict] | None) -> list[dict]:
    narration = data.get("narration")
    if not isinstance(narration, list) or not narration:
        raise ValueError("HIGH 输出 narration 为空")
    ids: set[int] = set()
    broad_refs: list[int] = []
    for sentence in narration:
        sentence_id = sentence.get("id")
        if sentence_id in ids:
            raise ValueError(f"HIGH 解说句 id 重复: {sentence_id}")
        ids.add(sentence_id)
        if not str(sentence.get("text", "")).strip():
            raise ValueError(f"解说句 {sentence_id} text 为空")
        if valid_beats:
            refs = sentence.get("related_beat_ids")
            if not isinstance(refs, list) or not refs:
                raise ValueError(f"解说句 {sentence_id} related_beat_ids 为空")
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError(f"解说句 {sentence_id} beat 引用必须是对象")
                key = (ref.get("segment_id"), ref.get("beat_id"))
                if key not in (beat_index or {}):
                    raise ValueError(f"解说句 {sentence_id} 引用未知 beat: {key}")
            if len(refs) > 8:
                broad_refs.append(sentence_id)
        else:
            refs = sentence.get("related_line_ids")
            if not isinstance(refs, list) or not refs:
                raise ValueError(f"解说句 {sentence_id} related_line_ids 为空")
            unknown = set(refs) - direct_ids
            if unknown:
                raise ValueError(f"解说句 {sentence_id} 引用未知或无配音台词行: {sorted(unknown)}")
    if broad_refs:
        print(f"⚠ 以下解说句引用超过 8 个剧情节拍，请闸口1人工复核: {broad_refs}")
    return narration


def _cache_metadata(plan: SegmentPlan, endpoint: LLMEndpoint) -> dict:
    return {
        "_route": "narrate_low",
        "_schema": SCHEMA,
        "_profile": endpoint.profile_id,
        "_model": endpoint.model,
        "_prompt_fp": LOW_PROMPT_FP,
        "_timeline_fp": plan.timeline_fingerprint(),
    }


def _read_valid_cache(plan: SegmentPlan, endpoint: LLMEndpoint) -> dict | None:
    if not plan.cache_path or not plan.cache_path.exists():
        return None
    try:
        data = json.loads(plan.cache_path.read_text(encoding="utf-8"))
        _validate_low_segment(data, plan)
    except Exception:
        return None
    expected = _cache_metadata(plan, endpoint)
    if any(data.get(key) != value for key, value in expected.items()):
        return None
    return data


def _drop_invalid_cache(plan: SegmentPlan, endpoint: LLMEndpoint) -> None:
    if plan.cache_path and plan.cache_path.exists():
        if _read_valid_cache(plan, endpoint) is None:
            plan.cache_path.unlink()


def _run_low(plan: SegmentPlan, endpoint: LLMEndpoint, output_dir: Path) -> dict:
    label = plan.segment_id
    data, raw = _chat_json(
        endpoint,
        plan.prompt,
        max_tokens=endpoint.profile.max_tokens,
        temperature=endpoint.profile.temperature,
        label=label,
        output_dir=output_dir,
    )
    data = {
        **data,
        "video_id": plan.video_id,
        "chunk_id": plan.chunk_id,
        "segment_id": plan.segment_id,
    }
    try:
        data = _validate_low_segment(data, plan)
    except Exception as exc:
        path = _failure_path(output_dir, endpoint.route, label)
        path.write_text(raw, encoding="utf-8")
        raise RuntimeError(
            f"LOW 输出防伪校验失败 segment={plan.segment_id}: {exc}；"
            f"失败响应: {path}"
        ) from exc
    data.update(_cache_metadata(plan, endpoint))
    if plan.cache_path:
        plan.cache_path.parent.mkdir(parents=True, exist_ok=True)
        plan.cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    plan.cache_hit = False
    return data


def _remap_beat_refs(narration: list[dict], segments: list[dict]) -> list[dict]:
    beat_index: dict[tuple[str, int], list] = {}
    for segment in segments:
        segment_id = segment["segment_id"]
        for beat in segment.get("beats", []):
            beat_index[(segment_id, beat["id"])] = beat.get("related_line_ids", [])

    output: list[dict] = []
    seen_sentence_ids: set[int] = set()
    for sentence in narration:
        sentence_id = sentence["id"]
        if sentence_id in seen_sentence_ids:
            raise ValueError(f"HIGH 解说句 id 重复: {sentence_id}")
        seen_sentence_ids.add(sentence_id)
        refs: list[dict] = []
        seen_refs: set[tuple[str, int]] = set()
        for ref in sentence.get("related_beat_ids", []):
            key = (ref.get("segment_id"), ref.get("beat_id"))
            if key not in beat_index:
                raise ValueError(f"HIGH 引用未知 beat: {key}")
            if key in seen_refs:
                continue
            seen_refs.add(key)
            for line_id in beat_index[key]:
                refs.append({"video_id": key[0].split("::", 1)[0], "line_id": line_id})
        if not refs:
            raise ValueError(f"解说句 {sentence_id} 映射后 related_line_ids 为空")
        deduped: list[dict] = []
        seen_lines: set[tuple[str, int]] = set()
        for ref in refs:
            key = (ref["video_id"], ref["line_id"])
            if key not in seen_lines:
                seen_lines.add(key)
                deduped.append(ref)
        output.append({
            "id": sentence_id,
            "text": sentence["text"],
            "related_line_ids": deduped,
        })
    return output


def _normalize_direct_refs(narration: list[dict], timeline: dict) -> list[dict]:
    default_video = (
        (timeline.get("videos") or [{}])[0].get("video_id")
        or timeline.get("video_id")
        or (timeline.get("lines") or [{}])[0].get("video_id")
        or ""
    )
    return [{
        "id": sentence["id"],
        "text": sentence["text"],
        "related_line_ids": [
            {"video_id": ref if isinstance(ref, dict) else default_video,
             "line_id": ref.get("line_id") if isinstance(ref, dict) else ref}
            for ref in sentence.get("related_line_ids", [])
        ],
    } for sentence in narration]


def _to_markdown(narration: list[dict], timeline: dict) -> str:
    lines_by_key = {
        (line.get("video_id"), line["id"]): line
        for line in timeline.get("lines", [])
    }
    md = [
        "# 解说稿（闸口1 审阅）\n",
        f"> 来源：{timeline.get('_source', 'timeline.json')}\n",
    ]
    for sentence in narration:
        md.append(f"\n## 句{sentence['id']}\n")
        md.append(f"{sentence['text']}\n")
        times = []
        quoted = []
        for ref in sentence.get("related_line_ids", []):
            line = lines_by_key.get((ref.get("video_id"), ref.get("line_id")))
            label = f"{ref.get('video_id')}:{ref.get('line_id')}"
            if line and line.get("start") is not None:
                times.append((line["start"], line["end"]))
                quoted.append(f"- [{label}] {line.get('speaker') or '？'}：{line.get('text')}")
        if times:
            md.append(f"**时间区间**：{min(t[0] for t in times):.1f}s - {max(t[1] for t in times):.1f}s\n")
        md.append("**引用台词**：\n")
        md.extend(quoted)
        md.append("")
    return "\n".join(md)


def _evidence_fingerprint(segments: list[dict]) -> str:
    raw = json.dumps(
        [
            {
                "segment_id": segment["segment_id"],
                "schema": segment.get("_schema"),
                "profile": segment.get("_profile"),
                "model": segment.get("_model"),
                "prompt_fp": segment.get("_prompt_fp"),
                "timeline_fp": segment.get("_timeline_fp"),
            }
            for segment in segments
        ],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _planned_evidence_fingerprint(segments: list[SegmentPlan], endpoint: LLMEndpoint) -> str:
    return _evidence_fingerprint([
        {
            "segment_id": plan.segment_id,
            "_schema": SCHEMA,
            "_profile": endpoint.profile_id,
            "_model": endpoint.model,
            "_prompt_fp": LOW_PROMPT_FP,
            "_timeline_fp": plan.timeline_fingerprint(),
        }
        for plan in segments
    ])


def _timeline_fingerprint(timeline: dict) -> str:
    raw = json.dumps(
        {"lines": timeline.get("lines", []), "shots": timeline.get("shots", [])},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _estimate_segments(segments: list[SegmentPlan], endpoint: LLMEndpoint) -> list[dict]:
    """用已有缓存或逐行合成 beat 做保守 HIGH 输入估算。"""
    estimated: list[dict] = []
    for plan in segments:
        cached = _read_valid_cache(plan, endpoint)
        if cached is not None:
            estimated.append(cached)
            continue
        beats = []
        for index, line in enumerate(_usable_lines(plan.timeline), start=1):
            beats.append({
                "id": index,
                "summary": line.get("text", ""),
                "characters": [line.get("speaker") or "未知"],
                "cause": "",
                "effect": "",
                "key_quotes": [],
                "related_line_ids": [line.get("id")],
                "importance": "core",
                "confidence": "high",
            })
        estimated.append({
            "segment_id": plan.segment_id,
            "beats": beats,
        })
    return estimated


def _high_reuse(
    output_dir: Path,
    timeline: dict,
    target_minutes: float,
    high_endpoint: LLMEndpoint,
    segments: list[SegmentPlan] | None,
    low_endpoint: LLMEndpoint | None,
    force: bool,
    notes: str = "",
) -> tuple[bool, str, dict | None]:
    path = output_dir / "narration.json"
    if force:
        return False, "显式 --force", None
    if not path.exists():
        return False, "缺少 narration.json", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"旧终稿无法解析: {exc}", None
    if data.get("_human_edited") is True:
        return False, "人工编辑稿受保护，需 --force", None
    models = data.get("models") or {}
    high = models.get("narrate_high") or {}
    if high.get("profile") != high_endpoint.profile_id or high.get("model") != high_endpoint.model:
        return False, "HIGH profile/model 不匹配", None
    fingerprints = data.get("prompt_fingerprints") or {}
    if fingerprints.get("narrate_high") != HIGH_PROMPT_FP:
        return False, "HIGH prompt 不匹配", None
    if data.get("timeline_fingerprint") != _timeline_fingerprint(timeline):
        return False, "timeline 不匹配", None
    if segments is not None:
        if low_endpoint is None:
            return False, "LOW endpoint 缺失", None
        if data.get("evidence_fingerprint") != _planned_evidence_fingerprint(segments, low_endpoint):
            return False, "LOW 证据配置不匹配", None
    if data.get("target_minutes") != target_minutes:
        return False, "target_minutes 不匹配", None
    if data.get("narration_notes") != notes:
        return False, "narration_notes 不匹配", None
    narration = data.get("narration")
    if not isinstance(narration, list) or not narration:
        return False, "终稿 narration 为空", None
    line_keys = {
        (line.get("video_id"), line.get("id"))
        for line in timeline.get("lines", [])
        if line.get("align") != "unvoiced"
    }
    for sentence in narration:
        refs = sentence.get("related_line_ids")
        if not isinstance(refs, list) or not refs:
            return False, "终稿存在空引用", None
        for ref in refs:
            if not isinstance(ref, dict) or (ref.get("video_id"), ref.get("line_id")) not in line_keys:
                return False, "终稿存在未知或无效引用", None
    return True, "元数据全部匹配", data


def _human_edited_final(output_dir: Path) -> bool:
    path = output_dir / "narration.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("_human_edited") is True


def build_plan(timeline_path: Path, output_dir: Path, *,
               target_minutes: float = 15.0, mode: str = "auto",
               force: bool = False) -> NarratePlan:
    if mode not in {"auto", "segment", "oneshot"}:
        raise ValueError(f"mode 必须是 auto/segment/oneshot: {mode}")
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    timeline["_source"] = str(timeline_path)
    videos = timeline.get("videos", [])
    force_segment = mode == "segment"
    is_multi = len(videos) > 1
    if mode == "oneshot" and is_multi:
        raise ValueError("多视频任务不能使用 oneshot 模式")

    high_endpoint = load_endpoint("narrate_high")
    notes = _narrate_notes(output_dir)
    direct_prompt = None
    needs_split = force_segment
    if not force_segment:
        direct_prompt = _build_high_direct_prompt(timeline, target_minutes, notes)
        direct_tokens = estimate_tokens(direct_prompt)
        needs_split = direct_tokens > _context_available(high_endpoint)

    segments: list[SegmentPlan] = []
    low_endpoint = None
    if needs_split or is_multi:
        low_endpoint = load_endpoint("narrate_low")
        segments = _build_segments(timeline, low_endpoint)
        seg_dir = output_dir / "narration_segments"
        for segment in segments:
            segment.cache_path = seg_dir / f"{segment.segment_id}.json"
            segment.cache_hit = _read_valid_cache(segment, low_endpoint) is not None

    if needs_split or is_multi or segments:
        fuse_source = _estimate_segments(segments, low_endpoint)
        high_prompt = _build_high_fuse_prompt(fuse_source, target_minutes, notes)
        estimated = estimate_tokens(high_prompt)
        if estimated > _context_available(high_endpoint):
            raise RuntimeError(
                f"HIGH 融合 prompt 估算 {estimated} tokens，超过可用输入 "
                f"{_context_available(high_endpoint)} tokens"
            )
        reusable, reason, _ = _high_reuse(
            output_dir, timeline, target_minutes, high_endpoint,
            segments, low_endpoint, force, notes,
        )
        return NarratePlan(
            mode="segment",
            segments=segments,
            high_prompt=high_prompt,
            high_cache_reusable=reusable,
            high_cache_reason=reason,
            high_endpoint=high_endpoint,
            low_endpoint=low_endpoint,
            low_requests=sum(0 if segment.cache_hit else 1 for segment in segments),
            high_requests=0 if reusable else 1,
            estimated_high_prompt_tokens=estimated,
            needs_beat_remap=True,
            force=force,
        )

    assert direct_prompt is not None
    estimated = estimate_tokens(direct_prompt)
    if estimated > _context_available(high_endpoint):
        raise RuntimeError("单视频 prompt 超出 HIGH 上下文，但未进入分片分支")
    reusable, reason, _ = _high_reuse(
        output_dir, timeline, target_minutes, high_endpoint, None, None, force, notes
    )
    return NarratePlan(
        mode="oneshot",
        segments=[],
        high_prompt=direct_prompt,
        high_cache_reusable=reusable,
        high_cache_reason=reason,
        high_endpoint=high_endpoint,
        low_endpoint=None,
        low_requests=0,
        high_requests=0 if reusable else 1,
        estimated_high_prompt_tokens=estimated,
        needs_beat_remap=False,
        force=force,
    )


def plan_summary(plan: NarratePlan) -> dict:
    return {
        "mode": plan.mode,
        "low_segments": len(plan.segments),
        "low_cache_hits": sum(1 for segment in plan.segments if segment.cache_hit),
        "low_requests": plan.low_requests,
        "high_requests": plan.high_requests,
        "high_cache_reusable": plan.high_cache_reusable,
        "high_cache_reason": plan.high_cache_reason,
        "high_profile": plan.high_endpoint.profile_id,
        "high_model": plan.high_endpoint.model,
        "high_max_attempts": plan.high_endpoint.profile.max_retries + 1,
        "high_prompt_tokens_estimated": plan.estimated_high_prompt_tokens,
        "high_input_context_tokens": plan.high_endpoint.profile.input_context_tokens,
        "high_safety_margin_tokens": plan.high_endpoint.profile.safety_margin_tokens,
        "low_profile": plan.low_endpoint.profile_id if plan.low_endpoint else None,
        "low_model": plan.low_endpoint.model if plan.low_endpoint else None,
        "low_max_attempts": (
            plan.low_endpoint.profile.max_retries + 1 if plan.low_endpoint else None
        ),
        "force": plan.force,
        "cost_incurred": False,
    }


def _clean_stale_segments(output_dir: Path, planned_names: set[str]) -> None:
    directory = output_dir / "narration_segments"
    if not directory.exists():
        return
    for path in directory.glob("*.json"):
        if path.name not in planned_names:
            path.unlink()


def run(timeline_path: Path, output_dir: Path, *, target_minutes: float = 15.0,
        mode: str = "auto", force: bool = False) -> dict:
    """生成解说稿；所有旧 schema 终稿与旧分片一律不复用。"""
    plan = build_plan(
        timeline_path,
        output_dir,
        target_minutes=target_minutes,
        mode=mode,
        force=force,
    )
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    timeline["_source"] = str(timeline_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    notes = _narrate_notes(output_dir)

    if not force and _human_edited_final(output_dir):
        raise RuntimeError("narration.json 标记为人工编辑稿，覆盖前必须显式使用 --force")

    if not plan.high_cache_reusable:
        for filename in ("narration.json", "narration.md"):
            path = output_dir / filename
            if path.exists():
                path.unlink()

    if plan.mode == "oneshot":
        reusable, _, existing = _high_reuse(
            output_dir, timeline, target_minutes, plan.high_endpoint,
            None, None, force, notes,
        )
        if reusable and existing:
            narration = existing["narration"]
        else:
            data, raw = _chat_json(
                plan.high_endpoint,
                plan.high_prompt or "",
                max_tokens=plan.high_endpoint.profile.max_tokens,
                temperature=plan.high_endpoint.profile.temperature,
                label=f"oneshot:{output_dir.name}",
                output_dir=output_dir,
            )
            try:
                narration = _validate_high_output(
                    data,
                    valid_beats=False,
                    direct_ids=_timeline_line_ids(timeline),
                    beat_index=None,
                )
            except Exception as exc:
                path = _save_raw_failure(
                    output_dir, "narrate_high", f"oneshot:{output_dir.name}", raw
                )
                raise RuntimeError(
                    f"HIGH oneshot 输出防伪校验失败: {exc}；失败响应: {path}"
                ) from exc
            narration = _normalize_direct_refs(narration, timeline)
        models = {"narrate_low": None, "narrate_high": {
            "profile": plan.high_endpoint.profile_id,
            "model": plan.high_endpoint.model,
        }}
        prompt_fingerprints = {"narrate_low": None, "narrate_high": HIGH_PROMPT_FP}
        evidence_fp = ""
        used_mode = "oneshot"
        segments: list[dict] = []
    else:
        assert plan.low_endpoint is not None
        planned_names = {
            segment.cache_path.name for segment in plan.segments if segment.cache_path
        }
        _clean_stale_segments(output_dir, planned_names)
        for segment in plan.segments:
            _drop_invalid_cache(segment, plan.low_endpoint)

        segment_results: dict[str, dict] = {}
        workers = plan.low_endpoint.profile.narration_segment_workers

        def load_or_run(segment: SegmentPlan) -> dict:
            cached = _read_valid_cache(segment, plan.low_endpoint)  # type: ignore[arg-type]
            if cached is not None:
                segment.cache_hit = True
                return cached
            return _run_low(segment, plan.low_endpoint, output_dir)  # type: ignore[arg-type]

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(load_or_run, segment): segment.segment_id
                    for segment in plan.segments
                }
                for future in as_completed(futures):
                    segment_id = futures[future]
                    segment_results[segment_id] = future.result()
        else:
            for segment in plan.segments:
                segment_results[segment.segment_id] = load_or_run(segment)

        ordered_segments = [
            segment_results[segment.segment_id] for segment in plan.segments
        ]
        beat_index = {
            (segment["segment_id"], beat["id"]): beat
            for segment in ordered_segments for beat in segment.get("beats", [])
        }
        fuse_prompt = _build_high_fuse_prompt(ordered_segments, target_minutes, notes)
        reusable, _, existing = _high_reuse(
            output_dir,
            timeline,
            target_minutes,
            plan.high_endpoint,
            plan.segments,
            plan.low_endpoint,
            force,
            notes,
        )
        if reusable and existing:
            narration = existing["narration"]
        else:
            data, raw = _chat_json(
                plan.high_endpoint,
                fuse_prompt,
                max_tokens=plan.high_endpoint.profile.max_tokens,
                temperature=plan.high_endpoint.profile.temperature,
                label=f"fuse:{output_dir.name}",
                output_dir=output_dir,
            )
            try:
                high_output = _validate_high_output(
                    data,
                    valid_beats=True,
                    direct_ids=set(),
                    beat_index=beat_index,
                )
            except Exception as exc:
                path = _save_raw_failure(
                    output_dir, "narrate_high", f"fuse:{output_dir.name}", raw
                )
                raise RuntimeError(
                    f"HIGH 融合输出防伪校验失败: {exc}；失败响应: {path}"
                ) from exc
            narration = _remap_beat_refs(high_output, ordered_segments)
        models = {
            "narrate_low": {
                "profile": plan.low_endpoint.profile_id,
                "model": plan.low_endpoint.model,
            },
            "narrate_high": {
                "profile": plan.high_endpoint.profile_id,
                "model": plan.high_endpoint.model,
            },
        }
        prompt_fingerprints = {
            "narrate_low": LOW_PROMPT_FP,
            "narrate_high": HIGH_PROMPT_FP,
        }
        evidence_fp = _evidence_fingerprint(ordered_segments)
        used_mode = "segment"
        segments = ordered_segments

    result = {
        "mode": used_mode,
        "target_minutes": target_minutes,
        "narration_notes": notes,
        "routes": ["narrate_high"] if used_mode == "oneshot" else ["narrate_low", "narrate_high"],
        "prompt_fingerprints": prompt_fingerprints,
        "timeline_fingerprint": _timeline_fingerprint(timeline),
        "evidence_fingerprint": evidence_fp,
        "_human_edited": False,
        "models": models,
        "narration": narration,
    }
    if segments:
        result["segment_order"] = [segment["segment_id"] for segment in segments]
    (output_dir / "narration.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "narration.md").write_text(
        _to_markdown(narration, timeline), encoding="utf-8"
    )
    return {
        "sentences": len(narration),
        "mode": used_mode,
        "output_dir": str(output_dir),
        "low_segments": len(segments),
        "high_reused": plan.high_cache_reusable,
    }
