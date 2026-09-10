-- 统一素材台账（ledger.sqlite）：管道侧登记 + mmm 侧认领，共用一本。
-- 用途：迁移/重建时执行  sqlite3 ledger.sqlite < db/schema.sql
-- 规范：docs/2026/0910-统一素材台账与命名规范.md §3
-- 约定：主键用整型 id、显式 REFERENCES 外键（可移植 MySQL）；asset.path 一律相对 DATA_ROOT。
-- 旧 catalog / task_map(video_id) / pipeline.sqlite 已废除，不做迁移（测试数据可弃）。

-- 游戏（code 定死后永不改名：genshin/zzz/starrail/wave/endfield）
CREATE TABLE IF NOT EXISTS game (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- 版本（no 保留小数点：2.4 / 1.6，避免 16 与 1.6 歧义）
CREATE TABLE IF NOT EXISTS version (
    id         INTEGER PRIMARY KEY,
    game_code  TEXT NOT NULL REFERENCES game(code),
    no         TEXT NOT NULL,
    name       TEXT,
    UNIQUE(game_code, no)
);

-- 任务线（type 为普通属性，缺省 event，不参与身份；身份 = (version, slug)）
CREATE TABLE IF NOT EXISTS quest (
    id         INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES version(id),
    type       TEXT NOT NULL DEFAULT 'event',
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    UNIQUE(version_id, slug)
);

-- 素材（id 内部主键供 FK 引用；asset_key 业务键，目录名 + CLI 稳定锚点，UNIQUE）
CREATE TABLE IF NOT EXISTS asset (
    id         INTEGER PRIMARY KEY,
    game_code  TEXT NOT NULL REFERENCES game(code),
    version_id INTEGER NOT NULL REFERENCES version(id),
    quest_id   INTEGER REFERENCES quest(id),
    kind       TEXT NOT NULL,
    seg_no     INTEGER,
    asset_key  TEXT NOT NULL UNIQUE,
    path       TEXT NOT NULL,
    source_url TEXT,
    sha256     TEXT,
    size       INTEGER,
    meta_json  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 成片任务（认领 quest；variant 存 -b/-v2 等后缀，不靠字符串反推）
CREATE TABLE IF NOT EXISTS task (
    task_id    TEXT PRIMARY KEY,
    quest_id   INTEGER NOT NULL REFERENCES quest(id),
    mode       TEXT NOT NULL DEFAULT 'narrate',
    variant    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 任务-素材关联（替代原 task_map；seq = 剧情顺序）
CREATE TABLE IF NOT EXISTS task_asset (
    task_id  TEXT NOT NULL REFERENCES task(task_id),
    asset_id INTEGER NOT NULL REFERENCES asset(id),
    seq      INTEGER NOT NULL,
    PRIMARY KEY(task_id, asset_id)
);

-- 执行台账：对象 key（asset_key / task_id / {task_id}:{asset_key}）× 阶段的运行状态。
-- 断点续跑依据。易失，机器自动写
CREATE TABLE IF NOT EXISTS jobs (
    key         TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (key, stage)
);

-- 片段使用登记：已进入成片的镜头，防止同一源画面在多个成片中复用。
-- B 模式豁免不记账；片头素材豁免
CREATE TABLE IF NOT EXISTS footage_usage (
    asset_id   INTEGER NOT NULL REFERENCES asset(id),
    shot_id    INTEGER NOT NULL,
    task_id    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_id, shot_id)
);
