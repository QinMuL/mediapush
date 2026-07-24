"""TelegramService 测试：构建（concurrent_updates + handlers）、生命周期（fake app）。"""
import pytest

from app.telegram.bot import TelegramService


# ---- 构建 ----
def test_build_app_sets_concurrent_updates():
    """旧项目约束：必须 concurrent_updates(True)，否则长 handler 阻塞队列。"""
    svc = TelegramService("1:fake")
    app = svc._build_app()
    assert app.concurrent_updates > 1  # True → 256；默认串行为 1


def test_build_app_registers_handlers():
    class FakeContainer:
        pass

    svc = TelegramService("1:fake", container=FakeContainer())
    app = svc._build_app()
    # 7 个命令：start help status stop find 115 refresh
    total = sum(len(v) for v in app.handlers.values())
    assert total == 7


def test_build_app_with_proxy_does_not_crash():
    svc = TelegramService("1:fake", proxy_url="http://127.0.0.1:7890")
    app = svc._build_app()
    assert app is not None


async def test_is_running_false_before_start():
    svc = TelegramService("1:fake")
    assert await svc.is_running() is False


# ---- 生命周期（fake app 注入）----
class FakeUpdater:
    def __init__(self):
        self.running = False
        self.started = False
        self.stopped = False

    async def start_polling(self, **kw):
        self.started = True
        self.running = True

    async def stop(self):
        self.stopped = True
        self.running = False


class FakeApp:
    def __init__(self):
        self.updater = FakeUpdater()
        self.running = False
        self._initialized = False
        self._started = False
        self._shutdown = False
        self.handlers = {}

    async def initialize(self):
        self._initialized = True

    async def start(self):
        self._started = True
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        self._shutdown = True


async def test_start_lifecycle_sequence():
    svc = TelegramService("1:fake")
    fake = FakeApp()
    svc._build_app = lambda: fake  # 注入 fake
    await svc.start()
    assert fake._initialized is True
    assert fake._started is True
    assert fake.updater.started is True
    # 重复 start 安全
    await svc.start()


async def test_stop_lifecycle_sequence():
    svc = TelegramService("1:fake")
    fake = FakeApp()
    svc._build_app = lambda: fake
    await svc.start()
    await svc.stop()
    assert fake.updater.stopped is True
    assert fake._shutdown is True
    assert svc._app is None


async def test_stop_when_not_started_is_safe():
    svc = TelegramService("1:fake")
    await svc.stop()  # 不应抛


async def test_send_message_requires_running():
    svc = TelegramService("1:fake")
    with pytest.raises(RuntimeError):
        await svc.send_message("chat", "hi")


async def test_send_photo_requires_running():
    svc = TelegramService("1:fake")
    with pytest.raises(RuntimeError):
        await svc.send_photo("chat", "http://x/x.png", "cap")
