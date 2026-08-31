---
name: mini-movie-maker
description: 长视频浓缩工作流。将几小时长视频（+准确台词）浓缩为十几分钟解说短视频：镜头检测→台词对齐→视觉理解→写稿→选片→合成，双导出（MP4/剪映草稿）。当用户要求处理视频、生成解说视频、跑浓缩管线时使用。
---

# mini-movie-maker

设计文档：`docs/2026/0817-长视频浓缩工作流.md`（唯一事实源）
术语表：`CONTEXT.md`　物料契约：`docs/2026/0817-物料规范.md`
全项目流程图：`docs/2026/0828-全项目流程图.md`

## 流程总览

```
登记(add) → 建任务(task-create) → shots → align → vision → index → narrate →【闸口1】→ select →【闸口2】→ tts-plan →【闸口3】→ tts → render / export-jianying
```

> **shots / vision 可提前于任务创建**：这两个阶段是 video 级，仅依赖 `source.mp4`（vision 还需 shots 产物），不读 task.json、不读 BGM/黑边等配置。拿到视频即可先跑，待配置确认后再 `add → task-create → index`，index 自动复用已有 `shots_meta.json`。详见「视觉预处理（可提前）」。

## 闸口协议（铁律）

1. `mmm run narrate` 完成后**必须停下**，通知用户审 `tasks/{task_id}/narration.md`，不得擅自执行 `select`
2. `mmm run select` 完成后**必须停下**，通知用户审 `storyboard.html`，用户可能已手改 `edl.json`
3. `mmm run tts-plan` 完成后**必须停下**，通知用户审 `tasks/{task_id}/tts_plan.html`
4. 闸口3 必须逐句核对术语发音、停顿、语气、情绪；LLM 只能标注表演意图，**不得修改解说稿文本**
5. 闸口3 报告必须给用户看**中文名词**：`gasps` 显示为「倒吸气」，`sighs` 显示为「叹气」；英文协议值只保留在内部 JSON 和供应商请求里
6. 闸口3 必须明确告知费用：dry 的 Edge TTS 免费，但生成计划的 LLM 调用可能已计费；prod 默认 MiniMax，确认后将按字符产生 TTS 费用，失败重试可能再次计费
7. 用户必须明确说「确认，接受费用」这类话术后，Agent 才能执行 `mmm tts-approve`；仅说「看一下」「先跑」不算确认
8. 用户口头确认「通过」即闸门开关；继续前校验产物（JSON 可解析、引用 ID 存在、TTS plan 指纹一致）
9. 可以替用户做的：读打回意见重跑、重跑受影响片段、解释选镜理由；不得代替用户接受付费责任

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
| TTS 模式 | dry 固定 Edge；prod 指定供应商，默认 MiniMax | dry |
| prod TTS 供应商/模型 | 正式合成供应商与模型 | minimax / speech-2.8-hd |
| prod 音色 | 必须是账户侧确认可用的 voice_id | 空（必须显式配置） |
| TTS 语速 | dry 与 prod 的基础语速 | 1.1 / 1.0 |
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
| `mmm run shots <video_id>` | 阶段1：镜头切分 + 黑白屏检测（仅需 source.mp4，可提前于任务创建） |
| `mmm run align <video_id>` 或 `mmm run align --task <task_id>` | 阶段2：ASR + 台词对齐；多视频任务全局对齐。`--task` 模式复用各视频已落盘的 `asr.json`，转录过的不重跑 |
| `mmm run vision <video_id>` | 阶段3：抽帧 + 视觉理解（mimo-v2.5）；仅需 source.mp4 + shots 产物，可提前于任务创建 |
| `mmm run index <video_id>` | 阶段4：多信号融合 → timeline.json |
| `mmm run narrate <task_id>` | 阶段5：解说稿生成 → 闸口1 |
| `mmm run select <video_id> --task <task_id>` | 阶段6：选片 + footage_usage 排除 + 分镜板 → 闸口2（任务模式必须带 `--task`，否则按单视频 workspace 解析） |
| `mmm run tts-plan --task <task_id> [--profile dry\|prod]` | 阶段6.5：LLM 生成发音/停顿/语气/情绪标注 → 闸口3 |
| `mmm tts-approve --task <task_id> --plan-sha256 <sha256>` | 记录用户对 TTS 表演计划的显式确认 |
| `mmm run tts --task <task_id>` | 阶段6.6：完整合成一次，按时间戳切回 `tts_XXX.wav` |
| `mmm run render --task <task_id>` | 阶段7：ffmpeg 直出（含 transform/BGM/字幕/片头拼接） |
| `mmm export-jianying <task_id>` | 导出器B：剪映草稿；只复用已生成的 TTS 片段，缺失时失败 |
| `mmm status / locate / find` | 进度 / 路径直查 / 模糊检索 |

