"""元数据清洗测试：规则单测（注入 ffprobe JSON）+ ffmpeg 集成（本机有则真跑）。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.media.cleaner import (
    CleanReport,
    clean,
    inspect,
    report_from_ffprobe,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="本机无 ffmpeg/ffprobe")


# ---------------------------------------------------------------------- #
# 规则单测（纯函数，注入 ffprobe JSON）
# ---------------------------------------------------------------------- #
def _probe_data(*, title="", chapters=(), streams=()):
    return {
        "format": {"tags": ({"title": title} if title else {})},
        "chapters": [{"tags": {"title": t}} for t in chapters],
        "streams": list(streams),
    }


def _stream(idx, kind, title=""):
    return {"index": idx, "codec_type": kind,
            "tags": ({"title": title} if title else {})}


def test_clean_file_no_junk():
    rpt = report_from_ffprobe(_probe_data(
        title="藏锋 (2026)", chapters=("正片", "片尾"),
        streams=[_stream(0, "video"), _stream(1, "audio", "国语 DDP 5.1")],
    ))
    assert not rpt.has_junk
    assert rpt.summary() == ""


def test_junk_container_title():
    rpt = report_from_ffprobe(_probe_data(title="更多资源请访问 www.xxx.com"))
    assert rpt.has_junk and len(rpt.junk_tags) == 1
    assert "title=" in rpt.junk_tags[0]


def test_junk_chapters_only_bad_ones():
    rpt = report_from_ffprobe(_probe_data(
        chapters=("正片", "广告 http://ad.com"),
    ))
    assert rpt.junk_chapters == ["广告 http://ad.com"]
    assert rpt.summary().startswith("垃圾章节×1")


def test_junk_tracks_by_title_keyword():
    streams = [
        _stream(0, "video"),
        _stream(1, "audio", "国语 DDP 5.1"),
        _stream(2, "audio", "本资源由 XX压制组 promo"),
        _stream(3, "subtitle", "中文字幕"),
        _stream(4, "subtitle", "sub by www.subad.net"),
    ]
    rpt = report_from_ffprobe(_probe_data(streams=streams))
    assert [t["index"] for t in rpt.junk_tracks] == [2, 4]
    assert rpt.junk_tracks[0]["kind"] == "音轨"
    assert rpt.junk_tracks[1]["kind"] == "字幕"


def test_normal_track_titles_not_matched():
    """正常轨名（含"压制"字样的正常描述）不误删——L1 关键词保守集。"""
    streams = [
        _stream(0, "video"),
        _stream(1, "audio", "国语 压制版 DDP"),
        _stream(2, "subtitle", "简体中文字幕组出品"),
    ]
    rpt = report_from_ffprobe(_probe_data(streams=streams))
    assert not rpt.has_junk


def test_summary_all_three():
    rpt = CleanReport(
        junk_tags=["title=ad"], junk_chapters=["广告章节"],
        junk_tracks=[{"index": 2, "kind": "音轨", "title": "promo"}],
    )
    s = rpt.summary()
    assert "容器标签×1" in s and "垃圾章节×1" in s and "音轨#2" in s


# ---------------------------------------------------------------------- #
# ffmpeg 集成（真实 remux）
# ---------------------------------------------------------------------- #
def _make_mkv(path: Path, *, title="", audio_titles=(), chapters=()):
    """生成测试 mkv：1s testsrc 视频 + N 条静音音轨 + 指定 title/章节。

    ffmpeg 选项顺序：所有 -i 在前 → 输出选项（-metadata/-map_chapters/-c）
    在后 → 输出文件（输出选项必须全部位于其对应输出之前且不可穿插 -i）。
    """
    cmd = ["ffmpeg", "-y"]
    cmd += ["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"]
    for _ in audio_titles:
        cmd += ["-f", "lavfi", "-i", "anullsrc=duration=1:channel_layout=stereo"]
    meta_file = None
    if chapters:
        meta = ";FFMETADATA1\n"
        for i, name in enumerate(chapters):
            start, end = i * 500, (i + 1) * 500
            meta += f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={name}\n"
        meta_file = path.with_suffix(".meta")
        meta_file.write_text(meta, encoding="utf-8")  # 无 BOM（ffmpeg 不认 BOM）
        cmd += ["-i", str(meta_file)]
    # 输出选项（全部 -i 之后）：显式映射每条音轨（默认自动映射每类型只选一路，
    # 会丢第二条音轨）
    cmd += ["-map", "0:v"]
    for i in range(len(audio_titles)):
        cmd += ["-map", f"{i + 1}:a"]
    for i, t in enumerate(audio_titles):
        cmd += [f"-metadata:s:a:{i}", f"title={t}"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if meta_file is not None:
        cmd += ["-map_chapters", str(1 + len(audio_titles))]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    if meta_file is not None:
        meta_file.unlink(missing_ok=True)


@requires_ffmpeg
def test_inspect_detects_real_junk(tmp_path: Path):
    f = tmp_path / "dirty.mkv"
    _make_mkv(f, title="关注 www.foo.com 获取更多",
              audio_titles=("国语", "广告 ed2k://|file|x|1|a|/"),
              chapters=("正片", "推广 http://ad.net"))
    rpt = asyncio.run(inspect(str(f)))
    assert rpt is not None and rpt.has_junk
    assert len(rpt.junk_tags) == 1
    assert rpt.junk_chapters == ["推广 http://ad.net"]
    assert [t["index"] for t in rpt.junk_tracks] == [2]


@requires_ffmpeg
def test_inspect_clean_file(tmp_path: Path):
    f = tmp_path / "clean.mkv"
    _make_mkv(f, title="藏锋", audio_titles=("国语", "英语"))
    rpt = asyncio.run(inspect(str(f)))
    assert rpt is not None and not rpt.has_junk


@requires_ffmpeg
def test_clean_remux_removes_junk_keeps_content(tmp_path: Path):
    """端到端：脏 mkv 清洗 → 广告轨/垃圾章节/tags 消失，视频轨与时长保留。"""
    from app.media.cleaner import _ffprobe_json

    src = tmp_path / "dirty.mkv"
    dst = tmp_path / "cleaned.mkv"
    _make_mkv(src, title="更多资源 www.foo.com",
              audio_titles=("国语", "promo广告轨"),
              chapters=("正片", "广告章节 http://ad.net"))
    rpt = asyncio.run(inspect(str(src)))
    assert rpt is not None and rpt.has_junk

    asyncio.run(clean(str(src), str(dst), rpt))
    assert dst.is_file()

    out = asyncio.run(_ffprobe_json(str(dst)))
    titles = [
        (s.get("tags") or {}).get("title", "")
        for s in out["streams"] if s["codec_type"] == "audio"
    ]
    assert titles == ["国语"]                       # 广告轨已删、国语 title 保留
    fmt_tags = out["format"].get("tags") or {}
    assert not any("www" in v or "foo" in v for v in fmt_tags.values())  # 广告 tags 已清
    chap_titles = [
        (c.get("tags") or {}).get("title", "")
        for c in out.get("chapters") or []
    ]
    assert chap_titles == ["正片"]                  # 垃圾章节已删、正常章节保留
    # 校验内含：视频轨数/时长已在 clean() 断言（能走到这即通过）
    src_v = sum(1 for s in asyncio.run(_ffprobe_json(str(src)))["streams"]
                if s["codec_type"] == "video")
    dst_v = sum(1 for s in out["streams"] if s["codec_type"] == "video")
    assert src_v == dst_v == 1


@requires_ffmpeg
def test_clean_failure_leaves_dst_removed(tmp_path: Path, monkeypatch):
    """清洗失败（ffmpeg 伪造失败）→ dst 半成品删除、抛 CleanError。"""
    from app.media import cleaner as mod

    src = tmp_path / "dirty.mkv"
    dst = tmp_path / "out.mkv"
    _make_mkv(src, title="ad www.x.com")
    rpt = asyncio.run(inspect(str(src)))
    assert rpt is not None

    async def fail_run(cmd):
        return False

    monkeypatch.setattr(mod, "_run", fail_run)
    with pytest.raises(mod.CleanError):
        asyncio.run(clean(str(src), str(dst), rpt))
    assert not dst.exists()
    assert src.exists()  # 原件未动
