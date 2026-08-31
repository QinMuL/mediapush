"""namer.py 命名引擎单测：模板渲染 / 硬门槛匹配 / 文件名预处理。"""

import asyncio

from app.media.namer import match_tmdb, parse_for_naming, render_name
from app.media.probe import ProbeTags
from app.parser.media_parser import MediaData


# ---------------- 模板渲染 ---------------- #
def _probe() -> ProbeTags:
    return ProbeTags(
        resolution="2160p",
        effect="DoVi P8",
        video_encode="H.265",
        audio_encode="DDP 5.1",
        bit_depth="10-bit",
        frame_rate="23.976fps",
    )


def test_render_movie_full():
    p = MediaData(
        title="旧名", year=2026, media_type="movie",
        raw="The.Whisper.Man.2026.2160p.WEB-DL.DoVi.H265.DDP.5.1.Atmos.mkv",
        source="WEB-DL", release_group="GROUP",
    )
    details = {"title": "低语者", "year": 2026}
    assert render_name(p, _probe(), details) == (
        "低语者 (2026) - 2160p.WEB-DL.DoVi P8.H.265.10-bit.23.976fps.DDP 5.1-GROUP.mkv"
    )


def test_render_tv_full():
    p = MediaData(
        title="旧名", year=2026, media_type="tv", season=1, episode=2,
        raw="Mousetrap.S01E02.2026.2160p.NF.WEB-DL.H265.DDP.5.1.Atmos.2Audio.mkv",
        source="WEB-DL", release_group="",
    )
    details = {"title": "捕鼠器", "year": 2026}
    assert render_name(p, _probe(), details) == (
        "捕鼠器.2026.S01E02.第02集.2160p.Netflix.WEB-DL.DoVi P8.H.265."
        "10-bit.23.976fps.DDP 5.1.mkv"
    )


def test_render_collapse_missing_vars():
    """变量缺失连同分隔点折叠；无发布组时不留 '-'。"""
    p = MediaData(
        title="片名", year=2025, media_type="movie",
        raw="movie.2025.mkv", release_group="",
    )
    probe = ProbeTags(resolution="1080p", effect="SDR",
                      video_encode="H.264", audio_encode="AAC 2.0")
    # 无 web_source（文件名未标注）→ 该段整体消失
    assert render_name(p, probe, None) == (
        "片名 (2025) - 1080p.SDR.H.264.AAC 2.0.mkv"
    )


def test_render_tv_ep_padding_and_long_ep():
    p = MediaData(
        title="航海王", year=1999, media_type="tv", season=23, episode=1175,
        raw="航海王.1999.WEB-DL.S23E1175.mkv",
    )
    name = render_name(p, ProbeTags(), None)
    assert "S23E1175" in name
    assert "第1175集" in name


def test_render_remux_source():
    p = MediaData(
        title="切腹", year=1962, media_type="movie",
        raw="Harakiri.1962.1080p.BluRay.REMUX.AVC.LPCM.1.0.mkv",
        source="REMUX",
    )
    probe = ProbeTags(resolution="1080p", video_encode="H.264",
                      audio_encode="LPCM 1.0", effect="SDR")
    name = render_name(p, probe, {"title": "切腹", "year": 1962})
    assert "1080p.BluRay.REMUX.SDR.H.264.LPCM 1.0" in name


def test_render_uhd_tag():
    """原文件名 UHD token 保留（"2160p.UHD.BluRay.REMUX" 惯例）。"""
    p = MediaData(
        title="神奇四侠：初露锋芒", year=2025, media_type="movie",
        raw="神奇四侠：初露锋芒 (2025) - 2160p.UHD BluRay REMUX.DoVi P7.H.265.mkv",
        source="BluRay",
    )
    probe = ProbeTags(resolution="2160p", effect="DoVi P7",
                      video_encode="H.265", audio_encode="TrueHD 7.1")
    assert "2160p.UHD.BluRay.REMUX.DoVi P7" in render_name(p, probe, None)

    # 无 UHD token：不加
    p2 = MediaData(
        title="某剧", year=2026, media_type="tv", season=1, episode=1,
        raw="Some.Show.S01E01.2026.2160p.WEB-DL.H265.mkv",
        source="WEB-DL",
    )
    assert ".WEB-DL." in render_name(p2, probe, None)


