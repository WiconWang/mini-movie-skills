"""TTS 计划、审批、供应商选择与片段产物生命周期。"""

from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..llm import chat, load_endpoint, LLMCallError, LLMEndpoint
from ..media import ffmpeg_bin, probe_duration
from .align import align_segments, clamp_pause_ms
from .glossary import load_series_pronunciations
from .providers import estimate_plan_cost, get_provider
from .splitter import split_master_audio
from .types import (
    PronunciationRule, TtsArtifact, TtsPerformance, TtsProfile, TtsSegment,
    dataclasses_to_dict, sha256_object,
)


PLAN_PATH_NAME = "tts_plan.json"
APPROVAL_PATH_NAME = "tts_plan.approved.json"
HTML_PATH_NAME = "tts_plan.html"
ARTIFACTS_PATH_NAME = "render_segments/tts_artifacts.json"
SCHEMA_VERSION = "tts_plan_v1"
ALLOWED_TONES = {
    "breath", "sighs", "chuckle", "clear-throat", "gasps", "emm",
}
TONE_LABELS_ZH = {
    "breath": "换气",
    "sighs": "叹气",
    "chuckle": "轻笑",
    "clear-throat": "清嗓子",
    "gasps": "倒吸气",
    "emm": "嗯",
}
ALLOWED_EMOTIONS = {
    "calm", "surprised", "sad", "happy", "angry", "fearful", "fluent",
}
EMOTION_LABELS_ZH = {
    "calm": "平稳",
    "surprised": "惊讶",
    "sad": "惋惜",
    "happy": "开心",
    "angry": "生气",
    "fearful": "紧张",
    "fluent": "流畅",
}


class TtsArtifactsMissing(RuntimeError):
    """已批准计划存在，但切分后的 TTS 片段缺失或指纹不一致。"""


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dataclasses_to_dict(value) if not isinstance(value, dict) else value,
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def narration_clips(edl: dict) -> list[dict]:
    """按最终 EDL 顺序提取解说片段；raw_insert 不进入 TTS。"""
    return [
        {**clip, "_edl_index": index}
        for index, clip in enumerate(edl.get("clips", []))
        if clip.get("type") == "narration_clip" and not clip.get("keep_audio")
    ]


SENTENCE_TERMINATORS = "。！？；…"
_OPENING_CHARS = "“‘「『（[{<"
_CLOSING_CHARS = "”’」』）]}>"


def split_sentences(text: str) -> list[str]:
    """按句末标点拆句，保留标点和闭合引号，供 TTS 句级表演标注。"""
    sentences: list[str] = []
    current: list[str] = []
    quote_stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        current.append(ch)
        if ch in _OPENING_CHARS:
            quote_stack.append(ch)
        elif ch in _CLOSING_CHARS and quote_stack:
            quote_stack.pop()
        if ch in SENTENCE_TERMINATORS and not quote_stack:
            j = i + 1
            while j < len(text) and text[j] in "…":
                j += 1
            if ch in "…" and j < len(text) and text[j] in _OPENING_CHARS:
                i += 1
                continue
            i += 1
            while i < len(text) and text[i] in "…":
                current.append(text[i])
                i += 1
            while i < len(text) and text[i] in _CLOSING_CHARS:
                current.append(text[i])
                i += 1
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
        else:
            i += 1
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _sentence_units(clips: list[dict]) -> list[dict]:
    """把一个 EDL 解说片段拆成句级标注单元，保留 narration_id 与 clip_index。"""
    units: list[dict] = []
    for clip in clips:
        for sentence in split_sentences(clip["text"]):
            units.append({
                "index": len(units),
                "narration_id": clip["narration_id"],
                "source_text": sentence,
                "clip_index": clip.get("index", 0),
            })
    return units


