"""台账数据库访问层。

单文件 SQLite，结构由 db/schema.sql 定义。
迁移重建：sqlite3 pipeline.sqlite < db/schema.sql
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import CODE_ROOT, DATA_ROOT, PROJECT_ROOT  # noqa: F401 (PROJECT_ROOT 过渡别名)

SCHEMA_PATH = CODE_ROOT / "db" / "schema.sql"


def default_db_path() -> Path:
    """台账路径：数据根下 pipeline.sqlite。运行时解析，不冻结于 import 时。"""
    return DATA_ROOT / "pipeline.sqlite"


# 兼容旧引用（cli.py 展示路径用）；运行时取值，与 default_db_path() 一致。
DB_PATH = default_db_path()

_INITIALIZED: set[Path] = set()


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """按 schema.sql 建库（幂等），返回启用 WAL 并发设置的连接。"""
    db_path = db_path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    # 每个连接都要设置；WAL 只在首次初始化时切换，减少并发写锁竞争。
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if db_path not in _INITIALIZED:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _INITIALIZED.add(db_path)
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
