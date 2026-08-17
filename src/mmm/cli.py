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
    video_id: str = "",
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
def run_align(video_id: str) -> None:
    """阶段2：ASR + 台词对齐 → lines.json（含覆盖率报告）。"""
    raise NotImplementedError("M1 待实现")


@run_app.command("vision")
def run_vision(video_id: str) -> None:
    """阶段3：抽帧 + 视觉理解 → shots_meta.json。"""
    raise NotImplementedError("M2 待实现")


@run_app.command("index")
def run_index(video_id: str) -> None:
    """阶段4：合并时间轴索引 → timeline.json。"""
    raise NotImplementedError("M2 待实现")


@run_app.command("narrate")
def run_narrate(task_id: str) -> None:
    """阶段5：生成解说稿。完成后进入闸口1，等待人工确认。"""
    raise NotImplementedError("M3 待实现")


@run_app.command("select")
def run_select(task_id: str) -> None:
    """阶段6：选片段 + 自检回环 + 分镜板。完成后进入闸口2，等待人工确认。"""
    raise NotImplementedError("M4 待实现")


@run_app.command("render")
def run_render(task_id: str) -> None:
    """阶段7 导出器A：ffmpeg 直出 MP4。"""
    raise NotImplementedError("M4 待实现")


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
