"""Bot 命令菜单注册测试。"""

import asyncio

from app.telegram.handlers import _BOT_COMMANDS, setup_commands


class _FakeBot:
    def __init__(self) -> None:
        self.deleted = False
        self.set_commands = None

    async def delete_my_commands(self, *args, **kwargs) -> bool:
        self.deleted = True
        return True

    async def set_my_commands(self, commands, *args, **kwargs) -> bool:
        self.set_commands = commands
        return True


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def test_bot_commands_structure():
    """命令清单顺序与描述非空。"""
    cmds = [c.command for c in _BOT_COMMANDS]
    assert cmds == ["start", "help", "115", "status", "refresh"]
    for c in _BOT_COMMANDS:
        assert c.description, f"{c.command} 描述为空"


def test_setup_commands_clears_then_sets():
    """setup_commands 先删除旧菜单，再设置新菜单。"""
    app = _FakeApp()
    asyncio.run(setup_commands(app))
    assert app.bot.deleted is True  # 先清除残留
    assert app.bot.set_commands is _BOT_COMMANDS  # 再注册新菜单
