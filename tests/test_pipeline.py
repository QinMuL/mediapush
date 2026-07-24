"""pipeline 单测：去重入库、互斥、可取消（FIRST_COMPLETED，1-2 秒中断）。"""
import asyncio
import tempfile
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import repository
from app.db.base import Base
from app.pipeline import Pipeline, PipelineContext


@pytest.fixture
async def session_factory():
    """文件 sqlite（多 session 共享同一 DB）。"""
    db_path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_pan115(files_per_share=2):
    async def iter_files(code, pwd):
        for i in range(files_per_share):
            yield {"name": f"Show.S01E0{i}.1080p.mkv", "size": 1000}

    pan = AsyncMock()
    pan.iter_share_files = iter_files
    return pan


def _make_tmdb():
    tmdb = AsyncMock()
    tmdb.search.return_value = [{"id": 42}]
    tmdb.get_details.return_value = {
        "name": "Show", "original_name": "Show",
        "overview": "", "poster_path": "/x.png",
        "number_of_episodes": 10, "status": "Ended",
    }
    tmdb.get_total_episodes.return_value = 10
    tmdb.fill_episodes_from_season.return_value = 10
    return tmdb


# ---- 去重 + 入库 ----
async def test_pipeline_dedup_and_insert(session_factory):
    pan = _make_pan115()
    tmdb = _make_tmdb()
    pipeline = Pipeline(pan, tmdb, session_factory)
    result = await pipeline.run([("c1", "p1"), ("c2", "p2")], trigger="manual")
    assert result["new"] == 2
    assert result["existing"] == 0
    assert result["cancelled"] is False

    # 第二次跑同样 code 应全部去重
    pipeline2 = Pipeline(pan, tmdb, session_factory)
    result2 = await pipeline2.run([("c1", "p1"), ("c2", "p2")], trigger="manual")
    assert result2["new"] == 0
    assert result2["existing"] == 2


# ---- 互斥 ----
async def test_pipeline_mutex(session_factory):
    pan = _make_pan115()
    tmdb = _make_tmdb()
    ctx = PipelineContext()
    pipeline = Pipeline(pan, tmdb, session_factory, context=ctx)
    # 模拟已在运行
    assert ctx.start() is True
    result = await pipeline.run([("c1", "p1")], trigger="manual")
    assert result == {"skipped": True}
    ctx.finish()


# ---- 可取消（核心约束：1-2 秒中断）----
async def test_pipeline_cancels_fast(session_factory):
    async def slow_files(code, pwd):
        # 每个文件 sleep，模拟慢速 115 接口
        for i in range(50):
            await asyncio.sleep(0.3)
            yield {"name": f"Slow.S01E{i:02d}.mkv", "size": 1}

    pan = AsyncMock()
    pan.iter_share_files = slow_files
    tmdb = _make_tmdb()
    pipeline = Pipeline(pan, tmdb, session_factory, concurrency=3)

    codes = [("c1", "p1"), ("c2", "p2"), ("c3", "p3")]
    task = asyncio.create_task(pipeline.run(codes, trigger="manual"))
    await asyncio.sleep(0.5)  # 等待启动并开始抓取
    t0 = asyncio.get_event_loop().time()
    pipeline.ctx.stop()
    result = await asyncio.wait_for(task, timeout=3.0)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result["cancelled"] is True
    # 必须在 2 秒内中断（旧项目约束）
    assert elapsed < 2.0, f"取消耗时 {elapsed:.2f}s，超过 2 秒"


# ---- stop 入口检查 ----
async def test_pipeline_stop_before_fetch(session_factory):
    pan = _make_pan115()
    tmdb = _make_tmdb()
    pipeline = Pipeline(pan, tmdb, session_factory)
    pipeline.ctx.start()
    pipeline.ctx.stop()  # 预先停止
    # _fetch_one 入口应立即返回 None
    result = await pipeline._fetch_one(asyncio.Semaphore(1), "c1", "p1")
    assert result is None
    pipeline.ctx.finish()


# ---- pusher 集成：插入后推送并标记 pushed ----
async def test_pipeline_pushes_after_insert(session_factory):

    pan = _make_pan115()
    tmdb = _make_tmdb()

    pushed_shares = []

    class FakePusher:
        async def push_share(self, share, media):
            pushed_shares.append((share.share_code, media is not None))
            return True

    pipeline = Pipeline(pan, tmdb, session_factory, pusher=FakePusher())
    result = await pipeline.run([("c1", "p1")], trigger="manual")
    assert result["new"] == 1
    assert result["pushed"] == 1
    assert pushed_shares == [("c1", True)]  # 推送了一次，media 非空

    # DB 中 share.pushed 应为 True
    async with session_factory() as s:
        share = await repository.get_share_by_code(s, "c1", "p1")
        assert share.pushed is True
        assert share.pushed_at is not None


async def test_pipeline_pusher_failure_does_not_mark_pushed(session_factory):

    pan = _make_pan115()
    tmdb = _make_tmdb()

    class FailPusher:
        async def push_share(self, share, media):
            return False  # 推送失败

    pipeline = Pipeline(pan, tmdb, session_factory, pusher=FailPusher())
    result = await pipeline.run([("c1", "p1")], trigger="manual")
    assert result["new"] == 1
    assert result["pushed"] == 0  # 推送失败不计
    async with session_factory() as s:
        share = await repository.get_share_by_code(s, "c1", "p1")
        assert share.pushed is False  # 未标记
