#!/usr/bin/env python3
"""
拉片素材抽取工具。

把一部片子压成拉片员 skill 能吃的输入：
  - 按转场检测抽关键帧（不是均匀抽帧 —— 均匀抽会漏掉快切段落）
  - 提取字幕轨并转成带时间码的纯文本
  - 生成镜头时长分布统计（分镜向笔记直接要这个数据）

用法:
  python3 tools/ripfilm.py 片子.mp4 --out ripdata/renling
  python3 tools/ripfilm.py 片子.mp4 --out ripdata/x --threshold 0.35

依赖 ffmpeg / ffprobe，需自行安装（apt install ffmpeg）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def need(binname: str):
    if not shutil.which(binname):
        sys.exit(f"缺少 {binname}，请先安装：sudo apt install ffmpeg")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def detect_cuts(path: Path, threshold: float) -> list[float]:
    """转场检测。返回每个切点的时间（秒）。"""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-filter:v",
         f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True)
    times = []
    for m in re.finditer(r"pts_time:([0-9.]+)", proc.stderr):
        times.append(float(m.group(1)))
    return sorted(set(times))


def extract_frames(path: Path, times: list[float], outdir: Path) -> int:
    fdir = outdir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, t in enumerate(times):
        dest = fdir / f"{i:04d}_{t:.2f}s.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(path),
             "-frames:v", "1", "-q:v", "3", str(dest)],
            capture_output=True)
        if r.returncode == 0:
            n += 1
    return n


def extract_subs(path: Path, outdir: Path) -> Path | None:
    dest = outdir / "subtitle.srt"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0:s:0", str(dest)],
        capture_output=True)
    if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.unlink(missing_ok=True)
    return None


def shot_stats(cuts: list[float], duration: float) -> dict:
    """镜头时长分布。分镜向笔记直接消费这份数据。"""
    if not cuts:
        return {}
    bounds = [0.0] + cuts + ([duration] if duration > cuts[-1] else [])
    lens = [round(bounds[i + 1] - bounds[i], 2) for i in range(len(bounds) - 1)]
    lens = [x for x in lens if x > 0]
    if not lens:
        return {}
    srt = sorted(lens)
    return {
        "shot_count": len(lens),
        "total_sec": round(duration, 2),
        "avg_shot_sec": round(sum(lens) / len(lens), 2),
        "median_shot_sec": srt[len(srt) // 2],
        "shortest_sec": srt[0],
        "longest_sec": srt[-1],
        "under_2s_ratio": round(sum(1 for x in lens if x < 2) / len(lens), 3),
        "over_8s_ratio": round(sum(1 for x in lens if x > 8) / len(lens), 3),
        "shot_lengths": lens,
    }


BRIEF = """# 拉片素材包：{name}

抽取时间：{ts}

- 总时长：{dur} 秒
- 检测到镜头切点：{cuts} 个
- 关键帧：frames/ 目录，{frames} 张
- 字幕：{sub}

## 镜头时长分布

{stats}

## 下一步

把本目录喂给拉片员 skill，产出三份分流笔记：

```
avctl run film_analyst --brief "拉这部片，输出编剧向/选角向/分镜向三份笔记" \\
    --series <剧集> --ripdata {out}
```

注意：笔记里禁止出现原片的具体台词、人名、地名、独有设定。
只提取机制，不复述内容。
"""


def main():
    ap = argparse.ArgumentParser(description="拉片素材抽取")
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="转场检测灵敏度，越小越敏感（默认 0.30）")
    args = ap.parse_args()

    need("ffmpeg")
    need("ffprobe")
    src = Path(args.video)
    if not src.exists():
        sys.exit(f"找不到文件: {src}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 读取时长…")
    dur = probe_duration(src)
    print(f"      {dur:.1f} 秒")

    print(f"[2/4] 转场检测（阈值 {args.threshold}）…")
    cuts = detect_cuts(src, args.threshold)
    print(f"      检测到 {len(cuts)} 个切点")

    print(f"[3/4] 抽取关键帧…")
    n = extract_frames(src, cuts, out)
    print(f"      {n} 张")

    print(f"[4/4] 提取字幕…")
    sub = extract_subs(src, out)
    print(f"      {'已提取 subtitle.srt' if sub else '无内嵌字幕轨'}")

    stats = shot_stats(cuts, dur)
    (out / "shot_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    import datetime
    stat_lines = "\n".join(
        f"- {k}: {v}" for k, v in stats.items() if k != "shot_lengths")
    (out / "README.md").write_text(BRIEF.format(
        name=src.stem, ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        dur=f"{dur:.1f}", cuts=len(cuts), frames=n,
        sub="subtitle.srt" if sub else "无",
        stats=stat_lines or "（切点不足，无法统计）", out=args.out,
    ), encoding="utf-8")

    print(f"\n完成。素材包: {out}/")
    print(f"  frames/ {n} 张关键帧")
    print(f"  shot_stats.json 镜头时长分布")
    print(f"  README.md 素材包说明")


if __name__ == "__main__":
    main()