def resolve_profile(tts_cfg: dict, profile: str | None = None) -> TtsProfile:
    """把新旧 task/series 配置解析成统一执行 profile。"""
    cfg = tts_cfg or {}
    mode = str(profile or cfg.get("profile") or "").strip().lower()
    if not mode:
        legacy_engine = str(cfg.get("engine", "edge")).lower()
        mode = "prod" if legacy_engine == "minimax" else "dry"
    if mode not in {"dry", "prod"}:
        raise ValueError(f"TTS profile 只支持 dry/prod，当前: {mode}")

    def _provider_options(section: dict, provider: str) -> dict:
        provider_section = dict(cfg.get(provider) or {})
        options = {**provider_section, **section}
        for key in options:
            if any(token in key.lower() for token in ("api_key", "token", "secret")):
                raise ValueError(f"TTS 配置 {key} 不允许写入 task.json；请使用环境变量")
        return options

    section = dict(cfg.get(mode) or {})
    if mode == "dry":
        provider = str(section.get("provider") or cfg.get("provider") or "edge")
        if provider != "edge":
            raise ValueError("dry profile 固定使用 edge，不允许替换成付费供应商")
        model = str(section.get("model") or cfg.get("model") or "edge-neural")
        voice = str(section.get("voice") or section.get("voice_id")
                    or cfg.get("voice") or "zh-CN-XiaoyiNeural")
        speed = float(section.get("speed", cfg.get("speed", 1.1)))
        options = _provider_options(section, provider)
        return TtsProfile(mode, provider, model, voice, speed, None, options)

    provider = str(section.get("provider") or cfg.get("provider") or cfg.get("engine") or "minimax")
    options = _provider_options(section, provider)
    model = str(section.get("model") or options.get("model")
                or cfg.get("model") or "speech-2.8-hd")
    voice = str(section.get("voice_id") or section.get("voice")
                or options.get("voice_id") or cfg.get("voice_id") or "")
    if not voice:
        raise ValueError("prod TTS 缺少 voice_id")
    speed = float(section.get("speed", options.get("speed", cfg.get("speed", 1.0))))
    emotion = section.get("emotion") or options.get("emotion") or cfg.get("emotion")
    options.pop("provider", None)
    options.pop("model", None)
    options.pop("voice_id", None)
    options.pop("voice", None)
    options.pop("speed", None)
    options.pop("emotion", None)
    return TtsProfile(mode, provider, model, voice, speed, emotion, options)


def _load_plan_endpoint() -> LLMEndpoint:
    """优先使用独立 tts_plan route；未配置时回退现有 narrate_low。"""
    try:
        return load_endpoint("tts_plan")
    except RuntimeError:
        endpoint = load_endpoint("narrate_low")
        return replace(endpoint, route="tts_plan")


def _extract_json(text: str) -> dict:
    source = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", source, re.DOTALL)
    if fenced:
        source = fenced.group(1)
    start = source.find("{")
    end = source.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 未返回 JSON")
    return json.loads(source[start:end + 1])


def _build_llm_prompt(units: list[dict], capabilities: object,
                      maximum_pause_ms: int,
                      glossary: list[PronunciationRule] | None = None) -> str:
    lines = []
    for unit in units:
        lines.append(json.dumps({
            "index": unit["index"],
            "narration_id": unit["narration_id"],
            "source_text": unit.get("source_text") or unit.get("text"),
        }, ensure_ascii=False))

    glossary_section = ""
    if glossary:
        glossary_section = (
            "\n系统兜底发音（仅供 LLM 未识别时参考；你给出的规则优先）：\n"
            + "\n".join(
                json.dumps({"term": rule.term, "pinyin": rule.pinyin}, ensure_ascii=False)
                for rule in glossary
            )
            + "\n"
        )

    return f"""你是中文解说 TTS 表演标注器，不是编辑。

绝对职责边界：
1. source_text 是终稿按句号、问号、感叹号、分号等切分后的单个句子；禁止修改、增删、合并、拆分或润色一个字。
2. 只为容易读错的术语、人名、地名和多音字添加发音标注。
3. 只添加停顿、语气、情绪、语速提示等语音表演意图。
4. 不要输出供应商专属语法（例如 MiniMax 的 <#0.35#> 或 (breath)）。
5. 输出必须是严格 JSON，不输出解释和 Markdown。

表演约束：
- pause_before_ms/pause_after_ms 是整数毫秒，范围 0~{maximum_pause_ms}，多数应为 0。
- tone 只能使用：breath、sighs、chuckle、clear-throat、gasps、emm；不确定则为 null。
- emotion 只能使用：calm、surprised、sad、happy、angry、fearful、fluent；不确定则 calm。
- speed_hint 是 0.8~1.2 的小数；通常保持 1.0，不逐句乱调。
- pronunciations 只用于确实容易读错的术语；示例：{{"term":"安柏","pinyin":"an1 bo2"}}。
- pronunciation.pinyin 使用空格分隔的带声调拼音。

供应商能力：
{json.dumps(capabilities, ensure_ascii=False)}
{glossary_section}
解说句：
{chr(10).join(lines)}

严格输出 JSON：
{{"segments":[{{"index":0,"narration_id":1,"source_text":"原文","performance":{{"pause_before_ms":0,"pause_after_ms":0,"tone":null,"emotion":"calm","speed_hint":1.0}},"pronunciations":[],"warnings":[]}}]}}"""