**断点续跑**：所有 `mmm run` / `export-jianying` 阶段命令启动时自动检查 jobs 表——
已完成（done/gate_waiting）且产物存在则跳过，加 `--force` 强制重跑。
vision 阶段逐镜头落盘 `shots_meta/shot_XXX.json`，中断重跑只补缺失/失败镜头。

## 视觉预处理（可提前）

shots / vision 是 **video 级**阶段，输入只有 `source.mp4`（vision 额外需 `shots.json`），**不依赖** task、台词、BGM、黑边或任何剪辑配置。因此可以在 BGM / 黑边 / 片头尚未敲定时提前跑，产物落 `workspace/{video_id}/`，后续 `index` 自动复用。

适用场景：拿到视频先做重活，配置确认后再建任务；夜间批量预处理多个视频。

```bash
# 物料就绪即可（materials/{video_id}/source.mp4 存在，无需 mmm add / task-create）
mmm run shots <video_id>     # 先切镜头 → shots.json
mmm run vision <video_id>    # 再视觉理解 → shots_meta.json（吃 shots 产物）

# 台词也到手时，可顺带提前 ASR（单视频模式落盘 asr.json，供后续 align --task 复用，不重跑）
mmm run align <video_id>
```

随后正式建任务时，`align --task` 会复用夜间落盘的 `asr.json`，`index` 会复用 `shots_meta.json`——**不要对这两个阶段加 `--force`**，否则会白白重跑夜间已完成的重活。

批量预处理（夜间挂多个视频）：

```bash
for vid in gs-16-p1 gs-16-p2 gs-16-p3; do
  mmm run shots  "$vid"
  mmm run vision "$vid"
done
```

> 注：`mmm run shots/vision` 未 `mmm add` 也能跑（产物落在 `workspace/{video_id}/`）；但后续 `align`、`select`、`render` 等需任务上下文的阶段仍要求 `mmm add` + `task-create` 完成。

## 冒烟测试入口（免台账）

用于开发调试，跳过 catalog/task。任务模式的 dry/prod 统一 TTS 闸口不适用于这些入口：

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

### TTS 适配层

- dry 固定 `provider: edge`，走 `full_then_split`
- prod 默认 `provider: minimax`，可替换为后续接入的其他供应商
- dry 和 prod 的主流程相同：完整合成 → 字/词时间轴 → 切回 `tts_XXX.wav` → 渲染/剪映
- LLM 只输出 provider 无关的表演计划；Edge 不支持的能力在闸口报告中明确显示降级
- MiniMax API Key 放 `.env` 的 `MMM_MINIMAX_TTS_API_KEY`
- 如需独立 TTS 计划模型，配置 `MMM_TTS_PLAN_PROFILE/MODEL/BASE_URL/API_KEY`；未配置时运行时回退 `narrate_low`

### 剪映导出边界（铁律）

1. `export-jianying` 不需要先生成完整 MP4。
2. `export-jianying` 不允许隐式触发 TTS。
3. 任务模式必须已有 `render_segments/tts_artifacts.json` 和全部 `tts_XXX.wav`。
4. 缺失或指纹不一致时直接报错，提示用户执行 `mmm run tts --task <task_id>`。
5. 推荐顺序固定为：闸口3 → `run tts` → `run render` 或 `export-jianying`。

## 闸口产物位置

- 闸口1：`tasks/{task_id}/narration.md`（解说稿审阅）
- 闸口2：`tasks/{task_id}/storyboard.html`（分镜板）
- 闸口3：`tasks/{task_id}/tts_plan.html` + `tts_plan.approved.json`（付费 TTS 前确认）
- 最终成片：`output/{task_id}/{title}.mp4`（含 `edl.final.json` 归档）
