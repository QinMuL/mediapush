"""监控账号登录 CLI（备选方式）：

    python -m app.monitor.login

推荐方式：直接向 Bot 发送 /mon login，在对话中完成手机号 → 验证码 →
两步验证密码的交互式登录（无需 SSH 进容器）。

前置：.env 配置 TG_API_ID / TG_API_HASH（https://my.telegram.org → API development tools 申请）。
成功后生成 monitor.session（MONITOR_SESSION 可配路径），容器内监控服务自动复用；
session 文件随 ./data 卷持久化，切勿泄露（等同账号登录态）。
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.monitor.service import parse_proxy


async def main() -> None:
    settings = Settings.load()
    if not settings.tg_api_id or not settings.tg_api_hash:
        print("❌ 请先在 .env 配置 TG_API_ID / TG_API_HASH")
        print("   申请入口：https://my.telegram.org → API development tools")
        return

    from telethon import TelegramClient

    proxy = parse_proxy(settings.proxy_url)
    if settings.proxy_url and proxy is None:
        print(f"⚠️ 代理 {settings.proxy_url} 解析失败，将尝试直连（国内网络可能无法访问）")

    client = TelegramClient(
        settings.monitor_session,
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=proxy,
    )
    # client.start 交互式：手机号 → 验证码 → 两步验证密码（如有）
    await client.start()
    me = await client.get_me()
    print(f"✅ 登录成功：{getattr(me, 'first_name', '')}（{getattr(me, 'phone', '')}）")
    print(f"session 已保存：{settings.monitor_session}")
    print("重启 Bot 后频道监控将自动启用（/mon add @频道 开始监控）")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