def _parse_llm_segments(raw: dict, units: list[dict], profile: TtsProfile,
                        maximum_pause_ms: int) -> list[TtsSegment]:
    items = raw.get("segments")
    if not isinstance(items, list):
        raise ValueError("LLM 表演计划缺少 segments 数组")
    if len(items) != len(units):
        raise ValueError(
            f"LLM 表演计划句数不匹配：期望 {len(units)}，实际 {len(items)}"
        )

    parsed: list[TtsSegment] = []
    for index, (unit, item) in enumerate(zip(units, items)):
        if int(item.get("index", -1)) != index:
            raise ValueError(f"LLM 表演计划 index 错误：位置 {index}")
        if int(item.get("narration_id", -1)) != int(unit["narration_id"]):
            raise ValueError(f"LLM 表演计划 narration_id 错误：位置 {index}")
        expected_text = unit.get("source_text") or unit.get("text")
        source_text = item.get("source_text")
        if source_text != expected_text:
            raise ValueError(
                f"LLM 试图修改 narration_id={unit['narration_id']} 的解说句文本"
            )
        performance = item.get("performance") or {}
        tone_value = performance.get("tone")
        tone = str(tone_value).strip().strip("()").lower() if tone_value else None
        if tone and tone not in ALLOWED_TONES:
            tone = None
        emotion_value = str(performance.get("emotion") or "calm").lower()
        emotion = emotion_value if emotion_value in ALLOWED_EMOTIONS else "calm"
        try:
            speed_hint = float(performance.get("speed_hint", 1.0))
        except (TypeError, ValueError):
            speed_hint = 1.0
        speed_hint = max(0.8, min(speed_hint, 1.2))

        rules: list[PronunciationRule] = []
        for rule in item.get("pronunciations") or []:
            term = str(rule.get("term", "")).strip()
            pinyin = str(rule.get("pinyin", "")).strip()
            if term and pinyin and term in source_text:
                rules.append(PronunciationRule(
                    term=term, pinyin=pinyin,
                    note=str(rule.get("note", "")),
                ))

        parsed.append(TtsSegment(
            index=index,
            narration_id=int(unit["narration_id"]),
            source_text=source_text,
            performance=TtsPerformance(
                pause_before_ms=clamp_pause_ms(
                    performance.get("pause_before_ms"), maximum_pause_ms
                ),
                pause_after_ms=clamp_pause_ms(
                    performance.get("pause_after_ms"), maximum_pause_ms
                ),
                tone=tone,
                emotion=emotion,
                speed_hint=speed_hint,
            ),
            pronunciations=rules,
        ))
    return parsed

def _apply_glossary(segments: list[TtsSegment], units: list[dict],
                    glossary: list[PronunciationRule]) -> list[TtsSegment]:
    """词库兜底：LLM 已给出的发音优先，仅补齐 LLM 未识别的词。"""
    if not glossary:
        return segments
    for index, (segment, unit) in enumerate(zip(segments, units)):
        text = unit.get("source_text") or unit.get("text") or ""
        existing = {rule.term: rule for rule in segment.pronunciations}
        additions = [
            rule for rule in glossary
            if rule.term in text and rule.term not in existing
        ]
        if additions:
            segments[index] = replace(
                segment,
                pronunciations=list(existing.values()) + additions,
            )
    return segments


def _plan_document(clips: list[dict], profile: TtsProfile, segments: list[TtsSegment],
                   llm_result=None) -> tuple[dict, str]:
    capabilities = vars(get_provider(profile.provider, profile.options).capabilities)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile.mode,
        "strategy": "full_then_split",
        "execution": {
            "provider": profile.provider,
            "model": profile.model,
            "voice": profile.voice,
            "speed": profile.speed,
            "emotion": profile.emotion,
            "options": profile.options,
            "config_fingerprint": profile.fingerprint(),
            "capabilities": capabilities,
        },
        "cost_estimate": estimate_plan_cost(profile.provider, segments, profile),
        "segments": dataclasses_to_dict(segments),
    }
    plan_sha256 = sha256_object(plan)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "plan_sha256": plan_sha256,
        "plan": plan,
        "llm": {} if llm_result is None else {
            "route": llm_result.route,
            "profile": llm_result.profile_id,
            "model": llm_result.model,
            "response_hash": llm_result.response_hash,
        },
    }
    return document, plan_sha256


