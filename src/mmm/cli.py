"""mmm CLI 入口。

命令总览见设计文档第 11 章。闸口协议：narrate/select 完成后必须停下等待人工确认。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import typer

from . import db


def _skip_if_done(key: str, stage: str, *anchors: Path, force: bool = False) -> bool:
    """断点续跑守卫：jobs 表为 done/gate_waiting 且全部产物锚点存在 → 跳过。

    产物文件才算数（防空跑后误标 done）；--force 强制重跑。
    """
    if force:
        return False
    if db.job_status(key, stage) not in ("done", "gate_waiting"):
        return False
    if not all(a.exists() for a in anchors):
        return False
    typer.echo(f"⏭ {key} · {stage} 已完成且产物齐全，跳过（--force 强制重跑）")
    return True

app = typer.Typer(help="mini-movie-maker：长视频浓缩工作流", no_args_is_help=True)
run_app = typer.Typer(help="分阶段执行管线（阶段1~7）")
app.add_typer(run_app, name="run")


def _parse_bgm_paths(bgm: str) -> list[str]:
    """解析 --bgm 参数；文件名可含逗号，用分号或换行分隔，逗号仅作兼容。"""
    if not bgm:
        return []
    # 优先按分号/换行分隔；如果没有，才退回到逗号（兼容旧用法）
    if ";" in bgm or "\n" in bgm:
        return [p.strip() for p in re.split(r"[;\n]", bgm) if p.strip()]
    return [p.strip() for p in bgm.split(",") if p.strip()]


def _render_title(cfg: dict) -> str:
    """按 task.json 的 title_template 渲染成片文件名（清理非法字符）。"""
    template = cfg.get("title_template") or "{task_id}"
    # 字段优先级：task.json 字段 > 首个视频的 catalog 字段
    videos = cfg.get("videos", [])
    vid0 = videos[0].get("video_id") if videos else ""
    fields = {
        "task_id": cfg.get("task_id", ""),
        "series": cfg.get("series", ""),
        "version": cfg.get("version", ""),
        "chapter": cfg.get("chapter", ""),
        "video_id": vid0,
    }
    # 允许 task.json 中直接写 version/chapter；缺省从 catalog 补（task-create 会写入）
    title = template.format(**fields)
    # 清理文件名非法字符
    for ch in r'\/:*?"<>|':
        title = title.replace(ch, "_")
    return title or cfg.get("task_id", "render")


@app.command("db-init")
def db_init() -> None:
    """按 db/schema.sql 初始化台账数据库（幂等，迁移后第一步）。"""
    conn = db.init_db()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    typer.echo(f"✓ 台账已就绪: {db.DB_PATH}  表: {', '.join(tables)}")


@app.command("add")
def add(
    video_id: str,
    series: str = typer.Option(..., "--series", "-s", help="系列（关联 config/series/ 配置）"),
    version: str = typer.Option("", "--version", "-v"),
    chapter: str = typer.Option("", "--chapter", "-c"),
) -> None:
    """登记素材：校验物料 + 台词预检 + 台账登记（物料规范 §6）。"""
    from . import catalog

    try:
        report = catalog.add_video(video_id, series, version, chapter)
    except FileNotFoundError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ 已登记 {video_id}（{series} {version} {chapter}）")
    typer.echo(f"  台词预检: {report['lines']} 行, 无配音 {report['unvoiced']} 行"
               + (f", ⚠ 坏行 {report['bad_lines']}" if report["bad_lines"] else ""))


@app.command("task-create")
def task_create(
    task_id: str,
    videos: str = typer.Option(..., "--videos", help="逗号分隔的 video_id，顺序即剧情顺序"),
    series: str = typer.Option("", "--series", "-s", help="系列（缺省取首个视频的系列）"),
) -> None:
    """建任务：引用视频 + 生成 task.json（类型适配层配置继承系列默认）。"""
    from . import catalog

    video_ids = [v.strip() for v in videos.split(",") if v.strip()]
    try:
        task = catalog.create_task(task_id, video_ids, series)
    except KeyError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ 任务已创建: {task_id}（{task['series']}，{len(video_ids)} 个视频，"
               f"目标 {task['target_minutes']} 分钟）")
    typer.echo(f"  产物: tasks/{task_id}/task.json")
    typer.echo(f"  下一步: 逐视频跑 mmm run shots/align/vision/index，然后 mmm run narrate {task_id}")


@run_app.command("shots")
def run_shots(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用，跳过台账）"),
    threshold: float = typer.Option(0.3, "--threshold", "-t", help="场景突变阈值"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段1：场景检测 + 黑白屏检测 → shots.json / fades.json。"""
    from . import stage_shots

    if path:
        video = Path(path)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        video = db.PROJECT_ROOT / "materials" / video_id / "source.mp4"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
        if _skip_if_done(video_id, "shots", out_dir / "shots.json", force=force):
            return
    if not video.exists():
        typer.echo(f"✗ 视频不存在: {video}", err=True)
        raise typer.Exit(1)

    summary = stage_shots.run(video, out_dir, threshold)
    typer.echo(f"✓ {video.name}: 时长 {summary['duration']}s, "
               f"切点 {summary['cuts']}, 镜头 {summary['shots']}, 黑白屏 {summary['fades']}")
    typer.echo(f"  产物: {out_dir}/shots.json, fades.json")
    if not path:
        db.record_job(video_id, "shots", "done",
                      f"{summary['shots']} 镜头, 黑白屏 {summary['fades']}")


