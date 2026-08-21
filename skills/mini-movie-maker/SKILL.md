---
name: mini-movie-maker
description: 长视频浓缩工作流。将几小时长视频（+准确台词）浓缩为十几分钟解说短视频：镜头检测→台词对齐→视觉理解→写稿→选片→合成，双导出（MP4/剪映草稿）。当用户要求处理视频、生成解说视频、跑浓缩管线时使用。
---

# mini-movie-maker

设计文档：`docs/2026/0817-长视频浓缩工作流.md`（唯一事实源）
术语表：`CONTEXT.md`　物料契约：`docs/2026/0817-物料规范.md`

## 流程总览

```
登记(add) → 建任务(task-create) → shots → align → vision → index → narrate →【闸口1】→ select →【闸口2】→ render / export-jianying
```

## 闸口协议（铁律）

1. `mmm run narrate` 完成后**必须停下**，通知用户审 `tasks/{task_id}/narration.md`，不得擅自执行 `select`
2. `mmm run select` 完成后**必须停下**，通知用户审 `storyboard.html`，用户可能已手改 `edl.json`
3. 用户口头确认「通过」即闸门开关；继续前校验产物（JSON 可解析、引用 ID 存在）
4. 可以替用户做的：读打回意见重跑、重跑受影响片段、解释选镜理由

## 关键约束

- `materials/` 与 `assets/` 全程只读，原视频永不被修改
- 中间产物写 `workspace/`，任务产物写 `tasks/`，成品写 `output/`
- 一切定位用 `(video_id, 源内时间)`，禁止成片绝对时间（相对时间轴铁律）
- 台账操作走 `pipeline.sqlite`（结构见 `db/schema.sql`）

## 剪辑前配置确认（铁律：必须逐项询问）

**任务创建后、开始剪辑前，Agent 必须逐项询问用户确认以下配置**（用户不直接改配置文件，全部通过自然语言答复）。用户不提供某项时用系列默认值：

| 配置项 | 含义 | 系列默认（原神） |
|--------|------|-----------------|
| 输出分辨率 / FPS | 成片规格，所有素材适配 | 1920×1080 / 30fps |
| 黑边（letterbox） | 上下加黑边电影画幅，还是满屏 | overlay（满屏硬字幕） |
| 缩放（transform） | 是否放大裁 LOGO/UID，scale/offset | scale 1.10 / 顶部对齐 |
| 字幕模式 | overlay 硬字幕 / letterbox / none | overlay |
| BGM 歌单 | 背景音乐列表 | 空（须指定） |
| 片头（composition） | 是否拼片头 | 空（须指定） |
| TTS 音色 | 解说声音 + 语速 | edge / zh-CN-XiaoyiNeural / 1.1 |
| 目标时长 | 正片分钟数（不含 raw_insert） | 15 |
| 保留区间 | raw_insert 原声段（见下节） | 空 |

**执行方式**：Agent 逐项列出默认值 + 询问"是否调整"，用户可用自然语言一次答多项（如"1080p，加黑边，BGM 用这两首，第300秒要原声"）。所有答复写入 task.json，确认完毕后开始流水线。

## 保留区间配置（raw_insert）

用户可用自然语言指定「原始素材哪些位置要保留原声/保留画面」，写入 `tasks/{task_id}/task.json` 的 `keep_requirements` 数组，select 阶段自动生成 raw_insert 片段（原声原画）并入 EDL。

```json
"keep_requirements": [
  {"video_id": "gs-16-p1", "start": 300.5, "end": 320.0, "note": "第5分钟战斗原声"},
  {"video_id": "gs-16-p1", "start": 600.0, "end": 615.0, "note": "名场面保留"}
]
```

- `start/end` 为**源视频内本地秒数**（相对时间轴铁律）
- 区间内不排解说句；解说片段与保留区间重叠时自动裁剪避让
- 插入后**后续内容整体后移**（成片时间轴自动累计，零成本）
- BGM 全局 50%，raw_insert 段额外压低（-26dB），**区间结束自动恢复**
- 操作方式：用户自然语言说明 → 写入 task.json → 重跑 `mmm run select --task <id>` + `mmm run render --task <id>`

## 命令手册

| 命令 | 用途 |
|------|------|
| `mmm db-init` | 初始化台账（迁移后第一步） |
| `mmm add <video_id> --series <系列> [--version] [--chapter]` | 登记素材 + 台词预检 |
| `mmm task-create <task_id> --videos a,b,c [--series]` | 建任务（顺序即 seq），生成 task.json |
| `mmm run shots <video_id>` | 阶段1：镜头切分 + 黑白屏检测 |
| `mmm run align <video_id>` 或 `mmm run align --task <task_id>` | 阶段2：ASR + 台词对齐；多视频任务全局对齐 |
| `mmm run vision <video_id>` | 阶段3：抽帧 + 视觉理解（mimo-v2.5） |
| `mmm run index <video_id>` | 阶段4：多信号融合 → timeline.json |
| `mmm run narrate <task_id>` | 阶段5：解说稿生成 → 闸口1 |
| `mmm run select <video_id> --task <task_id>` | 阶段6：选片 + footage_usage 排除 + 分镜板 → 闸口2（任务模式必须带 `--task`，否则按单视频 workspace 解析） |
| `mmm run render --task <task_id>` | 阶段7：ffmpeg 直出（含 transform/BGM/字幕/片头拼接） |
| `mmm export-jianying <task_id>` | 导出器B：剪映草稿（单向终点） |
| `mmm status / locate / find` | 进度 / 路径直查 / 模糊检索 |

**断点续跑**：所有 `mmm run` / `export-jianying` 阶段命令启动时自动检查 jobs 表——
已完成（done/gate_waiting）且产物存在则跳过，加 `--force` 强制重跑。
vision 阶段逐镜头落盘 `shots_meta/shot_XXX.json`，中断重跑只补缺失/失败镜头。

## 冒烟测试入口（免台账）

用于开发调试，跳过 catalog/task：

```bash
mmm run shots --path <视频文件>
mmm run align --path <视频> --script <台词.jsonl>
mmm run index --path <workspace 目录>
mmm run narrate --timeline <timeline.json> [--target-minutes N]
mmm run select --path <workspace 目录>
mmm run render --path <workspace> --video <视频> [--bgm "a.mp3;b.mp3"] [--subtitle overlay]
```

## 系列配置（类型适配层）

`config/series/{系列}.yaml` 控制分级表、命名模板、composition、transform、subtitle_mode、TTS 音色、bgm_playlist。任务创建时继承系列默认，task.json 可覆盖。

## 闸口产物位置

- 闸口1：`tasks/{task_id}/narration.md`（解说稿审阅）
- 闸口2：`tasks/{task_id}/storyboard.html`（分镜板）
- 最终成片：`output/{task_id}/{title}.mp4`（含 `edl.final.json` 归档）
