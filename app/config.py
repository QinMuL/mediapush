"""配置加载：从 .env / 环境变量读取，dataclass 承载，启动期校验。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                logger.warning("配置 %s 含非法 ID：%r，已忽略", name, part)
    return out


@dataclass
class Settings:
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    tmdb_language: str = "zh-CN"
    pan115_cookie: str = ""
    pan115_use_proxy: bool = False
    proxy_url: str = ""
    log_level: str = "INFO"
    log_color: bool = True
    db_path: str = "./data/cache.db"

    @classmethod
    def load(cls) -> Settings:
        """从环境变量加载；dotenv 仅在本地存在 .env 时生效。"""
        try:  # 本地开发：读 .env；生产容器由 env_file 注入，无需 dotenv
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:  # noqa: S110, BLE001 - dotenv 可选，失败静默
            pass

        settings = cls(
            tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
            tg_chat_id=os.getenv("TG_CHAT_ID", "").strip(),
            tg_admin_ids=_env_int_list("TG_ADMIN_IDS"),
            tmdb_api_key=os.getenv("TMDB_API_KEY", "").strip(),
            tmdb_language=os.getenv("TMDB_LANGUAGE", "zh-CN").strip() or "zh-CN",
            pan115_cookie=os.getenv("PAN115_COOKIE", "").strip(),
            pan115_use_proxy=_env_bool("PAN115_USE_PROXY", False),
            proxy_url=os.getenv("PROXY_URL", "").strip(),
            log_level=(os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
            log_color=_env_bool("LOG_COLOR", True),
            db_path=os.getenv("DB_PATH", "./data/cache.db").strip() or "./data/cache.db",
        )
        settings._ensure_dirs()
        return settings

    def _ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """返回告警列表（不抛错，缺失项由 Container 决定是否阻塞启动）。"""
        warns: list[str] = []
        if not self.tg_bot_token:
            warns.append("TG_BOT_TOKEN 未配置，Bot 将不启动。")
        if not self.tg_chat_id:
            warns.append("TG_CHAT_ID 未配置，无法推送。")
        if not self.tg_admin_ids:
            warns.append("TG_ADMIN_IDS 未配置，无人可使用 Bot。")
        if not self.tmdb_api_key:
            warns.append("TMDB_API_KEY 未配置，无法拉取 TMDB 元数据。")
        # PAN115_COOKIE 可选：读取分享走匿名 web，无需 cookie；cookie 仅用于 /status 健康检查
        if not self.proxy_url:
            warns.append("PROXY_URL 未配置，TG/TMDB 在国内网络可能无法访问。")
        return warns

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.tg_admin_ids
