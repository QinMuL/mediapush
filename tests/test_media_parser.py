from app.parser.media_parser import (
    analyze_share,
    extract_season_episode,
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
