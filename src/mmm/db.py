"""统一台账数据库访问层。

单文件 SQLite（DATA_ROOT/ledger.sqlite），结构由 db/schema.sql 定义。
迁移重建：sqlite3 ledger.sqlite < db/schema.sql（或 mmm db-init）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import CODE_ROOT, DATA_ROOT

SCHEMA_PATH = CODE_ROOT / "db" / "schema.sql"
LEDGER_NAME = "ledger.sqlite"


def default_db_path() -> Path:
    """台账路径：数据根下 ledger.sqlite。运行时解析，不冻结于 import 时。"""
    return DATA_ROOT / LEDGER_NAME


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


def record_job(key: str, stage: str, status: str, message: str = "") -> None:
    """执行台账打点：对象 key × 阶段状态（幂等 upsert）。

    key 取值：asset_key（video 级阶段）/ task_id（任务级）/
    {task_id}:{asset_key}（任务级 per-asset 阶段，如 index）。
    """
    conn = init_db()
    conn.execute(
        """INSERT INTO jobs (key, stage, status, message)
           VALUES (?,?,?,?)
           ON CONFLICT(key, stage) DO UPDATE SET
             status=excluded.status, message=excluded.message,
             updated_at=datetime('now')""",
        (key, stage, status, message),
    )
    conn.commit()


def job_status(key: str, stage: str) -> str | None:
    """查询某对象（asset_key 或 task_id）在某阶段的状态，无记录返回 None。"""
    conn = init_db()
    row = conn.execute(
        "SELECT status FROM jobs WHERE key=? AND stage=?", (key, stage)
    ).fetchone()
    return row[0] if row else None
