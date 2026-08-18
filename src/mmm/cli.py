"""mmm CLI 入口。

命令总览见设计文档第 11 章。闸口协议：narrate/select 完成后必须停下等待人工确认。
"""

from __future__ import annotations

import typer

from . import db

app = typer.Typer(help="mini-movie-maker：长视频浓缩工作流", no_args_is_help=True)
run_app = typer.Typer(help="分阶段执行管线（阶段1~7）")
app.add_typer(run_app, name="run")


@app.command("db-init")
def db_init() -> None:
    """按 db/schema.sql 初始化台账数据库（幂等，迁移后第一步）。"""
    conn = db.init_db()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    typer.echo(f"✓ 台账已就绪: {db.DB_PATH}  表: {', '.join(tables)}")


@app.command("add")
def add(video: str, script: str = "", series: str = "", version: str = "", chapter: str = "") -> None:
    """登记素材：入库 + 台账登记 + 台词预检（物料规范 §6）。"""
    raise NotImplementedError("M1 待实现")


@app.command("task-create")
def task_create(videos: str, template: str = "") -> None:
    """建任务：引用视频（逗号分隔=seq 顺序）、选系列模板。"""
    raise NotImplementedError("M1 待实现")


@run_app.command("shots")
def run_shots(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用，跳过台账）"),
    threshold: float = typer.Option(0.3, "--threshold", "-t", help="场景突变阈值"),
) -> None:
    """阶段1：场景检测 + 黑白屏检测 → shots.json / fades.json。"""
    from pathlib import Path

    from . import stage_shots

    if path:
        video = Path(path)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        video = db.PROJECT_ROOT / "materials" / video_id / "source.mp4"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
    if not video.exists():
        typer.echo(f"✗ 视频不存在: {video}", err=True)
        raise typer.Exit(1)

    summary = stage_shots.run(video, out_dir, threshold)
    typer.echo(f"✓ {video.name}: 时长 {summary['duration']}s, "
               f"切点 {summary['cuts']}, 镜头 {summary['shots']}, 黑白屏 {summary['fades']}")
    typer.echo(f"  产物: {out_dir}/shots.json, fades.json")


@run_app.command("align")
def run_align(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用）"),
    script: str = typer.Option("", "--script", help="台词 JSONL 路径（冒烟测试用）"),
    model: str = typer.Option("small", "--model", "-m", help="ASR 模型档位"),
) -> None:
    """阶段2：ASR + 台词对齐 → asr.json / lines.json（含覆盖率报告）。"""
    from pathlib import Path

    from . import stage_asr

    if path and script:
        video, script_p = Path(path), Path(script)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        base = db.PROJECT_ROOT / "materials" / video_id
        video, script_p = base / "source.mp4", base / "script.jsonl"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
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


@run_app.command("vision")
def run_vision(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给视频路径（冒烟测试用，跳过台账）"),
    model: str = typer.Option("mimo-v2.5", "--model", "-m", help="视觉模型"),
) -> None:
    """阶段3：抽帧 + 视觉理解 → shots_meta.json。"""
    from pathlib import Path

    from . import stage_vision

    if path:
        video = Path(path)
        out_dir = db.PROJECT_ROOT / "workspace" / "_smoke" / video.stem
    else:
        video = db.PROJECT_ROOT / "materials" / video_id / "source.mp4"
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
    if not video.exists():
        typer.echo(f"✗ 视频不存在: {video}", err=True)
        raise typer.Exit(1)

    summary = stage_vision.run(video, out_dir, model=model)
    typer.echo(f"✓ {video.name}: 分析 {summary['total']} 个镜头, 失败 {len(summary['errors'])}")
    typer.echo(f"  产物: {out_dir}/shots_meta.json")


@run_app.command("index")
def run_index(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
) -> None:
    """阶段4：合并时间轴索引 → timeline.json。"""
    from pathlib import Path

    from . import stage_index

    if path:
        out_dir = Path(path)
    else:
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
    if not (out_dir / "shots.json").exists():
        typer.echo(f"✗ workspace 不存在或缺少 shots.json: {out_dir}", err=True)
        raise typer.Exit(1)

    stats = stage_index.run(out_dir)
    by_class = stats["by_class"]
    typer.echo(f"✓ 时间轴索引已生成: {stats['shots']} 镜头, "
               f"E={by_class['E']} D={by_class['D']} C={by_class['C']} B={by_class['B']} A={by_class['A']}")
    typer.echo(f"  产物: {out_dir}/timeline.json")