@run_app.command("align")
def run_align(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用）"),
    script: str = typer.Option("", "--script", help="台词 JSONL 路径（冒烟测试用）"),
    model: str = typer.Option("medium", "--model", "-m", help="ASR 模型档位"),
    task: str = typer.Option("", "--task", help="任务模式：多视频全局对齐（台词横跨全部视频）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段2：ASR + 台词对齐 → asr.json / lines.json（含覆盖率报告）。"""
    from . import stage_asr

    if task:
        from . import catalog

        task_dir = db.PROJECT_ROOT / "tasks" / task
        anchors = [task_dir / "align_global.json"] + [
            db.PROJECT_ROOT / "workspace" / v["video_id"] / "lines.json"
            for v in catalog.task_videos(task)]
        if _skip_if_done(task, "align", *anchors, force=force):
            return
        report = stage_asr.align_task(task, model)
        typer.echo(f"✓ 任务 {task} 全局对齐: 总行数 {report['total']}, "
                   f"matched {report['matched']}, interpolated {report['interpolated']}, "
                   f"unmatched {report['unmatched']}, unvoiced {report['unvoiced']}, "
                   f"覆盖率 {report['coverage']:.1%}")
        for vid, r in report["per_video"].items():
            typer.echo(f"  分段 {vid}: {r['matched']} matched / {r['voiced_total']} 行, "
                       f"覆盖率 {r['coverage']:.1%}")
        if report["coverage"] < 0.85:
            typer.echo("⚠ 覆盖率低于 85%，建议人工核查物料（设计文档 §6 风险表）")
        db.record_job(task, "align", "done", f"覆盖率 {report['coverage']:.1%}")
        return

    if path and script:
        video, script_p = Path(path), Path(script)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        base = db.PROJECT_ROOT / "materials" / video_id
        video, script_p = base / "source.mp4", base / "script.jsonl"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
        if _skip_if_done(video_id, "align", out_dir / "lines.json", force=force):
            return
    for p in (video, script_p):
        if not p.exists():
            typer.echo(f"✗ 文件不存在: {p}", err=True)
            raise typer.Exit(1)

    report = stage_asr.run(video, script_p, out_dir, model)
    typer.echo(f"✓ {video.name}: 总行数 {report['total']}, "
               f"matched {report['matched']}, interpolated {report['interpolated']}, "
               f"unmatched {report['unmatched']}, 覆盖率 {report['coverage']:.1%}")
    if report["coverage"] < 0.85:
        typer.echo("⚠ 覆盖率低于 85%，建议人工核查物料（设计文档 §6 风险表）")
    typer.echo(f"  产物: {out_dir}/asr.json, lines.json")
    if not (path and script):
        db.record_job(video_id, "align", "done", f"覆盖率 {report['coverage']:.1%}")


@run_app.command("vision")
def run_vision(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用，跳过台账）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段3：抽帧 + 视觉理解 → shots_meta.json。"""
    from . import stage_vision

    if path:
        video = Path(path)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        video = db.PROJECT_ROOT / "materials" / video_id / "source.mp4"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
        if _skip_if_done(video_id, "vision", out_dir / "shots_meta.json", force=force):
            return
    if not video.exists():
        typer.echo(f"✗ 视频不存在: {video}", err=True)
        raise typer.Exit(1)

    summary = stage_vision.run(video, out_dir)
    reused = summary.get("reused", 0)
    typer.echo(f"✓ {video.name}: 分析 {summary['total']} 个镜头"
               f"{f'（断点复用 {reused}）' if reused else ''}, 失败 {len(summary['errors'])}")
    typer.echo(f"  产物: {out_dir}/shots_meta.json")
    if not path:
        status = "done" if not summary["errors"] else "failed"
        db.record_job(video_id, "vision", status,
                      f"{summary['total']} 镜头, 失败 {summary['errors']}")


@run_app.command("index")
def run_index(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段4：合并时间轴索引 → timeline.json。"""
    from . import stage_index

    if path:
        out_dir = Path(path)
    else:
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
        if _skip_if_done(video_id, "index", out_dir / "timeline.json", force=force):
            return
    if not (out_dir / "shots.json").exists():
        typer.echo(f"✗ workspace 不存在或缺少 shots.json: {out_dir}", err=True)
        raise typer.Exit(1)

    stats = stage_index.run(out_dir)
    by_class = stats["by_class"]
    typer.echo(f"✓ 时间轴索引已生成: {stats['shots']} 镜头, "
               f"E={by_class['E']} D={by_class['D']} C={by_class['C']} B={by_class['B']} A={by_class['A']}")
    typer.echo(f"  产物: {out_dir}/timeline.json")
    if not path:
        if (out_dir / "shots_meta.json").exists():
            db.record_job(video_id, "index", "done",
                          f"{stats['shots']} 镜头, 分级 {by_class}")
        else:
            # 无 vision 产物时分级全 A，是中间态而非完成态——不打 done，
            # 否则 vision 补跑后守卫会错误跳过 index 重跑
            typer.echo("⚠ 缺少 shots_meta.json（阶段3 未跑），分级退化为全 A；"
                       "不标记完成，vision 完成后请重跑 index")


@run_app.command("narrate")
def run_narrate(
    task_id: str = typer.Argument(""),
    timeline: str = typer.Option("", "--timeline", help="直接给 timeline.json 路径（冒烟测试用，跳过 task_id）"),
    target_minutes: float = typer.Option(15.0, "--target-minutes", "-t", help="目标正片时长（分钟）"),
    mode: str = typer.Option("auto", "--mode", help="auto/segment/oneshot（多视频合一篇用 segment）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅输出请求计划，不发起 LLM 请求"),
) -> None:
    """阶段5：生成解说稿。完成后进入闸口1，等待人工确认。"""
    from . import stage_index, stage_narrate

    if timeline:
        timeline_path = Path(timeline)
        out_dir = timeline_path.parent
    elif task_id:
        out_dir = db.PROJECT_ROOT / "tasks" / task_id
        timeline_path = out_dir / "global_timeline.json"
        if not timeline_path.exists():
            # 阶段4.5：多视频合流（单视频任务同样走此路径，结构统一）
            stats = stage_index.build_global(task_id)
            typer.echo(f"✓ 全局时间轴: {stats['shots']} 镜头, 总时长 {stats['duration']}s, "
                       f"分级 {stats['by_class']}")
    else:
        typer.echo("✗ 必须提供 task_id 或 --timeline", err=True)
        raise typer.Exit(1)

    if not timeline_path.exists():
        typer.echo(f"✗ timeline 不存在: {timeline_path}", err=True)
        raise typer.Exit(1)

    if dry_run:
        plan = stage_narrate.build_plan(
            timeline_path, out_dir, target_minutes=target_minutes,
            mode=mode, force=force,
        )
        info = stage_narrate.plan_summary(plan)
        typer.echo(f"✓ narrate dry-run: mode={info['mode']}, "
                   f"LOW segments={info['low_segments']}, cache hits={info['low_cache_hits']}, "
                   f"LOW requests={info['low_requests']}, HIGH requests={info['high_requests']}")
        typer.echo(f"  HIGH: profile={info['high_profile']}, model={info['high_model']}, "
                   f"prompt≈{info['high_prompt_tokens_estimated']} tokens, "
                   f"context={info['high_input_context_tokens']}, "
                   f"margin={info['high_safety_margin_tokens']}, "
                   f"max attempts={info['high_max_attempts']}")
        if info["low_model"]:
            typer.echo(f"  LOW: profile={info['low_profile']}, model={info['low_model']}, "
                       f"max attempts={info['low_max_attempts']}")
        typer.echo(f"  HIGH result cache: reusable={info['high_cache_reusable']} "
                   f"({info['high_cache_reason']})")
        typer.echo("  未发起任何 LLM 请求，未产生费用，未修改任务产物")
        return

    summary = stage_narrate.run(timeline_path, out_dir, target_minutes=target_minutes,
                                mode=mode, force=force)
    typer.echo(f"✓ 解说稿生成完成: {summary['sentences']} 句")
    typer.echo(f"  产物: {out_dir}/narration.json, narration.md")
    if task_id:
        db.record_job(task_id, "narrate", "gate_waiting", "等待闸口1人工审阅 narration.md")
    typer.echo("  ⏸ 闸口1：请审阅 narration.md，确认后再继续阶段6")


@run_app.command("select")
def run_select(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
    task: str = typer.Option("", "--task", help="任务模式：读 tasks/{task_id} 的 narration + 全局时间轴"),
    chars_per_sec: float = typer.Option(4.5, "--chars-per-sec", help="TTS 语速估算（字/秒）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段6：选片段 + 自检回环 + 分镜板。完成后进入闸口2，等待人工确认。"""
    from . import stage_select

    if task:
        out_dir = db.PROJECT_ROOT / "tasks" / task
        if _skip_if_done(task, "select", out_dir / "edl.json",
                         out_dir / "storyboard.html", force=force):
            return
        summary = stage_select.run(out_dir, task, timeline_name="global_timeline.json",
                                   exclude_task=task, chars_per_sec=chars_per_sec)
        db.record_job(task, "select", "gate_waiting", "等待闸口2人工审阅 storyboard.html")
        label = task
    else:
        out_dir = Path(path) if path else db.PROJECT_ROOT / "workspace" / video_id
        if not (out_dir / "narration.json").exists():
            typer.echo(f"✗ 缺少 narration.json: {out_dir}", err=True)
            raise typer.Exit(1)
        label = video_id or out_dir.name
        summary = stage_select.run(out_dir, label,
                                   workspace_of=lambda _vid: out_dir,
                                   chars_per_sec=chars_per_sec)
    typer.echo(f"✓ EDL 生成完成: {summary['clips']} 片段, "
               f"源视频总长 {summary['total_source_seconds']:.1f}s, "
               f"复用排除 {summary['excluded_used_shots']} 个已登记镜头")
    if summary["needs_review"]:
        typer.echo(f"  ⚠ {summary['needs_review']} 个片段候选被占用耗尽，需闸口2人工复核")
    typer.echo(f"  产物: {summary['edl']}, {summary['storyboard']}")
    typer.echo("  ⏸ 闸口2：请审阅 storyboard.html，确认或调整后再继续阶段7")


@run_app.command("render")
def run_render(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
    video: str = typer.Option("", "--video", help="直接给视频路径（冒烟测试用）"),
    task: str = typer.Option("", "--task", help="任务模式：按 task_map 解析各片段源视频 + 命名模板 + transform + BGM + 字幕"),
    bgm: str = typer.Option("", "--bgm", help="BGM 播放列表（分号分隔路径），缺省用 task.json bgm_playlist"),
    subtitle: str = typer.Option("", "--subtitle", help="字幕模式 overlay/letterbox/none，缺省用 task.json subtitle_mode"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段7 导出器A：ffmpeg 直出 MP4（MVP：本机 say 占位 TTS）。"""
    from . import catalog, stage_render

    if task:
        task_dir = db.PROJECT_ROOT / "tasks" / task
        cfg = json.loads((task_dir / "task.json").read_text())
        videos = {v["video_id"]: db.PROJECT_ROOT / v["source_path"] / "source.mp4"
                  for v in catalog.task_videos(task)}
        out_name = _render_title(cfg)
        out_dir = db.PROJECT_ROOT / "output" / task
        out_dir.mkdir(parents=True, exist_ok=True)
        # 每次渲染输出带时间戳（历史不覆盖，用户可反悔）；latest 固定名指向最新
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"{out_name}_{stamp}.mp4"
        latest_path = out_dir / f"{out_name}_latest.mp4"
        if _skip_if_done(task, "render", latest_path, force=force):
            return
        work_dir = task_dir
        # BGM：CLI --bgm 优先，否则用 task.json bgm_playlist
        from . import stage_bgm

        bgm_list = _parse_bgm_paths(bgm) if bgm else cfg.get("bgm_playlist", [])
        subtitle_mode = subtitle or cfg.get("subtitle_mode", "overlay")
    elif path and video:
        work_dir, video_p = Path(path), Path(video)
        videos = {work_dir.name: video_p}
        out_path = None
        bgm_list = _parse_bgm_paths(bgm)
        subtitle_mode = subtitle or "overlay"
    elif video_id:
        work_dir = db.PROJECT_ROOT / "workspace" / video_id
        videos = {video_id: db.PROJECT_ROOT / "materials" / video_id / "source.mp4"}
        out_path = None
        bgm_list = _parse_bgm_paths(bgm)
        subtitle_mode = subtitle or "overlay"
    else:
        typer.echo("✗ 必须提供 --task 或 video_id 或 --path + --video", err=True)
        raise typer.Exit(1)

    if not (work_dir / "edl.json").exists():
        typer.echo(f"✗ 缺少 edl.json: {work_dir}", err=True)
        raise typer.Exit(1)
    for vid, p in videos.items():
        if not p.exists():
            typer.echo(f"✗ 视频不存在: {p}（{vid}）", err=True)
            raise typer.Exit(1)

    summary = stage_render.run(work_dir, videos, out_path, task_id=task or "",
                               bgm_playlist=bgm_list if bgm_list else None,
                               subtitle_mode=subtitle_mode)
    typer.echo(f"✓ 渲染完成: {summary['clips']} 片段, 成片时长 {summary['duration']}s")
    if summary.get("bgm"):
        typer.echo(f"  BGM: {summary['bgm']}")
    typer.echo(f"  产物: {summary['output']}")
    if task:
        typer.echo(f"  已登记 footage_usage: {summary['footage_registered']} 个镜头"
                   f"（edl.final.json 已归档）")
        # 最新版固定名（副本），历史时间戳版本保留不覆盖
        shutil.copy2(Path(summary["output"]), latest_path)
        typer.echo(f"  最新版: {latest_path}")
        db.record_job(task, "render", "done", summary["output"])


@app.command("export-jianying")
def export_jianying(
    task_id: str,
    drafts_dir: str = typer.Option("", "--drafts-dir", help="剪映草稿根目录（缺省自动探测 ~/Movies/JianyingPro Drafts）"),
    bgm_volume: float = typer.Option(0.3, "--bgm-volume", help="BGM 轨预设音量（人工精修再调）"),
    force: bool = typer.Option(False, "--force", help="忽略断点续跑守卫，强制重跑"),
) -> None:
    """阶段7 导出器B：生成剪映草稿（单向终点，不回流）。"""
    from . import catalog, stage_jianying

    task_dir = db.PROJECT_ROOT / "tasks" / task_id
    if not (task_dir / "edl.json").exists():
        typer.echo(f"✗ 缺少 edl.json（先跑 mmm run select --task {task_id}）", err=True)
        raise typer.Exit(1)
    cfg = json.loads((task_dir / "task.json").read_text())
    draft_name = _render_title(cfg)

    dd = Path(drafts_dir) if drafts_dir else stage_jianying.detect_drafts_dir()
    anchor = dd / draft_name / "draft_content.json" if dd else task_dir / "narration.json"
    if _skip_if_done(task_id, "export-jianying", anchor, force=force):
        return

    videos = {v["video_id"]: db.PROJECT_ROOT / v["source_path"] / "source.mp4"
              for v in catalog.task_videos(task_id)}
    summary = stage_jianying.export(
        task_dir, videos, draft_name, task_id=task_id,
        drafts_dir=Path(drafts_dir) if drafts_dir else None,
        bgm_playlist=cfg.get("bgm_playlist") or None, bgm_volume=bgm_volume)
    typer.echo(f"✓ 剪映草稿已生成: {summary['draft_name']}（{summary['clips']} 片段, "
               f"{summary['duration']}s）")
    typer.echo(f"  草稿目录: {summary['drafts_dir']}")
    typer.echo(f"  已登记 footage_usage: {summary['footage_registered']} 个镜头（edl.final.json 已归档）")
    typer.echo("  ※ 剪映内手调不回流，成片口径以 edl.final.json 为准")
    db.record_job(task_id, "export-jianying", "done", f"草稿 {summary['draft_name']}")


@app.command("catalog-import")
def catalog_import() -> None:
    """把本机 catalog.yaml 导入台账（真实登记文件不入 Git）。"""
    from . import catalog

    if not catalog.CATALOG_YAML.exists():
        typer.echo("✗ 未找到 catalog.yaml（本机登记文件，不入 Git）")
        typer.echo("  请先复制模板: cp catalog.example.yaml catalog.yaml，再填入本机素材")
        raise typer.Exit(1)
    added, updated = catalog.import_catalog()
    typer.echo(f"✓ 台账导入完成: 新增 {added}, 更新 {updated}")


@app.command("status")
def status() -> None:
    """任务 × 阶段进度总览。"""
    from . import catalog

    rows = catalog.status_board()
    if not rows:
        typer.echo("（台账为空，尚未运行任何任务）")
        return
    icons = {"done": "✓", "failed": "✗", "running": "▶",
             "gate_waiting": "⏸", "pending": "·"}
    for r in rows:
        typer.echo(f"{icons.get(r['status'], '?')} {r['task_id']:<24} {r['stage']:<10} "
                   f"{r['status']:<12} 重试{r['retry_count']}  {r['message'] or ''}")


@app.command("locate")
def locate(task_id: str, open_dir: bool = typer.Option(False, "--open", help="在 Finder 中打开任务目录")) -> None:
    """按 task_id 直查全部关联路径。"""
    from . import catalog

    info = catalog.locate_task(task_id)
    if not info["videos"]:
        typer.echo(f"✗ 未找到任务: {task_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"task_id: {info['task_id']}")
    for v in info["videos"]:
        typer.echo(f"  素材[{v['seq']}] {v['video_id']}  {v['series']} {v['version'] or ''} {v['chapter'] or ''}")
        typer.echo(f"    物料: {v['source_path']}")
    typer.echo(f"  任务目录: {info['paths']['task_dir']}")
    typer.echo(f"  成品目录: {info['paths']['output_dir']}")
    if info["stages"]:
        typer.echo("  阶段状态: " + ", ".join(f"{s['stage']}={s['status']}" for s in info["stages"]))
    if open_dir:
        import subprocess

        subprocess.run(["open", str(db.PROJECT_ROOT / info["paths"]["task_dir"])])


@app.command("find")
def find(keyword: str) -> None:
    """按系列/版本/章节名模糊检索。"""
    from . import catalog

    rows = catalog.find_videos(keyword)
    if not rows:
        typer.echo(f"（无匹配: {keyword}）")
        return
    for r in rows:
        typer.echo(f"{r['video_id']:<12} {r['series']} {r['version'] or ''} {r['chapter'] or ''}  → {r['source_path']}")


if __name__ == "__main__":
    app()
