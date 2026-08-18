# PROGRESS · 进度与迁移指南

> 最后更新：2026-08-17
> 用途：跨机迁移后还原进度；日常记录当前所处阶段与下一步。

---

## 一、迁移还原步骤（新机器上执行）

```bash
# 1. 克隆仓库
git clone <仓库地址> movie && cd movie

# 2. 系统依赖：ffmpeg（必装）
brew install ffmpeg          # macOS；其他平台自行安装

# 3. Python 3.11+ 环境
pip install -e .             # mmm CLI + 基础依赖（typer/pyyaml）
pip install faster-whisper av "ctranslate2>=4.0,<5" "onnxruntime>=1.14,<2"   # ASR 用

# 4. 重建台账（结构来自 db/schema.sql，数据来自 catalog.yaml）
python3 -c "import sys; sys.path.insert(0,'src'); from mmm import db; db.init_db()"
#   或安装后：mmm db-init && mmm catalog-import

# 5. 放置物料（不进 git，需单独拷贝）
#    materials/{video_id}/source.mp4 + script.jsonl   ← 按 catalog.yaml 的 path 对应
#    assets/intros/ assets/bgm/                       ← 片头与 BGM

# 6. 验证
python3 tests/test_align.py   # 对齐算法回归测试
mmm status                    # 台账就绪
```

**迁移检查单**：git 仓库 + ffmpeg + Python 依赖 + `mmm db-init` + 物料拷贝。其中物料是体积主体，建议整块硬盘/rsync 拷贝。

## 二、当前进度（截至 2026-08-17）

| 模块 | 状态 | 说明 |
|------|------|------|
| 方案文档 v2 | ✅ 定稿 | `docs/2026/0817-长视频浓缩工作流.md`（十一章） |
| 术语表 / 物料规范 | ✅ 定稿 | `CONTEXT.md`、`docs/2026/0817-物料规范.md` |
| 台账 schema | ✅ 完成 | `db/schema.sql` 四表，重建已验证 |
| 阶段1 镜头检测 | ✅ 原型通过 | `stage_shots.py`，3 部 PV 冒烟通过（阈值 0.3） |
| 阶段2 对齐算法 | ✅ 完成（未接真机） | `stage_align.py`，合成测试三场景全绿 |
| ASR 选型实测 | ✅ 完成 | **定型：medium+VAD+关上下文连锁（25倍实时无幻觉）**；small 4.7x 但幻觉严重已淘汰 |
| CLI 管理命令 | ✅ 完成 | db-init / catalog-import / status / locate / find |
| 阶段2 真机集成 | ✅ M1 通过 | 真机验收：多视频全局对齐覆盖率 92.3%，ASR 定型 medium+VAD（25x） |
| 阶段3 视觉理解 | ⬜ 待开始 | key 已到位（.env），注意 zen 风控：限速+退避已实现于 llm.py |
| 分镜板 reviewer | ✅ 完成 | 固化 HTML 工具 + 构建器，浏览器实测通过 |
| 阶段5~7 | ⬜ 未开始 | 依赖前序 |

## 三、待办任务清单（按优先级）

1. ~~M1 收尾~~（已通过：覆盖率 92.3%，P1+P2 全局对齐验证）
2. **阶段3 视觉理解原型**：PV/海岛视频抽帧喂 mimo-v2.5 → 验证描述/UI检测/分类质量（key 已就位，注意限速）
3. ~~分镜板 reviewer 工具~~（已完成 408f2b2）
4. 合成管线原型：transform 滤镜链（放大裁 LOGO）、片头拼接、字幕烧录、BGM crossfade（可用 PV 验证）
5. ~~阶段2 与 CLI 集成~~（已完成，`mmm run align`）
6. ASR 备选实测：mlx large-v3-turbo（可能兼顾速度与质量）
7. SKILL.md 完善：随阶段实现同步更新命令手册
