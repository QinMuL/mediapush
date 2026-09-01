"""app/media/service.py 单测：退避/布局/净化/稳定性/字幕伴行/冒烟流转。

纯函数直测 + tmp_path 集成冒烟（假 TMDB，不依赖网络/ffprobe）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.media.namer import NamingResult, sanitize_name
from app.media.service import (
    LocalMediaReport,
    LocalMediaService,
    build_dest_dir,
    is_temp_file,
    is_video_file,
    pick_subtitles,
    retry_backoff_seconds,
)
from app.parser.media_parser import MediaData


# ---------------- 退避 ---------------- #
def test_retry_backoff():
    assert retry_backoff_seconds(0) == 0
    assert retry_backoff_seconds(1) == 3600
    assert retry_backoff_seconds(2) == 7200
    assert retry_backoff_seconds(3) == 14400
    assert retry_backoff_seconds(8) == 24 * 3600   # 24h 封顶
    assert retry_backoff_seconds(20) == 24 * 3600   # 永不超过封顶


# ---------------- 临时/视频文件判定 ---------------- #
def test_is_temp_file():
    assert is_temp_file(Path("movie.mkv.!qb"))
    assert is_temp_file(Path("movie.part"))
    assert is_temp_file(Path("movie.crdownload"))
    assert not is_temp_file(Path("movie.mkv"))
    assert not is_temp_file(Path("movie.mp4"))


def test_is_video_file():
    assert is_video_file(Path("a.mkv"))
    assert is_video_file(Path("B.MP4"))
    assert not is_video_file(Path("a.srt"))
    assert not is_video_file(Path("a.nfo"))


# ---------------- 目标目录布局 ---------------- #
def _result(media_type: str, season: int | None = 1) -> NamingResult:
    return NamingResult(
        parsed=MediaData(title="旧名", year=2026, media_type=media_type,
                        season=season, episode=1, raw="x.mkv"),
        details={"title": "藏锋", "year": 2026},
        proposed="藏锋.2026.S01E01.第01集.2160p.WEB-DL.H.265.mkv",
    )


def test_build_dest_dir_movie_flat():
    """电影平铺：直接进 B 根目录。"""
    out = Path("B")
    assert build_dest_dir(_result("movie"), out) == out


def test_build_dest_dir_tv_flat():
    """剧集也平铺：直接进 B 根目录。"""
    out = Path("B")
    assert build_dest_dir(_result("tv", 2), out) == out


def test_build_dest_dir_tv_flat_any_title():
    """无论标题如何，剧集目标目录都是平铺的 output_dir。"""
    r = _result("tv")
    r.details = {"title": "Mission: Impossible", "year": 1996}
    assert build_dest_dir(r, Path("B")) == Path("B")


# ---------------- 文件名净化 ---------------- #
def test_sanitize_name():
    # 冒号后原文的空格保留（"Mission: Impossible" 惯例写法）
    assert sanitize_name("Mission: Impossible - Fallout") == "Mission： Impossible - Fallout"
    assert sanitize_name('a<b>c"d|e?f*g') == "abcdefg"
    assert sanitize_name("trailing dot. ") == "trailing dot"
    assert sanitize_name("  multi  space  ") == "multi space"
    assert sanitize_name("") == "unnamed"


def test_sanitize_name_length_cap():
    long_stem = "片" * 250
    name = f"{long_stem}.mkv"
    out = sanitize_name(name, max_len=180)
    assert len(out) == 180
    assert out.endswith(".mkv")


def test_sanitize_name_length_cap_no_ext():
    out = sanitize_name("片" * 250, max_len=180)
    assert len(out) == 180


# ---------------- 字幕伴行 ---------------- #
def test_pick_subtitles(tmp_path: Path):
    v = tmp_path / "show.S01E01.mkv"
    v.write_bytes(b"v")
    (tmp_path / "show.S01E01.srt").write_text("srt")
    (tmp_path / "show.S01E01.ass").write_text("ass")
    (tmp_path / "show.S01E01.nfo").write_text("nfo")  # 非字幕不伴行
    subs = pick_subtitles(v)
    assert {s.name for s in subs} == {"show.S01E01.srt", "show.S01E01.ass"}


# ---------------- 集成冒烟：dry-run 全链路 ---------------- #
class _FakeTMDB:
    """analyze_file 依赖 TMDBHelper：仅用到其存在性（matcher 由 namer mock）。"""


class _FakeSettings:
    local_media_interval_seconds = 10.0
    local_media_stable_rounds = 2
    local_media_stuck_days = 7.0
    local_media_dry_run = True
    local_media_input_dir = ""
    local_media_output_dir = ""


class _FakeContainer:
    tmdb = _FakeTMDB()


@pytest.fixture()
def dirs(tmp_path: Path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return a, b


def _mk_service(a: Path, b: Path, monkeypatch) -> LocalMediaService:
    st = _FakeSettings()
    st.local_media_input_dir = str(a)
    st.local_media_output_dir = str(b)
    svc = LocalMediaService(_FakeContainer(), st)
    # 状态文件指到 tmp（实例属性覆盖，避免污染 ./data/）
    svc.state_file = a.parent / "_state_test.json"
    return svc


def test_smoke_dry_run_flow(dirs, monkeypatch):
    """稳定 → 分析（mock 高置信）→ dry-run 模拟移动：A 原文件不动。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.Disney+.WEB-DL.H265.mkv"
    video.write_bytes(b"0" * 64)

    svc = _mk_service(a, b, monkeypatch)
    result = NamingResult(
        parsed=MediaData(title="Furious", year=2026, media_type="tv",
                        season=1, episode=4, raw=video.name),
        details={"title": "狂怒追缉", "year": 2026},
        proposed="狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv",
    )
    monkeypatch.setattr(
        "app.media.service.analyze_file",
        lambda path, tmdb: asyncio.sleep(0, result=result),
    )

    r1 = asyncio.run(svc.run_once())  # 第 1 轮：记录快照
    assert r1.scanned == 1 and r1.stable == 0
    r2 = asyncio.run(svc.run_once())  # 第 2 轮：稳定（2 轮阈值）
    assert r2.stable == 1 and r2.dry_moved == 1
    assert video.exists()  # dry-run 不实际移动
    assert not list(b.rglob("*.mkv"))