def test_render_platform_variants():
    """播放平台标注保留原文件名写法，位置在分辨率后来源前。"""
    p = MediaData(
        title="狂怒追缉", year=2026, media_type="tv", season=1, episode=4,
        raw="Furious.S01E04.2026.2160p.Disney+.WEB-DL.H265.DDP5.1.mkv",
        source="WEB-DL",
    )
    probe = ProbeTags(resolution="2160p", effect="DoVi P8",
                      video_encode="H.265", audio_encode="DDP 5.1")
    assert "2160p.Disney+.WEB-DL.DoVi P8" in render_name(p, probe, None)

    p2 = MediaData(
        title="某剧", year=2026, media_type="tv", season=1, episode=1,
        raw="Some.Show.S01E01.2026.2160p.AMZN.WEB-DL.H265.mkv",
        source="WEB-DL",
    )
    assert "2160p.Prime Video.WEB-DL" in render_name(p2, probe, None)

    # 无平台标注：该段整体折叠
    p3 = MediaData(
        title="某剧", year=2026, media_type="tv", season=1, episode=1,
        raw="Some.Show.S01E01.2026.2160p.WEB-DL.H265.mkv",
        source="WEB-DL",
    )
    assert ".WEB-DL." in render_name(p3, probe, None)


# ---------------- 文件名预处理 ---------------- #
def test_parse_platform_not_group():
    """平台名被 guessit 误判为发布组时剔除（friDay 已在质量段，不重复）。"""
    parsed = parse_for_naming(
        "共感细胞.2026.S01E04.第4集.1080p.friDay.WEB-DL.SDR.H.264.mkv"
    )
    assert parsed.release_group == ""
    # 正常发布组不受影响
    parsed2 = parse_for_naming(
        "Flex.x.Cop.2024.S02E02.1080p.DSNP.WEB-DL.AAC2.0.H.264-HiveWeb.mkv"
    )
    assert parsed2.release_group == "HiveWeb"


def test_parse_leading_se_title():
    """SxxExx 前置命名（标题在季集之后）：标题取剥前缀后的重解析。"""
    p = parse_for_naming(
        "S02E08.One.Hundred.Years.of.Solitude.2160p.NF.WEB-DL.DDP.5.1.Atmos.HDR10.mkv"
    )
    assert p.title.lower().startswith("one hundred")
    assert p.season == 2
    assert p.episode == 8


def test_parse_dup_suffix():
    """浏览器重复下载 ' (1)' 后缀：剥离后解析。"""
    p = parse_for_naming("Movie.Name.2026.1080p.WEB-DL.H264 (1).mkv")
    assert "1" not in p.title
    assert p.title.lower().startswith("movie name")


def test_parse_tv_default_season():
    """剧集有集号无季号 → 默认 S1。"""
    p = parse_for_naming("E07.Some.Show.2026.1080p.mkv")
    if p.episode is not None:
        assert p.season == 1


# ---------------- TMDB 硬门槛匹配 ---------------- #
class _FakeTMDB:
    def __init__(self, results: list[dict], details: dict) -> None:
        self._results = results
        self._details = details
        self.searched: list[tuple] = []      # (query, language)
        self.detail_ids: list[int] = []

    async def search(self, title, year, media_type="auto", language=None):
        self.searched.append((title, language))
        return self._results

    async def get_details(self, tmdb_id, media_type):
        self.detail_ids.append(tmdb_id)
        return self._details


class _LangFakeTMDB:
    """zh 搜索返回中文标题结果（标题对不上），en-US 返回英文名结果。"""

    def __init__(self) -> None:
        self.searched: list[tuple] = []

    async def search(self, title, year, media_type="auto", language=None):
        self.searched.append((title, language))
        if language == "en-US":
            return [{"id": 280133, "name": "Sharp Turns", "original_name": "藏锋",
                     "first_air_date": "2026-08-17"}]
        return [{"id": 280133, "name": "藏锋", "original_name": "藏锋",
                 "first_air_date": "2026-08-17"}]

    async def get_details(self, tmdb_id, media_type):
        return {"id": tmdb_id, "title": "藏锋", "year": 2026}


def _run_match(tmdb, parsed):
    return asyncio.run(match_tmdb(tmdb, parsed))


def test_match_high_confidence():
    tmdb = _FakeTMDB(
        [{"id": 1, "title": "低语者", "original_title": "The Whisper Man",
          "release_date": "2026-01-01"}],
        {"id": 1, "title": "低语者", "year": 2026},
    )
    p = MediaData(title="The Whisper Man", year=2026, media_type="movie",
                  raw="x.mkv")
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons


