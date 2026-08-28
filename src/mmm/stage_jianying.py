"""阶段7 导出器B：EDL → 剪映草稿（人工精修通道，单向终点不回流）。

轨道布局（时间轴规则与导出器A 一致：片段时长 = max(源区间, TTS 时长)）：
- 视频轨：按 EDL 顺序铺设，解说片段静音，raw_insert 保留原声；
  TTS 超长时**视频段仍按源区间 1:1 放置**（不搞变速），空隙留给人工拉帧——
  精修通道的原则是「摆好素材、暴露问题」，不替人做决定
- 解说轨：复用 render_segments/tts_XXX.wav；缺失时直接失败，不隐式触发付费 TTS
- 字幕轨：import_srt 导入（按成片时间轴生成，见 stage_subtitle）
- BGM 轨：playlist 顺序铺满，音量预设低位；ducking 留给人工（剪映里人比滤镜调得好）

设计文档 §4 阶段7：剪映手调不回流，成片口径以 edl.final.json 为准。

依赖 pyJianYingDraft（可选依赖，pip install -e ".[jianying]"）。
"""

from __future__ import annotations

import json
from pathlib import Path

WIDTH, HEIGHT, FPS = 1280, 720, 30
BGM_VOLUME = 0.3   # 预设低位，人工精修时再调

# macOS 剪映专业版草稿根目录候选（按常见安装路径探测）
DRAFTS_DIR_CANDIDATES = [
    Path.home() / "Movies/JianyingPro Drafts",
    Path.home() / "Documents/JianyingPro Drafts",
]


def detect_drafts_dir() -> Path | None:
    """探测本机剪映草稿根目录，未安装剪映时返回 None。"""
    for p in DRAFTS_DIR_CANDIDATES:
        if p.exists():
            return p
    return None


def _clip_settings(transform: dict, out_w: int = 1920, out_h: int = 1080):
    """系列 transform（ffmpeg crop 语义）→ 剪映 ClipSettings。

    单位换算：剪映位移以「半画布宽/高」为单位（如 1920x1080 → x/960, y/540），
    且 y 轴负向朝下（剪映字幕底部参考值 -0.8），与 ffmpeg crop 偏移方向相反。
    """
    import pyJianYingDraft as draft

    scale = transform.get("scale", 1.0)
    return draft.ClipSettings(
        scale_x=scale, scale_y=scale,
        transform_x=transform.get("offset_x", 0) / (out_w / 2),
        transform_y=-transform.get("offset_y", 0) / (out_h / 2),
    )


