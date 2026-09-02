# 解说生成（narrate）Dry/Prod 模式方案

> 状态：待开发（M1）
> 日期：2026-09-02
> 关联：`0824-v1.0.5-多供应商LLM路由与解说模型分层方案.md`、`0828-v1.0.6-TTS适配层与供应商闸口方案.md`

## 1. 背景与问题

### 1.1 现状

解说生成（`stage_narrate.py`）的 LOW/HIGH 是**流水线两段**，不是二选一：

| 环节 | route | 触发条件 | 作用 |
|------|-------|---------|------|
| LOW | `narrate_low` | 内容超长 / 多视频分片时 | 分片摘要，产出 evidence |
| HIGH | `narrate_high` | **必经**（单视频直 prompt 或融合 LOW evidence） | 生成终稿连贯解说词 |

`build_plan`（`stage_narrate.py:823`）写死 `load_endpoint("narrate_high")`——HIGH 环节每次无脑调用高档 LLM。

### 1.2 痛点

部分机器仅用于**验证视频是否值得精做**，需先跑出小样验证结构与时间轴，没必要烧高档 LLM。HIGH 是必经环节无法跳过，每次验证都产生高档调用费用。

### 1.3 对比 TTS

TTS 已有 `profile` 机制（`tts/runtime.py:resolve_profile`）：dry（edge 免费）/ prod（MiniMax 付费），缺省按 `engine` 反推。narrate 缺乏同等机制。

## 2. 方案概述

给 narrate 引入独立 `profile`（dry/prod），控制 HIGH 环节使用哪个 endpoint：

| profile | HIGH 环节 endpoint | 适用场景 |
|---------|-------------------|---------|
| **dry** | `load_endpoint("narrate_low")`（复用 LOW route） | 小样验证、结构/时间轴跑通 |
| **prod** | `load_endpoint("narrate_high")`（保持现状） | 精做终稿 |

- LOW 环节不受影响，始终用 `narrate_low`。
- dry/prod 仅切换 HIGH 环节的 endpoint，环节本身不缺、流水线完整。
- dry 复用 `narrate_low` route，**无需新增环境变量**。

## 3. 详细设计

### 3.1 profile 解析（新增 `resolve_narrate_profile`）

在 `stage_narrate.py` 新增解析函数，对齐 TTS 的 `resolve_profile` 命名与风格：

```python
def resolve_narrate_profile(narrate_cfg: dict | None, profile: str | None = None) -> str:
    """解析 narrate 执行 profile：dry（HIGH 用 LOW LLM）/ prod（HIGH 用 HIGH LLM）。

    优先级：显式传入 > task.json narrate.profile > 缺省 dry。
    缺省 dry：小样验证场景默认省钱，需精做时显式指定 prod。
    """
    mode = str(profile or (narrate_cfg or {}).get("profile") or "").strip().lower()
    if not mode:
        mode = "dry"   # 缺省 dry（与 TTS 缺省逻辑不同：TTS 按 engine 反推，narrate 无 engine 字段）
    if mode not in {"dry", "prod"}:
        raise ValueError(f"narrate profile 只支持 dry/prod，当前: {mode}")
    return mode
```

**缺省值取 dry**（非 TTS 的 engine 反推），理由：narrate 无 `engine` 旧字段可反推，且"验证机器多、精做机器少"是常态，默认省钱符合第一性。

### 3.2 endpoint 选择（改 `build_plan`）

`build_plan` 增加 `profile` 参数，HIGH endpoint 按 profile 选择：

```python
def build_plan(timeline_path: Path, output_dir: Path, *,
               target_minutes: float = 15.0, mode: str = "auto",
               force: bool = False, profile: str = "dry") -> NarratePlan:
    ...
    narrate_profile = resolve_narrate_profile(None, profile)
    # HIGH 环节 endpoint：prod 用 narrate_high，dry 复用 narrate_low（省钱）
    high_route = "narrate_high" if narrate_profile == "prod" else "narrate_low"
    high_endpoint = load_endpoint(high_route)
    ...
```

