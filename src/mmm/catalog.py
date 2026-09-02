"""素材台账：catalog.yaml 导入与查询。

人维护 YAML，机器用 SQLite。冲突以 SQLite 为准（YAML 导入即覆盖同名 video_id）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from .db import PROJECT_ROOT, init_db

# 版本物料扫码：BGM / 片头按版本目录组织（assets/bgm/V{版本}版本/ 等），
# task-create 时扫描目录生成文件清单写入 task.json，下游 stage 直接消费清单。
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _resolve_dir(dir_path: str) -> Path:
    """版本目录路径解析：相对项目根 / 绝对路径。不存在则报错（不静默回退）。"""
    p = Path(dir_path) if Path(dir_path).is_absolute() else PROJECT_ROOT / dir_path
    if not p.is_dir():
        raise FileNotFoundError(f"版本目录不存在: {p}")
    return p


def _relpath(p: Path) -> str:
    """转相对项目根路径；不在项目根下则返回绝对路径。"""
    try:
        return str(p.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(p.resolve())


def scan_bgm_playlist(bgm_dir: str) -> list[str]:
    """扫描版本 BGM 目录，按文件名排序返回音频文件清单（相对项目根路径）。

    空目录返回 []（下游 stage_bgm 据此走静音轨兜底）。
    """
    d = _resolve_dir(bgm_dir)
    files = sorted(
        f for f in d.iterdir() if f.is_file() and f.suffix.lower() in _AUDIO_EXTS)
    return [_relpath(f) for f in files]


def scan_intros(intro_dir: str) -> list[dict]:
    """扫描版本片头目录，取文件名排序首个视频作为 intro_special 片头。

    空目录返回 []（下游 stage_compose 据此走无片头兜底）；
    多个视频只取首个（约定每版本仅一个片头）。
    """
    d = _resolve_dir(intro_dir)
    files = sorted(
        f for f in d.iterdir() if f.is_file() and f.suffix.lower() in _VIDEO_EXTS)
    if not files:
        return []
    return [{"type": "intro_special", "src": _relpath(files[0])}]


CATALOG_YAML = PROJECT_ROOT / "catalog.yaml"
CATALOG_EXAMPLE = PROJECT_ROOT / "catalog.example.yaml"


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


def create_task(task_id: str, video_ids: list[str], series: str = "",
                bgm_dir: str = "", intro_dir: str = "") -> dict:
    """建任务：task_map 登记（seq=给定顺序）+ tasks/{task_id}/task.json。

    系列配置（类型适配层）从 config/series/{series}.yaml 读取，缺省用内置默认。
    BGM / 片头为版本物料，不来自系列 yaml：由 bgm_dir/intro_dir 扫码生成文件清单。
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

    v0 = known[video_ids[0]]
    # BGM / 片头：版本物料扫码生成清单（不来自系列 yaml）。
    # 不传目录 → 清单为空（下游兜底静音轨 / 无片头），由 Skill 在配置确认时补扫。
    bgm_playlist = scan_bgm_playlist(bgm_dir) if bgm_dir else []
    composition = scan_intros(intro_dir) if intro_dir else []
    task = {
        "task_id": task_id,
        "series": series,
        "version": v0.get("version") or cfg.get("version", ""),
        "chapter": v0.get("chapter") or cfg.get("chapter", ""),
        "videos": [{"video_id": v, "seq": i} for i, v in enumerate(video_ids)],
        "target_minutes": cfg.get("target_minutes", 15),
        "title_template": cfg.get("title_template", "{chapter}"),
        "composition": composition,
        "subtitle_mode": cfg.get("subtitle_mode", "overlay"),
        "subtitle": cfg.get("subtitle") or {},
        "bgm_playlist": bgm_playlist,
        "tts": cfg.get("tts") or {},
        "output": cfg.get("output") or {"width": 1920, "height": 1080, "fps": 30},
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


def used_shots(exclude_task: str = "") -> set[tuple[str, int]]:
    """已被占用登记的 (video_id, shot_id) 集合；exclude_task 排除本任务（允许重跑选片）。"""
    conn = init_db()
    if exclude_task:
        rows = conn.execute(
            "SELECT video_id, shot_id FROM footage_usage WHERE task_id != ?",
            (exclude_task,)).fetchall()
    else:
        rows = conn.execute("SELECT video_id, shot_id FROM footage_usage").fetchall()
    return {(r[0], r[1]) for r in rows}


def register_usage(task_id: str, clips: list[dict]) -> int:
    """按导出时 EDL 登记片段使用（镜头级，幂等：先清本任务旧登记再写入）。

    设计文档 §4 阶段6：以导出时 EDL 为准——闸口2 人工调整后的 edl.json 是事实源。
    """
    conn = init_db()
    conn.execute("DELETE FROM footage_usage WHERE task_id=?", (task_id,))
    n = 0
    for c in clips:
        for sid in c.get("shot_ids", []):
            conn.execute(
                "INSERT OR IGNORE INTO footage_usage (video_id, shot_id, task_id) VALUES (?,?,?)",
                (c["video_id"], sid, task_id))
            n += 1
    conn.commit()
    return n



def import_catalog(yaml_path: Path = CATALOG_YAML) -> tuple[int, int]:
    """把本机 catalog.yaml 导入台账。返回 (新增数, 更新数)。

    真实登记文件不入 Git；新机器请从 catalog.example.yaml 复制后编辑。
    """
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"未找到台账文件: {yaml_path.name}（本机登记文件，不入 Git）\n"
            "请先复制模板: cp catalog.example.yaml catalog.yaml"
        )
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