def test_smoke_real_move_flow(dirs, monkeypatch):
    """关 dry-run：实际移动 + 字幕伴行 + 空目录清理。"""
    a, b = dirs
    sub = a / "sub"
    sub.mkdir()
    video = sub / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    (sub / "Furious.S01E04.2026.2160p.srt").write_text("srt")

    svc = _mk_service(a, b, monkeypatch)
    svc.settings.local_media_dry_run = False
    result = NamingResult(
        parsed=MediaData(title="Furious", year=2026, media_type="tv",
                        season=1, episode=4, raw=video.name),
        details={"title": "狂怒追缉", "year": 2026},
        proposed="狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv",
    )
    monkeypatch.setattr(
        "app.media.service.analyze_file",
        lambda path, tmdb: asyncio.sleep(0, result=result),
    )

    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())

    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    assert dest.is_file()
    assert dest.with_suffix(".srt").is_file()  # 字幕伴行
    assert not sub.exists()  # 空目录已清理


def test_smoke_low_conf_backoff(dirs, monkeypatch):
    """低置信：退避状态记录 + 未到期不重复分析。"""
    a, b = dirs
    video = a / "Unknown.Show.S01E01.mkv"
    video.write_bytes(b"0" * 64)

    svc = _mk_service(a, b, monkeypatch)
    low = NamingResult(
        parsed=MediaData(title="Unknown", year=None, media_type="tv",
                        season=1, episode=1, raw=video.name),
        reasons=["TMDB 无搜索结果"],
    )
    calls = {"n": 0}

    async def fake_analyze(path, tmdb):
        calls["n"] += 1
        return low

    monkeypatch.setattr("app.media.service.analyze_file", fake_analyze)

    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # 稳定后首析
    assert calls["n"] == 1
    st = svc._retry_state[str(video)]
    assert st["failures"] == 1

    asyncio.run(svc.run_once())  # 退避未到期：不再分析
    asyncio.run(svc.run_once())
    assert calls["n"] == 1
    assert svc._retry_state[str(video)]["failures"] == 1


