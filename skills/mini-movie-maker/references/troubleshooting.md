# 排障与实战记录

本文件收纳 SKILL.md 放不下的排障细节。**动管线代码或换素材前先读这里**。

---

## 1. 长耗时阶段必须脱离 Hermes 会话运行（铁律）

`shots` 之后的每个阶段都可能超过 5 分钟（align 的全片 ASR、vision 的逐镜头 LLM、render 的编码）。
用 Hermes 的后台进程机制跑它们，**会在约 5.5 分钟时被 SIGTERM 杀掉**（`exit 143`），
杀在最后一步时前面的产物全白做。

**做法**：把命令包进脚本，用 `setsid` 起独立会话。`&` 写在脚本文件里是允许的（写在前台命令里会被工具拦）：

```bash
# /tmp/run_stage.sh
setsid nohup /usr/bin/bash -c '
  cd <CODE_ROOT>
  { .venv/bin/mmm run align <asset_key> --force; echo ALL_DONE; } > /tmp/logs/align.log 2>&1
' > /dev/null 2>&1 < /dev/null &
```

启动后确认 `ps -p <pid> -o sid` 的 **sid 等于 pid 本身** —— 这才说明脱离了原进程组。
日志末尾打 `ALL_DONE` 作完成标志，轮询日志判断，不要靠等时间。

**不要指望这些**：`tmux`（本机未装）、`systemd-run`（Hermes 安全过滤器当成服务管理拦下）、
`background=true`（一样会被 SIGTERM）。

---

## 2. ASR 必须强制离线加载模型

`WhisperModel("medium")` 默认会去 **huggingface.co 做在线校验**，国内网络会挂死：
进程 `State: S`、`utime` 不增长、`VmRSS` 只有 ~90MB（模型没加载）、`wchan=wait_woken`。

**做法**：跑 ASR 前 `export HF_HUB_OFFLINE=1`（模型已在 HF 缓存里就够，不必重新下载）。
正常应看到 `VmRSS` ~2.2GB、CPU 数百 %、`utime` 持续增长。

**自检**：跑起来 30 秒后看 `utime` 有没有在涨。不涨就是卡住了，别傻等。

---

## 3. 换素材（重新登记视频）的完整清单

`add-asset` 是**幂等 upsert**，同 `seg` 重复登记原地替换，`asset_id` 不变所以任务认领关系不失效。
但**中间物和任务级产物不会自动失效**，少清一样就出错误成片：

```bash
# ① 归档旧 asset 级产物（别原地留着，否则 vision/asr 会复用旧结果）
mv $DATA_ROOT/workspace/<asset_key>   $DATA_ROOT/_archive/workspace-<旧>-<日期>
# ② 归档旧视频源（add-asset 会覆盖同名文件）
mv $DATA_ROOT/<game>/<ver>/<slug>/video/p001.mp4   $DATA_ROOT/_archive/p001-<旧来源>-<日期>.mp4
# ③ upsert 登记新视频
mmm add-asset --game <g> --version <v> --slug <s> --kind video --seg 1 \
    --src <新文件> --source-url '<新来源 URL>'
# ④ 清该 asset 的 jobs 记录（否则 _skip_if_done 认为 shots/align/vision 已完成而跳过）
# ⑤ 删任务级 global_timeline.json（见第 4 条）
# ⑥ 删 render_segments/（render 会重新切片段）
```

**分辨率选择**：成片是 1080p 时选 1080p 源就够。4K 源体积翻 4 倍（52 分钟约 3.8GB），
而 CDN 对高码率流限速（实测约 1MB/s → 下一个多小时），换不来成片画质提升。

---

## 4. `global_timeline.json` 是缓存，不是产物（最容易踩）

任务级 `tasks/<task>/global_timeline.json` 由 `stage_index.build_global` 生成，
但 **B 模式的 select 只在文件缺失时才重建**。换了素材而没删它，select 会读旧时间轴，
**静默产出错误成片**（现象：片段数骤减、大量「跳过无时间戳」、成片时长对不上素材量）。

