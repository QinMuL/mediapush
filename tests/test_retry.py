"""retry_async 测试：瞬时重试、永久不重试、重试耗尽。"""
import pytest

from app.core.retry import retry_async


async def test_succeeds_first_try():
    calls = []

    async def factory():
        calls.append(1)
        return "ok"

    assert await retry_async(factory, retries=3, base_delay=0) == "ok"
    assert len(calls) == 1


async def test_retries_then_succeeds():
    state = {"n": 0}

    async def factory():
        state["n"] += 1
        if state["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert await retry_async(
        factory, retries=3, base_delay=0, is_transient=lambda e: True
    ) == "ok"
    assert state["n"] == 3


async def test_permanent_error_no_retry():
    calls = []

    async def factory():
        calls.append(1)
        raise KeyError("perm")

    with pytest.raises(KeyError):
        await retry_async(
            factory, retries=3, base_delay=0, is_transient=lambda e: False
        )
    assert len(calls) == 1  # 永久错误立即抛，不重试


async def test_exhausts_retries_then_raises():
    state = {"n": 0}

    async def factory():
        state["n"] += 1
        raise ValueError("always")

    with pytest.raises(ValueError):
        await retry_async(
            factory, retries=3, base_delay=0, is_transient=lambda e: True
        )
    assert state["n"] == 3


async def test_default_none_is_transient_retries_all():
    state = {"n": 0}

    async def factory():
        state["n"] += 1
        if state["n"] < 2:
            raise RuntimeError("x")
        return "ok"

    assert await retry_async(factory, retries=3, base_delay=0) == "ok"
    assert state["n"] == 2
