---
name: mini-movie-maker
description: 长视频浓缩工作流。支持两种模式：A 模式（TTS 解说短视频）与 B 模式（原声高光直拼，无解说无配音，LOW LLM 标注挑选，成本低）。当用户要求处理视频、生成解说视频、原声直拼、跑浓缩管线时使用。
---

# mini-movie-maker

设计文档：`docs/2026/0817-长视频浓缩工作流.md`（唯一事实源）
术语表：`CONTEXT.md`　物料契约：统一台账与命名规范（`game-storyline-pipeline/docs/2026/0910-统一素材台账与命名规范.md` §7）
全项目流程图：`docs/2026/0828-全项目流程图.md`
B 模式方案：`docs/2026/0905-B模式原声高光直拼方案.md`（v1.1.0 评审定稿）

## 流程总览

```
A 模式：登记 → 建任务 → shots → align → vision → index → narrate →【闸口1】→ select →【闸口2】→ tts-plan →【闸口3】→ tts → render / export-jianying
B 模式：登记 → 建任务 → shots → align → vision → index → select --mode raw →【闸口2】→ render
                                                    （narrate/tts 全跳过，0 次 HIGH LLM）
```

> **shots / vision 可提前于任务创建**：这两个阶段是 asset 级，仅依赖视频文件（vision 还需 shots 产物），不读 task.json、不读 BGM/黑边等配置。拿到视频即可先跑，待配置确认后再 `add-asset → task-create --claim → index`，index 自动复用已有 `shots_meta.json`。详见「视觉预处理（可提前）」。

> **台词定位须在 align 之后**：`mmm locate-keep` 依赖阶段2 `asr.json`，用于把「保留某句台词」的自然语言翻译成 `keep_requirements` 时间区间；目标视频未 ASR 时先 `mmm run align`。

## A/B 模式选择（任务开始必问）

任务开始时 Agent 必须先判断/询问 A/B 模式：

1. **解析自然语言**：用户提到"原声高光/不要解说/原声直拼/原文对白拼接" → 直接 B；提到"解说/总结/配音/TTS" → 直接 A
2. **不明确时 `AskUserQuestion`**：附 A/B 简述（A：LLM 解说+TTS 配音，链路重；B：原声高光直拼，无解说无配音，LOW LLM 标注挑选，0 次 HIGH 调用，成本低）
3. **弃选默认 B**：典型路径是"先用 B 出低成本快照看底子，值得讲再补 A"
4. **选完模式后逐段确认成片模板**（四段式，见下节）
5. **先 A 后 B**：建两个任务认领同一 quest（`--mode narrate` 与 `--mode raw` 各一次，task_id 以 `-b` 后缀区分）；B 任务软链 A 的 `narration_segments`（含 line_marks），跳过 low 标注（0 LLM）直接 select-raw。先 B 后 A 同理反向（A 补跑 1 次 high 终稿）

```bash
# A 模式（task_id 无后缀）
mmm task-create --game genshin --version 1.6 --slug midsummer-islands --mode narrate
# B 模式（同素材，task_id 自动加 -b 后缀）
mmm task-create --game genshin --version 1.6 --slug midsummer-islands --mode raw
# 先A后B：B 任务复用 A 的 segments（软链，缓存指纹匹配则 0 次 LLM）
ln -s ../genshin-1.6-midsummer-islands/narration_segments tasks/genshin-1.6-midsummer-islands-b/narration_segments
```

## 成片模板四段式（A/B 共用）

固定顺序：**Cover（封面图·默认2s）→ 片头（外部视频·可选 transform）→ 正片 body → 片尾（图片·默认2s）**。task.json `composition` 列表未声明的段缺省跳过；视频片尾彩蛋（outro_special）已废弃（ADR-0001），原声结尾片段改以 raw_insert 纳入 EDL。

**选完 A/B 后逐段确认**（用户可一次答多项，如"要封面，不要片头，要片尾"）：

| 段 | 内容 | 询问要点 |
|---|---|---|
| Cover | intro-maker 产出的 1920×1080 JPG | 要不要封面？给路径 |
| 片头 | 外部提供视频（登记为 intro 资产，claim 自动引用） | 用哪个片头？可选 transform 缩放/位移 |
| body | 解说正片（A）/ raw_insert 拼接（B） | 恒有，不询问 |
| 片尾 | 静态图片（与 Cover 同构） | 要不要片尾？给图片路径 |

