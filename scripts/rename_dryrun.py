"""重命名 dry-run 调校工具：只输出模拟结果，不改动任何文件。

用法：
    python scripts/rename_dryrun.py [目录]（默认 D:/Downloads）

每输出一条：原名 → 解析（片名/年份/季集/发布组）→ 实测（分辨率/HDR/编码/音频）
→ TMDB 匹配 → 拟命名。低置信文件给出原因和参考预览，供人工核对解析差在哪。
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.media.namer import NamingResult, analyze_file
from app.tmdb.client import TMDBHelper

VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".iso", ".avi", ".wmv", ".flv", ".mov", ".m2ts"}

LINE = "─" * 100


def _fmt_result(r: NamingResult) -> list[str]:
    p, probe = r.parsed, r.probe
    out = [f"■ 原名: {p.raw}"]
    se = (
        f"S{p.season:02d}E{p.episode:02d}" if p.episode is not None
        else f"S{p.season:02d}" if p.season is not None else "无"
    )
    out.append(
        f"  解析 : 片名「{p.title}」 年份 {p.year or '无'} · "
        f"{'剧集' if p.media_type == 'tv' else '电影'} · {se} · "
        f"发布组 {p.release_group or '无'}"
    )
    if probe:
        depth = f" · {probe.bit_depth}" if probe.bit_depth else ""
        out.append(
            f"  探测 : {probe.resolution or '?'} · {probe.effect or '?'} · "
            f"{probe.video_encode or '?'}{depth} · {probe.frame_rate or '?'} · "
            f"{probe.audio_encode or '?'} · 音轨×{probe.audio_tracks}"
        )
    else:
        out.append("  探测 : ⚠ ffprobe 失败")
    if r.high_confidence:
        d = r.details or {}
        orig = f" ({d['original_title']})" if d.get("original_title") else ""
        out.append(
            f"  TMDB : ✅ 高置信 · 《{d.get('title')}》{orig} ({d.get('year')})"
        )
        out.append(f"  拟名 : {r.proposed}")
    else:
        out.append(f"  TMDB : ❌ 低置信（{'；'.join(r.reasons)}）")
        out.append(f"  参考 : {r.preview}（仅预览，不重命名）")
    return out


async def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/Downloads")
    files = sorted(
        f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS
    )
    if not files:
        print(f"目录 {root} 下没有视频文件")
        return

    s = Settings.load()
    tmdb = TMDBHelper(s.tmdb_api_key, proxy_url=s.proxy_url)

    lines: list[str] = [f"重命名 dry-run 报告 · {root} · {len(files)} 个文件", LINE]
    n_high = 0
    low_reasons: Counter[str] = Counter()
    for f in files:
        r = await analyze_file(str(f), tmdb)
        if r.high_confidence:
            n_high += 1
        else:
            low_reasons.update(r.reasons)
        lines.extend(_fmt_result(r))
        lines.append(LINE)

    lines.append(f"汇总: 高置信 {n_high}/{len(files)} · 低置信 {len(files) - n_high}")
    if low_reasons:
        lines.append("低置信原因分布: " + "，".join(
            f"{k}×{v}" for k, v in low_reasons.most_common()
        ))
    report = "\n".join(lines)
    print(report)

    out_file = Path("data/rename_dryrun_report.txt")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report, encoding="utf-8")
    print(f"\n报告已保存: {out_file.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
