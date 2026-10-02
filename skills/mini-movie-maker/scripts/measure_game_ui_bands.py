#!/usr/bin/env python3
"""测量游戏 UI 的纵向位置（说话人名字 / 原生对白 / 底部按钮），为字幕 margin_v 定标。

为什么需要它：解说字幕要避开「说话人名字」又压住「原生对白」，这两个 y 必须实测。
凭感觉估会估错（实测用户估 margin_v=110，真实需求对应 80~95）。

方法（详见 references/troubleshooting.md §18）：
  1. 从 render_segments/seg_*.mp4 抽帧 —— 这是**遮罩前**的原始画面，游戏 UI 位置真实
     （遮罩在阶段7 终混才加，seg 里没有）。跨多个 seg 抽帧：UI 是固定位置，任意片段可测。
  2. ffmpeg edgedetect 取边缘图（无需 opencv）→ 逐行统计边缘能量。
  3. **跨帧取 25 分位**（不是平均！）——「≥75% 的帧这一行都有边缘」的才是固定 UI；
     平均会把每帧都在变的对白文字洗掉。
  4. 输出候选带，并用 --ruler 出带 y 标尺的裁图供人工复核
     （绝对坐标必须靠标尺确认，让视觉模型凭空估 y 会飘 ~50px）。

注意：暖色调场景（金色街景/灯笼）会毁掉 HSV 黄色检测，别用颜色找说话人名字，
用「跨帧稳定的边缘行」找。

依赖：只需 ffmpeg + numpy（项目 venv 即可，不需要 opencv）。用法：
  python measure_game_ui_bands.py --task-dir <任务目录> [--frames-per-seg 4] [--ruler]

产出：
  终端表格 = 候选文字带；--ruler 时另出 <out-dir>/ui_ruler.png（带 y 标尺的半屏裁图）
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess

import numpy as np

Y_FROM = 600        # 只看下半屏
ROW_THR_RATIO = 0.06  # 行能量阈值 = 背景 + (峰值-背景)*该比例


def ffprobe_dur(p: pathlib.Path) -> float:
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'csv=p=0', str(p)], capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def edge_profile(frame: pathlib.Path, height: int = 1080, width: int = 1920) -> np.ndarray | None:
    """用 ffmpeg edgedetect 出边缘图，返回逐行平均边缘能量（长度 height）。"""
    r = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', str(frame),
                        '-vf', 'format=gray,edgedetect', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                       capture_output=True)
    buf = r.stdout
    need = height * width
    if len(buf) < need:
        return None
    arr = np.frombuffer(buf[:need], dtype=np.uint8).reshape(height, width)
    return arr.astype(np.float32).mean(axis=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--task-dir', required=True)
    ap.add_argument('--frames-per-seg', type=int, default=4)
    ap.add_argument('--seg-limit', type=int, default=6, help='最多用几个片段抽帧')
    ap.add_argument('--ruler', action='store_true', help='另出带 y 标尺的裁图供复核')
    ap.add_argument('--out-dir', default='/tmp/ui_bands')
    args = ap.parse_args()

    task = pathlib.Path(args.task_dir).resolve()
    seg_dir = task / 'render_segments'
    segs = sorted(seg_dir.glob('seg_*.mp4'))[:args.seg_limit]
    if not segs:
        raise SystemExit(f'✗ {seg_dir} 下没有 seg_*.mp4（阶段7 渲染后才有）')

    out = pathlib.Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    print(f'═══ 抽帧：{len(segs)} 个片段 × {args.frames_per_seg} 帧 ═══')
    frames = []
    for si, seg in enumerate(segs):
        dur = ffprobe_dur(seg)
        if dur <= 1:
            continue
        for j in range(args.frames_per_seg):
            p = out / f'{si:02d}_{j}.jpg'
            subprocess.run(['ffmpeg', '-y', '-v', 'quiet', '-ss', f'{dur*(j+1)/(args.frames_per_seg+1):.2f}',
                            '-i', str(seg), '-frames:v', '1', '-q:v', '2', str(p)], check=True)
            frames.append(p)
    print(f'  ✓ {len(frames)} 帧')

    profs = []
    for p in frames:
        prof = edge_profile(p)
        if prof is not None:
            profs.append(prof[Y_FROM:])
    if not profs:
        raise SystemExit('✗ 无法计算边缘行分布（ffmpeg 输出异常）')

    P = np.vstack(profs)
    p25 = np.percentile(P, 25, axis=0)   # 跨帧 25 分位 = 固定 UI
    p50 = np.percentile(P, 50, axis=0)
    base = np.percentile(p25, 20)
    thr = base + (p25.max() - base) * ROW_THR_RATIO

    print()
    print(f'═══ 跨帧 25 分位行分布（阈值 {thr:.1f}，只看 y≥{Y_FROM}）═══')
    for y in range(Y_FROM, Y_FROM + len(p25), 5):
        v = p25[y - Y_FROM]
        if v > thr * 0.6:
            mark = '█' * min(40, int(v / max(p25.max(), 1e-6) * 40))
            print(f'  y={y:4d}  25分位={v:6.1f}  50分位={p50[y-Y_FROM]:6.1f}  {mark}')

    bands, cur = [], None
    for y in range(Y_FROM, Y_FROM + len(p25)):
        on = p25[y - Y_FROM] > thr
        if on and cur is None:
            cur = y
        elif not on and cur is not None:
            if y - cur >= 5:
                bands.append((cur, y - 1))
            cur = None
    if cur is not None and Y_FROM + len(p25) - cur >= 5:
        bands.append((cur, Y_FROM + len(p25) - 1))

    print()
    print('═══ 候选文字带 ═══')
    for y0, y1 in bands:
        peak = p25[y0 - Y_FROM:y1 - Y_FROM + 1].max()
        strong = peak > p25.max() * 0.5
        kind = ('强且稳定 → 横贯整屏的固定 UI（原生对白行 / 底部按钮行）' if strong
                else '较弱 → 内容逐帧在变（对白文字本体会被拉低，属正常）')
        print(f'  y = {y0:4d} ~ {y1:4d}  高 {y1-y0+1:3d}px  峰值 {peak:6.1f}  {kind}')
    print()
    print('  判读建议：最强的带 = 原生对白或按钮这类横贯元素；其上一带通常就是说话人名字。')
    print('  ⚠️ 绝对坐标必须用 --ruler 出图标尺复核，别让视觉模型凭空估（会飘 ~50px）。')

    if args.ruler and frames:
        ruler = out / 'ui_ruler.png'
        # 用 ffmpeg 画标尺（避免依赖 opencv/PIL）
        draw = []
        for y in range(Y_FROM - 100, 1080, 20):
            major = y % 100 == 0
            color = 'yellow' if major else 'green'
            th = 3 if major else 1
            draw.append(f"drawbox=x=0:y={y}:w=1920:h={th}:color={color}@0.9:t=fill")
        vf = 'crop=1920:380:0:700,' + ','.join(draw)
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(frames[0]),
                        '-vf', vf, str(ruler)], check=True)
        print(f'\n  ✓ 标尺图 {ruler}（原始 y700~1080，黄线=y%100==0，绿线=每 20px）')


if __name__ == '__main__':
    main()
