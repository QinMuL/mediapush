import asyncio

from app.core.link_parser import ParsedShare
from app.core.processor import ShareProcessor
from app.providers.base import ShareFile


class FakePan115:
    def __init__(self, files):
        self._files = files

    async def list_share(self, code, password):
        return self._files


class FakeTMDB:
    def __init__(self, best=None, details=None, details_by_id=None):
        self._best = best
        self._details = details
        self._details_by_id = details_by_id or {}
        self.search_calls = []
        self.details_calls = []

    async def search_best(self, title, year, media_type):
        self.search_calls.append((title, year, media_type))
        return self._best

    async def get_details(self, tmdb_id, media_type):
        self.details_calls.append((tmdb_id, media_type))
        if tmdb_id in self._details_by_id:
            d = self._details_by_id[tmdb_id]
            if isinstance(d, Exception):
                raise d
            return d
        return self._details


class FakeCache:
    def __init__(self, pushed_codes=None):
        self._pushed = set(pushed_codes or [])
        self.marked = []

    async def is_pushed(self, code):
        return code in self._pushed

    async def mark_pushed(self, code):
        self.marked.append(code)

    async def delete_tmdb(self, tmdb_id):
        return 0


class FakePusher:
    def __init__(self):
        self.pushed = []

    async def push_share(self, details, media, code, password, files=None, provider="115"):
        self.pushed.append((code, password, files, provider))
        return True, "已推送（测试）"


class FakeContainer:
    def __init__(self, pusher):
        self._pusher = pusher

    @property
    def pusher(self):
        return self._pusher


def _tv_files():
    return [ShareFile(f"Show.S01E0{i}.1080p.WEB-DL.mkv", 0, False) for i in (1, 2)]


def test_dedup_skips_pushed():
    cache = FakeCache(pushed_codes=["CODE"])
    proc = ShareProcessor(FakePan115(_tv_files()), None, FakeTMDB(), cache, FakeContainer(FakePusher()))

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert not r.ok
        assert "已推送过" in r.message

    asyncio.run(run())


def test_no_tmdb_match():
    cache = FakeCache()
    proc = ShareProcessor(FakePan115(_tv_files()), None, FakeTMDB(best=None), cache, FakeContainer(FakePusher()))

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert not r.ok
        assert "未匹配" in r.message
        assert r.file_count == 2

    asyncio.run(run())


def test_success_marks_pushed():
    details = {"tmdb_id": 1, "media_type": "tv", "title": "Show", "year": 2020,
               "poster_path": None, "seasons": [{"season": 1, "episode_count": 2}]}
    cache = FakeCache()
    pusher = FakePusher()
    container = FakeContainer(pusher)
    proc = ShareProcessor(FakePan115(_tv_files()), None, FakeTMDB(best=(1, "tv"), details=details), cache, container)

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", "pwd"))
        assert r.ok
        assert r.file_count == 2
        # push_share 收到 (code, password, files, provider) 四元组
        assert len(pusher.pushed) == 1
        code, pwd, files, provider = pusher.pushed[0]
        assert code == "CODE"
        assert pwd == "pwd"
        assert files is not None
        assert len(files) == 2
        assert provider == "115"
        assert cache.marked == ["CODE"]

    asyncio.run(run())


def test_pan115_not_configured():
    cache = FakeCache()
    proc = ShareProcessor(None, None, FakeTMDB(), cache, FakeContainer(FakePusher()))

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert not r.ok
        assert "115" in r.message

    asyncio.run(run())


def test_tmdb_not_configured():
    cache = FakeCache()
    proc = ShareProcessor(FakePan115(_tv_files()), None, None, cache, FakeContainer(FakePusher()))

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert not r.ok
        assert "TMDB" in r.message

    asyncio.run(run())


# -------------------- ed2k 路由 -------------------- #
from app.providers.ed2k import Ed2kProvider  # noqa: E402

_ED2K_URL = (
    "ed2k://|file|宾虚 (1959) - 2160p.BluRay REMUX.DoVi P7.H.265.10-bit.23.976fps.TrueHD 7.1-WF.mkv"
    "|135915637476|3E874DEBD5E4A7AF8B1EEE7F41E7DD51|/"
)


def test_ed2k_provider_parses_link():
    """Ed2kProvider 解析 ed2k URL 为单文件。"""
    import asyncio

    provider = Ed2kProvider()

    async def run():
        files = await provider.list_share(_ED2K_URL, None)
        assert len(files) == 1
        f = files[0]
        assert f.is_dir is False
        assert "宾虚" in f.name
        assert f.size == 135915637476

    asyncio.run(run())


