#!/usr/bin/env python3
"""字幕纵向位置预览：出 5 秒样片对比不同 margin_v 的落位（免去 15 分钟整片重渲）。

为什么需要它：解说字幕要塞进「游戏说话人名字之下、原生对白之上」这条窄缝，
凭感觉估 margin_v 会估错（实测用户估 110，实际 80~95）。这个脚本用**真实字幕文件**
（任务的 subtitles.ass）+ 真实字体 + 真实遮罩出样片，并把参考线画进画面，一眼可判。

与成片完全同源的关键三点：
  1. 改的是同一份 .ass 的 MarginV（字段索引 21，不是 20 —— 20 是 MarginR）
  2. Dialogue 时间要平移到预览时间轴（ass 里是成片绝对时间）
  3. 滤镜链顺序必须与管线一致：先 gblur + 遮罩，再烧字幕

用法：
  python preview_subtitle_position.py --task-dir <任务目录> [--window 300] [--margins 80,95,110,130]
  # 任务目录形如 /home/share/mini-movie-materials/tasks/<task_id>
  # 素材取自该目录下 render_segments/seg_*.mp4（遮罩前的原始画面）

产出：/tmp/subtitle_pos_preview.mp4（2xN 网格，含参考线）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess

# 原神 UI 实测坐标（1080p，经 overlay_transform 裁 UID 后）。
# 见 references/troubleshooting.md §18；换游戏必须重测后覆盖这三个值。
DEFAULT_BANDS = {'name': (880, 897), 'text': (904, 929)}
PLAY_RES_H = 720      # 与 config/game/*.yaml 的 play_res 一致
OUT_W, OUT_H = 1920, 1080
PANEL_W, PANEL_H = 960, 540
DUR = 5.0


def sh(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fmt_ts(t: float) -> str:
    return f"{int(t // 3600)}:{int((t % 3600) // 60):02d}:{t % 60:05.2f}"


def rewrite_ass(raw: str, margin_v: int, win_start: float, dur: float, out: pathlib.Path) -> int:
    """改 Style 的 MarginV 并把 Dialogue 时间平移到预览轴，返回窗口内事件数。"""
    lines, kept = [], 0
    for line in raw.splitlines():
        if line.startswith('Style:'):
            f = line.split(',')
            # ASS Style 字段：[15]=BorderStyle [16]=Outline [17]=Shadow [18]=Alignment
            #                 [19]=MarginL [20]=MarginR [21]=MarginV [22]=Encoding
            f[21] = str(margin_v)
            lines.append(','.join(f))
        elif line.startswith('Dialogue:'):
            # Dialogue: <layer>,<start>,<end>,<style>,...  注意第一个字段是图层号
            m = re.match(r'Dialogue:\s*\d+,(\d+):(\d+):(\d+)\.(\d+),(\d+):(\d+):(\d+)\.(\d+),(.*)', line)
            if not m:
                continue
            g = [int(x) for x in m.groups()[:8]]
            s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 100
            e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 100
            if e <= win_start or s >= win_start + dur:
                continue
            s = max(0.0, s - win_start)
            e = min(dur, e - win_start)
            if e - s < 0.05:
                continue
            lines.append(f"Dialogue: 0,{fmt_ts(s)},{fmt_ts(e)},{m.group(9)}")
            kept += 1
        else:
            lines.append(line)
    out.write_text('\n'.join(lines), encoding='utf-8')
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--task-dir', required=True)
    ap.add_argument('--window', type=float, default=300.0,
                    help='预览在成片时间轴上的起点（秒），需落在某个 seg_* 片段内')
    ap.add_argument('--margins', default='80,95,110,130',
                    help='逗号分隔的 margin_v 候选')
    ap.add_argument('--fonts-dir', default=None, help='字体目录，默认仓库 assets/fonts')
    ap.add_argument('--name-band', default=None, help='名字带 y0,y1（覆盖默认实测值）')
    ap.add_argument('--text-band', default=None, help='原生白字带 y0,y1')
    ap.add_argument('--out', default='/tmp/subtitle_pos_preview.mp4')
    args = ap.parse_args()

    task = pathlib.Path(args.task_dir).resolve()
    seg_dir = task / 'render_segments'
    if not (task / 'subtitles.ass').exists():
        raise SystemExit(f'✗ 找不到 {task}/subtitles.ass（阶段7 渲染后才有）')
    segs = sorted(seg_dir.glob('seg_*.mp4'))
    if not segs:
        raise SystemExit(f'✗ {seg_dir} 下没有 seg_*.mp4')

    # 用 tts_artifacts 累计时长定位 window 落在哪一段
    arts = json.loads((seg_dir / 'tts_artifacts.json').read_text(encoding='utf-8'))['artifacts']
    cum, hit = 0.0, None
    for a in arts:
        d = float(a.get('duration_s') or 0)
        if cum <= args.window < cum + d:
            hit = (int(a['index']), cum)
        cum += d
    if hit is None:
        raise SystemExit(f'✗ window={args.window}s 超出成片总长 {cum:.1f}s')
    idx, seg_start = hit
    offset = args.window - seg_start
    seg = seg_dir / f'seg_{idx:03d}.mp4'
    if not seg.exists():
        seg = segs[min(idx, len(segs)) - 1]
    print(f'  窗口 {args.window}s → 片段 {seg.name}（段内偏移 {offset:.1f}s）')

    fonts = pathlib.Path(args.fonts_dir) if args.fonts_dir else \
        pathlib.Path(__file__).resolve().parents[3] / 'assets' / 'fonts'
    font_file = next(iter(fonts.glob('*.ttf')), None)
    if font_file is None:
        raise SystemExit(f'✗ {fonts} 下没有字体文件')

    name_band = tuple(int(x) for x in args.name_band.split(',')) if args.name_band else DEFAULT_BANDS['name']
    text_band = tuple(int(x) for x in args.text_band.split(',')) if args.text_band else DEFAULT_BANDS['text']
    margins = [int(x) for x in args.margins.split(',')]

    task_cfg = json.loads((task / 'task.json').read_text(encoding='utf-8'))
    mask = (task_cfg.get('subtitle') or {}).get('overlay_mask') or {}
    mx = mask.get('x', [0, OUT_W])
    my = mask.get('y', [800, OUT_H])
    blur = mask.get('blur_sigma', 3)

    tmp = pathlib.Path('/tmp/subpos'); tmp.mkdir(exist_ok=True)
    src = tmp / 'src.mp4'
    mask_png = tmp / 'mask.png'
    sh(['ffmpeg', '-y', '-v', 'quiet', '-ss', f'{offset:.2f}', '-t', str(DUR), '-i', str(seg),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-an', str(src)])
    sh(['ffmpeg', '-y', '-v', 'quiet', '-f', 'lavfi', '-i', f'color=black:{OUT_W}x{OUT_H}:r=1:d=1',
        '-frames:v', '1', '-vf',
        f'drawbox=x={mx[0]}:y={my[0]}:w={mx[1]-mx[0]}:h={my[1]-my[0]}:color=white:t=fill,'
        'gblur=sigma=34,format=gray', str(mask_png)])

    # 参考线：名字下沿红线 + 原生白字范围青色块（必须画进画面，否则肉眼会估错位置）
    ref = (f"drawbox=x=0:y={name_band[1]-2}:w={OUT_W}:h=4:color=red@0.9:t=fill,"
           f"drawbox=x=0:y={text_band[0]}:w={OUT_W}:h={text_band[1]-text_band[0]}:color=cyan@0.18:t=fill")

    raw_ass = (task / 'subtitles.ass').read_text(encoding='utf-8')
    panels, kept_all = [], []
    for mv in margins:
        ass = tmp / f'mv{mv}.ass'
        kept = rewrite_ass(raw_ass, mv, args.window, DUR, ass)
        kept_all.append(kept)
        bottom = OUT_H - mv * (OUT_H / PLAY_RES_H)
        top = bottom - 63
        label = f'mv={mv}  top={int(top)} bottom={int(bottom)}'
        fc = ("[1:v]format=gray[mk];[0:v]split=2[vb][vo];"
              f"[vb]gblur=sigma={blur},format=rgba[b];[b][mk]alphamerge[a];"
              f"[vo][a]overlay=format=auto,ass={str(ass).replace(':', chr(92) + ':')}"
              f":fontsdir={str(fonts).replace(':', chr(92) + ':')},{ref},"
              f"drawtext=fontfile={font_file}:text='{label}':x=18:y=16:fontsize=30:"
              "fontcolor=white:box=1:boxcolor=black@0.75[o]")
        out = tmp / f'mv{mv}.mp4'
        # 遮罩那路 -loop 1 必须配 -framerate + -t，否则输出无限循环
        sh(['ffmpeg', '-y', '-v', 'error', '-i', str(src), '-loop', '1', '-framerate', '30',
            '-t', str(DUR), '-i', str(mask_png), '-filter_complex', fc, '-map', '[o]',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-t', str(DUR), str(out)])
        panels.append(out)
        print(f'  mv={mv:<4} 字幕带 y{int(top)}~{int(bottom)}，窗口内字幕事件 {kept} 条')

    cols = 2
    rows = (len(panels) + cols - 1) // cols
    while len(panels) < rows * cols:
        panels.append(panels[-1])
    parts = [f'[{i}:v]scale={PANEL_W}:{PANEL_H}[a{i}];' for i in range(len(panels))]
    for r in range(rows):
        parts.append(''.join(f'[a{r*cols+c}]' for c in range(cols)) + f'hstack=inputs={cols}[r{r}];')
    parts.append(''.join(f'[r{r}]' for r in range(rows)) + f'vstack=inputs={rows}[o]')
    sh(['ffmpeg', '-y', '-v', 'error'] + sum([['-i', str(p)] for p in panels], []) +
       ['-filter_complex', ''.join(parts), '-map', '[o]', '-c:v', 'libx264', '-preset', 'veryfast',
        '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', args.out])
    print(f'\n  ✓ {args.out}')
    print(f'  参考线：红线=说话人名字下沿 y{name_band[1]} ｜ 青块=原生白字 y{text_band[0]}~{text_band[1]}')
    if any(k == 0 for k in kept_all):
        print('  ⚠️ 有候选在窗口内没有字幕事件，样片看不出效果，换个 --window')


if __name__ == '__main__':
    main()