据答登记为版本级资产（`mmm add-asset --kind cover/outro/intro/bgm`），`task-create --claim` 自动以 asset_id 引用写入 task.json `composition`（`cover`/`intro`/`outro` 类型；B 模式典型为 Cover + raw_insert 拼接 + 片尾，无片头）。

## 闸口协议（铁律）

> **B 模式（mode=raw）仅闸口2**：narrate/tts-plan/tts 全跳过（无解说终稿、无配音）。`select --mode raw` 完成后停在闸口2 审 storyboard.html，确认后直接 render。B 模式 EDL 全 raw_insert（纯原声），render 自动跳过 TTS 闸口。

1. `mmm run narrate` 完成后**必须停下**，通知用户审 `tasks/{task_id}/narration.md`，不得擅自执行 `select`。dry 模式须提示"这是 LOW LLM 出的验证小样稿，精做终稿需 `--profile prod` 重跑"。**B 模式任务自动跳过此闸口**（quality 标注由 select-raw 兜底）
2. `mmm run select` 完成后**必须停下**，通知用户审 `storyboard.html`，用户可能已手改 `edl.json`。B 模式分镜板展示每条高光段的时间/说话人/原文/quality 徽章+reason/首帧预览，支持调边界、删除、板上插入高光段
3. `mmm run tts-plan` 完成后**必须停下**，通知用户审 `tasks/{task_id}/tts_plan.html`
4. 闸口3 必须逐句核对术语发音、停顿、语气、情绪；TTS 计划按句号/问号/感叹号/分号拆成句级标注，一个 EDL 解说片段会拆成多行；LLM 只能标注表演意图，**不得修改解说稿文本**
5. 闸口3 报告必须给用户看**中文名词**：`gasps` 显示为「倒吸气」，`sighs` 显示为「叹气」；英文协议值只保留在内部 JSON 和供应商请求里
6. 闸口3 必须明确告知费用：dry 的 Edge TTS 免费，但生成计划的 LLM 调用可能已计费；prod 默认 MiniMax，确认后将按字符产生 TTS 费用，失败重试可能再次计费
7. 用户必须明确说「确认，接受费用」这类话术后，Agent 才能执行 `mmm tts-approve`；仅说「看一下」「先跑」不算确认
8. 用户口头确认「通过」即闸门开关；继续前校验产物（JSON 可解析、引用 ID 存在、TTS plan 指纹一致）
9. 可以替用户做的：读打回意见重跑、重跑受影响片段、解释选镜理由；不得代替用户接受付费责任

## 关键约束

- 素材文件入库后只读（`add-asset` 复制进 `{game}/{version}/` 树），原视频永不被修改
- 中间产物写 `workspace/{asset_key}/`，任务产物写 `tasks/{task_id}/`，成品写 `output/{task_id}/`，均落 `$MINIMOVIE_DATA_ROOT`
- 一切定位用 `(asset_id, 源内时间)`，禁止成片绝对时间（相对时间轴铁律）
- 台账走统一 `ledger.sqlite`（结构见 `db/schema.sql`）；取素材一律查台账，不翻目录

## 剪辑前配置确认（铁律：必须逐项询问）

**任务创建后、开始剪辑前，Agent 必须逐项询问用户确认以下配置**（用户不直接改配置文件，全部通过自然语言答复）。用户不提供某项时用游戏默认值：

| 配置项 | 含义 | 游戏默认（genshin） |
|--------|------|-----------------|
| 输出分辨率 / FPS | 成片规格，所有素材适配 | 1920×1080 / 30fps |
| 黑边（letterbox） | 上下加黑边电影画幅，还是满屏 | overlay（满屏硬字幕） |
| overlay 画面适配 | 是否放大裁 LOGO/UID（overlay 模式），scale/offset | scale 1.024 / 上移 12.96（UID 出画） |
| 字幕模式 | overlay 硬字幕 / letterbox / none | overlay |
| 字幕字体 | 解说字幕字体（ASS Fontname） | LXGW WenKai Medium |
| BGM 歌单 | 背景音乐列表（claim 自动引用已登记版本 BGM，此处确认/调序） | 空（须指定） |
| 片头（composition） | 是否拼片头（claim 自动引用已登记版本 intro，此处确认） | 空（须指定） |
| 解说模式 | dry（HIGH 融合用 LOW LLM 省钱出小样）/ prod（HIGH LLM 精做终稿） | dry |
| TTS 模式 | dry 固定 Edge；prod 指定供应商，默认 MiniMax | dry |
| prod TTS 供应商/模型 | 正式合成供应商与模型 | minimax / speech-2.8-hd |
| prod 音色 | 必须是账户侧确认可用的 voice_id | 空（必须显式配置） |
| TTS 语速 | dry 与 prod 的基础语速 | 1.1 / 1.0 |
| 目标时长 | 正片分钟数（不含 raw_insert） | 15 |
| 保留区间 | raw_insert 原声段（见下节） | 空 |

