# mini-movie-maker (mmm)

**长视频浓缩工作流**：把几小时长的游戏剧情录像（带准确台词文稿）+ 解说配音，自动浓缩成十几分钟的解说短视频。

输入：`台词文案 (script.jsonl)` + `游戏录屏 (source.mp4)` → 输出：`解说短视频 (MP4)` 或 `剪映草稿`。

---

## 核心能力

- **7 阶段流水线**：镜头切分 → ASR 台词对齐 → 视觉理解 → 时间轴索引 → 解说稿生成 → 选片段 → 合成导出
- **人工闸口**：解说稿审阅（闸口1）→ 分镜板审阅（闸口2），质量由人把关
- **保留区间**：自然语言指定原始素材哪些位置保留原声（raw_insert），后续内容自动后移
- **双导出器**：ffmpeg 直出 MP4（无人值守）+ 剪映草稿（人工精修通道）
- **输出规格可配**：分辨率/FPS 默认 1920×1080 30fps，支持 overlay（满屏字幕）/ letterbox（上下黑边电影画幅）
- **断点续跑**：各阶段幂等，中途崩溃可续跑，vision 逐镜头落盘
- **结果不覆盖**：每次渲染输出带时间戳，历史版本保留可回退

---

## 环境要求

| 项 | 要求 | 说明 |
|----|------|------|
| 操作系统 | macOS（开发环境）；Linux/Windows 需适配 | ffmpeg/libass 依赖系统 |
| Python | **3.11+**（开发用 3.13） | 推荐 conda/mamba 管理环境 |
| ffmpeg | 需带 **libass**（ASS 硬字幕用） | 见下方安装 |
| 网络 | edge-tts 需联网（验证级 TTS） | 正式 TTS 选型后可替换 |
| LLM 网关 | OpenAI 兼容接口（opencode zen） | 需 `.env` 配置 key |

### Python 依赖

| 依赖 | 用途 | 安装方式 |
|------|------|---------|
| typer / pyyaml | CLI 框架 / 配置解析 | 基础依赖 `pip install -e .` |
| faster-whisper | ASR 转录（medium+VAD） | 可选 `pip install -e ".[asr]"` |
| edge-tts | 验证级语音合成 | 可选 `pip install -e ".[tts-edge]"` |
| pyJianYingDraft | 剪映草稿生成 | 可选 `pip install -e ".[jianying]"` |

---

## 安装（新电脑）

```bash
# 1. 克隆仓库
git clone <仓库地址> movie && cd movie

# 2. 创建 Python 环境（3.11+）
conda create -n mmm python=3.11 -y && conda activate mmm
# 或直接用已有环境

# 3. 安装项目 + 全部依赖
pip install -e .                    # 基础（typer/pyyaml）
pip install -e ".[asr,tts-edge,jianying]"   # 全部可选依赖
# 等价于：
#   pip install faster-whisper av "ctranslate2>=4.0,<5" "onnxruntime>=1.14,<2"
#   pip install edge-tts
#   pip install pyJianYingDraft

# 4. ffmpeg（必须带 libass，否则硬字幕降级）
brew install ffmpeg                 # macOS Homebrew（可能不带 libass）
# 若 brew 版无 libass（.has_ass_filter() 返回 False），下载带 libass 的 static build：
#   arm64: https://ffmpeg.martin-riedl.de/download/macos/arm64/<版本>/ffmpeg.zip + ffprobe.zip
#   解压到 temp/ffmpeg-static/ 下（ffmpeg + ffprobe），管线自动优先使用

# 5. 配置 LLM 网关（.env，已被 gitignore）
cp .env.example .env   # 若存在；否则手动创建
# OPENCODE_ZEN_BASE_URL=<网关地址>
# OPENCODE_ZEN_API_KEY=<你的 key>

# 6. 初始化台账 + 导入素材登记
python3 -m mmm.cli db-init
python3 -m mmm.cli catalog-import

# 7. 放置物料（不进 git）
#   materials/{video_id}/source.mp4     ← 视频文件
#   materials/{video_id}/script.jsonl   ← 台词（工具 tools/md2jsonl.py 可从 BWIKI markdown 转换）
#   catalog.yaml 登记每个 video_id

# 8. 验证
python3 -m mmm.cli status
```