def export(work_dir: Path, videos: dict[str, Path], draft_name: str, *,
           task_id: str = "", drafts_dir: Path | None = None,
           bgm_playlist: list[str] | None = None,
           bgm_volume: float = BGM_VOLUME) -> dict:
    """按 edl.json 生成剪映草稿。videos: video_id → 源视频路径。

    task_id 非空时：导出后登记 footage_usage 并归档 edl.final.json
    （与导出器A 同一口径，register_usage 幂等，两导出器都跑不会重复登记）。
    """
    import pyJianYingDraft as draft

    from . import stage_render, stage_subtitle
    from .db import PROJECT_ROOT
    from .media import probe_duration

    edl = json.loads((work_dir / "edl.json").read_text())
    clips = edl["clips"]

    # transform / TTS / 输出规格继承链与导出器A 相同：task.json（系列继承）> edl 级 > per-clip 覆盖
    transform = edl.get("transform") or {}
    tts_cfg: dict = {}
    out_w, out_h, out_fps = 1920, 1080, 30
    if task_id:
        cfg_path = PROJECT_ROOT / "tasks" / task_id / "task.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            transform = cfg.get("transform") or transform
            tts_cfg = cfg.get("tts") or {}
            out_cfg = cfg.get("output") or {}
            out_w = int(out_cfg.get("width", out_w))
            out_h = int(out_cfg.get("height", out_h))
            out_fps = int(out_cfg.get("fps", out_fps))

    drafts_dir = drafts_dir or detect_drafts_dir()
    if drafts_dir is None:
        raise FileNotFoundError(
            "未找到剪映草稿目录（试过 " +
            ", ".join(str(p) for p in DRAFTS_DIR_CANDIDATES) +
            "）；请安装剪映专业版或用 --drafts-dir 指定")
    drafts_dir.mkdir(parents=True, exist_ok=True)

    # 任务模式只复用已切分 TTS；剪映导出不隐式触发付费合成。
    seg_dir = work_dir / "render_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prepared_tts: dict[int, Path] | None = None
    if task_id:
        from .tts import runtime as tts_runtime

        prepared_tts = tts_runtime.load_render_artifacts(work_dir, tts_cfg)

    folder = draft.DraftFolder(str(drafts_dir))
    script = folder.create_draft(draft_name, out_w, out_h, fps=out_fps, allow_replace=True)
    video_track = script.append_track(draft.TrackSpec(draft.TrackType.video, name="正片"))
    tts_track = script.append_track(draft.TrackSpec(draft.TrackType.audio, name="解说"))

    t = 0.0
    seg_durations: list[float] = []
    for i, clip in enumerate(clips):
        video = videos.get(clip["video_id"])
        if video is None:
            raise KeyError(f"EDL 片段引用未提供的 video_id: {clip['video_id']}")
        v_dur = clip["end"] - clip["start"]
        wav = None
        a_dur = 0.0
        if not clip.get("keep_audio"):
            wav = seg_dir / f"tts_{i:03d}.wav"
            if prepared_tts is not None:
                wav = prepared_tts[i]
                a_dur = probe_duration(wav)
            else:
                a_dur = (probe_duration(wav) if wav.exists()
                         else stage_render.synthesize(clip["text"], wav, tts_cfg))
        dur = max(v_dur, a_dur, 0.5)
        seg_durations.append(dur)

        # 全程整数微秒，避免字符串毫秒舍入导致片段间 1ms 重叠（剪映拒绝重叠段）
        t_us = round(t * 1e6)
        xf = dict(transform)
        xf.update(clip.get("transform") or {})
        script.add_segment(draft.VideoSegment(
            str(video.resolve()),
            draft.trange(t_us, round(v_dur * 1e6)),
            source_timerange=draft.trange(round(clip["start"] * 1e6), round(v_dur * 1e6)),
            volume=1.0 if clip.get("keep_audio") else 0.0,
            clip_settings=_clip_settings(xf, out_w, out_h),
        ), video_track)
        if wav is not None and a_dur > 0:
            # 时长以剪映探测的素材时长为准（ms 精度），ffprobe 更精确会溢出几百 µs
            mat = draft.AudioMaterial(str(wav.resolve()))
            use_us = min(round(a_dur * 1e6), mat.duration)
            script.add_segment(draft.AudioSegment(
                mat, draft.trange(t_us, use_us),
                source_timerange=draft.trange(0, use_us)), tts_track)
        t += dur

    # 字幕轨：SRT 直接导入（时间轴与导出器A 同一算法）
    narration = json.loads((work_dir / "narration.json").read_text())["narration"]
    srt_path = work_dir / "subtitles.jianying.srt"
    srt_path.write_text(
        stage_subtitle.build_srt(narration, clips, seg_durations=seg_durations),
        encoding="utf-8")
    script.append_track(draft.TrackSpec(draft.TrackType.text, name="字幕"))
    script.import_srt(str(srt_path), "字幕")

    # BGM 轨：playlist 顺序铺满全长，末首截断；音量低位预设，ducking 留人工
    if bgm_playlist:
        bgm_track = script.append_track(draft.TrackSpec(draft.TrackType.audio, name="BGM"))
        tb, idx = 0.0, 0
        while tb < t:
            p = Path(bgm_playlist[idx % len(bgm_playlist)])
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            mat = draft.AudioMaterial(str(p.resolve()))
            use_us = min(mat.duration, round((t - tb) * 1e6))   # 素材时长以剪映探测为准
            if use_us <= 0:
                break   # 尾部不足 1µs，视为已铺满
            script.add_segment(draft.AudioSegment(
                mat, draft.trange(round(tb * 1e6), use_us),
                source_timerange=draft.trange(0, use_us),
                volume=bgm_volume), bgm_track)
            tb += use_us / 1e6
            idx += 1

    script.save()

    registered = 0
    if task_id:
        from .catalog import register_usage

        registered = register_usage(task_id, clips)
        out_dir = PROJECT_ROOT / "output" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "edl.final.json").write_text(
            json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"draft_name": draft_name, "drafts_dir": str(drafts_dir),
            "clips": len(clips), "duration": round(t, 1),
            "footage_registered": registered}