def test_match_year_mismatch_low():
    """电影：文件名年份与 TMDB 不符 → 低置信（硬门槛严格相等）。"""
    tmdb = _FakeTMDB(
        [{"id": 1, "title": "Lioness", "release_date": "2020-07-01"}],
        {},
    )
    p = MediaData(title="Lioness", year=2026, media_type="movie", raw="x.mkv")
    details, reasons = _run_match(tmdb, p)
    assert details is None
    assert any("不匹配" in r for r in reasons)


def test_match_tv_year_loose():
    """剧集：资源年份可晚于 TMDB 首播年（在播剧 Lioness S03 → 首播 2023）。"""
    tmdb = _FakeTMDB(
        [{"id": 113962, "name": "特别行动：母狮", "original_name": "Lioness",
          "first_air_date": "2023-07-23"}],
        {"id": 113962, "title": "特别行动：母狮", "year": 2023},
    )
    p = MediaData(title="Lioness", year=2026, media_type="tv",
                  raw="x.mkv", season=3, episode=4)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons


def test_match_tv_same_name_pick_latest():
    """同名剧集多命中：取首播最晚且 ≤ 资源年份的。"""
    tmdb = _FakeTMDB(
        [
            {"id": 232553, "name": "Lioness", "original_name": "Lioness",
             "first_air_date": "2021-01-28"},
            {"id": 113962, "name": "特别行动：母狮", "original_name": "Lioness",
             "first_air_date": "2023-07-23"},
        ],
        {"id": 113962, "title": "特别行动：母狮", "year": 2023},
    )
    p = MediaData(title="Lioness", year=2026, media_type="tv",
                  raw="x.mkv", season=3, episode=4)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons
    assert tmdb.detail_ids == [113962]


def test_match_en_fallback():
    """英文片名 + TMDB 中文结果标题对不上 → en-US 回退搜索匹配英文名。"""
    tmdb = _LangFakeTMDB()
    p = MediaData(title="Sharp Turns", year=2026, media_type="tv",
                  raw="Sharp.Turns.S01E01.2026.2160p.WEB-DL.H265.mkv",
                  season=1, episode=1)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons
    assert ("Sharp Turns", "en-US") in tmdb.searched


def test_match_bracket_cjk_title():
    """原名方括号中文标注（"[藏锋].Sharp.Turns..."）作为搜索候选直接命中。"""
    tmdb = _FakeTMDB(
        [{"id": 280133, "name": "藏锋", "original_name": "藏锋",
          "first_air_date": "2026-08-17"}],
        {"id": 280133, "title": "藏锋", "year": 2026},
    )
    p = MediaData(title="Sharp Turns", year=2026, media_type="tv",
                  raw="[藏锋].Sharp.Turns.S01E01.2026.2160p.WEB-DL.H265.mp4",
                  season=1, episode=1)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons
    assert ("藏锋", None) in tmdb.searched


def test_match_missing_year_tmdb_lookup():
    """无年份：TMDB 查补年份，标题匹配候选唯一 → 高置信。"""
    tmdb = _FakeTMDB(
        [{"id": 1, "name": "侠探杰克", "original_name": "Reacher",
          "first_air_date": "2022-02-04"}],
        {"id": 1, "title": "侠探杰克", "year": 2022},
    )
    p = MediaData(title="Reacher", year=None, media_type="tv",
                  raw="x.mkv", season=4, episode=1)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons


def test_match_missing_year_ambiguous():
    """无年份：多个同名候选无法消歧 → 低置信。"""
    tmdb = _FakeTMDB(
        [
            {"id": 1, "name": "Lioness", "original_name": "Lioness",
             "first_air_date": "2021-01-28"},
            {"id": 2, "name": "特别行动：母狮", "original_name": "Lioness",
             "first_air_date": "2023-07-23"},
        ],
        {},
    )
    p = MediaData(title="Lioness", year=None, media_type="tv",
                  raw="x.mkv", season=3, episode=4)
    details, reasons = _run_match(tmdb, p)
    assert details is None
    assert any("查补" in r for r in reasons)


def test_match_missing_year_no_result():
    """无年份且 TMDB 无搜索结果 → 低置信。"""
    tmdb = _FakeTMDB([], {})
    p = MediaData(title="Cang Feng", year=None, media_type="tv",
                  raw="x.mkv", season=1, episode=13)
    details, reasons = _run_match(tmdb, p)
    assert details is None
    assert "TMDB 无搜索结果" in reasons


def test_match_tv_requires_episode():
    """剧集无集号 → 低置信。"""
    tmdb = _FakeTMDB([], {})
    p = MediaData(title="Show", year=2026, media_type="tv", raw="x.mkv")
    details, reasons = _run_match(tmdb, p)
    assert details is None
    assert "剧集无 SxxExx 季集信息" in reasons


