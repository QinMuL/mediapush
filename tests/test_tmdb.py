"""TMDB _normalize 地区优先级测试。

背景：TMDB 个别条目 production_countries 被错误标注（如 283961「你是迟来的欢喜」
标成智利 CL），但 origin_country 正确（CN）。_normalize 应优先 origin_country。
"""

from app.tmdb.client import TMDBHelper


def _helper() -> TMDBHelper:
    return TMDBHelper("fake-key")


def test_normalize_tv_origin_country_preferred():
    """origin_country 优先于 production_countries。"""
    h = _helper()
    data = {
        "id": 1, "name": "X", "origin_country": ["CN"],
        "production_countries": [{"iso_3166_1": "CL"}],
        "seasons": [], "created_by": [], "credits": {"cast": [], "crew": []},
    }
    out = h._normalize(data, "tv")
    assert out["countries"] == ["CN"]


def test_normalize_tv_fallback_production_when_no_origin():
    """origin_country 为空时回退 production_countries。"""
    h = _helper()
    data = {
        "id": 1, "name": "X", "origin_country": [],
        "production_countries": [{"iso_3166_1": "US"}],
        "seasons": [], "created_by": [], "credits": {"cast": [], "crew": []},
    }
    out = h._normalize(data, "tv")
    assert out["countries"] == ["US"]


def test_normalize_movie_origin_country_preferred():
    """movie 同样优先 origin_country。"""
    h = _helper()
    data = {
        "id": 2, "title": "Y", "origin_country": ["CN"],
        "production_countries": [{"iso_3166_1": "CL"}],
        "credits": {"cast": [], "crew": []},
    }
    out = h._normalize(data, "movie")
    assert out["countries"] == ["CN"]


# -------------------- C：search_best 匹配优先级（年份/标题/类型） -------------------- #
import asyncio


class _StubTMDB(TMDBHelper):
    """stub：search 返回预置候选，绕过网络。"""

    def __init__(self, results):
        super().__init__("fake-key")
        self._results = results

    async def search(self, title, year=None, media_type="auto"):
        return self._results


def test_search_best_year_wins_over_rating():
    """年份吻合优先于评分（防同名老片/新片混淆）。"""
    # 2019 正确条目评分低，2001 同名片评分高 → 应选 2019
    h = _StubTMDB([
        {"id": 1, "title": "少年的你", "release_date": "2019-10-25", "vote_average": 7.6},
        {"id": 2, "title": "少年的你", "release_date": "2001-01-01", "vote_average": 9.5},
    ])
    best = asyncio.run(h.search_best("少年的你", 2019, "movie"))
    assert best == (1, "movie")


def test_search_best_exact_title_beats_rating():
    """标题精确匹配优先于评分（评分高的近似名不应胜出）。"""
    h = _StubTMDB([
        {"id": 3, "name": "三体", "first_air_date": "2023-01-15", "vote_average": 6.0},
        {"id": 4, "name": "三体大传", "first_air_date": "2023-01-15", "vote_average": 9.0},
    ])
    best = asyncio.run(h.search_best("三体", 2023, "tv"))
    assert best == (3, "tv")


def test_search_best_original_title_alias_fallback():
    """中文文件名搜到英文 original_title 的条目（别名兜底）。"""
    h = _StubTMDB([
        {"id": 5, "title": "Interstellar", "original_title": "星际穿越",
         "release_date": "2014-11-12", "vote_average": 8.0},
        {"id": 6, "title": "Interstellar Wars", "release_date": "2014-01-01",
         "vote_average": 8.5},
    ])
    best = asyncio.run(h.search_best("星际穿越", 2014, "movie"))
    assert best == (5, "movie")


def test_search_best_media_type_filters_namespace():
    """auto 混合候选时显式 media_type 过滤异型（命名空间冲突兜底）。"""
    h = _StubTMDB([
        {"id": 7, "title": "教父", "release_date": "1972-03-24", "vote_average": 8.5},
        {"id": 8, "name": "教父", "first_air_date": "2022-01-01", "vote_average": 9.9},
    ])
    best = asyncio.run(h.search_best("教父", 1972, "movie"))
    assert best == (7, "movie")


def test_search_best_no_year_falls_to_title_and_rating():
    """无年份时：标题精确 > 评分（原行为兜底）。"""
    h = _StubTMDB([
        {"id": 9, "title": "无问西东", "release_date": "2018-01-12", "vote_average": 6.5},
        {"id": 10, "title": "无问东西", "release_date": "2018-01-12", "vote_average": 9.0},
    ])
    best = asyncio.run(h.search_best("无问西东", None, "movie"))
    assert best == (9, "movie")
