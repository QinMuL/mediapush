"""handlers 命令测试（mock update/context/container）。"""
from unittest.mock import AsyncMock

from app.telegram import handlers


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


class FakeContainer:
    """最小 container mock，按需配置返回值。"""

    def __init__(self):
        self.status = {
            "bot_running": True,
            "pipeline_running": False,
            "unpushed": 0,
            "config_health": {
                "tg_token": True, "pan115_cookie": True, "tmdb_key": True,
                "proxy": True, "schedule_interval": "5",
            },
        }
        self.run_result = {"new": 1, "pushed": 1, "cancelled": False}
        self.pan115_ready_val = True
        self.refresh_count = 1
        self.stop_called = False
        self.monitored = [("abc12345", "pwd123")]

    async def get_status(self):
        return self.status

    def is_pipeline_running(self):
        return self.status["pipeline_running"]

    def stop_pipeline(self):
        self.stop_called = True

    async def run_pipeline_once(self, codes, trigger="manual"):
        return self.run_result

    def pan115_ready(self):
        return self.pan115_ready_val

    async def refresh_tmdb(self, tmdb_id):
        return self.refresh_count

    async def get_monitored_shares(self):
        return self.monitored


# ---- /start /help ----
async def test_start_replies_help():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    await handlers.handle_start(u, c, cont)
    assert "MediaPush 机器人" in u.message.replies[0]


async def test_help_replies_help():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    await handlers.handle_help(u, c, cont)
    assert "/115" in u.message.replies[0]


# ---- /status ----
async def test_status_replies_config_health():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    await handlers.handle_status(u, c, cont)
    reply = u.message.replies[0]
    assert "Bot" in reply
    assert "TG Token" in reply
    assert "115 Cookie" in reply
    assert "未推送" in reply


# ---- /stop ----
async def test_stop_when_idle():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    cont.status["pipeline_running"] = False
    await handlers.handle_stop(u, c, cont)
    assert "没有" in u.message.replies[0]
    assert not cont.stop_called


async def test_stop_when_running():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    cont.status["pipeline_running"] = True
    await handlers.handle_stop(u, c, cont)
    assert cont.stop_called is True
    assert "停止" in u.message.replies[0]


# ---- /find ----
async def test_find_no_monitored():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    cont.monitored = []
    await handlers.handle_find(u, c, cont)
    assert "未配置监控" in u.message.replies[0]


async def test_find_pan115_not_ready():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    cont.pan115_ready_val = False
    await handlers.handle_find(u, c, cont)
    assert "Cookie 未配置" in u.message.replies[0]


async def test_find_success():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    await handlers.handle_find(u, c, cont)
    # [0] 处理中, [1] 结果
    assert "扫描完成" in u.message.replies[1]
    assert "新增 1" in u.message.replies[1]


async def test_find_skipped():
    u, c, cont = FakeUpdate(), FakeContext(), FakeContainer()
    cont.run_result = {"skipped": True}
    await handlers.handle_find(u, c, cont)
    assert "在运行" in u.message.replies[1]


# ---- /115 ----
async def test_115_no_args():
    u, c, cont = FakeUpdate(), FakeContext([]), FakeContainer()
    await handlers.handle_115(u, c, cont)
    assert "用法" in u.message.replies[0]


async def test_115_unparseable():
    u, c, cont = FakeUpdate(), FakeContext(["hello", "world"]), FakeContainer()
    await handlers.handle_115(u, c, cont)
    # 无法解析时直接回复，无"处理中"
    assert "无法解析" in u.message.replies[0]


async def test_115_pan115_not_ready():
    u = FakeUpdate()
    c = FakeContext(["https://115.com/s/abc12345?password=xyz"])
    cont = FakeContainer()
    cont.pan115_ready_val = False
    await handlers.handle_115(u, c, cont)
    # ready 检查在"处理中"之前，只有 1 条回复
    assert "Cookie 未配置" in u.message.replies[0]


async def test_115_success():
    u = FakeUpdate()
    c = FakeContext(["https://115.com/s/abc12345?password=xyz"])
    cont = FakeContainer()
    await handlers.handle_115(u, c, cont)
    assert "新增 1 条" in u.message.replies[1]
    assert "已推送 1 条" in u.message.replies[1]


async def test_115_already_exists():
    u = FakeUpdate()
    c = FakeContext(["abc12345"])
    cont = FakeContainer()
    cont.run_result = {"new": 0, "pushed": 0, "cancelled": False}
    await handlers.handle_115(u, c, cont)
    assert "已存在" in u.message.replies[1]


async def test_115_skipped():
    u = FakeUpdate()
    c = FakeContext(["abc12345"])
    cont = FakeContainer()
    cont.run_result = {"skipped": True}
    await handlers.handle_115(u, c, cont)
    assert "在运行" in u.message.replies[1]


async def test_115_cancelled():
    u = FakeUpdate()
    c = FakeContext(["abc12345"])
    cont = FakeContainer()
    cont.run_result = {"new": 1, "pushed": 0, "cancelled": True}
    await handlers.handle_115(u, c, cont)
    assert "停止" in u.message.replies[1]


async def test_115_permanent_error():
    from app.pan115.client import Pan115PermanentError

    u = FakeUpdate()
    c = FakeContext(["abc12345"])
    cont = FakeContainer()
    cont.run_pipeline_once = AsyncMock(side_effect=Pan115PermanentError("405"))
    await handlers.handle_115(u, c, cont)
    assert "永久错误" in u.message.replies[1]


# ---- /refresh ----
async def test_refresh_valid():
    u = FakeUpdate()
    c = FakeContext(["295558"])
    cont = FakeContainer()
    cont.refresh_count = 2
    await handlers.handle_refresh(u, c, cont)
    assert "删除 2 条" in u.message.replies[0]


async def test_refresh_invalid_id():
    u = FakeUpdate()
    c = FakeContext(["abc"])
    cont = FakeContainer()
    await handlers.handle_refresh(u, c, cont)
    assert "数字" in u.message.replies[0]


async def test_refresh_not_found():
    u = FakeUpdate()
    c = FakeContext(["999"])
    cont = FakeContainer()
    cont.refresh_count = 0
    await handlers.handle_refresh(u, c, cont)
    assert "未找到" in u.message.replies[0]


# ---- 容错导入验证（旧项目核心约束）----
def test_pan115_error_import_is_exception_subclass():
    """p115client 装坏时 fallback 的 Pan115Error 仍可被 except 捕获。"""
    assert issubclass(handlers.Pan115Error, Exception)
    assert issubclass(handlers.Pan115PermanentError, handlers.Pan115Error)


# ---- register_handlers ----
def test_register_handlers_registers_all():
    class FakeApp:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h):
            self.handlers.append(h)

    app = FakeApp()
    handlers.register_handlers(app, FakeContainer())
    # 7 个命令：start help status stop find 115 refresh
    assert len(app.handlers) == 7