def test_ed2k_provider_check_health_none():
    import asyncio

    provider = Ed2kProvider()

    async def run():
        assert await provider.check_health() is None

    asyncio.run(run())


def test_ed2k_routes_to_ed2k_provider():
    """ed2k parsed → 路由到 Ed2kProvider → pusher 收到 provider='ed2k'。"""
    details = {
        "tmdb_id": 1, "media_type": "movie", "title": "宾虚", "year": 1959,
        "poster_path": None, "genres": ["剧情"], "cast": [],
        "countries": ["US"], "status": "Released", "release_date": "1959-11-18",
    }
    cache = FakeCache()
    pusher = FakePusher()
    proc = ShareProcessor(
        FakePan115([]),  # 115 不应被调用
        Ed2kProvider(),
        FakeTMDB(best=(1, "movie"), details=details),
        cache,
        FakeContainer(pusher),
    )

    async def run():
        r = await proc.process(ParsedShare("ed2k", _ED2K_URL, None))
        assert r.ok, r.message
        assert len(pusher.pushed) == 1
        code, pwd, files, provider = pusher.pushed[0]
        assert provider == "ed2k"
        assert code == _ED2K_URL  # 完整 URL 作为去重 key
        assert pwd is None
        assert files is not None and len(files) == 1
        assert files[0].size == 135915637476
        assert cache.marked == [_ED2K_URL]

    asyncio.run(run())


def test_ed2k_not_configured():
    """ed2k provider 为 None 时报错。"""
    cache = FakeCache()
    proc = ShareProcessor(FakePan115([]), None, FakeTMDB(), cache, FakeContainer(FakePusher()))

    async def run():
        r = await proc.process(ParsedShare("ed2k", _ED2K_URL, None))
        assert not r.ok
        assert "ed2k" in r.message

    asyncio.run(run())


# -------------------- {tmdb-XXX} 标注路由 -------------------- #
def _movie_files_with_tmdb_tag():
    """文件含 {tmdb-1311031} 目录标注的电影分享。"""
    return [
        ShareFile("Demon Slayer Infinity Castle (2025) {tmdb-1311031}", 0, True),
        ShareFile("Demon Slayer (2025) - 1080p.BluRay.mkv", 42_000_000_000, False),
    ]


def test_tmdb_tag_skips_search():
    """有 {tmdb-XXX} 标注时直接用该 ID 取详情，不调 search_best。"""
    details = {
        "tmdb_id": 1311031, "media_type": "movie", "title": "鬼灭之刃：无限城篇",
        "year": 2025, "poster_path": None, "genres": ["动画"], "cast": [],
        "countries": ["JP"], "status": "Released", "release_date": "2025-01-01",
    }
    tmdb = FakeTMDB(
        best=(999, "movie"),  # 故意给错的 search 结果，验证不会被用到
        details_by_id={1311031: details},
    )
    cache = FakeCache()
    pusher = FakePusher()
    proc = ShareProcessor(
        FakePan115(_movie_files_with_tmdb_tag()), None, tmdb, cache, FakeContainer(pusher),
    )

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert r.ok, r.message
        # 直接用标注 ID，未走搜索
        assert tmdb.search_calls == []
        assert tmdb.details_calls == [(1311031, "movie")]
        assert r.title == "鬼灭之刃：无限城篇"

    asyncio.run(run())


def test_tmdb_tag_fallback_on_failure():
    """标注 TMDB ID 获取失败时回退到 search_best。"""
    details = {
        "tmdb_id": 999, "media_type": "movie", "title": "搜索结果",
        "year": 2025, "poster_path": None, "genres": [], "cast": [],
        "countries": [], "status": "Released", "release_date": "2025-01-01",
    }
    tmdb = FakeTMDB(
        best=(999, "movie"),
        details_by_id={
            1311031: Exception("404 not found"),  # 标注 ID 失效
            999: details,  # 搜索回退命中
        },
    )
    cache = FakeCache()
    pusher = FakePusher()
    proc = ShareProcessor(
        FakePan115(_movie_files_with_tmdb_tag()), None, tmdb, cache, FakeContainer(pusher),
    )

    async def run():
        r = await proc.process(ParsedShare("115", "CODE", None))
        assert r.ok, r.message
        # 先试标注 ID（失败），再走搜索
        assert len(tmdb.search_calls) == 1
        assert (1311031, "movie") in tmdb.details_calls
        assert (999, "movie") in tmdb.details_calls
        assert r.title == "搜索结果"

    asyncio.run(run())