def _plan_html(document: dict) -> str:
    plan = document["plan"]
    rows = []
    def tone_label(value: str | None) -> str:
        if not value:
            return "无"
        return TONE_LABELS_ZH.get(value, value)

    def emotion_label(value: str | None) -> str:
        if not value:
            return "无"
        return EMOTION_LABELS_ZH.get(value, value)

    last_narration = None
    sub_index = 0
    for segment in plan["segments"]:
        if segment["narration_id"] != last_narration:
            last_narration = segment["narration_id"]
            sub_index = 1
        else:
            sub_index += 1
        perf = segment["performance"]
        pronunciations = "、".join(
            f"{html.escape(rule['term'])}={html.escape(rule['pinyin'])}"
            for rule in segment.get("pronunciations", [])
        ) or "-"
        tone = html.escape(tone_label(perf.get("tone")))
        pause = f"前{perf.get('pause_before_ms', 0)}ms / 后{perf.get('pause_after_ms', 0)}ms"
        id_cell = f"{segment['narration_id']}.{sub_index}"
        rows.append(
            "<tr>"
            f"<td>{segment['index'] + 1}</td>"
            f"<td>{id_cell}</td>"
            f"<td>{html.escape(segment['source_text'])}</td>"
            f"<td>{html.escape(pronunciations)}</td>"
            f"<td>{html.escape(pause)}</td>"
            f"<td>{tone}</td>"
            f"<td>{html.escape(emotion_label(perf.get('emotion')))}</td>"
            "</tr>"
        )

    execution = plan["execution"]
    cost = plan["cost_estimate"]
    fee = cost.get("amount", 0.0)
    warning = (
        "<p class='warn'>确认后将立即使用 Edge TTS 合成。TTS 本身免费，"
        "但生成表演计划的 LLM 调用可能已产生费用。</p>"
        if plan["profile"] == "dry" else
        "<p class='warn danger'>确认后将立即调用付费 TTS 服务，并按供应商规则产生费用。"
        "失败重试也可能再次计费。</p>"
    )
    return f"""<!doctype html><meta charset="utf-8"><title>TTS 计划确认</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1400px;margin:24px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:8px;vertical-align:top}}th{{background:#f3f4f6}}.warn{{background:#fff7ed;border:1px solid #fdba74;padding:12px}}.danger{{background:#fee2e2;border-color:#f87171}}code{{background:#f5f5f5;padding:2px 4px}}</style>
<h1>闸口3：TTS 计划确认</h1>
<p>profile：{html.escape(plan['profile'])} ｜ provider：{html.escape(execution['provider'])} ｜ model：{html.escape(execution['model'])} ｜ voice：{html.escape(execution['voice'])}</p>
<p>预估计费字符：{cost.get('billing_characters', 0)} ｜ 预估 TTS 费用：{fee:.4f} {html.escape(cost.get('currency', 'CNY'))}</p>
{warning}
<p>plan_sha256：<code>{html.escape(document['plan_sha256'])}</code></p>
<table><thead><tr><th>#</th><th>片段.句</th><th>原文（不可修改）</th><th>发音</th><th>停顿</th><th>语气</th><th>情绪</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>确认命令：<code>mmm tts-approve --task &lt;task_id&gt; --plan-sha256 {html.escape(document['plan_sha256'])}</code></p>"""


