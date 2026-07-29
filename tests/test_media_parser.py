from app.parser.media_parser import (
    analyze_share,
    extract_season_episode,
    extract_tmdb_id,
    get_hdr,
    get_quality,
    get_source,
    parse_filename,
)
from app.providers.base import ShareFile


def test_extract_single_episode():
    assert extract_season_episode("Show.S01E03") == (1, 3, 1)


def test_extract_range():
    assert extract_season_episode("Show.S01E01-E12") == (1, 1, 12)


def test_extract_season_only():
    assert extract_season_episode("Show.S02") == (2, None, 0)


def test_extract_none():
    assert extract_season_episode("Some.Movie.1999") == (None, None, 0)


def test_quality():
    assert get_quality("x.2160p.UHD") == "4K / 2160P"
    assert get_quality("x.4K") == "4K / 2160P"
    assert get_quality("x.1080p") == "1080P"
    assert get_quality("x.720p") == "720P"
    assert get_quality("x.sd") == ""


def test_source():
    assert get_source("a.BluRay.b") == "BluRay"
    assert get_source("a.WEB-DL.b") == "WEB-DL"
    assert get_source("a.REMUX.b") == "REMUX"
    assert get_source("a.HDTV.b") == "HDTV"
    assert get_source("a.CAM.b") == ""


def test_hdr():
    assert "Dolby Vision" in get_hdr("x.DV.y")
    assert get_hdr("x.HDR10+.y") == "HDR10+"
    assert get_hdr("x.HDR10.y") == "HDR10"
    assert get_hdr("x.SDR.y") == "SDR"
    assert get_hdr("x.y") == ""


def test_parse_movie():
    p = parse_filename("The.Matrix.1999.2160p.UHD.BluRay.HDR10.Atmos.mkv")
    assert p.title == "The Matrix"
    assert p.year == 1999
    assert p.media_type == "movie"
    assert p.quality == "4K / 2160P"
    assert p.source == "BluRay"
    assert "HDR10" in p.hdr


def test_parse_tv_episode():
    p = parse_filename("Breaking.Bad.S01E01.1080p.WEB-DL.DDP5.1.mkv")
    assert p.media_type == "tv"
    assert p.season == 1
    assert p.episode == 1
    assert p.episode_end == 1
    assert p.source == "WEB-DL"


def test_parse_tv_range():
    p = parse_filename("No.Game.No.Life.S01E01-E12.1080p.BluRay.x264.mkv")
    assert p.season == 1
    assert p.episode == 1
    assert p.episode_end == 12


def test_analyze_share_tv_multiple():
    files = [
        ShareFile(f"Show.S01E0{i}.1080p.WEB-DL.mkv", 0, False) for i in (1, 2, 3)
    ]
    a = analyze_share(files)
    assert a is not None
    assert a.media_type == "tv"
    assert a.season == 1
    assert a.episode_start == 1
    assert a.episode_end == 3
    assert a.file_count == 3
    assert a.total_episodes == 3


def test_analyze_share_movie_single():
    files = [ShareFile("Inception.2010.1080p.BluRay.mkv", 0, False)]
    a = analyze_share(files)
    assert a.media_type == "movie"
    assert a.title == "Inception"
    assert a.year == 2010
    assert a.file_count == 1


def test_analyze_empty():
    assert analyze_share([]) is None


# -------------------- {tmdb-XXX} 标注提取 -------------------- #
def test_extract_tmdb_id_from_dir():
    """目录名带 {tmdb-1311031} 标注时优先提取。"""
    files = [
        ShareFile("Demon Slayer: Infinity Castle (2025) {tmdb-1311031}", 0, True),
        ShareFile("Demon Slayer (2025) - 1080p.BluRay.mkv", 42_000_000_000, False),
    ]
    a = analyze_share(files)
    assert a is not None
    assert a.tmdb_id == 1311031


def test_extract_tmdb_id_from_filename():
    """目录无标注时从文件名提取。"""
    files = [
        ShareFile("Movie.2024.{tmdb-999}.mkv", 1_000_000, False),
    ]
    a = analyze_share(files)
    assert a is not None
    assert a.tmdb_id == 999


def test_extract_tmdb_id_none():
    """无标注时 tmdb_id 为 None。"""
    files = [ShareFile("Inception.2010.1080p.mkv", 0, False)]
    a = analyze_share(files)
    assert a is not None
    assert a.tmdb_id is None


def test_extract_tmdb_id_case_insensitive():
    """{TMDB-xxx} 大小写不敏感。"""
    assert extract_tmdb_id(["file.{TMDB-12345}.mkv"]) == 12345
    assert extract_tmdb_id(["no match"]) is None
