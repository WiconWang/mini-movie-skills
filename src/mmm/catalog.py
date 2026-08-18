"""素材台账：catalog.yaml 导入与查询。

人维护 YAML，机器用 SQLite。冲突以 SQLite 为准（YAML 导入即覆盖同名 video_id）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from .db import PROJECT_ROOT, init_db

CATALOG_YAML = PROJECT_ROOT / "catalog.yaml"


def add_video(video_id: str, series: str, version: str = "", chapter: str = "") -> dict:
    """登记单个素材：校验物料 + 台词预检 + upsert catalog（物料规范 §6）。"""
    base = PROJECT_ROOT / "materials" / video_id
    src, script = base / "source.mp4", base / "script.jsonl"
    if not src.exists():
        raise FileNotFoundError(f"缺少视频: {src}")
    if not script.exists():
        raise FileNotFoundError(f"缺少台词: {script}")

    # 台词预检：JSONL 逐行可解析、text 必填、统计无配音行
    lines, bad = [], []
    for i, raw in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            bad.append(i)
            continue
        if not obj.get("text"):
            bad.append(i)
            continue
        lines.append(obj)
    unvoiced = sum(1 for l in lines if l.get("voiced") is False)

    conn = init_db()
    conn.execute(
        """INSERT INTO catalog (video_id, series, version, chapter, source_path, script_path)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET
             series=excluded.series, version=excluded.version,
             chapter=excluded.chapter, source_path=excluded.source_path,
             script_path=excluded.script_path""",
        (video_id, series, version or None, chapter or None,
         f"materials/{video_id}", f"materials/{video_id}/script.jsonl"),
    )
    conn.commit()
    return {"lines": len(lines), "unvoiced": unvoiced, "bad_lines": bad}


def create_task(task_id: str, video_ids: list[str], series: str = "") -> dict:
    """建任务：task_map 登记（seq=给定顺序）+ tasks/{task_id}/task.json。

    系列配置（类型适配层）从 config/series/{series}.yaml 读取，缺省用内置默认。
    """
    conn = init_db()
    conn.row_factory = sqlite3.Row
    known = {r["video_id"]: dict(r) for r in conn.execute("SELECT * FROM catalog")}
    missing = [v for v in video_ids if v not in known]
    if missing:
        raise KeyError(f"video_id 未登记: {', '.join(missing)}（先 mmm add 或 catalog-import）")
    if not series:
        series = known[video_ids[0]]["series"]

    conn.execute("DELETE FROM task_map WHERE task_id=?", (task_id,))
    for seq, vid in enumerate(video_ids):
        conn.execute("INSERT INTO task_map (task_id, video_id, seq) VALUES (?,?,?)",
                     (task_id, vid, seq))
    conn.commit()

    cfg = {}
    series_cfg = PROJECT_ROOT / "config" / "series" / f"{series}.yaml"
    if series_cfg.exists():
        cfg = yaml.safe_load(series_cfg.read_text(encoding="utf-8")) or {}

    task = {
        "task_id": task_id,
        "series": series,
        "videos": [{"video_id": v, "seq": i} for i, v in enumerate(video_ids)],
        "target_minutes": cfg.get("target_minutes", 15),
        "title_template": cfg.get("title_template", "{chapter}"),
        "composition": cfg.get("composition", []),
        "transform": cfg.get("transform"),
        "subtitle_mode": cfg.get("subtitle_mode", "overlay"),
        "bgm_playlist": cfg.get("bgm_playlist", []),
    }
    task_dir = PROJECT_ROOT / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return task


def task_videos(task_id: str) -> list[dict]:
    """task_id → 按 seq 排序的素材行（含 catalog 字段）。"""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        """SELECT c.*, m.seq FROM task_map m JOIN catalog c ON c.video_id = m.video_id
           WHERE m.task_id = ? ORDER BY m.seq""", (task_id,)).fetchall()]



def import_catalog(yaml_path: Path = CATALOG_YAML) -> tuple[int, int]:
    """把 catalog.yaml 导入台账。返回 (新增数, 更新数)。"""
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    videos = data.get("videos", [])
    conn = init_db()
    added = updated = 0
    for v in videos:
        vid = v["video_id"]
        exists = conn.execute("SELECT 1 FROM catalog WHERE video_id=?", (vid,)).fetchone()
        conn.execute(
            """INSERT INTO catalog (video_id, series, version, chapter, source_path, script_path)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 series=excluded.series, version=excluded.version,
                 chapter=excluded.chapter, source_path=excluded.source_path,
                 script_path=excluded.script_path""",
            (vid, v.get("series", ""), v.get("version"), v.get("chapter"),
             v.get("path", f"materials/{vid}"), v.get("script_path")),
        )
        added, updated = added + (not exists), updated + bool(exists)
    conn.commit()
    return added, updated


def find_videos(keyword: str) -> list[sqlite3.Row]:
    """按系列/版本/章节名模糊检索。"""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    like = f"%{keyword}%"
    return conn.execute(
        """SELECT * FROM catalog
           WHERE series LIKE ? OR version LIKE ? OR chapter LIKE ? OR video_id LIKE ?
           ORDER BY series, version, chapter""",
        (like, like, like, like),
    ).fetchall()


def locate_task(task_id: str) -> dict:
    """task_id → 全部关联路径。"""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    videos = conn.execute(
        """SELECT c.*, m.seq FROM task_map m JOIN catalog c ON c.video_id = m.video_id
           WHERE m.task_id = ? ORDER BY m.seq""",
        (task_id,),
    ).fetchall()
    stages = conn.execute(
        "SELECT stage, status, updated_at FROM jobs WHERE task_id=? ORDER BY updated_at",
        (task_id,),
    ).fetchall()
    return {
        "task_id": task_id,
        "videos": [dict(v) for v in videos],
        "stages": [dict(s) for s in stages],
        "paths": {
            "task_dir": f"tasks/{task_id}",
            "output_dir": f"output/{task_id}",
            "workspaces": [f"workspace/{v['video_id']}" for v in videos],
            "materials": [v["source_path"] for v in videos],
        },
    }


def status_board() -> list[sqlite3.Row]:
    """任务 × 阶段进度总览。"""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT task_id, stage, status, retry_count, message, updated_at FROM jobs ORDER BY task_id, stage"
    ).fetchall()
