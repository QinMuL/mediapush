"""share_normalizer 单元测试。

覆盖：
- 季目录名解析（正则 + 中文数字）
- 标准目录名构建
- 四种场景的标准化逻辑（mock pan115 + tmdb）
- 幂等性
- 失败容错
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.share_normalizer import (
    ShareNormalizer,
    _cn_to_int,
    _has_tmdb_tag,
    build_resource_name,
    format_season_dir,
    parse_season_dir,
)

# ---------------------------------------------------------------------- #
# 纯函数测试
# ---------------------------------------------------------------------- #

class TestParseSeasonDir:
    def test_season_english(self):
        assert parse_season_dir("Season 1") == 1
        assert parse_season_dir("Season 2") == 2
        assert parse_season_dir("season 12") == 12
        assert parse_season_dir("Season1") == 1

    def test_s_prefix(self):
        assert parse_season_dir("S01") == 1
        assert parse_season_dir("S02") == 2
        assert parse_season_dir("s1") == 1

    def test_chinese_numeral(self):
        assert parse_season_dir("第1季") == 1
        assert parse_season_dir("第 2 季") == 2
        assert parse_season_dir("第3季") == 3

    def test_chinese_number(self):
        assert parse_season_dir("第一季") == 1
        assert parse_season_dir("第二季") == 2
        assert parse_season_dir("第十季") == 10

    def test_already_padded(self):
        assert parse_season_dir("01") == 1
        assert parse_season_dir("02") == 2
        assert parse_season_dir("12") == 12

    def test_non_season(self):
        assert parse_season_dir("Extras") is None
        assert parse_season_dir("字幕") is None
        assert parse_season_dir("Specials") is None
        assert parse_season_dir("垃圾") is None

    def test_cn_to_int(self):
        assert _cn_to_int("一") == 1
        assert _cn_to_int("十") == 10
        assert _cn_to_int("十一") == 11
        assert _cn_to_int("二十") == 20
        assert _cn_to_int("二十三") == 23
        assert _cn_to_int("") is None


class TestFormatSeasonDir:
    def test_padding(self):
        assert format_season_dir(1) == "01"
        assert format_season_dir(9) == "09"
        assert format_season_dir(10) == "10"
        assert format_season_dir(99) == "99"


class TestBuildResourceName:
    def test_with_year_and_tmdb(self):
        assert build_resource_name("葬送的芙莉莲", 2026, 246389, "tv") == \
            "葬送的芙莉莲 (2026) {tmdb-246389}"

    def test_without_year(self):
        assert build_resource_name("沙丘", None, 693134, "movie") == \
            "沙丘 {tmdb-693134}"

    def test_without_tmdb(self):
        assert build_resource_name("新剧", 2026, None, "tv") == \
            "新剧 (2026)"


class TestHasTmdbTag:
    def test_present(self):
        assert _has_tmdb_tag("葬送的芙莉莲 (2026) {tmdb-246389}") is True

    def test_absent(self):
        assert _has_tmdb_tag("葬送的芙莉莲 (2026)") is False


# ---------------------------------------------------------------------- #
# ShareNormalizer 场景测试
# ---------------------------------------------------------------------- #

def _make_container(tmdb_search_result=None, tmdb_details=None):
    """构建 mock container，含 tmdb client。"""
    container = MagicMock()
    tmdb = MagicMock()
    tmdb.search_best = AsyncMock(return_value=tmdb_search_result)
    tmdb.get_details = AsyncMock(return_value=tmdb_details)
    container.tmdb = tmdb
    return container


def _make_settings(enabled=True, dry_run=True):
    """构建 mock settings。"""
    s = MagicMock()
    s.share_normalize_enabled = enabled
    s.share_normalize_dry_run = dry_run
    s.tmdb_api_key = "fake-key"
    return s


def _mock_pan115(list_dir_return=None):
    """构建 mock pan115 provider。"""
    pan = MagicMock()
    pan.list_dir = AsyncMock(return_value=list_dir_return or [])
    pan.fs_rename = AsyncMock(return_value=None)
    pan.fs_move = AsyncMock(return_value=None)
    pan.fs_makedirs = AsyncMock(return_value=999)
    pan._login_client = MagicMock(return_value=MagicMock())
    pan._call_with_margin = AsyncMock(return_value={"data": {"file_id": 999}})
    return pan


class TestNormalizeDisabled:
    @pytest.mark.asyncio
    async def test_disabled_returns_unchanged(self):
        container = _make_container()
        settings = _make_settings(enabled=False)
        norm = ShareNormalizer(container, settings)
        pan = _mock_pan115()

        result = await norm.normalize(pan, 123, "Some Movie", True, 0)

        assert result.fid == 123
        assert result.changed is False
        assert result.actions == []


class TestScenarioASeasonSubdirs:
    """场景 A：已有季子目录 → 重命名根目录 + 季子目录。"""

    @pytest.mark.asyncio
    async def test_rename_season_dirs_dry_run(self):
        """dry-run 模式：只记日志不实际操作。"""
        container = _make_container(
            tmdb_search_result=(246389, "tv"),
            tmdb_details={"title": "葬送的芙莉莲", "year": 2026},
        )
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        items = [
            {"fid": 200, "name": "Season 1", "is_dir": True, "size": 0},
            {"fid": 201, "name": "Season 2", "is_dir": True, "size": 0},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, "Frieren", True, 0)

        assert result.changed is True
        assert result.fid == 100  # dry-run 不改变 fid
        assert any("重命名目录" in a for a in result.actions)
        assert any("Season 1" in a and "01" in a for a in result.actions)
        assert any("Season 2" in a and "02" in a for a in result.actions)
        # dry-run 不应调用任何写操作
        pan.fs_rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_season_dirs_real(self):
        """实际模式：调用 fs_rename。"""
        container = _make_container(
            tmdb_search_result=(246389, "tv"),
            tmdb_details={"title": "葬送的芙莉莲", "year": 2026},
        )
        settings = _make_settings(dry_run=False)
        norm = ShareNormalizer(container, settings)

        items = [
            {"fid": 200, "name": "Season 1", "is_dir": True, "size": 0},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, "Frieren", True, 0)

        assert result.changed is True
        # 应调用 fs_rename：根目录 + 季目录
        assert pan.fs_rename.call_count == 2

    @pytest.mark.asyncio
    async def test_already_normalized_skipped(self):
        """已是标准名 → 跳过。"""
        container = _make_container()
        settings = _make_settings(dry_run=False)
        norm = ShareNormalizer(container, settings)

        std_name = "葬送的芙莉莲 (2026) {tmdb-246389}"
        items = [
            {"fid": 200, "name": "01", "is_dir": True, "size": 0},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, std_name, True, 0)

        # 已标准名 + 季目录已是 01 → 不变更
        assert result.changed is False
        pan.fs_rename.assert_not_called()


class TestScenarioBLooseEpisodeFiles:
    """场景 B：剧集散集文件 → 按季号建子目录。"""

    @pytest.mark.asyncio
    async def test_group_by_season_dry_run(self):
        container = _make_container(
            tmdb_search_result=(246389, "tv"),
            tmdb_details={"title": "葬送的芙莉莲", "year": 2026},
        )
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        items = [
            {"fid": 300, "name": "S01E01.mkv", "is_dir": False, "size": 1000},
            {"fid": 301, "name": "S01E02.mkv", "is_dir": False, "size": 1000},
            {"fid": 302, "name": "S02E01.mkv", "is_dir": False, "size": 1000},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, "Frieren", True, 0)

        assert result.changed is True
        assert any("建季目录" in a and "01" in a for a in result.actions)
        assert any("建季目录" in a and "02" in a for a in result.actions)
        # dry-run 不应建目录
        pan.fs_move.assert_not_called()


class TestScenarioCMovieFolder:
    """场景 C：电影文件夹 → 只重命名根目录。"""

    @pytest.mark.asyncio
    async def test_movie_rename_only(self):
        container = _make_container(
            tmdb_search_result=(693134, "movie"),
            tmdb_details={"title": "沙丘", "year": 2021},
        )
        settings = _make_settings(dry_run=False)
        norm = ShareNormalizer(container, settings)

        items = [
            {"fid": 400, "name": "Dune.2021.mkv", "is_dir": False, "size": 50000},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, "Dune", True, 0)

        assert result.changed is True
        # 电影只重命名根目录，不建季目录
        assert pan.fs_rename.call_count == 1
        pan.fs_move.assert_not_called()


class TestScenarioDLooseFile:
    """场景 D：监控目录散文件 → 建资源目录包裹。"""

    @pytest.mark.asyncio
    async def test_wrap_tv_file_dry_run(self):
        container = _make_container(
            tmdb_search_result=(246389, "tv"),
            tmdb_details={"title": "葬送的芙莉莲", "year": 2026},
        )
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        pan = _mock_pan115()

        result = await norm.normalize(pan, 500, "Frieren.S01E01.mkv", False, 10)

        assert result.changed is True
        assert any("建资源目录" in a for a in result.actions)
        assert any("建季目录" in a and "01" in a for a in result.actions)
        assert any("移入文件" in a for a in result.actions)
        # dry-run 返回原 fid
        assert result.fid == 500

    @pytest.mark.asyncio
    async def test_wrap_movie_file_dry_run(self):
        container = _make_container(
            tmdb_search_result=(693134, "movie"),
            tmdb_details={"title": "沙丘", "year": 2021},
        )
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        pan = _mock_pan115()

        result = await norm.normalize(pan, 500, "Dune.2021.mkv", False, 10)

        assert result.changed is True
        assert any("建资源目录" in a for a in result.actions)
        assert any("移入文件" in a for a in result.actions)
        # dry-run 返回原 fid
        assert result.fid == 500


class TestFailureTolerance:
    """标准化失败不阻断，降级返回原 fid。"""

    @pytest.mark.asyncio
    async def test_tmdb_failure_fallback(self):
        """TMDB 匹配失败 → 降级用原名。"""
        container = _make_container(tmdb_search_result=None)
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        items = [
            {"fid": 200, "name": "Season 1", "is_dir": True, "size": 0},
        ]
        pan = _mock_pan115(list_dir_return=items)

        result = await norm.normalize(pan, 100, "未知剧", True, 0)

        # 无 TMDB → 无法构建标准名，但季目录仍可重命名
        assert result.fid == 100
        # 季目录重命名不依赖 TMDB
        assert any("Season 1" in a and "01" in a for a in result.actions)

    @pytest.mark.asyncio
    async def test_exception_fallback(self):
        """list_dir 异常 → 降级返回原值。"""
        container = _make_container()
        settings = _make_settings(dry_run=True)
        norm = ShareNormalizer(container, settings)

        pan = MagicMock()
        pan.list_dir = AsyncMock(side_effect=RuntimeError("network error"))

        result = await norm.normalize(pan, 100, "Some Dir", True, 0)

        assert result.fid == 100
        assert result.changed is False
        assert result.actions == []
