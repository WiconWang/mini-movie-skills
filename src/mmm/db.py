"""台账数据库访问层。

单文件 SQLite，结构由 db/schema.sql 定义。
迁移重建：sqlite3 pipeline.sqlite < db/schema.sql
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "pipeline.sqlite"
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """按 schema.sql 建库（幂等），返回连接。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def record_job(task_id: str, stage: str, status: str, message: str = "") -> None:
    """执行台账打点：任务 × 阶段状态（幂等 upsert）。"""
    conn = init_db()
    conn.execute(
        """INSERT INTO jobs (task_id, stage, status, message)
           VALUES (?,?,?,?)
           ON CONFLICT(task_id, stage) DO UPDATE SET
             status=excluded.status, message=excluded.message,
             updated_at=datetime('now')""",
        (task_id, stage, status, message),
    )
    conn.commit()


def job_status(key: str, stage: str) -> str | None:
    """查询某对象（task_id 或 video_id）在某阶段的状态，无记录返回 None。"""
    conn = init_db()
    row = conn.execute(
        "SELECT status FROM jobs WHERE task_id=? AND stage=?", (key, stage)
    ).fetchone()
    return row[0] if row else None
