"""运行时配置默认值。

Web 管理后台可覆盖，持久化在 DB app_config 表。
首次启动由 ensure_default_config 写入；admin_password 留空，由启动逻辑生成随机密码。
"""

DEFAULT_CONFIG: dict[str, str] = {
    "admin_password": "",  # 首次启动生成随机密码
    "web_secret": "",  # 会话签名密钥，首次启动生成（itsdangerous）
    "tg_bot_token": "",
    "tg_chat_id": "",
    "pan115_cookie": "",
    "tmdb_api_key": "",
    "schedule_interval": "5",  # 分钟，5-10 推荐
    "full_scan_interval_runs": "24",  # 目标 2 小时全量一次
    "pan115_health_interval": "300",  # 秒
    "log_level": "INFO",
    "proxy_enabled": "true",
    "proxy_url": "http://host.docker.internal:7890",  # 容器内访问宿主机代理
    "proxy_targets": "tg,tmdb",  # 走代理的服务：tg/tmdb/115
    # 定时扫描的监控分享列表：逗号分隔，code:password 或 code
    "monitored_shares": "",
}