def test_smoke_conflict_no_overwrite(dirs, monkeypatch):
    """目标已存在：跳过不覆盖。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    svc = _mk_service(a, b, monkeypatch)
    svc.settings.local_media_dry_run = False
    result = NamingResult(
        parsed=MediaData(title="Furious", year=2026, media_type="tv",
                        season=1, episode=4, raw=video.name),
        details={"title": "狂怒追缉", "year": 2026},
        proposed="狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv",
    )
    monkeypatch.setattr(
        "app.media.service.analyze_file",
        lambda path, tmdb: asyncio.sleep(0, result=result),
    )
    dest = b / result.proposed
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"existing")

    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())

    assert dest.read_bytes() == b"existing"  # 未被覆盖
    assert video.exists()  # 源文件原地保留


def test_report_summary():
    r = LocalMediaReport(scanned=5, stable=2, moved=3, low_conf=1, conflict=1)
    s = r.summary()
    assert "移动 3" in s and "低置信保留 1" in s and "同名跳过 1" in s


# ---------------------------------------------------------------------- #
# 建议 1：批量移动上限 LOCAL_MEDIA_BATCH_MOVE_MAX（默认 5）防 IO/TMDB 打爆
# ---------------------------------------------------------------------- #
class _FakeSettingsWithBatch(_FakeSettings):
    """补一个 batch_move_max 字段。"""
    local_media_batch_move_max = 3  # 上限 3 用于测试


def _mk_service_batch(a: Path, b: Path) -> LocalMediaService:
    st = _FakeSettingsWithBatch()
    st.local_media_input_dir = str(a)
    st.local_media_output_dir = str(b)
    st.local_media_stable_rounds = 1   # 一轮即稳，便于构造大批量 stable 场景
    st.local_media_dry_run = False
    svc = LocalMediaService(_FakeContainer(), st)
    svc.state_file = a.parent / "_state_test_batch.json"
    return svc


def test_batch_move_cap_is_enforced(dirs, monkeypatch):
    """A 里放 6 个视频都稳定：1 轮最多移 batch_move_max=3 个，下轮再移 3 个。"""
    import asyncio as _as
    a, b = dirs
    episodes = list(range(1, 7))  # E01–E06
    for ep in episodes:
        (a / f"Furious.S01E{ep:02d}.2026.2160p.mkv").write_bytes(b"0" * 64)

    st = _FakeSettingsWithBatch()
    st.local_media_input_dir = str(a)
    st.local_media_output_dir = str(b)
    st.local_media_stable_rounds = 2   # 2 轮快照一致才算稳定
    st.local_media_dry_run = False
    svc = LocalMediaService(_FakeContainer(), st)
    svc.state_file = a.parent / "_state_test_batch.json"

    def _mk_result(ep: int):
        return NamingResult(
            parsed=MediaData(
                title="Furious", year=2026, media_type="tv",
                season=1, episode=ep, raw=f"Furious.S01E{ep:02d}.2026.2160p.mkv",
            ),
            details={"title": "狂怒追缉", "year": 2026},
            proposed=f"狂怒追缉.2026.S01E{ep:02d}.第{ep:02d}集.2160p.WEB-DL.H.265.mkv",
        )

    async def fake_analyze(path, tmdb):
        name = Path(path).name
        for ep in episodes:
            if f"S01E{ep:02d}" in name:
                return _mk_result(ep)
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("app.media.service.analyze_file", fake_analyze)

    # 第 1 轮：仅首拍快照（stable < 2），不移动任何
    r1 = _as.run(svc.run_once())
    assert r1.scanned == 6 and r1.stable == 0

    # 第 2 轮：快照一致，全部达到阈值；但 batch 上限=3 → 只移 3
    r2 = _as.run(svc.run_once())
    assert r2.scanned == 6
    assert r2.stable == 3, f"批量上限应只处理 3 个，实际 stable={r2.stable}"
    assert len(list(b.rglob("*.mkv"))) == 3
    assert len(list(a.rglob("*.mkv"))) == 3  # A 里剩下 3 个未处理

    # 第 3 轮：剩余 3 个仍在 A、仍稳定 → 再移 3
    r3 = _as.run(svc.run_once())
    assert r3.stable == 3
    assert len(list(b.rglob("*.mkv"))) == 6
    assert len(list(a.rglob("*.mkv"))) == 0


def test_batch_move_defaults_to_five_when_unset(dirs, monkeypatch):
    """老 settings 没有 batch_move_max 属性：Service 兜底到默认 5，不报错。"""
    import asyncio as _as
    a, b = dirs
    for ep in range(1, 11):
        (a / f"S{ep:02d}.mkv").write_bytes(b"x")
    svc = _mk_service(a, b, monkeypatch)
    svc.settings.local_media_stable_rounds = 2
    svc.settings.local_media_dry_run = False

    async def fake_analyze(path, tmdb):
        name = Path(path).stem
        return NamingResult(
            parsed=MediaData(title=name, year=2026, media_type="tv",
                             season=1, episode=1, raw=f"{name}.mkv"),
            details={"title": name, "year": 2026},
            proposed=f"{name}.done.mkv",
        )

    monkeypatch.setattr("app.media.service.analyze_file", fake_analyze)
    _as.run(svc.run_once())               # R1: 首拍快照，stable=1 < 2
    r = _as.run(svc.run_once())            # R2: 10 个稳定，但兜底上限 5 → 移 5
    assert r.stable == 5, f"兜底 5 应移 5 个，实际 stable={r.stable}"
    assert len(list(b.rglob("*.mkv"))) == 5
