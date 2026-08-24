"""频道监控模块：Telethon 用户账号监控公开频道 → 提取 ed2k → Bot 推送。

- store.py   配置持久化（SQLite，独立 monitor.db）
- watcher.py ed2k 提取/验证/过滤/渲染（纯函数）
- service.py Telethon 封装 + 生命周期
- login.py   首次登录 CLI（python -m app.monitor.login）
"""
