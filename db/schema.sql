-- mini-movie-maker 台账结构
-- 用途：迁移/重建时执行  sqlite3 pipeline.sqlite < db/schema.sql
-- 两本账：素材台账（catalog + task_map，人维护）+ 执行台账（jobs，机器写）
-- 另：footage_usage 片段使用登记（镜头级，防跨成片复用）

-- 素材台账：一条输入视频一行。video_id 不编码业务含义
CREATE TABLE IF NOT EXISTS catalog (
    video_id    TEXT PRIMARY KEY,
    series      TEXT NOT NULL,          -- 系列，关联类型适配层配置
    version     TEXT,                   -- 版本号，参与命名模板
    chapter     TEXT,                   -- 章节名，参与命名模板
    source_path TEXT NOT NULL,          -- materials/{video_id}
    script_path TEXT,                   -- 台词 JSONL（视频级；任务级台词见 task.json）
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 任务-素材关联：一个任务可引用多个视频，seq 人工指定（剧情顺序）
CREATE TABLE IF NOT EXISTS task_map (
    task_id  TEXT NOT NULL,
    video_id TEXT NOT NULL REFERENCES catalog(video_id),
    seq      INTEGER NOT NULL,
    PRIMARY KEY (task_id, video_id)
);

-- 执行台账：任务 × 阶段的运行状态，断点续跑依据。易失，机器自动写
CREATE TABLE IF NOT EXISTS jobs (
    task_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,          -- shots/align/vision/index/narrate/select/render/export
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/gate_waiting
    retry_count INTEGER NOT NULL DEFAULT 0,
    message     TEXT,                   -- 失败原因或备注
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, stage)
);

-- 片段使用登记：已进入成片的镜头，防止同一源画面在多个成片中复用
CREATE TABLE IF NOT EXISTS footage_usage (
    video_id   TEXT NOT NULL,
    shot_id    INTEGER NOT NULL,
    task_id    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (video_id, shot_id)
);