def create_tts_plan(work_dir: Path, tts_cfg: dict, *, profile: str | None = None,
                    force: bool = False) -> dict:
    """生成 LLM 表演计划，并停在闸口 3。"""
    edl_path = work_dir / "edl.json"
    if not edl_path.exists():
        raise FileNotFoundError(f"缺少 EDL: {edl_path}")
    clips = [
        {**clip, "index": index}
        for index, clip in enumerate(narration_clips(_read_json(edl_path)))
    ]
    if not clips:
        raise ValueError("EDL 中没有 narration_clip，无需生成 TTS 计划")
    units = _sentence_units(clips)
    if not units:
        raise ValueError("EDL 解说片段没有可拆分的句子，无需生成 TTS 计划")

    task_cfg = {}
    task_json = work_dir / "task.json"
    if task_json.exists():
        task_cfg = _read_json(task_json)
    glossary = load_series_pronunciations(
        task_cfg.get("series", ""), str(task_cfg.get("version") or "")
    )

    resolved = resolve_profile(tts_cfg, profile)
    provider = get_provider(resolved.provider, resolved.options)
    capabilities = vars(provider.capabilities)
    maximum_pause_ms = int(tts_cfg.get("maximum_pause_ms", 2000))
    prompt = _build_llm_prompt(units, capabilities, maximum_pause_ms, glossary)
    endpoint = _load_plan_endpoint()
    max_tokens = min(32768, 2048 + len(units) * 180)
    result = chat(
        endpoint,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        label="tts_plan",
    )
    raw = _extract_json(result.content)
    segments = _parse_llm_segments(raw, units, resolved, maximum_pause_ms)
    segments = _apply_glossary(segments, units, glossary)
    document, plan_sha256 = _plan_document(clips, resolved, segments, result)
    document["warnings"] = []
    if not resolved.provider == "edge":
        unsupported = [
            segment["performance"]["tone"]
            for segment in document["plan"]["segments"]
            if segment["performance"]["tone"] and not capabilities["tone_tags"]
        ]
        if unsupported:
            document["warnings"].append("当前供应商不支持部分语气词，将在执行时降级。")

    plan_path = work_dir / PLAN_PATH_NAME
    _write_json(plan_path, document)
    (work_dir / HTML_PATH_NAME).write_text(_plan_html(document), encoding="utf-8")
    approval_path = work_dir / APPROVAL_PATH_NAME
    approval_path.unlink(missing_ok=True)
    artifact_meta = work_dir / ARTIFACTS_PATH_NAME
    artifact_meta.unlink(missing_ok=True)
    return {
        "segments": len(segments),
        "profile": resolved.mode,
        "provider": resolved.provider,
        "model": resolved.model,
        "plan_sha256": plan_sha256,
        "cost_estimate": document["plan"]["cost_estimate"],
        "llm_route": result.route,
    }


def load_plan_document(work_dir: Path) -> dict:
    path = work_dir / PLAN_PATH_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"缺少 TTS 计划: {path}；先执行 mmm run tts-plan --task <task_id>"
        )
    document = _read_json(path)
    expected = document.get("plan_sha256")
    actual = sha256_object(document.get("plan") or {})
    if not expected or expected != actual:
        raise ValueError("tts_plan.json 已被修改且指纹不一致；请重新生成计划")
    return document


def validate_plan_against_edl(document: dict, work_dir: Path) -> list[dict]:
    edl_path = work_dir / "edl.json"
    clips = narration_clips(_read_json(edl_path))
    segments = document.get("plan", {}).get("segments", [])
    cursor = 0
    for clip in clips:
        for sentence in split_sentences(clip["text"]):
            if cursor >= len(segments):
                raise ValueError(
                    "TTS 计划句数不足，与当前 EDL 不一致；请重新生成计划"
                )
            segment = segments[cursor]
            if int(segment.get("narration_id", -1)) != int(clip["narration_id"]):
                raise ValueError(
                    f"TTS 计划句 {cursor} narration_id 与 EDL 不一致"
                )
            if segment.get("source_text") != sentence:
                raise ValueError(
                    f"TTS 计划句 {cursor} 文本与 EDL 不一致；请重新生成计划"
                )
            cursor += 1
    if cursor != len(segments):
        raise ValueError(
            "TTS 计划句数多于当前 EDL；请重新生成计划"
        )
    return clips

def approve_plan(work_dir: Path, claimed_sha256: str) -> dict:
    document = load_plan_document(work_dir)
    actual = document["plan_sha256"]
    if claimed_sha256 != actual:
        raise ValueError(f"plan_sha256 不匹配：期望 {actual}，收到 {claimed_sha256}")
    plan = document["plan"]
    approval = {
        "schema_version": "tts_approval_v1",
        "plan_sha256": actual,
        "profile": plan["profile"],
        "provider": plan["execution"]["provider"],
        "model": plan["execution"]["model"],
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "approved_by": "user",
    }
    _write_json(work_dir / APPROVAL_PATH_NAME, approval)
    return approval