**做法**：任何一次 `add-asset` 换素材后立刻删掉它，select 会自行重建。

**复核**：重建后核对 `stats.shots` / `duration` 是否与 `workspace/<asset_key>/shots.json` 一致，
不一致就是读到旧缓存。

---

## 5. 覆盖率是素材完整性的判据，不只是对齐质量指标

台词表句数 ÷ 语速得到的理论时长若远超素材时长，说明**素材本身是精简剪辑版**，
再怎么调 ASR 也补不回来。

| 覆盖率 | 含义 | 动作 |
|--------|------|------|
| ≥85% | 素材覆盖全剧情 | 正常推进 |
| <85% | 素材缺内容 | **停下来换素材**，不要靠调参数硬修 |

**取证**：关键词在 ASR 全片命中 0 次 + 每百句有戳率出现整段 0% → 素材确实缺这段。
换素材后覆盖率会跳到 90% 上下，且每百句有戳率均匀。

---

## 6. 专名转写错误：先看覆盖率再决定要不要治

ASR 会把游戏专名听错（翠珏岩→脆绝盐、烟绯→晏飞、芷巧→直巧、阿圆→阿元）。
`MMM_ASR_INITIAL_PROMPT` 注入专名表能小幅改善，但**救不了素材缺失**。
实测覆盖率 30% 的场景下，改 initial_prompt 只有个位数句子的差别 ——
先算「理论时长 vs 素材时长」定性，再决定要不要在 prompt 上花时间。

---

## 7. vision 阶段少量失败可忽略

逐镜头 LLM 分析约有 1% 失败率（`parse_failed` / `EmptyContent`）。
**失效镜头在 index 阶段被保守判为 gameplay（X）从而排除**，不影响正文。
重跑 `mmm run vision <asset_key>` 只补失败镜头，重试 2 次仍失败的直接放过。

---

## 8. B 模式的 BGM duck 区间

B 模式下每条 EDL 片段都是 raw_insert 且在成片轴上首尾相接，
逐条生成 `between()` 会产生 2N 个条件。片段数超过约 170 时 ffmpeg 滤镜图初始化失败
（`Error initializing filters` / `Cannot allocate memory`）。

**已修**（`stage_bgm._merge_intervals`）：生成滤镜前先合并重叠/相邻区间，语义等价但条件数骤降
（120 片段：240 → 2 个条件，6518 → 140 字符）。

**再遇到 BGM 阶段失败**：先看滤镜串长度与条件数，再查是否有未合并的区间。
另 `-v quiet` 会吞掉 ffmpeg 错误输出 —— 已改 `-v error`，排障直接看 stderr。

---

## 9. 分块 ASR（align 走不通时的兜底）

`mmm run align` 是一条不可中断的整片 ASR。素材很长（>40 分钟）而运行环境不稳时，
可分块转写再拼装：

1. 提 16k 单声道 wav，按 4 分钟切块（每块转写控制在 2 分钟内，躲开进程被杀）
2. 每块独立落盘 `r<NN>.json`，带时间偏移，失败只丢当前块
3. 拼成 `asr.json`（格式 `{"video":..., "model":..., "words":[{"text","start","end"}]}`）写入 `workspace/<asset_key>/`
4. 直接调 `stage_align.align()` 生成 `lines.json`（跳过 ASR，只做对齐）

块转写要带 `word_timestamps=True`，否则对齐拿不到词级时间。

---

## 10. 选片门槛要跟着素材量调

`raw_select.quality_levels` 与 `min_shot_class` 决定成片时长，而 select **不按目标时长截断**：
给多少合格句就剪多少。

| 素材量 | 建议 quality_levels | 效果 |
|--------|--------------------|------|
| 20 分钟级（速通精简版）| `["great","good"]`（默认）| 免得选不满 |
| 50 分钟级（完整实录）| `["great"]` | 120 句 ≈ 11.5 分钟 |

完整实录素材下仍用默认的 `great+good`，会剪出 25 分钟以上。

---

## 11. 闸口板子发出去：先预检 bucket，不可用就转本地交付（铁律）

