"""阶段3：抽帧 + 视觉理解（拼图模式 + 并发 + ui_type 三分类）。

优化历史：
- v1.0.2（2026-08-21）：三帧 hstack 拼图→1次调用 + ThreadPool并发 + ≤2s废弃（详见 0821-v1.0.2-vision阶段加速方案.md）
- v1.0.4（2026-08-21）：has_ui(bool)+has_dialogue_scene(失效) 合并为 ui_type 三分类
  (none/dialogue/gameplay)，gameplay 一票否决不入选（详见 0821-v1.0.4-画面UI分类与选片准入方案.md）

A~E 最终分类 = 多信号融合（本模块的 ui_type/motion/is_cutscene + 阶段1黑白屏 + 阶段2台词覆盖），
融合逻辑在阶段4 索引构建时完成（gameplay 在准入层即排除，不参与分级）。
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .llm import LLMEndpoint, chat_with_image, load_endpoint

MAX_WORKERS = int(os.environ.get("MMM_VISION_WORKERS", "6"))
MIN_SHOT_DUR = float(os.environ.get("MMM_VISION_MIN_DUR", "2.0"))

PROMPT = """这是一段游戏剧情实录视频某个镜头的头/中/尾三个时刻，横向拼接成一张三格图（从左到右为时间顺序）。
请综合观察整个镜头，判断画面上的 UI 类型并用 JSON 回答：
{
  "description": "一句话描述画面内容（角色、场景、动作）",
  "ui_type": "none/dialogue/gameplay",
  "motion": "static/low/medium/high",
  "is_cutscene": true/false
}

ui_type 判定规则（单选，gameplay 优先否决）：
- gameplay：画面有任何操作界面元素（血条/小地图/任务追踪/技能图标/菜单/地图界面/商城/弹窗等）即判此项，即使同时有对话框
- dialogue：画面有对话框或对话分支选项（剧情演出），但无上述操作界面元素
- none：画面无任何 UI（过场动画/空镜/环境镜头）

motion：整个镜头的画面动态程度。is_cutscene：是否电影化过场演出（无UI、构图精致、非实机操作画面）。
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


def stitch_frames(frames: list[Path], out_path: Path) -> Path:
    """三帧横向拼接为一张图（一次调用看全镜头，省 2/3 调用量）。"""
    inputs: list[str] = []
    for f in frames:
        inputs += ["-i", str(f)]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "quiet", *inputs,
         "-filter_complex", "hstack=inputs=3", "-q:v", "3", str(out_path)],
        check=True)
    return out_path


def analyze_frame(image: Path, endpoint: LLMEndpoint,
                  prompt: str = PROMPT) -> dict:
    """单图视觉理解；HTTP 瞬态重试由 route 策略统一处理。

    注意：视觉模型可能是思考型模型，输出预算来自 vision route。
    """
    result = chat_with_image(
        endpoint,
        prompt,
        image,
        max_tokens=endpoint.profile.max_tokens,
        label=f"shot:{image.stem}",
    )
    raw = result.content
    try:
        text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        obj = json.loads(text)
        ut = obj.get("ui_type")
        if ut not in ("none", "dialogue", "gameplay"):
            obj["ui_type"] = "gameplay" if ut in ("ui", "true", True) else "none"
        return obj
    except json.JSONDecodeError:
        return {"_parse_error": True, "_raw": raw[:200]}


def analyze_shot(video: Path, shot: dict, work_dir: Path,
                 endpoint: LLMEndpoint) -> dict:
    """分析一个镜头：短镜头跳过；否则抽 3 帧拼图 → 1 次 VLM 调用。

    网关致命错误（4xx/5xx 重试耗尽、ffmpeg 抽帧失败等）不炸掉整批，
    标记 _error 后继续后续镜头；带 _error 的镜头在下次 run 时自动重试。
    """
    dur = shot["end"] - shot["start"]
    if dur < MIN_SHOT_DUR:
        return {
            "shot_id": shot["id"], "start": shot["start"], "end": shot["end"],
            "_skip": "short", "frame_count": 0,
            "description": "短镜头（<2s，已废弃）",
            "ui_type": "none", "motion": "static", "is_cutscene": False,
        }

    frames_dir = work_dir / "frames" / f"shot_{shot['id']:03d}"
    try:
        frames = extract_frames(video, shot["start"], shot["end"], frames_dir)
        grid = frames_dir / "grid.jpg"
        stitch_frames(frames, grid)
        r = analyze_frame(grid, endpoint)
    except Exception as e:
        return {"shot_id": shot["id"], "_error": f"{type(e).__name__}: {e}"}
    if r.get("_parse_error"):
        return {"shot_id": shot["id"], "_error": "parse_failed", "_raw": r.get("_raw", "")}

    return {
        "shot_id": shot["id"],
        "start": shot["start"],
        "end": shot["end"],
        "description": r.get("description", ""),
        "ui_type": r.get("ui_type", "none"),
        "motion": r.get("motion", "low"),
        "is_cutscene": r.get("is_cutscene", False),
        "frame_count": len(frames),
    }


def run(video: Path, work_dir: Path,
        max_workers: int = MAX_WORKERS) -> dict:
    """批量分析 workspace 下所有镜头 → shots_meta.json（拼图 + 并发）。

    断点续跑：每镜头结果先落盘 shots_meta/shot_XXX.json，已存在且无 _error
    直接复用；带 _error 的镜头自动重试。_skip 标记的短镜头也复用（不重跑）。
    """
    shots_path = work_dir / "shots.json"
    shots = json.loads(shots_path.read_text())["shots"]
    endpoint = load_endpoint("vision")
    metas_dir = work_dir / "shots_meta"
    metas_dir.mkdir(parents=True, exist_ok=True)

    metas: dict[int, dict] = {}
    pending: list[dict] = []
    reused = skipped = 0
    for shot in shots:
        p = metas_dir / f"shot_{shot['id']:03d}.json"
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
            if "_error" not in meta:
                metas[shot["id"]] = meta
                reused += 1
                if meta.get("_skip"):
                    skipped += 1
                continue
        pending.append(shot)

    total = len(shots)
    errors: list[int] = []
    done = 0

    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(analyze_shot, video, s, work_dir, endpoint): s for s in pending}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    meta = fut.result()
                except Exception as e:
                    meta = {"shot_id": s["id"], "_error": f"{type(e).__name__}: {e}"}
                (metas_dir / f"shot_{s['id']:03d}.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                metas[s["id"]] = meta
                done += 1
                if "_error" in meta:
                    errors.append(s["id"])
                    print(f"  [{reused+done}/{total}] shot {s['id']} ✗ {meta['_error'][:60]}", flush=True)
                elif meta.get("_skip"):
                    skipped += 1
                if done % 20 == 0 or reused + done == total:
                    print(f"  进度 {reused+done}/{total}（并发 {max_workers}，"
                          f"复用 {reused}，跳过 {skipped}，失败 {len(errors)}）", flush=True)

    all_metas = [metas[s["id"]] for s in shots if s["id"] in metas]
    out = {"model": endpoint.model, "profile": endpoint.profile_id,
           "shots": shots, "metas": all_metas}
    meta_path = work_dir / "shots_meta.json"
    meta_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": len(all_metas), "errors": errors,
            "reused": reused, "skipped": skipped}