**执行方式**：

1. **模式确认（必答，不得用默认值带过）**：Agent 先直接问"本次是 dry（验证小样，解说用 LOW LLM、TTS 用 Edge，基本不产生费用）还是 prod（精做终稿，解说用 HIGH LLM、TTS 用 MiniMax，按量计费）？"。用户必须明确回答 dry 或 prod；解说与 TTS 默认同档，用户也可分开指定（如"解说 prod、TTS dry"）。prod 模式须额外提醒费用风险。
2. **其余配置逐项确认**：Agent 逐项列出默认值 + 询问"是否调整"，用户可用自然语言一次答多项（如"1080p，加黑边，BGM 用这两首，第300秒要原声"）。用户不提供某项时用系列默认值。

所有答复写入 task.json，确认完毕后开始流水线。

## 保留区间配置（raw_insert）

用户可用自然语言指定「原始素材哪些位置要保留原声/保留画面」，写入 `tasks/{task_id}/task.json` 的 `keep_requirements` 数组，select 阶段自动生成 raw_insert 片段（原声原画）并入 EDL。

**两种指定方式**
- 时间区间：直接给 `start/end`（如上），写入 task.json。
- 台词定位：用户只说台词（允许不完全准确），程序在目标资产
  `workspace/{asset_key}/asr.json` 的词级时间戳里模糊匹配
  （去标点/空白 → 精确子串 → 编辑距离兜底），回填
  `{asset_id, start, end}`，`note` 写用户原话。

**时序铁律**：台词定位依赖阶段2 ASR 产物，必须在 `mmm run align`
之后执行；目标视频尚未 ASR 时，命令提示「先跑 align」。ASR 为本地
faster-whisper，不按量计费，但需模型权重与转录耗时，故不隐式触发。
`asr.json` 已是源视频本地秒，定位结果可直接写入 keep_requirements。

```json
"keep_requirements": [
  {"asset_id": 1, "start": 300.5, "end": 320.0, "note": "第5分钟战斗原声"},
  {"asset_id": 1, "start": 600.0, "end": 615.0, "note": "名场面保留"}
]
```

- `start/end` 为**源视频内本地秒数**（相对时间轴铁律）
- 区间内不排解说句；解说片段与保留区间重叠时自动裁剪避让
- 插入后**后续内容整体后移**（成片时间轴自动累计，零成本）
- BGM 全局 50%，raw_insert 段额外压低（-26dB），**区间结束自动恢复**
- 操作方式：用户自然语言说明 →（时间区间直接写 / 台词经 `mmm locate-keep` 定位）→ 写入 task.json → 重跑 `mmm run select --task <id>` + `mmm run render --task <id>`

## 命令手册

