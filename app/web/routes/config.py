"""配置管理：在线编辑 + 热生效。

热生效分发（ARCHITECTURE.md 第 9.2 / 决策第 6 条）：
- pan115_cookie / tmdb_api_key / tg_bot_token / tg_chat_id / proxy_* → container.on_config_changed
- schedule_interval → scheduler.update_interval（联动重算 full_scan / health）
- log_level → 仅持久化，重启生效
- 其余（monitored_shares / full_scan_interval_runs / pan115_health_interval）→ 仅持久化

密钥类字段（secret）提交留空表示不修改，避免明文回显与误清空。
"""
from fastapi import APIRouter, Request

from app.db import repository
from app.web.templates import templates

router = APIRouter(prefix="/config")

# (key, label, type, group, secret)
CONFIG_FIELDS: list[tuple[str, str, str, str, bool]] = [
    ("admin_password", "管理员密码", "password", "安全", True),
    ("tg_bot_token", "TG Bot Token", "password", "Telegram", True),
    ("tg_chat_id", "推送频道/群 ID", "text", "Telegram", False),
    ("pan115_cookie", "115 Cookie", "textarea", "115 网盘", True),
    ("tmdb_api_key", "TMDB API Key", "password", "TMDB", True),
    ("schedule_interval", "调度间隔（分钟，推荐 5-10）", "number", "调度", False),
    ("full_scan_interval_runs", "全量扫描周期（次，0=联动）", "number", "调度", False),
    ("pan115_health_interval", "健康检查间隔（秒）", "number", "调度", False),
    ("monitored_shares", "监控分享列表（逗号分隔 code:password）", "textarea", "调度", False),
    ("log_level", "日志级别（重启生效）", "select", "日志", False),
    ("proxy_enabled", "启用代理", "checkbox", "代理", False),
    ("proxy_url", "代理地址", "text", "代理", False),
    ("proxy_targets", "走代理的服务（逗号分隔 tg,tmdb,115）", "text", "代理", False),
]
SECRET_KEYS = {
    "admin_password", "pan115_cookie", "tmdb_api_key", "tg_bot_token",
}
# 改动需触发组件重建的键（tg_chat_id 改后重建 bot 以更新 pusher chat_id）
REBUILD_KEYS = {
    "pan115_cookie", "tmdb_api_key", "proxy_enabled", "proxy_url",
    "proxy_targets", "tg_bot_token", "tg_chat_id",
}
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


def _field_view(key, label, ftype, group, secret, value):
    return {
        "key": key,
        "label": label,
        "type": ftype,
        "group": group,
        "secret": secret,
        "value": "" if secret else value,
        "configured": bool(value) if secret else True,
        "options": LOG_LEVELS if ftype == "select" else [],
    }


@router.get("")
async def config_page(request: Request):
    container = request.app.state.container
    async with container.session_factory() as session:
        all_cfg = await repository.get_all_config(session)
    fields = [
        _field_view(k, label, ftype, group, secret, all_cfg.get(k, ""))
        for k, label, ftype, group, secret in CONFIG_FIELDS
    ]
    # 按组分組，保持插入顺序
    groups: dict[str, list] = {}
    for f in fields:
        groups.setdefault(f["group"], []).append(f)
    return templates.TemplateResponse(
        request, "config.html", {"groups": groups, "messages": []}
    )


@router.post("")
async def config_save(request: Request):
    container = request.app.state.container
    scheduler = getattr(request.app.state, "scheduler", None)
    form = await request.form()

    async with container.session_factory() as session:
        all_cfg = await repository.get_all_config(session)

    messages: list[str] = []
    for key, label, ftype, _group, secret in CONFIG_FIELDS:
        if ftype == "checkbox":
            new_val = "true" if form.get(key) == "on" else "false"
        else:
            new_val = (form.get(key) or "").strip()
        old_val = all_cfg.get(key, "")

        # 密钥留空 = 不修改
        if secret and not new_val:
            continue

        if new_val == old_val:
            continue

        async with container.session_factory() as session:
            await repository.set_config(session, key, new_val)

        if key == "schedule_interval":
            try:
                interval = int(new_val)
            except ValueError:
                messages.append(f"调度间隔非法：{new_val}")
                continue
            if scheduler is not None:
                full, health = await scheduler.update_interval(interval)
                messages.append(
                    f"调度间隔已联动：full_scan={full} 次, health={health}s"
                )
            else:
                messages.append("调度间隔已保存（scheduler 未启动，未联动）")
        elif key in REBUILD_KEYS:
            await container.on_config_changed(key)
            messages.append(f"{label} 已热生效")
        elif key == "log_level":
            messages.append(f"{label} 已保存（重启后生效）")
        else:
            messages.append(f"{label} 已保存")

    # 回显结果
    async with container.session_factory() as session:
        all_cfg = await repository.get_all_config(session)
    fields = [
        _field_view(k, label, ftype, group, secret, all_cfg.get(k, ""))
        for k, label, ftype, group, secret in CONFIG_FIELDS
    ]
    groups: dict[str, list] = {}
    for f in fields:
        groups.setdefault(f["group"], []).append(f)
    return templates.TemplateResponse(
        request, "config.html", {"groups": groups, "messages": messages}
    )
