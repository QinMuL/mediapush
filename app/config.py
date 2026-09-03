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


def _env_bool_alias(name: str, legacy: str, default: bool) -> bool:
    """布尔配置别名：新键优先，旧键兜底（键位治理迁移期兼容，旧键不删）。

    现存别名：PIPELINE_REPORT_ADMIN ← CD2_REPORT_ADMIN（统一流水线
    合并原 ed2k_push/cd2 两处报告开关后的兼容入口）。
    """
    raw = os.getenv(name)
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return _env_bool(legacy, default)


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
    log_media_file: str = "./data/logs/media.log"      # 本地媒体流水线独立日志
    log_max_bytes: int = 5 * 1024 * 1024               # 单文件轮转阈值（字节）
    log_retention_days: float = 7.0                     # 轮转归档保留天数（按 mtime 清理）
    db_path: str = "./data/cache.db"
    state_db_path: str = "./data/state.db"              # 各服务统一状态存储
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
    # 分享前目录结构标准化（季目录重命名 + 资源目录补全）
    share_normalize_enabled: bool = False
    share_normalize_dry_run: bool = True
    # 频道监控（Telethon）断连/重连/补扫等运行事件私信 admin
    monitor_notify: bool = True
    # 统一媒体流水线（方案二整合：A → 重命名 → B 资源库 → 哈希/推卡片 → CD2 上传 115，
    # 见 app/pipeline/service.py；替代原 local_media/ed2k/ed2k_push/cd2 四段服务）
    pipeline_enabled: bool = False
    pipeline_input_dir: str = ""       # 目录A：下载落地（递归监控，稳定判定在此）
    pipeline_library_dir: str = ""     # 目录B：资源库（重命名+哈希+待上传，CD2 挂载源）
    pipeline_rename_dry_run: bool = True   # ① 模拟：只出"拟移动"日志（仍真调 TMDB）
    pipeline_push_dry_run: bool = True     # ② 模拟：只出"将推送"日志（哈希/JSONL 仍真实）
    pipeline_upload_dry_run: bool = True   # ③ 模拟：只出"将上传"日志（仍真查重 115 目标）
    pipeline_interval_seconds: float = 10.0  # 轮询周期（哈希/上传追踪同轮进行）
    pipeline_stable_rounds: int = 3          # A 侧稳定判定轮数（×周期，唯一一处）
    pipeline_batch_max: int = 5              # 单轮最多重命名的文件数（防打爆 IO/TMDB）
    pipeline_min_size_mb: float = 10.0       # 体积守门（MB）：低于视为下载残缺拦截
    pipeline_stuck_days: float = 7.0         # 各阶段失败卡死告警阈值（天）
    pipeline_report_admin: bool = True       # 有动作的轮次把汇总+明细发给 TG_ADMIN_IDS
    # CD2 连接与路径（上传阶段用；src = 目录B 在 CD2 里的路径）
    cd2_address: str = "192.168.1.202:19798"       # CD2 gRPC 地址
    cd2_token: str = ""                           # API 令牌（推荐，UI 创建）
    cd2_username: str = ""                         # 或账号密码（token 优先）
    cd2_password: str = ""
    cd2_upload_src: str = ""                       # 目录B 在 CD2 里的路径（本地挂载）
    cd2_upload_dst: str = ""                       # 115 在 CD2 里的目标目录

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
            except FileNotFoundError:
                # 首次部署文件尚不存在（_ensure_dirs 随后创建空占位）：
                # 等价于未配置 cookie，匿名模式可用，静默即可
                pass
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
            log_media_file=os.getenv(
                "LOG_MEDIA_FILE", "./data/logs/media.log"
            ).strip(),
            log_max_bytes=max(
                64 * 1024, _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
            ),
            log_retention_days=max(
                0.5, _env_float("LOG_RETENTION_DAYS", 7.0)
            ),
            db_path=os.getenv("DB_PATH", "./data/cache.db").strip() or "./data/cache.db",
            state_db_path=os.getenv("STATE_DB_PATH", "./data/state.db").strip()
            or "./data/state.db",
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
            share_normalize_enabled=_env_bool("SHARE_NORMALIZE_ENABLED", False),
            share_normalize_dry_run=_env_bool("SHARE_NORMALIZE_DRY_RUN", True),
            monitor_notify=_env_bool("MONITOR_NOTIFY", True),
            pipeline_enabled=_env_bool("PIPELINE_ENABLED", False),
            pipeline_input_dir=os.getenv("PIPELINE_INPUT_DIR", "").strip(),
            pipeline_library_dir=os.getenv("PIPELINE_LIBRARY_DIR", "").strip(),
            pipeline_rename_dry_run=_env_bool("PIPELINE_RENAME_DRY_RUN", True),
            pipeline_push_dry_run=_env_bool("PIPELINE_PUSH_DRY_RUN", True),
            pipeline_upload_dry_run=_env_bool("PIPELINE_UPLOAD_DRY_RUN", True),
            pipeline_interval_seconds=max(
                1.0, _env_float("PIPELINE_INTERVAL_SECONDS", 10.0)
            ),
            pipeline_stable_rounds=max(1, _env_int("PIPELINE_STABLE_ROUNDS", 3)),
            pipeline_batch_max=max(1, _env_int("PIPELINE_BATCH_MAX", 5)),
            pipeline_min_size_mb=max(0.0, _env_float("PIPELINE_MIN_SIZE_MB", 10.0)),
            pipeline_stuck_days=max(1.0, _env_float("PIPELINE_STUCK_DAYS", 7.0)),
            pipeline_report_admin=_env_bool_alias(
                "PIPELINE_REPORT_ADMIN", "CD2_REPORT_ADMIN", True
            ),
            cd2_address=os.getenv("CD2_ADDRESS", "192.168.1.202:19798").strip()
            or "192.168.1.202:19798",
            cd2_token=os.getenv("CD2_TOKEN", "").strip(),
            cd2_username=os.getenv("CD2_USERNAME", "").strip(),
            cd2_password=os.getenv("CD2_PASSWORD", "").strip(),
            cd2_upload_src=os.getenv("CD2_UPLOAD_SRC", "").strip(),
            cd2_upload_dst=os.getenv("CD2_UPLOAD_DST", "").strip(),
        )
        settings._ensure_dirs()
        return settings

    def _ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if self.log_file:
            Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
        # cookie 文件首启自动创建空占位：用户可直接在宿主机编辑该文件写入
        # （也作为 /cookie 命令的落点）；空内容 = 未配置（匿名模式），各读取点
        # 对空串静默跳过，不会产生告警噪音
        if self.pan115_cookie_file and not Path(self.pan115_cookie_file).exists():
            try:
                Path(self.pan115_cookie_file).parent.mkdir(parents=True, exist_ok=True)
                Path(self.pan115_cookie_file).touch()
                logger.info("已创建 cookie 空文件：%s（/cookie 命令或直接编辑写入）",
                            self.pan115_cookie_file)
            except OSError as exc:
                logger.warning("cookie 空文件创建失败（不影响运行）：%s", exc)

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
        # 统一流水线校验
        if self.pipeline_enabled:
            if not self.pipeline_input_dir or not self.pipeline_library_dir:
                warns.append(
                    "PIPELINE_ENABLED 已开但 PIPELINE_INPUT_DIR/PIPELINE_LIBRARY_DIR 未配置，流水线不启动。"
                )
            else:
                a = Path(self.pipeline_input_dir)
                b = Path(self.pipeline_library_dir)
                if a != b and (b in a.parents or a in b.parents):
                    warns.append(
                        "PIPELINE_LIBRARY_DIR 与 PIPELINE_INPUT_DIR 不得互相嵌套（会被反复扫描）。"
                    )
            if not self.cd2_upload_src or not self.cd2_upload_dst:
                warns.append(
                    "流水线上传需配置 CD2_UPLOAD_SRC（B 的 CD2 路径）与 CD2_UPLOAD_DST。"
                )
            elif self.cd2_upload_src == self.cd2_upload_dst:
                warns.append("CD2_UPLOAD_SRC 与 DST 不得相同（源=目标会死循环）。")
            if not self.cd2_token and not (self.cd2_username and self.cd2_password):
                warns.append(
                    "CD2_TOKEN / CD2_USERNAME+PASSWORD 均未配置——上传阶段无法认证。"
                )
            elif not self.cd2_token:
                warns.append("CD2 建议使用 API 令牌（CD2_TOKEN）而非账号密码。")
        # 旧四段开关迁移提醒（读原始 env，不进 Settings 字段）
        _on = {"1", "true", "yes", "y", "on"}
        legacy_on = [
            k for k in ("LOCAL_MEDIA_ENABLED", "ED2K_ENABLED",
                        "ED2K_PUSH_ENABLED", "CD2_ENABLED")
            if (os.getenv(k) or "").strip().lower() in _on
        ]
        if legacy_on and not self.pipeline_enabled:
            warns.append(
                f"检测到旧版四段开关仍为 true（{', '.join(legacy_on)}）——"
                "已由统一流水线 PIPELINE_* 取代，请按 README「流水线迁移」更新 .env。"
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
    "share_normalize_enabled",
    "share_normalize_dry_run",
    "monitor_notify",
    "monitor_batch_seconds",
    "pipeline_rename_dry_run",
    "pipeline_push_dry_run",
    "pipeline_upload_dry_run",
    "pipeline_interval_seconds",
    "pipeline_stable_rounds",
    "pipeline_stuck_days",
    "pipeline_min_size_mb",
    "pipeline_batch_max",
    "pipeline_report_admin",
})