| 命令 | 用途 |
|------|------|
| `mmm db-init` | 初始化统一台账（迁移后第一步） |
| `mmm add-asset --game <code> --version <no> --slug <quest> --kind video --seg <N> --src <文件>`（dialog/bgm/cover/outro/intro 同理） | 登记素材 + 台词预检 + sha256，落新目录树并写 ledger |
| `mmm task-create --game <code> --version <no> --slug <quest> [--mode narrate\|raw] [--variant -v2]` | 认领 quest（video+dialog 写 task_asset，版本物料以 asset_id 引用），生成 task.json。禁止手写 task_id。`--mode raw`（B 模式）subtitle_mode 缺省 none、写入 raw_select 配置块 |
| `mmm run shots <asset_key>` | 阶段1：镜头切分 + 黑白屏检测（仅需视频文件，可提前于任务创建） |
| `mmm run align <asset_key>` 或 `mmm run align --task <task_id>` | 阶段2：ASR + 台词对齐；多资产任务全局对齐。`--task` 模式复用各资产已落盘的 `asr.json`，转录过的不重跑 |
| `mmm locate-keep <task_id> --quote "<台词>" [--asset <asset_key>]` | 阶段2后：把用户台词（允许不完全准确）模糊定位到源视频本地秒，输出/写入 `keep_requirements`；未 ASR 提示先跑 `align` |
| `mmm run vision <asset_key>` | 阶段3：抽帧 + 视觉理解（mimo-v2.5）；仅需视频文件 + shots 产物，可提前于任务创建 |
| `mmm run index <asset_key>` | 阶段4：多信号融合 → timeline.json |
| `mmm run narrate <task_id> [--profile dry\|prod]` | 阶段5：解说稿生成 → 闸口1。`--profile dry`（默认）HIGH 融合环节用 LOW LLM 省钱出小样；`prod` 用 HIGH LLM 精做终稿 |
| `mmm run select <asset_key> --task <task_id> [--mode narrate\|raw]` | 阶段6：选片 + 分镜板 → 闸口2（任务模式必须带 `--task`）。`--mode raw`（或 task.json mode=raw）走 B 模式：quality+画面双维度选片，全 raw_insert EDL；无 segments 时自动跑 narrate-low-only 兜底（0 次 HIGH） |
| `mmm run tts-plan --task <task_id> [--profile dry\|prod]` | 阶段6.5：按句拆分，LLM 逐句生成发音/停顿/语气/情绪标注 → 闸口3 |
| `mmm tts-approve --task <task_id> --plan-sha256 <sha256>` | 记录用户对 TTS 表演计划的显式确认 |
| `mmm run tts --task <task_id>` | 阶段6.6：完整合成一次，按词级时间轴切回句级 WAV，再合并回 EDL 片段 |
| `mmm run render --task <task_id>` | 阶段7：ffmpeg 直出（含 transform/BGM/字幕/片头拼接） |
| `mmm export-jianying <task_id>` | 导出器B：剪映草稿；只复用已生成的 TTS 片段，缺失时失败 |
| `mmm status / locate / find` | 进度 / 路径直查 / 模糊检索 |

**断点续跑**：所有 `mmm run` / `export-jianying` 阶段命令启动时自动检查 jobs 表——
已完成（done/gate_waiting）且产物存在则跳过，加 `--force` 强制重跑。
vision 阶段逐镜头落盘 `shots_meta/shot_XXX.json`，中断重跑只补缺失/失败镜头。

## 视觉预处理（可提前）

shots / vision 是 **asset 级**阶段，输入只有视频文件（vision 额外需 `shots.json`），**不依赖** task、台词、BGM、黑边或任何剪辑配置。因此可以在 BGM / 黑边 / 片头尚未敲定时提前跑，产物落 `workspace/{asset_key}/`，后续 `index` 自动复用。

适用场景：拿到视频先做重活，配置确认后再建任务；夜间批量预处理多个视频。

```bash
# 登记后即可（add-asset 已落盘，无需 task-create）
mmm run shots <asset_key>     # 先切镜头 → shots.json
mmm run vision <asset_key>    # 再视觉理解 → shots_meta.json（吃 shots 产物）

# 台词也到手时，可顺带提前 ASR（单资产模式落盘 asr.json，供后续 align --task 复用，不重跑）
mmm run align <asset_key>
```

随后正式建任务时，`align --task` 会复用夜间落盘的 `asr.json`，`index` 会复用 `shots_meta.json`——**不要对这两个阶段加 `--force`**，否则会白白重跑夜间已完成的重活。

批量预处理（夜间挂多个视频）：

```bash
for key in genshin-1.6-midsummer-islands-p001 genshin-1.6-midsummer-islands-p002; do
  mmm run shots  "$key"
  mmm run vision "$key"
done
```

> 注：`mmm run shots/vision` 登记后（`add-asset`）即可跑（产物落在 `workspace/{asset_key}/`）；但后续 `align --task`、`select`、`render` 等需任务上下文的阶段仍要求 `task-create --claim` 完成。

## 冒烟测试入口（免台账）