LOW endpoint 不变（仍 `load_endpoint("narrate_low")`）。

### 3.3 缓存隔离（天然安全，无需改动）

`_high_reuse`（`stage_narrate.py:765`）校验 `high_endpoint.profile_id/model`：
- dry 跑出的终稿标记 `narrate_low` 的 profile/model；
- prod 跑出的标记 `narrate_high` 的；
- 两者指纹不同，**切换 profile 时缓存不串、自动重跑**。

### 3.4 NarratePlan 透传 profile

`NarratePlan` dataclass 增加 `profile: str` 字段，`plan_summary` 输出之，便于 dry-run 展示与日志追溯。

### 3.5 CLI 改动（`cli.py:run_narrate`）

`run_narrate` 增加 `--profile` 参数，透传 `build_plan` 与 `run`：

```python
def run_narrate(
    ...
    mode: str = typer.Option("auto", "--mode", help="auto/segment/oneshot"),
    profile: str = typer.Option("dry", "--profile", help="dry（HIGH 用 LOW LLM 省钱）/ prod（HIGH 用 HIGH LLM 精做）"),
    force: bool = typer.Option(False, "--force", ...),
    dry_run: bool = typer.Option(False, "--dry-run", ...),
):
```

dry-run 输出补充 profile 信息。

### 3.6 `run` 函数改动

`stage_narrate.run` 增加 `profile` 参数，内部同样按 3.2 逻辑选 HIGH endpoint，并透传 `build_plan`（`run` 内部调用 `build_plan`）。

### 3.7 task.json 配置（可选）

task.json 的 `narrate` 块可配置 `profile`（与 TTS 的 `tts.profile` 对齐）：

```json
{
  "narrate": {
    "profile": "prod"
  },
  "tts": {
    "profile": "prod"
  }
}
```

CLI `--profile` 优先级高于 task.json，缺省 dry。

## 4. 改动清单

| 文件 | 改动 |
|------|------|
| `src/mmm/stage_narrate.py` | 新增 `resolve_narrate_profile`；`build_plan`/`run` 加 `profile` 参数，HIGH endpoint 按 profile 选 route；`NarratePlan` 加 `profile` 字段；`plan_summary` 输出 profile |
| `src/mmm/cli.py` | `run_narrate` 加 `--profile` 参数（缺省 dry），透传 `build_plan`/`run`，dry-run 输出补 profile |

不动：`llm.py`（route 机制不变）、`.env`（无新增变量）、`stage_select.py`、`stage_render.py`。

## 5. 风险与回退

| 风险 | 评估 | 对策 |
|------|------|------|
| dry 用 LOW LLM 跑 HIGH 融合，终稿质量下降 | **预期内**，dry 本就是小样验证 | 需精做时显式 `--profile prod` |
| LOW LLM 上下文不足导致 HIGH prompt 超限 | LOW 通常上下文更小 | `_context_available(high_endpoint)` 已有校验，超限会明确报错 |
| 缓存串味 | 无风险 | profile_id/model 指纹隔离（3.3） |
| 缺省 dry 与 TTS 缺省逻辑不一致 | 低 | 文档说明；narrate 无 engine 字段，无法反推 |

回退：`--profile prod` 即恢复原行为；删除新增参数即可完全回退。

## 6. 验收标准

1. `mmm run narrate <task> --dry-run` 默认显示 `profile=dry`，HIGH endpoint 为 `narrate_low` 配置；
2. `mmm run narrate <task> --dry-run --profile prod` 显示 HIGH endpoint 为 `narrate_high` 配置；
3. dry 模式实际跑通，终稿 `narration.json` 的 `models.narrate_high.profile/model` 标记为 LOW 的；
4. dry → prod 切换后重跑，缓存不复用、重新生成；
5. task.json 配 `narrate.profile=prod` 时，不带 `--profile` 也走 prod。
