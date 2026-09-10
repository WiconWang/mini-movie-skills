"""统一素材台账：登记（管道采集 / mmm 反向注册）与认领查询。

读写同一本 ledger.sqlite（DATA_ROOT/ledger.sqlite），不产生第二套台账。
身份规则（0910 规范 §5，Q2/Q3 决议）：
- quest 身份 = (version, slug)；type 为普通属性，缺省 event。
- asset 身份 = asset.id（FK 引用）；asset_key 为业务键（目录名 + CLI 稳定锚点）。
- task 身份 = task_id；后缀白名单 -b / -vN，禁止手写覆盖名。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import yaml

from .db import init_db
from .paths import CODE_ROOT, DATA_ROOT

# 游戏 code 定死后永不改名（Q6 决议）
GAME_SEED: tuple[tuple[str, str], ...] = (
    ("genshin", "原神"),
    ("zzz", "绝区零"),
    ("starrail", "星穹铁道"),
    ("wave", "鸣潮"),
    ("endfield", "终末地"),
)
GAME_NAMES = dict(GAME_SEED)

_VARIANT_RE = re.compile(r"-v\d+$")
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _conn() -> sqlite3.Connection:
    conn = init_db()
    conn.row_factory = sqlite3.Row
    return conn


def _check_game(game: str) -> str:
    if game not in GAME_NAMES:
        raise KeyError(f"未知 game code: {game}（允许：{', '.join(GAME_NAMES)}）")
    return game


def ensure_game(conn: sqlite3.Connection, game: str) -> None:
    """种子游戏行（幂等）。"""
    _check_game(game)
    conn.execute(
        "INSERT OR IGNORE INTO game (code, name) VALUES (?,?)",
        (game, GAME_NAMES[game]),
    )


def ensure_version(conn: sqlite3.Connection, game: str, no: str,
                   name: str = "") -> int:
    """取或建版本行，返回 id。no 保留小数点。"""
    ensure_game(conn, game)
    row = conn.execute(
        "SELECT id, name FROM version WHERE game_code=? AND no=?",
        (game, no)).fetchone()
    if row:
        if name and not row["name"]:
            conn.execute("UPDATE version SET name=? WHERE id=?", (name, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO version (game_code, no, name) VALUES (?,?,?)",
        (game, no, name or None))
    return cur.lastrowid


def ensure_quest(conn: sqlite3.Connection, version_id: int, slug: str,
                 name: str = "", type: str = "event") -> int:
    """取或建任务线行，返回 id。type 仅为属性，缺省 event。"""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError(f"quest_slug 须为 ASCII 短名（小写/数字/横杠）：{slug}")
    row = conn.execute(
        "SELECT id FROM quest WHERE version_id=? AND slug=?",
        (version_id, slug)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO quest (version_id, type, name, slug) VALUES (?,?,?,?)",
        (version_id, type or "event", name or slug, slug))
    return cur.lastrowid


def video_asset_key(game: str, version: str, slug: str, seg: int) -> str:
    return f"{game}-{version}-{slug}-p{seg:03d}"


def dialog_asset_key(game: str, version: str, slug: str) -> str:
    return f"{game}-{version}-{slug}"


def version_asset_key(game: str, version: str, kind: str, seq: int = 0) -> str:
    if kind == "bgm":
        return f"{game}-{version}-bgm-{seq:02d}"
    return f"{game}-{version}-{kind}"


def build_task_id(game: str, version: str, slug: str,
                  mode: str = "narrate", variant: str = "") -> str:
    """task_id 生成（后缀白名单；禁止手写覆盖名）。"""
    if mode not in ("narrate", "raw"):
        raise ValueError(f"mode 只支持 narrate/raw，当前: {mode}")
    tid = f"{game}-{version}-{slug}"
    if mode == "raw":
        tid += "-b"
    if variant:
        if not _VARIANT_RE.fullmatch(variant):
            raise ValueError(f"variant 只允许 -vN 形态（如 -v2），当前: {variant}")
        tid += variant
    return tid


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_dialog(src: Path) -> dict:
    """台词 JSONL 预检（沿用物料规范验收项）。"""
    lines, bad = [], []
    for i, raw in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
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
    return {"lines": len(lines),
            "unvoiced": sum(1 for l in lines if l.get("voiced") is False),
            "bad_lines": bad}


def add_asset(*, game: str, version: str, slug: str, kind: str,
              src: str, seg: int = 0, type: str = "event",
              quest_name: str = "", version_name: str = "",
              source_url: str = "", meta: dict | None = None,
              no_hash: bool = False) -> dict:
    """登记一个 asset：落新目录树 + 写 ledger（幂等 upsert）。

    kind：video / dialog / bgm / cover / outro / intro。
    video 需 seg（quest 内剧情序号，非 bilibili 分P号）；
    其余 kind 不需要 seg（bgm 序号自动分配）。
    src：本地源文件路径（复制入库，原文件不动）。
    """
    _check_game(game)
    if kind not in ("video", "dialog", "bgm", "cover", "outro", "intro"):
        raise ValueError(f"未知 kind: {kind}")
    src_p = Path(src).expanduser()
    if not src_p.is_file():
        raise FileNotFoundError(f"源文件不存在: {src_p}")
    if kind == "video" and seg < 1:
        raise ValueError("video 登记必须指定 --seg（quest 内剧情序号，1 起）")
    if kind == "video" and src_p.suffix.lower() not in _VIDEO_EXTS:
        raise ValueError(f"video 源须为视频文件：{src_p}")
    if kind == "bgm" and src_p.suffix.lower() not in _AUDIO_EXTS:
        raise ValueError(f"bgm 源须为音频文件：{src_p}")

    conn = _conn()
    version_id = ensure_version(conn, game, version, version_name)
    quest_id: int | None = None
    if kind in ("video", "dialog"):
        quest_id = ensure_quest(conn, version_id, slug, quest_name, type)
        if kind == "video":
            asset_key = video_asset_key(game, version, slug, seg)
            dst = DATA_ROOT / game / version / slug / "video" / f"p{seg:03d}{src_p.suffix.lower()}"
            path = f"{game}/{version}/{slug}/video/{dst.name}"
        else:
            asset_key = dialog_asset_key(game, version, slug)
            report = _check_dialog(src_p)
            dst = DATA_ROOT / game / version / slug / "dialog" / f"{slug}.jsonl"
            path = f"{game}/{version}/{slug}/dialog/{dst.name}"
    else:
        if kind == "bgm":
            n = conn.execute(
                """SELECT COUNT(*) FROM asset a JOIN version v ON v.id=a.version_id
                   WHERE v.game_code=? AND v.no=? AND a.kind='bgm'""",
                (game, version)).fetchone()[0] + 1
            asset_key = version_asset_key(game, version, kind, n)
            dst = DATA_ROOT / game / version / "_version" / "bgm" / f"{asset_key}{src_p.suffix.lower()}"
            path = f"{game}/{version}/_version/bgm/{dst.name}"
        elif kind in ("cover", "outro"):
            asset_key = version_asset_key(game, version, kind)
            dst = DATA_ROOT / game / version / "_version" / "cover" / f"{kind}{src_p.suffix.lower()}"
            path = f"{game}/{version}/_version/cover/{dst.name}"
        else:  # intro
            asset_key = version_asset_key(game, version, kind)
            dst = DATA_ROOT / game / version / "_version" / "intro" / f"intro{src_p.suffix.lower()}"
            path = f"{game}/{version}/_version/intro/{dst.name}"

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_p, dst)
    sha = "" if no_hash else _sha256(dst)
    size = dst.stat().st_size
    meta_json = json.dumps(meta or {}, ensure_ascii=False) if (meta or kind == "dialog") else None
    if kind == "dialog":
        m = dict(meta or {})
        if source_url:
            m.setdefault("source_url", source_url)
        meta_json = json.dumps(m, ensure_ascii=False)

    conn.execute(
        """INSERT INTO asset (game_code, version_id, quest_id, kind, seg_no, asset_key,
                              path, source_url, sha256, size, meta_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(asset_key) DO UPDATE SET
             quest_id=excluded.quest_id, kind=excluded.kind, seg_no=excluded.seg_no,
             path=excluded.path, source_url=excluded.source_url, sha256=excluded.sha256,
             size=excluded.size, meta_json=excluded.meta_json""",
        (game, version_id, quest_id, kind, seg or None, asset_key, path,
         source_url or None, sha or None, size, meta_json),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM asset WHERE asset_key=?", (asset_key,)).fetchone()
    report = {"asset_id": row["id"], "asset_key": asset_key, "path": path,
              "sha256": sha, "size": size}
    if kind == "dialog":
        report.update(_check_dialog(dst))
    return report


def asset_by_id(asset_id: int) -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM asset WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise KeyError(f"asset 不存在: id={asset_id}")
    return dict(row)


def asset_by_key(asset_key: str) -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM asset WHERE asset_key=?", (asset_key,)).fetchone()
    if not row:
        raise KeyError(f"asset 不存在: {asset_key}")
    return dict(row)


def asset_key_of(asset_id: int) -> str:
    return asset_by_id(asset_id)["asset_key"]


def resolve_video(asset_key: str) -> Path:
    """asset_key → 视频文件绝对路径（查 kind=video 的 asset.path）。"""
    a = asset_by_key(asset_key)
    if a["kind"] != "video":
        raise KeyError(f"{asset_key} 不是 video 资产（kind={a['kind']}）")
    return DATA_ROOT / a["path"]


def resolve_dialog(quest_id: int) -> Path:
    """quest_id → 台词文件绝对路径（查 kind=dialog 的 asset.path）。"""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM asset WHERE quest_id=? AND kind='dialog' ORDER BY id",
        (quest_id,)).fetchone()
    if not row:
        raise KeyError(f"quest {quest_id} 无 dialog 资产")
    return DATA_ROOT / row["path"]


def resolve_bgm_playlist(items: list) -> list[str]:
    """task.json bgm_playlist（[{asset_id}]）→ 相对路径清单（供 stage_bgm）。"""
    out = []
    for it in items or []:
        if isinstance(it, dict) and "asset_id" in it:
            out.append(asset_by_id(it["asset_id"])["path"])
        elif isinstance(it, str):
            out.append(it)  # CLI --bgm 显式路径透传
    return out


def quest_of(game: str, version: str, slug: str) -> dict:
    """查 quest 行（含 version/game）。"""
    conn = _conn()
    row = conn.execute(
        """SELECT q.*, v.game_code AS game, v.no AS version
           FROM quest q JOIN version v ON v.id=q.version_id
           WHERE v.game_code=? AND v.no=? AND q.slug=?""",
        (game, version, slug)).fetchone()
    if not row:
        raise KeyError(f"quest 未登记: {game}/{version}/{slug}（先 add-asset 登记素材）")
    return dict(row)


def quest_assets(quest_id: int) -> list[dict]:
    """quest 的 video+dialog 资产（按 kind, seg_no 排序）。"""
    conn = _conn()
    return [dict(r) for r in conn.execute(
        """SELECT * FROM asset WHERE quest_id=? AND kind IN ('video','dialog')
           ORDER BY CASE kind WHEN 'video' THEN 0 ELSE 1 END, seg_no""",
        (quest_id,)).fetchall()]


def version_assets(game: str, version: str) -> list[dict]:
    """版本级装饰物料（bgm/cover/outro/intro）。"""
    conn = _conn()
    return [dict(r) for r in conn.execute(
        """SELECT a.* FROM asset a JOIN version v ON v.id=a.version_id
           WHERE v.game_code=? AND v.no=? AND a.quest_id IS NULL ORDER BY a.kind, a.id""",
        (game, version)).fetchall()]


def claim_task(*, game: str, version: str, slug: str,
               mode: str = "narrate", variant: str = "") -> dict:
    """认领：从 ledger 查 quest 资产 → 写 task_asset + tasks/{task_id}/task.json。

    管道与独立模式共用；区别仅在登记由谁执行。
    校验：无 video → 报错；无 dialog → narrate 报错、raw 放行。
    版本级物料（bgm/cover/outro/intro）在 task.json 以 asset_id 引用。
    """
    _check_game(game)
    q = quest_of(game, version, slug)
    assets = quest_assets(q["id"])
    videos = [a for a in assets if a["kind"] == "video"]
    dialogs = [a for a in assets if a["kind"] == "dialog"]
    if not videos:
        raise KeyError(f"quest {game}/{version}/{slug} 无 video 资产（先 add-asset --kind video）")
    if not dialogs and mode == "narrate":
        raise KeyError(f"quest {game}/{version}/{slug} 无 dialog 资产（A 模式必需；B 模式可放行）")

    task_id = build_task_id(game, version, slug, mode, variant)
    conn = _conn()
    conn.execute(
        "INSERT INTO task (task_id, quest_id, mode, variant) VALUES (?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET quest_id=excluded.quest_id, "
        "mode=excluded.mode, variant=excluded.variant",
        (task_id, q["id"], mode, variant or None))
    conn.execute("DELETE FROM task_asset WHERE task_id=?", (task_id,))
    for seq, a in enumerate(videos + dialogs):
        conn.execute("INSERT INTO task_asset (task_id, asset_id, seq) VALUES (?,?,?)",
                     (task_id, a["id"], seq))
    conn.commit()

    cfg = {}
    game_cfg = CODE_ROOT / "config" / "game" / f"{game}.yaml"
    if game_cfg.exists():
        cfg = yaml.safe_load(game_cfg.read_text(encoding="utf-8")) or {}

    vatt = version_assets(game, version)
    composition = [{"type": a["kind"], "asset_id": a["id"]}
                   for a in vatt if a["kind"] in ("cover", "outro", "intro")]
    bgm_playlist = [{"asset_id": a["id"]} for a in vatt if a["kind"] == "bgm"]
    task = {
        "task_id": task_id,
        "quest_id": q["id"],
        "game": game,
        "version": version,
        "quest": q["name"],
        "quest_slug": slug,
        "mode": mode,
        "variant": variant or "",
        "assets": [{"asset_id": a["id"], "asset_key": a["asset_key"],
                    "kind": a["kind"], "seq": i}
                   for i, a in enumerate(videos + dialogs)],
        "target_minutes": cfg.get("target_minutes", 15),
        "title_template": cfg.get("title_template", "{quest}"),
        "composition": composition,
        "subtitle_mode": "none" if mode == "raw" else cfg.get("subtitle_mode", "overlay"),
        "subtitle": cfg.get("subtitle") or {},
        "bgm_playlist": bgm_playlist,
        "tts": cfg.get("tts") or {},
        "output": cfg.get("output") or {"width": 1920, "height": 1080, "fps": 30},
    }
    if mode == "raw":
        task["raw_select"] = {
            "quality_levels": ["great"],
            "buffer_sec": 0.0,
            "min_shot_class": "B",
            "prefer_ui_types": ["dialogue"],
            "auto_low_extract": True,
        }
    task_dir = DATA_ROOT / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return task


def task_assets(task_id: str) -> list[dict]:
    """task_id → 按 seq 排序的资产行（含 asset 全字段）。"""
    conn = _conn()
    return [dict(r) for r in conn.execute(
        """SELECT a.*, m.seq FROM task_asset m JOIN asset a ON a.id = m.asset_id
           WHERE m.task_id = ? ORDER BY m.seq""", (task_id,)).fetchall()]


def used_shots(exclude_task: str = "") -> set[tuple[int, int]]:
    """已被占用登记的 (asset_id, shot_id) 集合；exclude_task 排除本任务。"""
    conn = _conn()
    if exclude_task:
        rows = conn.execute(
            "SELECT asset_id, shot_id FROM footage_usage WHERE task_id != ?",
            (exclude_task,)).fetchall()
    else:
        rows = conn.execute("SELECT asset_id, shot_id FROM footage_usage").fetchall()
    return {(r[0], r[1]) for r in rows}


def register_usage(task_id: str, clips: list[dict]) -> int:
    """按导出时 EDL 登记片段使用（镜头级，幂等：先清本任务旧登记再写入）。"""
    conn = _conn()
    conn.execute("DELETE FROM footage_usage WHERE task_id=?", (task_id,))
    n = 0
    for c in clips:
        for sid in c.get("shot_ids", []):
            conn.execute(
                "INSERT OR IGNORE INTO footage_usage (asset_id, shot_id, task_id) VALUES (?,?,?)",
                (c["asset_id"], sid, task_id))
            n += 1
    conn.commit()
    return n


def find_assets(keyword: str) -> list[dict]:
    """按游戏/版本/任务名/slug/asset_key 模糊检索资产。"""
    conn = _conn()
    like = f"%{keyword}%"
    return [dict(r) for r in conn.execute(
        """SELECT a.*, q.name AS quest_name, q.slug AS slug, v.no AS version_no
           FROM asset a LEFT JOIN quest q ON q.id=a.quest_id
           LEFT JOIN version v ON v.id=a.version_id
           WHERE a.game_code LIKE ? OR v.no LIKE ? OR q.name LIKE ?
             OR q.slug LIKE ? OR a.asset_key LIKE ?
           ORDER BY a.game_code, v.no, a.asset_key""",
        (like, like, like, like, like)).fetchall()]


def locate_task(task_id: str) -> dict:
    """task_id → 全部关联路径。"""
    conn = _conn()
    assets = task_assets(task_id)
    if not assets:
        t = conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        if not t:
            return {"task_id": task_id, "assets": [], "stages": [],
                    "paths": {"task_dir": f"tasks/{task_id}",
                              "output_dir": f"output/{task_id}",
                              "workspaces": [], "materials": []}}
    stages = conn.execute(
        "SELECT stage, status, updated_at FROM jobs WHERE key=? ORDER BY updated_at",
        (task_id,)).fetchall()
    return {
        "task_id": task_id,
        "assets": assets,
        "stages": [dict(s) for s in stages],
        "paths": {
            "task_dir": f"tasks/{task_id}",
            "output_dir": f"output/{task_id}",
            "workspaces": [f"workspace/{a['asset_key']}" for a in assets
                           if a["kind"] == "video"],
            "materials": [a["path"] for a in assets],
        },
    }


def status_board() -> list[dict]:
    """对象 key × 阶段进度总览。"""
    conn = _conn()
    return [dict(r) for r in conn.execute(
        "SELECT key, stage, status, retry_count, message, updated_at FROM jobs "
        "ORDER BY key, stage").fetchall()]