---

## 快速开始（完整流程）

```bash
# 0. 登记素材（catalog.yaml 维护或 mmm add）
mmm add gs-16-p1 --series 原神 --version 1.6 --chapter "盛夏海岛大冒险-P1"

# 1. 建任务（关联一个或多个视频，顺序即 seq）
mmm task-create hd-p1 --videos gs-16-p1 --series 原神

# 2. 阶段1-4：逐视频预处理（断点续跑，可反复执行）
mmm run shots gs-16-p1              # 镜头切分 + 黑白屏检测
mmm run align --task hd-p1          # ASR + 台词对齐（多视频全局对齐）
mmm run vision gs-16-p1             # 抽帧 + 视觉理解（耗时最长，逐镜头落盘）
mmm run index gs-16-p1              # 时间轴索引

# 3. 阶段5：生成解说稿 →【闸口1：审阅 tasks/{id}/narration.md】
mmm run narrate hd-p1 --target-minutes 15

# 4. 阶段6：选片段 →【闸口2：审阅 tasks/{id}/storyboard.html】
mmm run select --task hd-p1

# 5. 阶段7：合成成片（输出带时间戳 + latest 固定名）
mmm run render --task hd-p1            # ffmpeg 直出（默认 overlay 字幕）
mmm run render --task hd-p1 --subtitle letterbox   # 上下黑边电影画幅
mmm export-jianying hd-p1              # 剪映草稿（人工精修）

# 产物
#   output/{task_id}/{title}_YYYYMMDD_HHMMSS.mp4   ← 本次结果（历史保留）
#   output/{task_id}/{title}_latest.mp4            ← 最新版
#   output/{task_id}/bgm.wav + edl.final.json
```

---

## 配置说明

### 系列配置 `config/series/{系列}.yaml`

类型适配层：每种游戏/系列一份，任务创建时继承，task.json 可覆盖。

| 字段 | 说明 |
|------|------|
| `class_table` | 画面分级语义（E→A 选片优先级） |
| `title_template` | 成片命名模板（`{task_id}/{series}/{version}/{chapter}`） |
| `output` | 输出规格 `{width, height, fps}`，默认 1920×1080 30 |
| `transform` | 画面变换（放大裁 LOGO/UID），系列级 + per-clip 覆盖 |
| `subtitle_mode` | `overlay`（满屏硬字幕）/ `letterbox`（上下黑边，字幕落黑边） |
| `bgm_playlist` | BGM 歌单（顺序铺设、循环、ducking） |
| `tts` | 语音合成 `{engine, voice, speed}` |
| `target_minutes` | 目标正片时长（不含保留区间） |

### 任务配置 `tasks/{task_id}/task.json`

继承系列配置生成，可手工/ Skill 覆盖。关键字段：

```jsonc
{
  "task_id": "hd-p1",
  "videos": [{"video_id": "gs-16-p1", "seq": 0}],
  "output": {"width": 1920, "height": 1080, "fps": 30},
  "subtitle_mode": "letterbox",
  "keep_requirements": [           // 保留区间（raw_insert 原声段）
    {"video_id": "gs-16-p1", "start": 300.5, "end": 320.0, "note": "战斗原声"}
  ],
  "bgm_playlist": ["temp/xxx.mp3"],
  "composition": [{"type": "intro_common", "src": "temp/片头测试.mov"}]
}
```

### 保留区间

原始素材指定位置**保留原声原画**（raw_insert）：区间内不排解说，后续内容整体后移。用自然语言经 Skill 配置，或直接编辑 task.json `keep_requirements`。

### 输出规格