@run_app.command("narrate")
def run_narrate(
    task_id: str = typer.Argument(""),
    timeline: str = typer.Option("", "--timeline", help="直接给 timeline.json 路径（冒烟测试用，跳过 task_id）"),
    target_minutes: float = typer.Option(15.0, "--target-minutes", "-t", help="目标正片时长（分钟）"),
) -> None:
    """阶段5：生成解说稿。完成后进入闸口1，等待人工确认。"""
    from pathlib import Path

    from . import stage_narrate

    if timeline:
        timeline_path = Path(timeline)
        out_dir = timeline_path.parent
    elif task_id:
        timeline_path = db.PROJECT_ROOT / "tasks" / task_id / "global_timeline.json"
        out_dir = db.PROJECT_ROOT / "tasks" / task_id
    else:
        typer.echo("✗ 必须提供 task_id 或 --timeline", err=True)
        raise typer.Exit(1)

    if not timeline_path.exists():
        typer.echo(f"✗ timeline 不存在: {timeline_path}", err=True)
        raise typer.Exit(1)

    summary = stage_narrate.run(timeline_path, out_dir, target_minutes=target_minutes)
    typer.echo(f"✓ 解说稿生成完成: {summary['sentences']} 句")
    typer.echo(f"  产物: {out_dir}/narration.json, narration.md")
    typer.echo("  ⏸ 闸口1：请审阅 narration.md，确认后再继续阶段6")


@run_app.command("select")
def run_select(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
    chars_per_sec: float = typer.Option(4.5, "--chars-per-sec", help="TTS 语速估算（字/秒）"),
) -> None:
    """阶段6：选片段 + 自检回环 + 分镜板。完成后进入闸口2，等待人工确认。"""
    from pathlib import Path

    from . import stage_select

    if path:
        out_dir = Path(path)
    else:
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
    if not (out_dir / "narration.json").exists():
        typer.echo(f"✗ 缺少 narration.json: {out_dir}", err=True)
        raise typer.Exit(1)

    summary = stage_select.run(out_dir, video_id or out_dir.name, chars_per_sec=chars_per_sec)
    typer.echo(f"✓ EDL 生成完成: {summary['clips']} 片段, "
               f"源视频总长 {summary['total_source_seconds']:.1f}s")
    typer.echo(f"  产物: {summary['edl']}, {summary['storyboard']}")
    typer.echo("  ⏸ 闸口2：请审阅 storyboard.html，确认或调整后再继续阶段7")


@run_app.command("render")
def run_render(
    video_id: str = typer.Argument(""),
    path: str = typer.Option("", "--path", help="直接给 workspace 路径（冒烟测试用，跳过台账）"),
    video: str = typer.Option("", "--video", help="直接给视频路径（冒烟测试用）"),
) -> None:
    """阶段7 导出器A：ffmpeg 直出 MP4（MVP：本机 say 占位 TTS）。"""
    from pathlib import Path

    from . import stage_render

    if path and video:
        out_dir, video_p = Path(path), Path(video)
    elif video_id:
        out_dir = db.PROJECT_ROOT / "workspace" / video_id
        video_p = db.PROJECT_ROOT / "materials" / video_id / "source.mp4"
    else:
        typer.echo("✗ 必须提供 video_id 或 --path + --video", err=True)
        raise typer.Exit(1)
    for p in (out_dir / "edl.json", video_p):
        if not p.exists():
            typer.echo(f"✗ 文件不存在: {p}", err=True)
            raise typer.Exit(1)

    summary = stage_render.run(out_dir, video_p)
    typer.echo(f"✓ 渲染完成: {summary['clips']} 片段, 成片时长 {summary['duration']}s")
    typer.echo(f"  产物: {summary['output']}")


@app.command("export-jianying")
def export_jianying(task_id: str) -> None:
    """阶段7 导出器B：生成剪映草稿（单向终点，不回流）。"""
    raise NotImplementedError("M4 待实现")


@app.command("catalog-import")
def catalog_import() -> None:
    """把 catalog.yaml 导入台账（人维护 YAML，机器用 SQLite）。"""
    from . import catalog

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