def _require_approval(work_dir: Path) -> dict:
    approval_path = work_dir / APPROVAL_PATH_NAME
    if not approval_path.exists():
        raise PermissionError(
            "TTS 计划尚未确认；请审阅 tts_plan.html 后执行 "
            "mmm tts-approve --task <task_id> --plan-sha256 <plan_sha256>"
        )
    approval = _read_json(approval_path)
    document = load_plan_document(work_dir)
    if approval.get("plan_sha256") != document["plan_sha256"]:
        raise ValueError("TTS 审批指纹与当前计划不一致；请重新确认计划")
    return approval


def _plan_segments(document: dict) -> list[TtsSegment]:
    segments: list[TtsSegment] = []
    for index, item in enumerate(document["plan"]["segments"]):
        performance = item["performance"]
        segments.append(TtsSegment(
            index=index,
            narration_id=int(item["narration_id"]),
            source_text=item["source_text"],
            performance=TtsPerformance(
                pause_before_ms=int(performance.get("pause_before_ms", 0)),
                pause_after_ms=int(performance.get("pause_after_ms", 0)),
                tone=performance.get("tone"),
                emotion=performance.get("emotion"),
                speed_hint=float(performance.get("speed_hint", 1.0)),
            ),
            pronunciations=[
                PronunciationRule(**rule)
                for rule in item.get("pronunciations", [])
            ],
        ))
    return segments


def _artifacts_valid(work_dir: Path, document: dict,
                     profile: TtsProfile, clips: list[dict]) -> list | None:
    meta_path = work_dir / ARTIFACTS_PATH_NAME
    if not meta_path.exists():
        return None
    try:
        metadata = _read_json(meta_path)
        if metadata.get("plan_sha256") != document["plan_sha256"]:
            return None
        if metadata.get("config_fingerprint") != profile.fingerprint():
            return None
        artifacts = metadata.get("artifacts", [])
        if len(artifacts) != len(clips):
            return None
        for index, item in enumerate(artifacts):
            wav_path = work_dir / item["wav_path"]
            if not wav_path.exists() or probe_duration(wav_path) <= 0:
                return None
            if int(item.get("index", -1)) != int(clips[index].get("_edl_index", index)):
                return None
            if int(item.get("narration_id", -1)) != int(clips[index]["narration_id"]):
                return None
            if item.get("source_text") != clips[index].get("text"):
                return None
        return artifacts
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _concat_wavs(paths: list[Path], out: Path) -> None:
    """把同一 EDL 片段的句级 WAV 按顺序拼接成渲染可消费的单文件。"""
    list_file = out.with_suffix(".concat.txt")
    try:
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in paths) + "\n",
            encoding="utf-8",
        )
        r = subprocess.run([
            ffmpeg_bin(), "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(out),
        ], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"TTS 片段合并失败: {r.stderr[-500:]}")
    finally:
        list_file.unlink(missing_ok=True)


def _merge_clip_artifacts(clips: list[dict], sentence_artifacts: list,
                          output_dir: Path) -> list:
    """句级产物按 narration_id 合并回 EDL 片段级，保持渲染 index 一一对应。"""
    grouped: dict[int, list] = {}
    for artifact in sentence_artifacts:
        grouped.setdefault(artifact.narration_id, []).append(artifact)

    merged = []
    for clip_index, clip in enumerate(clips):
        parts = sorted(grouped.get(clip["narration_id"], []), key=lambda a: a.index)
        if not parts:
            raise RuntimeError(f"narration_id={clip['narration_id']} 缺少句级 TTS 产物")
        edl_index = int(clip.get("_edl_index", clip_index))
        if len(parts) == 1:
            wav_path = parts[0].wav_path
        else:
            wav_path = output_dir / f"tts_{edl_index:03d}.wav"
            _concat_wavs([p.wav_path for p in parts], wav_path)
        merged.append(TtsArtifact(
            index=edl_index,
            narration_id=clip["narration_id"],
            source_text=clip["text"],
            wav_path=wav_path,
            duration_s=probe_duration(wav_path),
            speech_start_ms=parts[0].speech_start_ms,
            speech_end_ms=parts[-1].speech_end_ms,
        ))
    return merged


