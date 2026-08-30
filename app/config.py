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


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw.strip()) if raw.strip() else default
    except ValueError:
        logger.warning("配置 %s 非法（%r），使用默认 %s", name, raw, default)
        return default


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw.strip()) if raw.strip() else default
    except ValueError:
        logger.warning("配置 %s 非法（%r），使用默认 %s", name, raw, default)
        return default


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
    tg_chat_id_115: str = ""
    tg_chat_id_ed2k: str = ""
    tg_admin_ids: list[int] = field(default_factory=list)
    tmdb_api_key: str = ""
    tmdb_language: str = "zh-CN"
    pan115_cookie: str = ""
    # cookie 真实来源标记：True=PAN115_COOKIE 环境变量直配；False=文件/未配置。
    # 注意 pan115_cookie 字段可能被文件内容回填，判“直配”必须用本标记
    # （/status 展示来源、/cookie 直配保护均依赖）
    pan115_cookie_direct: bool = False
    pan115_cookie_file: str = ""  # cookie 文件路径（挂载更新免重建容器）
    pan115_use_proxy: bool = False
    proxy_url: str = ""
    log_level: str = "INFO"
    log_color: bool = True
    log_file: str = "./data/logs/mediapush.log"
    db_path: str = "./data/cache.db"
    # 频道监控（Telethon 用户账号，见 app/monitor/）
    tg_api_id: int = 0
    tg_api_hash: str = ""
    monitor_enabled: bool = True
    monitor_session: str = "./data/monitor.session"
    monitor_db_path: str = "./data/monitor.db"
    monitor_batch_seconds: int = 0
    # 115 请求稳态间隔（秒，令牌桶自适应的基准；margin 限速时自动翻倍回落）
    pan115_request_interval: float = 1.0
    # 分享失效巡检（见 app/telegram/inspector.py）
    inspect_enabled: bool = True
    inspect_interval_hours: float = 6.0
    inspect_notify: bool = True
    # 巡检网络异常连续 N 轮 → 私信告警（避免每轮网络抖动都打扰）
    inspect_error_alert_rounds: int = 2
    # 巡检发现缺访问码（存活但需补档）时私信提醒（/edit 可重推补档）
    inspect_notify_code: bool = True
    # 115 cookie 失效告警私信（随巡检循环，24h 节流）
    cookie_alert: bool = True
    # 目录监控 → 自动建永久分享（见 app/core/share_watcher.py）
    share_watch_enabled: bool = True
    share_watch_interval_minutes: float = 10.0
    # 每轮扫描结果（成功/失败明细）私信 admin；静默轮不打扰
    share_watch_notify: bool = True
    # 推送成功后移入的归档目录（须在监控目录之外；空=不移动仅标记）
    share_archive_dir: str = "/已分享"
    # 频道监控（Telethon）断连/重连/补扫等运行事件私信 admin
    monitor_notify: bool = True
    # 本地媒体流水线（目录A监控 → namer 重命名 → 目录B，见 app/media/service.py）
    local_media_enabled: bool = False
    local_media_input_dir: str = ""    # 目录A：待处理资源（递归监控）
    local_media_output_dir: str = ""   # 目录B：规范化输出（须在A之外）
    local_media_dry_run: bool = True   # 模拟模式：只出日志不实际移动
    local_media_interval_seconds: float = 10.0  # 扫描周期
    local_media_stable_rounds: int = 3          # 稳定判定轮数（×周期）
    local_media_stuck_days: float = 7.0          # 低置信卡死告警阈值（天）
    # ed2k 流水线（目录B监控 → ed2k 哈希 → 目录C，见 app/media/ed2k_service.py）
    ed2k_enabled: bool = False
    ed2k_input_dir: str = ""                    # 目录B（= local_media_output_dir 通常一致）
    ed2k_output_dir: str = ""                   # 目录C：ed2k 产出后归档（须在B之外）
    ed2k_dry_run: bool = True                    # 模拟模式：只哈希+记 jsonl，不实际移动
    ed2k_interval_seconds: float = 30.0          # 扫描周期（哈希重，30s 起步）
    ed2k_stable_rounds: int = 3                  # 稳定判定轮数（×周期）
    ed2k_stuck_days: float = 7.0                  # 哈希失败卡死告警（天）
    # ed2k 推送（JSONL → ShareProcessor → TG_CHAT_ID_ED2K 频道）
    ed2k_push_enabled: bool = False
    ed2k_push_dry_run: bool = True                 # 模拟：只出日志不实际调用 processor
    ed2k_push_interval_seconds: float = 60.0       # 推送扫描周期秒（60s 起步，卡片+TMDB 较慢）
    ed2k_push_stuck_days: float = 7.0              # 推送失败卡死告警（天）
    ed2k_push_report_admin: bool = True            # 每轮结束把汇总发给 TG_ADMIN_IDS
    ed2k_push_report_channel: bool = False         # 每轮结束把汇总同步发到 TG_CHAT_ID_ED2K（慎用，会刷频道）

    @classmethod
    def load(cls, *, dotenv_override: bool = False) -> Settings:
        """从环境变量加载；dotenv 仅在本地存在 .env 时生效。

        dotenv_override=True 供 /reload 用：重读 .env 时覆盖进程内已有
        变量（否则 load_dotenv 默认不覆盖启动时的值，改文件永远不生效）。
        容器内 env_file 注入的变量不受影响（无 .env 文件时为 no-op）。
        """
        try:  # 本地开发：读 .env；生产容器由 env_file 注入，无需 dotenv
            from dotenv import load_dotenv

            load_dotenv(override=dotenv_override)
        except Exception:  # noqa: S110, BLE001 - dotenv 可选，失败静默
            pass

        # cookie：env 直配优先；否则从 PAN115_COOKIE_FILE 文件读（一整行字符串，
        # 容器挂载后改文件重启即生效；巡检器还会热更新）
        cookie = os.getenv("PAN115_COOKIE", "").strip()
        cookie_direct = bool(cookie)
        cookie_file = os.getenv("PAN115_COOKIE_FILE", "").strip()
        if not cookie and cookie_file:
            try:
                cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("PAN115_COOKIE_FILE 读取失败：%s", exc)

        settings = cls(
            tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
            tg_chat_id=os.getenv("TG_CHAT_ID", "").strip(),
            tg_chat_id_115=os.getenv("TG_CHAT_ID_115", "").strip(),
            tg_chat_id_ed2k=os.getenv("TG_CHAT_ID_ED2K", "").strip(),
            tg_admin_ids=_env_int_list("TG_ADMIN_IDS"),
            tmdb_api_key=os.getenv("TMDB_API_KEY", "").strip(),
            tmdb_language=os.getenv("TMDB_LANGUAGE", "zh-CN").strip() or "zh-CN",
            pan115_cookie=cookie,
            pan115_cookie_direct=cookie_direct,
            pan115_cookie_file=cookie_file,
            pan115_use_proxy=_env_bool("PAN115_USE_PROXY", False),
            proxy_url=os.getenv("PROXY_URL", "").strip(),
            log_level=(os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
            log_color=_env_bool("LOG_COLOR", True),
            log_file=os.getenv("LOG_FILE", "./data/logs/mediapush.log").strip(),
            db_path=os.getenv("DB_PATH", "./data/cache.db").strip() or "./data/cache.db",
            tg_api_id=_env_int("TG_API_ID", 0),
            tg_api_hash=os.getenv("TG_API_HASH", "").strip(),
            monitor_enabled=_env_bool("MONITOR_ENABLED", True),
            monitor_session=os.getenv("MONITOR_SESSION", "./data/monitor.session").strip()
            or "./data/monitor.session",
            monitor_db_path=os.getenv("MONITOR_DB_PATH", "./data/monitor.db").strip()
            or "./data/monitor.db",
            monitor_batch_seconds=_env_int("MONITOR_BATCH_SECONDS", 0),
            pan115_request_interval=max(
                0.0, _env_float("PAN115_REQUEST_INTERVAL", 1.0)
            ),
            inspect_enabled=_env_bool("INSPECT_ENABLED", True),
            inspect_interval_hours=_env_float("INSPECT_INTERVAL_HOURS", 6.0),
            inspect_notify=_env_bool("INSPECT_NOTIFY", True),
            inspect_error_alert_rounds=_env_int("INSPECT_ERROR_ALERT_ROUNDS", 2),
            inspect_notify_code=_env_bool("INSPECT_NOTIFY_CODE", True),
            cookie_alert=_env_bool("COOKIE_ALERT", True),
            share_watch_enabled=_env_bool("SHARE_WATCH_ENABLED", True),
            share_watch_interval_minutes=_env_float(
                "SHARE_WATCH_INTERVAL_MINUTES", 10.0
            ),
            share_watch_notify=_env_bool("SHARE_WATCH_NOTIFY", True),
            share_archive_dir=os.getenv("SHARE_ARCHIVE_DIR", "/已分享").strip(),
            monitor_notify=_env_bool("MONITOR_NOTIFY", True),
            local_media_enabled=_env_bool("LOCAL_MEDIA_ENABLED", False),
            local_media_input_dir=os.getenv("LOCAL_MEDIA_INPUT_DIR", "").strip(),
            local_media_output_dir=os.getenv("LOCAL_MEDIA_OUTPUT_DIR", "").strip(),
            local_media_dry_run=_env_bool("LOCAL_MEDIA_DRY_RUN", True),
            local_media_interval_seconds=max(
                1.0, _env_float("LOCAL_MEDIA_INTERVAL_SECONDS", 10.0)
            ),
            local_media_stable_rounds=max(
                1, _env_int("LOCAL_MEDIA_STABLE_ROUNDS", 3)
            ),
            local_media_stuck_days=max(
                1.0, _env_float("LOCAL_MEDIA_STUCK_DAYS", 7.0)
            ),
            ed2k_enabled=_env_bool("ED2K_ENABLED", False),
            ed2k_input_dir=os.getenv("ED2K_INPUT_DIR", "").strip(),
            ed2k_output_dir=os.getenv("ED2K_OUTPUT_DIR", "").strip(),
            ed2k_dry_run=_env_bool("ED2K_DRY_RUN", True),
            ed2k_interval_seconds=max(
                1.0, _env_float("ED2K_INTERVAL_SECONDS", 30.0)
            ),
            ed2k_stable_rounds=max(1, _env_int("ED2K_STABLE_ROUNDS", 3)),
            ed2k_stuck_days=max(1.0, _env_float("ED2K_STUCK_DAYS", 7.0)),
            ed2k_push_enabled=_env_bool("ED2K_PUSH_ENABLED", False),
            ed2k_push_dry_run=_env_bool("ED2K_PUSH_DRY_RUN", True),
            ed2k_push_interval_seconds=max(
                1.0, _env_float("ED2K_PUSH_INTERVAL_SECONDS", 60.0)
            ),
            ed2k_push_stuck_days=max(1.0, _env_float("ED2K_PUSH_STUCK_DAYS", 7.0)),
            ed2k_push_report_admin=_env_bool("ED2K_PUSH_REPORT_ADMIN", True),
            ed2k_push_report_channel=_env_bool("ED2K_PUSH_REPORT_CHANNEL", False),
        )
        settings._ensure_dirs()
        return settings

    def _ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if self.log_file:
            Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)

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
        if self.monitor_enabled and not (self.tg_api_id and self.tg_api_hash):
            warns.append("TG_API_ID/TG_API_HASH 未配置，频道监控不启动。")
        if self.local_media_enabled:
            if not self.local_media_input_dir or not self.local_media_output_dir:
                warns.append(
                    "LOCAL_MEDIA_ENABLED 已开但 INPUT/OUTPUT 目录未配置，本地媒体流水线不启动。"
                )
            else:
                inp, out = Path(self.local_media_input_dir), Path(self.local_media_output_dir)
                if inp != out and out in inp.parents:
                    warns.append("LOCAL_MEDIA_OUTPUT_DIR 不得位于 INPUT 目录内（会被反复扫描）。")
        if self.ed2k_enabled:
            if not self.ed2k_input_dir or not self.ed2k_output_dir:
                warns.append("ED2K_ENABLED 已开但 INPUT/OUTPUT 目录未配置，ed2k 流水线不启动。")
            else:
                b, c = Path(self.ed2k_input_dir), Path(self.ed2k_output_dir)
                if b != c and c in b.parents:
                    warns.append("ED2K_OUTPUT_DIR 不得位于 INPUT 目录内（会被反复哈希）。")
                bo = Path(self.local_media_output_dir) if self.local_media_enabled else None
                if bo and b != bo and bo.exists() and b.exists() and b.resolve() != bo.resolve():
                    warns.append(
                        f"ED2K_INPUT_DIR={self.ed2k_input_dir} 与 LOCAL_MEDIA_OUTPUT_DIR="
                        f"{self.local_media_output_dir} 不一致——A→B→C 链路需要衔接，确认你的配置。"
                    )
        if self.ed2k_push_enabled and not self.tg_chat_id_ed2k and not self.tg_chat_id:
                warns.append(
                    "ED2K_PUSH_ENABLED 已开但 TG_CHAT_ID_ED2K/TG_CHAT_ID 均未配置——推送无目标频道。"
                )
        return warns

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.tg_admin_ids


# /reload 可热加载的字段（其余变更需重启容器才生效）：
# 各类间隔/开关/通知开关/限速参数/cookie。TG token、chat_id、代理、
# TMDB key/language、服务启停等属于连接层或安全敏感项，不支持热加载。
HOT_RELOAD_FIELDS: frozenset[str] = frozenset({
    "log_level",
    "pan115_request_interval",
    "pan115_cookie",
    "pan115_cookie_file",
    "inspect_interval_hours",
    "inspect_notify",
    "inspect_notify_code",
    "inspect_error_alert_rounds",
    "cookie_alert",
    "share_watch_interval_minutes",
    "share_watch_notify",
    "share_archive_dir",
    "monitor_notify",
    "monitor_batch_seconds",
    "local_media_dry_run",
    "local_media_interval_seconds",
    "local_media_stable_rounds",
    "local_media_stuck_days",
    "ed2k_dry_run",
    "ed2k_interval_seconds",
    "ed2k_stable_rounds",
    "ed2k_stuck_days",
    "ed2k_push_dry_run",
    "ed2k_push_interval_seconds",
    "ed2k_push_stuck_days",
})
