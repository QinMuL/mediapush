"""异步重试工具：指数退避，区分瞬时/永久错误。

设计（ARCHITECTURE.md 第 8 节工程约束）：
- 405 等 HTTP 永久错误不重试（旧项目约束）
- 瞬时错误（超时、连接、429、5xx）指数退避重试
- is_transient 返回 False 的异常立即向上抛，不浪费重试次数
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    is_transient: Callable[[Exception], bool] | None = None,
    label: str = "操作",
) -> T:
    """重试一个可重建的协程。

    coro_factory 每次调用返回新协程（避免重复 await 同一协程）。
    - is_transient 为 None 时所有异常都重试；否则仅对返回 True 的异常重试，
      永久错误（返回 False）立即抛出。
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if is_transient is not None and not is_transient(e):
                raise  # 永久错误，不重试
            if attempt >= retries:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "%s 第 %d/%d 次失败：%r，%.1fs 后重试", label, attempt, retries, e, delay
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
