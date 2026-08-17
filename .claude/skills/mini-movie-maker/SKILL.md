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

1. `mmm run narrate` 完成后**必须停下**，通知用户审 `tasks/{task_id}/narration.json` 的评审稿，不得擅自执行 `select`
2. `mmm run select` 完成后**必须停下**，通知用户审 `storyboard.html`，用户可能已手改 `edl.json`
3. 用户口头确认「通过」即闸门开关；继续前校验产物（JSON 可解析、引用 ID 存在）
4. 可以替用户做的：读打回意见重跑、重跑受影响片段、解释选镜理由

## 关键约束

- `materials/` 与 `assets/` 全程只读，原视频永不被修改
- 中间产物写 `workspace/`，任务产物写 `tasks/`，成品写 `output/`
- 一切定位用 `(video_id, 源内时间)`，禁止成片绝对时间（相对时间轴铁律）
- 台账操作走 `pipeline.sqlite`（结构见 `db/schema.sql`）

## 命令手册

| 命令 | 用途 |
|------|------|
| `mmm db-init` | 初始化台账（迁移后第一步） |
| `mmm add <视频> [台词]` | 登记素材 + 预检 |
| `mmm task-create --videos a,b,c` | 建任务（顺序即 seq） |
| `mmm run shots/align/vision/index <video_id>` | 阶段1~4 |
| `mmm run narrate/select/render <task_id>` | 阶段5~7 |
| `mmm export-jianying <task_id>` | 剪映草稿 |
| `mmm status / locate / find` | 进度 / 路径直查 / 模糊检索 |
