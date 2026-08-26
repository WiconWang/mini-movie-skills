"""阶段2 集成：ASR 转录 → 台词对齐 → lines.json。

ASR 职责（设计文档）：只当钟用不当文本源——取词级时间戳，文本弃用。
实测选型：faster-whisper medium+VAD+关上下文连锁（25 倍实时、无幻觉）；
small 档 4.7 倍但幻觉严重，已淘汰。
"""

from __future__ import annotations

import json
from pathlib import Path

ASR_MODEL_SIZE = "medium"    # 实测定型：medium+VAD 25倍实时无幻觉；small 4.7倍但幻觉严重


def transcribe_words(video: Path, model_size: str = ASR_MODEL_SIZE) -> list[dict]:
    """视频 → 词级时间戳列表 [{text, start, end}]。

    实测定型配方：vad_filter 过滤音乐/静音，condition_on_previous_text=False 防幻觉连锁。
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video), language="zh", word_timestamps=True,
                                   condition_on_previous_text=False, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"text": w.word, "start": round(w.start, 3), "end": round(w.end, 3)})
    return words


def load_script(script_path: Path) -> list[dict]:
    """读取物料规范的 script.jsonl。"""
    lines = []
    for i, raw in enumerate(script_path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"{script_path.name} 第{i}行 JSON 解析失败: {e}")
        if not obj.get("text"):
            raise ValueError(f"{script_path.name} 第{i}行 text 为空")
        lines.append({"text": obj["text"], "speaker": obj.get("speaker"),
                      "voiced": obj.get("voiced", True)})
    return lines


def run(video: Path, script: Path, out_dir: Path, model_size: str = ASR_MODEL_SIZE) -> dict:
    """执行阶段2：ASR → 对齐 → asr.json / lines.json，返回覆盖率报告。"""
    from .stage_align import AsrWord, align

    out_dir.mkdir(parents=True, exist_ok=True)

    words = transcribe_words(video, model_size)
    (out_dir / "asr.json").write_text(
        json.dumps({"video": video.name, "model": model_size, "words": words},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    script_lines = load_script(script)
    asr_words = [AsrWord(text=w["text"], start=w["start"], end=w["end"]) for w in words]
    result = align(script_lines, asr_words)

    (out_dir / "lines.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result["report"]


def ensure_asr(video: Path, out_dir: Path, model_size: str = ASR_MODEL_SIZE) -> list[dict]:
    """确保 asr.json 存在（多视频任务共享：已转录过就直接读，避免重复跑 ASR）。"""
    asr_path = out_dir / "asr.json"
    if asr_path.exists():
        return json.loads(asr_path.read_text())["words"]
    out_dir.mkdir(parents=True, exist_ok=True)
    words = transcribe_words(video, model_size)
    asr_path.write_text(
        json.dumps({"video": video.name, "model": model_size, "words": words},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return words


def _video_duration(video: Path) -> float:
    import subprocess

    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def align_task(task_id: str, model_size: str = ASR_MODEL_SIZE) -> dict:
    """多视频任务全局对齐（设计文档 §4 阶段2 多视频任务的对齐）。

    流程：逐视频确保共享 ASR（asr.json 断点复用）→ 按 seq 以 offset 拼成全局词流
    → 任务级完整台词一次对齐 → 按 offset 写入任务目录 lines.json（本地时间）。
    台词来源：task.json 的 script_path（任务级整份台词），缺省取首个视频的台词。

    lines.json 属于“素材 × 任务台词”的结果，不写入共享视频工作区，
    避免多个任务引用同一源视频时互相覆盖。
    """
    from .catalog import task_videos
    from .db import PROJECT_ROOT
    from .stage_align import AsrWord, align

    videos = task_videos(task_id)
    if not videos:
        raise KeyError(f"任务无关联素材: {task_id}")

    task_dir = PROJECT_ROOT / "tasks" / task_id
    task_workspace = task_dir / "workspace"
    task_cfg = {}
    cfg_path = task_dir / "task.json"
    if cfg_path.exists():
        task_cfg = json.loads(cfg_path.read_text())
    script_rel = task_cfg.get("script_path") or videos[0].get("script_path")
    if not script_rel:
        raise KeyError(f"任务 {task_id} 无台词来源（task.json script_path 或素材 script_path）")
    script_path = PROJECT_ROOT / script_rel

    # 1. 逐视频 ASR + 拼接全局词流
    offsets: list[tuple[str, float, float]] = []   # (video_id, offset, duration)
    global_words: list[AsrWord] = []
    offset = 0.0
    for v in videos:
        vid = v["video_id"]
        video = PROJECT_ROOT / v["source_path"] / "source.mp4"
        work = PROJECT_ROOT / "workspace" / vid
        words = ensure_asr(video, work, model_size)
        for w in words:
            global_words.append(AsrWord(text=w["text"],
                                        start=w["start"] + offset,
                                        end=w["end"] + offset))
        dur = _video_duration(video)
        offsets.append((vid, offset, dur))
        offset += dur

    # 2. 全局对齐
    script_lines = load_script(script_path)
    result = align(script_lines, global_words)

    # 3. 按 offset 拆回各视频 lines.json（本地时间，保持原 id 供全局时间轴使用）
    per_video: dict[str, list[dict]] = {vid: [] for vid, _, _ in offsets}
    for line in result["lines"]:
        if line["start"] is None:
            # 未匹配行归到台词顺序上最近的已匹配视频；找不到则归首个视频
            line["video_id"] = offsets[0][0]
            per_video[offsets[0][0]].append(line)
            continue
        vid, off = next(
            ((vid, off) for vid, off, dur in offsets if off <= line["start"] < off + dur),
            (offsets[-1][0], offsets[-1][1]))
        line["video_id"] = vid
        line["local_start"] = round(line["start"] - off, 2)
        line["local_end"] = round(line["end"] - off, 2)
        per_video[vid].append({**line, "start": line["local_start"], "end": line["local_end"]})

    # 对齐结果变化后，旧的任务级时间轴和全局时间轴不再可信。
    (task_dir / "global_timeline.json").unlink(missing_ok=True)
    for vid, lines in per_video.items():
        work = task_workspace / vid
        work.mkdir(parents=True, exist_ok=True)
        (work / "timeline.json").unlink(missing_ok=True)
        matched = sum(1 for l in lines if l["align"] == "matched")
        interp = sum(1 for l in lines if l["align"] == "interpolated")
        voiced = sum(1 for l in lines if l["align"] != "unvoiced")
        sub = {"lines": lines,
               "report": {"total": len(lines), "voiced_total": voiced,
                          "matched": matched, "interpolated": interp,
                          "unmatched": voiced - matched - interp,
                          "coverage": round((matched + interp) / max(voiced, 1), 4)}}
        (work / "lines.json").write_text(
            json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")

    # 全局结果也归档到任务目录（排查用）
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "align_global.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    report = dict(result["report"])
    report["per_video"] = {vid: json.loads(
        (task_workspace / vid / "lines.json").read_text())["report"]
        for vid, _, _ in offsets}
    return report