用于开发调试，跳过 catalog/task。任务模式的 dry/prod 统一 TTS 闸口不适用于这些入口：

```bash
mmm run shots --path <视频文件>
mmm run align --path <视频> --script <台词.jsonl>
mmm run index --path <workspace 目录>
mmm run narrate --timeline <timeline.json> [--target-minutes N] [--profile dry|prod]
mmm run select --path <workspace 目录>
mmm run render --path <workspace> --video <视频> [--bgm "a.mp3;b.mp3"] [--subtitle overlay]
```

## 游戏配置（类型适配层）

`config/game/{game}.yaml`（game code：genshin/zzz/starrail/wave/endfield）控制分级表、命名模板、subtitle_mode、TTS 音色，以及 overlay 字幕样式与画面适配。其中：

- `subtitle`：overlay 字幕样式（`font_name`/`font_size`/`outline`/`margin_v`）+ 画面适配 `overlay_transform`（放大裁 UID）+ 底部羽化模糊遮罩 `overlay_mask`（含 `x`/`y`/`blur_sigma`/`feather_top`）。认领时经 `catalog.claim_task` 继承到 task.json，任务/片段可覆盖。
- `subtitle_mode`：`overlay`（默认，含底部模糊遮罩）/ `letterbox`（黑边电影画幅，未实现）。
- 字体文件位于 `assets/fonts/`（当前仅 `LXGWWenKai-Medium.ttf`）。渲染时通过运行时临时 fontconfig（`FONTCONFIG_FILE`）命中，不注册系统字体，保证跨机器可复现。

### BGM / 片头物料化（版本物料，非游戏配置）

BGM 与片头强版本相关，每版本都换，**不是游戏级配置**，不写入 game yaml。登记为版本级资产，认领时自动引用：

- `mmm add-asset --game <code> --version <no> --slug <quest> --kind bgm --src <音频>`（mp3/wav/m4a/flac，一版本多首各一行 asset）
- `mmm add-asset ... --kind cover/outro --src <图片>`（1920×1080 JPG，intro-maker 成品；底图每次手动 `--bg` 指定，不入库）
- `mmm add-asset ... --kind intro --src <视频>`（外部片头视频，只登记不生产）
- `task-create --claim` 自动把已登记版本物料以 asset_id 写入 task.json（`bgm_playlist: [{asset_id}]`，`composition: [{type, asset_id}]`）；未登记 → 清单为空（BGM 走静音轨兜底、片头走无片头），不报错。
- 剪辑前配置确认时，Agent 读 task.json 已生成清单，列文件名给用户确认/调顺序；缺物料时补 `add-asset` 后重跑 `task-create --claim`（幂等覆盖）。
- 下游 `stage_bgm` / `stage_compose` 只认 asset_id 引用，与来源无关；BGM 时长由 TTS 语音总长钉死（`render_segment` 片段时长 = `max(TTS时长, 0.5)`），不够循环、超过裁剪，无需单独配置。

### TTS 适配层

- dry 固定 `provider: edge`，走 `full_then_split`
- prod 默认 `provider: minimax`，可替换为后续接入的其他供应商
- dry 和 prod 的主流程相同：完整合成 → 字/词时间轴 → 切回句级 WAV → 合并回 EDL 片段 → 渲染/剪映
- TTS 计划按句拆分：句号/问号/感叹号/分号等作为句子边界，逐句标注停顿、语气、情绪、发音
- LLM 只输出 provider 无关的表演计划；Edge 不支持的能力在闸口报告中明确显示降级
- MiniMax API Key 放 `.env` 的 `MMM_MINIMAX_TTS_API_KEY`
- 如需独立 TTS 计划模型，配置 `MMM_TTS_PLAN_PROFILE/MODEL/BASE_URL/API_KEY`；未配置时运行时回退 `narrate_low`

### TTS 发音词库（兜底）

- 词库文件：`config/tts/{game}.yaml`，按 `common` + `versions.{版本}` 分层
- 词库只做兜底：LLM 已给出的发音规则优先，词库仅在 LLM 未识别该词时补齐
- 拼音格式统一为带声调数字（如 `an1 bo2`），格式非法时计划生成会报错
- 修改词库后重新执行 `mmm run tts-plan --task <task_id> --force` 才会进入新计划

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