def synthesize_approved_plan(work_dir: Path, tts_cfg: dict) -> dict:
    """执行已批准计划；这是唯一允许调用真实 TTS 的统一入口。"""
    document = load_plan_document(work_dir)
    validate_plan_against_edl(document, work_dir)
    _require_approval(work_dir)
    plan = document["plan"]
    profile = TtsProfile(
        mode=plan["profile"],
        provider=plan["execution"]["provider"],
        model=plan["execution"]["model"],
        voice=plan["execution"]["voice"],
        speed=float(plan["execution"]["speed"]),
        emotion=plan["execution"].get("emotion"),
        options=dict(plan["execution"].get("options") or {}),
    )
    clips = narration_clips(_read_json(work_dir / "edl.json"))
    segments = _plan_segments(document)
    valid_artifacts = _artifacts_valid(work_dir, document, profile, clips)
    if valid_artifacts is not None:
        return {
            "reused": True,
            "provider": profile.provider,
            "model": profile.model,
            "segments": len(segments),
            "artifacts": valid_artifacts,
        }

    output_dir = work_dir / "render_segments"
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in list(output_dir.glob("tts_*.wav")) + list(output_dir.glob("sent_*.wav")):
        old.unlink()
    (work_dir / ARTIFACTS_PATH_NAME).unlink(missing_ok=True)

    provider = get_provider(profile.provider, profile.options)
    raw = provider.synthesize(segments, profile, output_dir)
    spans = align_segments(segments, raw.timings)
    sentence_artifacts = split_master_audio(
        raw.audio_path, raw, segments, spans, output_dir
    )
    artifacts = _merge_clip_artifacts(clips, sentence_artifacts, output_dir)
    metadata = {
        "schema_version": "tts_artifacts_v1",
        "plan_sha256": document["plan_sha256"],
        "config_fingerprint": profile.fingerprint(),
        "provider": profile.provider,
        "model": profile.model,
        "warnings": raw.warnings,
        "master_audio": raw.audio_path.name,
        "provider_metadata": raw.metadata,
        "artifacts": [
            {
                **dataclasses_to_dict(artifact),
                "wav_path": str(artifact.wav_path.relative_to(work_dir)),
            }
            for artifact in artifacts
        ],
    }
    _write_json(work_dir / ARTIFACTS_PATH_NAME, metadata)
    return {
        "reused": False,
        "provider": profile.provider,
        "model": profile.model,
        "segments": len(artifacts),
        "warnings": raw.warnings,
        "metadata": raw.metadata,
        "artifacts": metadata["artifacts"],
    }


def prepare_render_artifacts(work_dir: Path, tts_cfg: dict) -> dict[int, Path]:
    """渲染器入口：复用有效片段；缺失时要求计划已批准并执行统一合成。"""
    try:
        return load_render_artifacts(work_dir, tts_cfg)
    except TtsArtifactsMissing:
        summary = synthesize_approved_plan(work_dir, tts_cfg)
    artifacts = summary["artifacts"]
    return {
        int(item["index"]): work_dir / item["wav_path"]
        for item in artifacts
    }


def load_render_artifacts(work_dir: Path, tts_cfg: dict) -> dict[int, Path]:
    """只读取已生成的片段产物；不触发任何 TTS 请求。

    剪映导出必须使用本入口，避免用户以为只是导出草稿时隐式产生付费调用。
    """
    document = load_plan_document(work_dir)
    validate_plan_against_edl(document, work_dir)
    _require_approval(work_dir)
    plan = document["plan"]
    profile = TtsProfile(
        mode=plan["profile"],
        provider=plan["execution"]["provider"],
        model=plan["execution"]["model"],
        voice=plan["execution"]["voice"],
        speed=float(plan["execution"]["speed"]),
        emotion=plan["execution"].get("emotion"),
        options=dict(plan["execution"].get("options") or {}),
    )
    clips = narration_clips(_read_json(work_dir / "edl.json"))
    valid_artifacts = _artifacts_valid(work_dir, document, profile, clips)
    if valid_artifacts is None:
        raise TtsArtifactsMissing(
            "TTS 片段缺失或指纹不一致；请先执行 mmm run tts --task <task_id>。"
            "导出剪映草稿不会隐式触发 TTS。"
        )
    return {
        int(item["index"]): work_dir / item["wav_path"]
        for item in valid_artifacts
    }
