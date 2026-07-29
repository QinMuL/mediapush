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