def test_match_title_containment():
    """归一化包含匹配（中文片名 vs 候选带副标题）。"""
    tmdb = _FakeTMDB(
        [{"id": 1, "name": "九阳武神", "original_name": "九阳武神",
          "first_air_date": "2025-01-01"}],
        {"id": 1, "title": "九阳武神", "year": 2025},
    )
    p = MediaData(title="九阳武神", year=2026, media_type="tv",
                  raw="x.mkv", season=1, episode=3)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons


# ================== 新韩剧标题匹配（多语言+标点折叠）==================
def test_match_kdrama_en_name_zh_cn_candidate():
    """zh-CN 搜索返回中文标题+韩语原名，查询词是英文名时通过
    details translations/AKA 拿到英文标题后匹配。"""
    class _TransFakeTMDB:
        def __init__(self):
            self.searched = []
            self.detail_ids = []

        async def search(self, title, year, media_type="auto", language=None):
            self.searched.append((title, language))
            # 真实 TMDB zh-CN 的返回：name 是中文，original_name 是韩语
            return [{"id": 305644, "name": "四手联弹，两首奏鸣曲",
                     "original_name": "포핸즈", "first_air_date": "2026-08-29"}]

        async def get_details(self, tmdb_id, media_type):
            self.detail_ids.append(tmdb_id)
            # alt_titles 是 TMDBHelper._normalize 已规范化后的结果
            return {
                "id": 305644, "title": "四手联弹，两首奏鸣曲",
                "year": 2026, "first_air_date": "2026-08-29",
                "alt_titles": [
                    "Four Hands, Two Sonatas",
                    "四手联弹，两首奏鸣曲",
                    "Four Hands",
                    "Pohaenjeu",
                ],
            }

    p = MediaData(title="Four Hands Two Sonatas", year=2026, media_type="tv",
                  raw="Four.Hands.Two.Sonatas.2026.S01E01.1080p.NF.WEB-DL.AAC2.0.H.264-HiveWeb.mkv",
                  season=1, episode=1)
    details, reasons = _run_match(_TransFakeTMDB(), p)
    assert details is not None and not reasons, f"reasons={reasons}"


def test_match_title_punctuation_fold_commas():
    """全角逗号「，」和半角「,」视为同一字符（含在_标题中匹配不上）。"""
    tmdb = _FakeTMDB(
        [{"id": 1, "name": "四手联弹，两首奏鸣曲", "original_name": "포핸즈",
          "first_air_date": "2026-08-29"}],
        {"id": 1, "title": "四手联弹，两首奏鸣曲", "year": 2026},
    )
    # 全角逗号 + 半角逗号（详情翻译名）都能与查询词「四手联弹，两首奏鸣曲」匹配
    p = MediaData(title="四手联弹，两首奏鸣曲", year=2026, media_type="tv",
                  raw="x.mkv", season=1, episode=1)
    details, reasons = _run_match(tmdb, p)
    assert details is not None and not reasons, f"reasons={reasons}"

    # 标题里全角逗号，查询词无逗号（文件名自动把标点当空格剥离了）
    p2 = MediaData(title="四手联弹 两首奏鸣曲", year=2026, media_type="tv",
                   raw="x.mkv", season=1, episode=1)
    details2, reasons2 = _run_match(tmdb, p2)
    assert details2 is not None and not reasons2, f"reasons={reasons2}"


def test_match_aka_titles_considered():
    """AKA（alternative_titles）里有英文短名时，也进入标题匹配池。"""
    class _AkaTMDB:
        def __init__(self):
            self.detail_ids = []

        async def search(self, title, year, media_type="auto", language=None):
            # 返回标题是中文，original_name 韩语，都对不上 Four Hands
            return [{"id": 305644, "name": "四手联弹，两首奏鸣曲",
                     "original_name": "포핸즈", "first_air_date": "2026-08-29"}]

        async def get_details(self, tmdb_id, media_type):
            self.detail_ids.append(tmdb_id)
            return {
                "id": 305644, "title": "四手联弹，两首奏鸣曲",
                "year": 2026,
                "alt_titles": ["Four Hands", "Pohaenjeu", "Po-haen-jeu"],
            }

    p = MediaData(title="Four Hands", year=2026, media_type="tv",
                  raw="Four.Hands.2026.S01E01.mkv", season=1, episode=1)
    details, reasons = _run_match(_AkaTMDB(), p)
    assert details is not None and not reasons, f"reasons={reasons}"
