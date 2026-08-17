"""素材台账：catalog.yaml 导入与查询。

人维护 YAML，机器用 SQLite。冲突以 SQLite 为准（YAML 导入即覆盖同名 video_id）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from .db import PROJECT_ROOT, init_db

CATALOG_YAML = PROJECT_ROOT / "catalog.yaml"


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
