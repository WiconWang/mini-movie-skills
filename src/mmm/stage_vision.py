"""阶段3：抽帧 + 视觉理解。

每镜头抽头/中/尾帧，喂视觉 LLM 产出结构化标签（设计文档 §4 阶段3）。
A~E 最终分类 = 多信号融合（本模块的 VLM 标签 + 阶段1黑白屏 + 阶段2台词覆盖），
融合逻辑在阶段4 索引构建时完成。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .llm import chat_with_image

VISION_MODEL = "mimo-v2.5"

PROMPT = """这是一帧游戏剧情实录视频的画面。请仔细观察并用 JSON 回答：
{
  "description": "一句话描述画面内容（角色、场景、动作）",
  "has_ui": true/false,        // 是否有游戏界面元素：血条/小地图/任务追踪/操作提示/对话框
  "has_dialogue_scene": true/false,  // 是否处于角色对话演出（人物对峙/说话特写）
  "motion": "static/low/medium/high",  // 画面动态程度
  "is_cutscene": true/false    // 是否是电影化过场演出（无UI、构图精致、非实机操作画面）
}
只输出 JSON，不要其他文字。"""


def extract_frames(video: Path, start: float, end: float, out_dir: Path,
                   n: int = 3) -> list[Path]:
    """镜头区间内均匀抽 n 帧（默认头/中/尾）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    span = end - start
    frames = []
    for i in range(n):
        t = start + span * (i + 0.5) / n
        p = out_dir / f"f_{start:.1f}_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "quiet", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=960:-1", "-q:v", "3", str(p)],
            check=True)
        frames.append(p)
    return frames


def analyze_frame(frame: Path, model: str = VISION_MODEL) -> dict:
    """单帧视觉理解，返回结构化标签（解析失败重试一次）。

    注意：mimo-v2.5 是思考型模型，max_tokens 要给推理留足预算（实测 512 会空输出）。
    """
    raw = chat_with_image(model, PROMPT, frame, max_tokens=1500)
    try:
        # 容忍模型输出包在 ```json 代码块里
        text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_parse_error": True, "_raw": raw[:200]}


def analyze_shot(video: Path, shot: dict, work_dir: Path,
                 model: str = VISION_MODEL) -> dict:
    """分析一个镜头：抽帧 + VLM 标签（多帧结果取并集式保守判定）。"""
    frames_dir = work_dir / "frames" / f"shot_{shot['id']:03d}"
    frames = extract_frames(video, shot["start"], shot["end"], frames_dir)
    results = [analyze_frame(f, model) for f in frames]
    ok = [r for r in results if not r.get("_parse_error")]
    if not ok:
        return {"shot_id": shot["id"], "_error": "all_frames_parse_failed"}

    return {
        "shot_id": shot["id"],
        "start": shot["start"],
        "end": shot["end"],
        "description": ok[1]["description"] if len(ok) > 1 else ok[0]["description"],  # 中帧为准
        "has_ui": any(r.get("has_ui") for r in ok),              # 任一帧有 UI 即算有
        "has_dialogue_scene": any(r.get("has_dialogue_scene") for r in ok),
        "motion": max((r.get("motion", "low") for r in ok),
                      key=lambda m: ["static", "low", "medium", "high"].index(m)),
        "is_cutscene": all(r.get("is_cutscene") for r in ok),    # 全部帧认为是过场才算
        "frame_count": len(frames),
    }