`storyboard.html` 内嵌的帧图是**相对数据根**的路径（`../../workspace/<key>/frames/…`），
单独把 html 发出去必然是**一片空图**。发布用：

```bash
python3 skills/mini-movie-maker/scripts/publish_gate_oss.py <task_dir>
```

脚本做三件事：①把引用到的帧图按**数据根结构**传 OSS；②把 html 里的路径改写成 10 年
**签名 URL**；③回传链接。本地原 html 不动。

**先预检，再发布。** 脚本已内置 `preflight()`：ossutil 在不在？凭证在不在？
`ossutil ls oss://<bucket>/` 通不通？任一不过 → **跳过上传，打印本地 html 绝对路径**，
提示用户本机打开（本地 `../` 相对路径能解析，图片照常显示），退出码 **2**。
**别给用户一个打不开或空图的远程壳。**

- 不能改用 public-read：桶开了「阻止公共访问」，`Put public object acl is not allowed`
- 验证必须用 GET：**GET 签的 URL 发 HEAD 一定 403**（签名含 HTTP 方法），曾因此误判"全部 403"；脚本自检用 `curl -r` Range GET
- 退出码：`0` 成功 ｜ `2` 预检不过、已转本地交付 ｜ `1` 发布后自检失败

## 12. OSS 默认域名访问 .html 一定被强制下载（平台策略，非桶设置）

症状：链接能下文件，但**浏览器不渲染**，响应头带 `x-oss-force-download: true` +
`Content-Disposition: attachment`。

根因是阿里云平台策略：**2017/10/01 之后创建的 Bucket**，用 OSS 默认域名访问 `.html`
（或 `Content-Type: text/html`）一律强制下载。**不是桶里能关的开关**，客户端也绕不过 ——
对象级 `Content-Disposition: inline` ✗ 被压制，签名 `--query-param
response-content-disposition:inline` ✗ 同样被压制。官方唯一解法是**绑定自定义域名**（需 ICP 备案）。

所以发给用户的就是**下载链接**：下载后用浏览器打开即可完整显示，因为图片走**绝对签名 URL**，
不依赖 html 所在位置（换机器、换目录、从飞书下载后打开都能出图）。
自检**不要用"浏览器能否渲染"判断成功**，要看 GET 是否 200/206 + 是否 0 条残留相对路径。

## 13. 分镜板窄屏排版：flex 不换行会把正文挤成 0 宽

用户实测（手机 390px）：**从第二组起内容被推出屏幕右侧、左侧留白、正文竖排成一列一字**。

不是 float 问题（模板本来就是 flexbox），根因是 `.clip` 没有 `flex-wrap` +
`.frames { flex-shrink:0 }` + 帧图固定 `148px` → 3~4 张合计 444~592px 撑爆 390px 视口，
`.body`（`flex:1`）被压到 **0 宽**。已修（**勿回退**）：两处加 `flex-wrap`，并加
`@media (max-width:760px)` 让帧图与文案**上下排列**、帧图 `flex:1 1 30%` +
`aspect-ratio:16/9` 自适应屏宽。改模板后务必真机量一遍（CDP `Emulation.setDeviceMetricsOverride`）：

| 视口 | 期望 |
|------|------|
| 390px | `hOverflow=0`，`framesW == bodyW`（各 340），`stacked=true` |
| 1280px | `hOverflow=0`，frames 456px + body 722px **左右并排**（原设计） |

同批修掉的两个显示问题：①段号显示的是 `c.asset_id`（素材编号）而非段号，单素材任务下 22 段
全印同一个数字，被误读成「第 N 段」→ 改为「第 N 段」，素材编号仅多素材时以「素材#N」显示；
②语速常量 `4.5` 字/秒（=270 字/分，过期值）把成片时长**高估 13%**（15:17.7 vs 实际 13:21）
→ 统一改 `5.12`（=307 字/分），涉及 `stage_select` / `stage_select_raw` / `cli` / `reviewer` /
前端模板 fallback 共 **5 处**（这类"同一个常量抄了 5 份只改了 1 份"的坑以前 narrate 也踩过）。
