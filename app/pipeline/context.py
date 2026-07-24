"""流水线运行态：is_running 互斥、_stop_event 取消。

手动触发与定时任务共享同一个 context，互斥不并发（决策记录第 4 条）。
"""
import asyncio


class PipelineContext:
    def __init__(self):
        self._stop_event = asyncio.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def start(self) -> bool:
        """启动；若已在运行返回 False（互斥）。"""
        if self._running:
            return False
        self._running = True
        self._stop_event.clear()
        return True

    def stop(self) -> None:
        """请求停止（设置 _stop_event）。"""
        self._stop_event.set()

    def finish(self) -> None:
        self._running = False
