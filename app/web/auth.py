"""管理员鉴权：itsdangerous 签名 cookie 会话 + 首次启动生成密钥。

设计（ARCHITECTURE.md 第 7 节 / 决策记录第 3 条）：
- 单管理员密码登录，密码持久化在 app_config.admin_password
- 会话用 itsdangerous URLSafeTimedSerializer 签名 cookie，密钥 web_secret
- 首次启动 admin_password / web_secret 为空时由 bootstrap_secrets 生成并持久化
- 不用 Starlette SessionMiddleware：其 secret_key 需在 app 构造时确定，
  而 web_secret 在 lifespan（DB 就绪后）才生成；签名 cookie 在请求期读取密钥更契合生命周期
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repository

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mediapush_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 天
_SALT = "mediapush-session"


async def _get_secret(session_factory: async_sessionmaker) -> str:
    async with session_factory() as session:
        secret = await repository.get_config(session, "web_secret", "")
    # bootstrap_secrets 应保证非空；兜底防崩
    return secret or "insecure-fallback-please-config-web_secret"


async def create_session_token(
    session_factory: async_sessionmaker, data: dict
) -> str:
    secret = await _get_secret(session_factory)
    serializer = URLSafeTimedSerializer(secret, salt=_SALT)
    return serializer.dumps(data)


async def verify_session_token(
    session_factory: async_sessionmaker, token: str | None
) -> dict | None:
    if not token:
        return None
    secret = await _get_secret(session_factory)
    serializer = URLSafeTimedSerializer(secret, salt=_SALT)
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


async def is_authed(session_factory: async_sessionmaker, token: str | None) -> bool:
    data = await verify_session_token(session_factory, token)
    return bool(data and data.get("authed"))


async def verify_password(session_factory: async_sessionmaker, password: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    async with session_factory() as session:
        stored = await repository.get_config(session, "admin_password", "")
    if not stored:
        return False
    return secrets.compare_digest(stored, password)


async def bootstrap_secrets(session_factory: async_sessionmaker) -> None:
    """首次启动生成 admin_password 与 web_secret 并持久化。

    admin_password 打印到日志供用户首次登录；web_secret 仅持久化不外露。
    """
    async with session_factory() as session:
        admin_pw = await repository.get_config(session, "admin_password", "")
        web_secret = await repository.get_config(session, "web_secret", "")
        changed = False
        if not admin_pw:
            admin_pw = secrets.token_urlsafe(12)
            await repository.set_config(session, "admin_password", admin_pw)
            changed = True
        if not web_secret:
            web_secret = secrets.token_hex(32)
            await repository.set_config(session, "web_secret", web_secret)
            changed = True
    if changed:
        logger.warning(
            "首次启动：已生成管理员密码与 Web 会话密钥。"
            "管理员密码：%s（登录后请在「配置管理」页修改）",
            admin_pw,
        )


def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
