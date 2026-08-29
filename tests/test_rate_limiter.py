"""AdaptiveLimiter 限速器测试：稳态节奏 / margin 降速 / 成功恢复 / 热更新。"""

import asyncio
import time

from app.core.rate_limiter import AdaptiveLimiter


def test_acquire_paces_requests():
    """稳态节奏：burst=1 时连续 acquire 的间隔 ≈ base_interval。"""
    lim = AdaptiveLimiter(0.05, burst=1)
    n = 6

    async def run():
        for _ in range(n):
            await lim.acquire()

    start = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - start
    # n 次取令牌：首発即取（burst 预置 1），其后每次需等 ~base_interval
    assert elapsed >= (n - 1) * 0.05 * 0.8


def test_burst_allows_initial_burst():
    """burst 允许开局小突发：前 burst 个 acquire 立即返回。"""
    lim = AdaptiveLimiter(10.0, burst=3)

    async def run():
        for _ in range(3):
            await lim.acquire()

    start = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - start < 1.0


def test_hit_limit_doubles_interval():
    """margin 限速信号 → interval 翻倍（上限 max_interval），成功计数归零。"""
    lim = AdaptiveLimiter(1.0, max_interval=60.0)
    lim.success()
    lim.success()
    lim.hit_limit(reason="margin")
    assert lim.interval == 2.0
    lim.hit_limit(reason="margin")
    assert lim.interval == 4.0
    # 上限截断
    for _ in range(10):
        lim.hit_limit(reason="margin")
    assert lim.interval == 60.0


def test_success_streak_decays_interval():
    """连续成功 N 次 → interval 向 base 减半衰减。"""
    lim = AdaptiveLimiter(1.0)
    lim.hit_limit(reason="margin")
    lim.hit_limit(reason="margin")
    assert lim.interval == 4.0

    for _ in range(20):
        lim.success()
    assert lim.interval == 2.0
    for _ in range(20):
        lim.success()
    assert lim.interval == 1.0
    # 已到 base，不再降
    for _ in range(20):
        lim.success()
    assert lim.interval == 1.0


def test_set_base_interval_converges():
    """/reload 热更新 base：更保守（新 base 大）时当前 interval 立即抬高。"""
    lim = AdaptiveLimiter(1.0, max_interval=60.0)
    lim.set_base_interval(3.0)
    assert lim.base_interval == 3.0
    assert lim.interval == 3.0

    # margin 抬到 6s 后，把 base 收紧到 0.5：保留 6s 直到自然衰减（防误调打爆）
    lim.hit_limit(reason="margin")
    assert lim.interval == 6.0
    lim.set_base_interval(0.5)
    assert lim.base_interval == 0.5
    assert lim.interval == 6.0
    # 衰减逐次减半：6 → 3 → 1.5 → 0.75 → 0.5（到 base 为止）
    for expected in (3.0, 1.5, 0.75, 0.5):
        for _ in range(20):
            lim.success()
        assert lim.interval == expected
