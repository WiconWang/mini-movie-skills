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
| 阶段3 视觉理解 | ✅ 原型通过 | `stage_vision.py` + `run_vision` CLI；mimo-v2.5 全量分析22镜头，UI/动态/角色识别准确 |
| 阶段4 索引融合 | ✅ 完成 | `stage_index.py` + `run_index` CLI；C级规则修正为高动态不强制台词，验证分布 C=4/A=18 |
| 阶段5 解说稿生成 | ✅ MVP通过 | `stage_narrate.py` + `run_narrate` CLI；deepseek-v4-flash 输出引用台词ID，闸口1 Markdown 已生成 |
| 阶段6 选片+EDL | ✅ MVP通过 | `stage_select.py` + `run_select` CLI；E→A 优先级选取 + 时长匹配；分镜板已生成；TTS 时长暂按字数估算，footage_usage 落 JSON 未接台账 |
| 阶段7 合成导出 | ✅ MVP通过 | `stage_render.py` + `run render` CLI；macOS say(Tingting) 占位 TTS + ffmpeg 片段级音画对齐 + concat 直出；冒烟 20片段→120s MP4；待补：云TTS、剪映导出 |
| 台账闭环 | ✅ 完成 | `mmm add`（物料校验+台词预检）、`task-create`（task_map+task.json+系列配置继承）、阶段4.5 全局时间轴合流 `build_global`（offset 表，合成双视频测试通过）、jobs 打点、narrate/select/render 均支持 task 模式 |
| footage_usage 闭环 | ✅ 完成 | 选片查台账排除已占用镜头（耗尽兜底标 needs_review）；导出时按 EDL 登记 + 归档 edl.final.json |
| 多视频全局对齐 | ✅ 完成 | `run align --task`：逐视频 ASR 断点复用 → offset 拼全局词流 → 整份台词一次对齐 → 拆回各视频本地时间；合成双视频测试通过；顺带修复 load_script 丢失 voiced 字段的 bug |
| 阶段7 打磨 | ✅ 完成 | transform 裁 LOGO（系列级+per-clip 覆盖）、成片命名模板、BGM 轨（loudnorm+crossfade+ducking）、字幕（ASS 硬字幕/SRT 软字幕 fallback）、片头拼接 composition 全部接入；待补仅云TTS |
| 断点续跑 | ✅ 完成 | 各阶段 `--force` + 守卫（jobs=done 且产物锚点存在才跳过）；vision 逐镜头落盘 shots_meta/ 防整批丢失；shots/align/vision/index 补 record_job 打点；index 缺 vision 产物时不标 done |
| 导出器B 剪映草稿 | ⬜ 未开始 | pyJianYingDraft |
| SKILL.md 同步 | ✅ 完成 | 命令手册全量更新；目录已从 .claude/skills/ 迁移至 skills/（CC-Switch 规范） |

## 三、待办任务清单（按优先级）

1. ~~M1 收尾~~（已通过：覆盖率 92.3%，P1+P2 全局对齐验证）
2. **阶段3 视觉理解原型**：PV/海岛视频抽帧喂 mimo-v2.5 → 验证描述/UI检测/分类质量（key 已就位，注意限速）
3. ~~分镜板 reviewer 工具~~（已完成 408f2b2）
4. 合成管线原型：transform 滤镜链（放大裁 LOGO）、片头拼接、字幕烧录、BGM crossfade（可用 PV 验证）
5. ~~阶段2 与 CLI 集成~~（已完成，`mmm run align`）
6. ASR 备选实测：mlx large-v3-turbo（可能兼顾速度与质量）
7. SKILL.md 完善：随阶段实现同步更新命令手册
