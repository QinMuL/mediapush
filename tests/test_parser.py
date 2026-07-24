"""parser 单测：覆盖旧项目踩坑约束。"""
from app.parser import parse_filename
from app.parser.filename import (
    extract_audio,
    extract_quality,
    extract_season_episode,
    extract_title,
    extract_year,
    parse_ed2k,
)


# ---- 季集跨度语义（核心约束）----
def test_se_single():
    assert extract_season_episode("S01E02") == (1, 2, 1)


def test_se_range_span():
    # S01E02-05 → 跨度 4（02,03,04,05），不是结束集号 5
    assert extract_season_episode("S01E02-05") == (1, 2, 4)


def test_se_range_e_sep():
    assert extract_season_episode("S01E02E05") == (1, 2, 4)


def test_whole_season_s01():
    # 整季文件夹：只有 S01 无集号
    assert extract_season_episode("Show.S01.1080p") == (1, None, None)


def test_episode_only():
    assert extract_season_episode("E02") == (None, 2, 1)
    assert extract_season_episode("EP02-05") == (None, 2, 4)


def test_cn_season_episode():
    assert extract_season_episode("第1季第02集") == (1, 2, 1)
    assert extract_season_episode("第1季第02-05集") == (1, 2, 4)


def test_cn_episode_only():
    assert extract_season_episode("第02集") == (None, 2, 1)


def test_cn_season_only():
    assert extract_season_episode("某剧 第1季") == (1, None, None)


# ---- episode_end = episode + ep_span - 1 ----
def test_episode_end_from_span():
    md = parse_filename("Breaking.Bad.S01E02-05.1080p.mkv")
    assert md.episode == 2
    assert md.episode_span == 4
    assert md.episode_end == 5  # 2 + 4 - 1
    assert md.total_episodes == 4  # E02-05 推断跨度


def test_episode_end_single():
    md = parse_filename("Show.S01E03.720p.mkv")
    assert md.episode == 3
    assert md.episode_end == 3
    assert md.is_whole_season is False


def test_whole_season_flag():
    md = parse_filename("Show.Name.S01.1080p.mkv")
    assert md.is_whole_season is True
    assert md.season == 1
    assert md.episode is None


# ---- 画质 ----
def test_quality_formal():
    assert extract_quality("1080p") == "1080p"
    assert extract_quality("2160p") == "2160p"
    assert extract_quality("720p") == "720p"
    assert extract_quality("4K") == "4K"
    assert extract_quality("8K") == "8K"


def test_quality_informal_ignored():
    # HQ/HD/FINE 不计入正式画质
    assert extract_quality("Show.HQ.HD") == ""
    assert extract_quality("Show.FINE") == ""


# ---- 音频（AAC2.0/DTS5.1）----
def test_audio_aac():
    assert extract_audio("AAC2.0") == "AAC2.0"
    assert extract_audio("AAC5.1") == "AAC5.1"
    assert extract_audio("AAC") == "AAC"


def test_audio_dts():
    assert extract_audio("DTS5.1") == "DTS5.1"
    assert extract_audio("DTS-HD MA") == "DTS-HD MA"


def test_audio_others():
    assert extract_audio("TrueHD Atmos") == "TrueHD Atmos"
    assert extract_audio("FLAC") == "FLAC"


# ---- 年份 ----
def test_year():
    assert extract_year("Show.2008.1080p") == 2008
    assert extract_year("Show.1999") == 1999
    assert extract_year("Show.NoYear") is None


# ---- ed2k 非贪婪 ----
def test_ed2k_nongreedy():
    text = "ed2k://|file|abc def.mkv|123|/ 后续文字"
    assert parse_ed2k(text) == ["ed2k://|file|abc def.mkv|123|/"]


def test_ed2k_multiple():
    text = "ed2k://|file|a.mkv|1|/ and ed2k://|file|b.mkv|2|/"
    assert len(parse_ed2k(text)) == 2


# ---- 标题清理 ----
def test_title_clean():
    title = extract_title("Breaking.Bad.S01E02.2008.1080p.BluRay.DTS5.1.x265.10bit.mkv")
    assert title == "Breaking Bad"


def test_title_cn():
    title = extract_title("权力的游戏.第1季.第02集.4K.mkv")
    assert title == "权力的游戏"


# ---- 综合解析 ----
def test_parse_full_english():
    md = parse_filename("Breaking.Bad.S01E02.2008.1080p.BluRay.DTS5.1.x265.10bit.mkv")
    assert md.title == "Breaking Bad"
    assert md.year == 2008
    assert md.season == 1
    assert md.episode == 2
    assert md.episode_end == 2
    assert md.quality == "1080p"
    assert md.audio == "DTS5.1"
    assert md.is_whole_season is False


def test_parse_full_cn():
    md = parse_filename("权力的游戏 第1季 第02-05集 4K AAC2.0.mkv")
    assert md.title == "权力的游戏"
    assert md.season == 1
    assert md.episode == 2
    assert md.episode_span == 4
    assert md.episode_end == 5
    assert md.quality == "4K"
    assert md.audio == "AAC2.0"