所有源素材等比缩放适配目标分辨率。`letterbox` 模式上下 crop 出 2.35:1 电影画幅 + 黑边（左右无黑边，字幕落黑边，右下角 UID 被底部裁剪消除），**默认不做缩放**（除非显式指定 transform）。

---

## Skill 使用（自然语言）

项目附带 `skills/mini-movie-maker`，Agent（Claude Code）可直接用自然语言驱动完整流程：

> "用 P1 到 P5 的素材建一个 15 分钟的任务，1080p，letterbox 黑边，第 5 分钟那场战斗要保留原声，BGM 用 temp 下那两首"

Agent 会：创建任务 → 逐项确认配置（分辨率/FPS/黑边/缩放/BGM/片头/TTS/保留区间）→ 跑流水线 → 到闸口停下让你审阅。

---

## 目录结构

```
├── src/mmm/              # 核心代码（7 阶段 + 台账 + LLM + 导出器）
│   ├── cli.py            # mmm 命令行入口
│   ├── stage_shots.py    # 阶段1 镜头切分
│   ├── stage_asr.py      # 阶段2 ASR + 全局对齐
│   ├── stage_align.py    # 阶段2 对齐算法
│   ├── stage_vision.py   # 阶段3 视觉理解
│   ├── stage_index.py    # 阶段4 时间轴索引
│   ├── stage_narrate.py  # 阶段5 解说稿生成
│   ├── stage_select.py   # 阶段6 选片 + EDL
│   ├── stage_render.py   # 阶段7 导出器A（ffmpeg 直出）
│   ├── stage_jianying.py # 阶段7 导出器B（剪映草稿）
│   ├── stage_bgm.py      # BGM 轨
│   ├── stage_subtitle.py # 字幕
│   ├── stage_compose.py  # 片头拼接
│   ├── catalog.py        # 台账
│   ├── media.py          # ffmpeg/ffprobe 解析 + 探测
│   └── llm.py            # LLM 客户端
├── config/series/        # 系列类型适配层配置
├── materials/            # 物料（video + script.jsonl，gitignore）
├── workspace/            # 中间产物（gitignore）
├── tasks/{task_id}/      # 任务产物（narration/edl/storyboard，gitignore）
├── output/{task_id}/     # 成片（gitignore）
├── temp/                 # 临时素材/static ffmpeg（gitignore）
├── db/schema.sql         # 台账结构
├── catalog.yaml          # 素材登记
└── tools/md2jsonl.py     # BWIKI markdown 台词 → script.jsonl
```

---

## 常见问题

**Q: `mmm` 命令找不到？**
A: 确认激活了安装项目依赖的 Python 环境（conda activate），且执行过 `pip install -e .`。

**Q: 硬字幕没烧进画面（只有软字幕轨）？**
A: 本机 ffmpeg 需带 libass。`python3 -c "from mmm.stage_render import _has_ass_filter; print(_has_ass_filter())"` 返回 `True` 才走 ASS 硬字幕；`False` 时用 SRT 软字幕（播放器需开启字幕）或 drawtext 硬字幕（代码有 fallback）。建议用带 libass 的 static build。

**Q: ASR 需要下载模型？**
A: faster-whisper 首次运行自动下载 medium 模型（约 1.5GB），需联网。之后缓存本地。

**Q: 渲染很慢？**
A: 1080p 成片编码耗时较长（像素约 720p 的 2.25 倍）。TTS 产物会复用，重复跑只重编码画面。可临时降输出规格验证流程。

**Q: 每次跑结果会覆盖吗？**
A: 不会。每次渲染输出带时间戳 `{title}_YYYYMMDD_HHMMSS.mp4`，历史版本全保留，`{title}_latest.mp4` 指向最新。

---

## 技术文档

- 设计文档：`docs/2026/0817-长视频浓缩工作流.md`
- 物料规范：`docs/2026/0817-物料规范.md`
- 验收文档：`docs/2026/08{18,19,20}-项目验收文档.md`（第三方评估参考）
- 进度：`PROGRESS.md`
