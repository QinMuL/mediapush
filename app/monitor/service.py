"""MonitorService：Telethon 用户账号封装 + 监控生命周期。

流程：
- start()：连接 → 鉴权检查 → 注册 NewMessage 处理器 → 加载监控频道（自动加入，
  Telethon 仅对已加入频道实时下发更新）→ 清理过期去重 → 补扫停机漏掉的消息
- 实时：监控频道新消息 → watcher 提取/验证/过滤 → mon_seen 去重
  → 聚合窗口（batch 秒，0=实时逐条）合并同频道链接 → Bot 推送目标频道
- 推送：复用 Bot（pusher）+ _send_with_retry（flood control）+ 锁 + 2s 限速
  + 3 次退避重试；成功才标记已推送（失败不标记，链接再次出现自动重试）
- 容错：Telethon 断线自动重连；事件处理异常不外抛（不拖垮更新循环）
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.logging_config import make_trace_id, trace_id
from app.monitor.store import (
    KEY_BATCH,
    KEY_TARGET,
    KIND_EXCLUDE,
    KIND_INCLUDE,
    MonitorStore,
    link_hash,
)
from app.monitor.watcher import (
    FilterRules,
    LinkItem,
    extract_ed2k,
    match_filters,
    parse_link,
    render_batch,
    validate,
)
from app.telegram.pusher import _send_with_retry

logger = logging.getLogger(__name__)

_CATCHUP_LIMIT = 100  # 补扫每频道最多回溯消息数（停机恢复）
_PUSH_RETRY = 3  # 推送失败重试次数（线性退避 5s/10s/15s）
_PUSH_INTERVAL = 2.0  # 相邻推送最小间隔（秒），防 flood control
_SEEN_TTL_DAYS = 30  # 去重记录保留天数（启动时清理）
_LOGIN_TIMEOUT = 300  # 交互式登录会话有效期（秒）
# 手机号：+国家码+号码（5-15 位）；国内 11 位裸号自动补 +86
_PHONE_RE = re.compile(r"\+\d{5,15}")
_PHONE_CN_RE = re.compile(r"1\d{10}")

# 服务状态（/mon 展示与排障用）
STATE_DISABLED = "disabled"  # MONITOR_ENABLED=false
STATE_NO_API = "no-api"  # 缺 TG_API_ID/TG_API_HASH
STATE_NO_LOGIN = "no-login"  # session 未登录
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"


def parse_proxy(url: str) -> dict | None:
    """PROXY_URL → Telethon 代理 dict（python-socks 格式）。

    支持 socks5:// socks4:// http:// 前缀；无 scheme 默认 socks5。
    解析失败返回 None（走直连并告警）。
    """
    if not url:
        return None
    raw = url if "://" in url else f"socks5://{url}"
    p = urlparse(raw)
    scheme = (p.scheme or "socks5").lower()
    if scheme not in {"socks4", "socks5", "http"}:
        logger.warning("不支持的代理协议 %r，监控走直连", p.scheme)
        return None
    if not p.hostname or not p.port:
        logger.warning("代理地址不完整 %r，监控走直连", url)
        return None
    proxy: dict = {"proxy_type": scheme, "addr": p.hostname, "port": p.port}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


@dataclass
class _Pending:
    """同频道聚合窗口内的待推送链接。"""

    title: str
    items: list[LinkItem] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    latest_ts: float = 0.0
    task: asyncio.Task | None = None


@dataclass
class _LoginFlow:
    """Bot 内交互式登录会话（/mon login 发起，单实例）。"""

    chat_id: int  # 发起登录的管理员（登录输入仅该会话接受）
    stage: str  # phone（等手机号）| code（等验证码）| password（等两步密码）
    phone: str
    client: object  # TelegramClient（stage=phone 时为 None）
    expires_at: float


class MonitorService:
    def __init__(self, settings, store: MonitorStore, container) -> None:
        self.settings = settings
        self.store = store
        self.container = container  # pusher 懒取（Bot build 后才就绪）
        self.state = STATE_STOPPED
        self._client = None  # TelegramClient
        self._monitored: dict[int, str] = {}  # chat_id -> title（内存缓存）
        self._pending: dict[int, _Pending] = {}
        self._seen_mem: set[str] = set()  # 进程内去重（关补扫/实时并发窗口的竞态）
        self._push_lock = asyncio.Lock()
        self._login: _LoginFlow | None = None  # 进行中的登录会话
        self._notifier = None  # AdminNotifier（懒建：bot 就绪后）
        self._watchdog: asyncio.Task | None = None  # 断连/重连监测任务

    # ------------------------------------------------------------------ #
    # admin 私信通知（MONITOR_NOTIFY 开关）
    # ------------------------------------------------------------------ #
    def _get_notifier(self):
        """通知器（懒建，需 container.telegram.bot 就绪）。"""
        if not self.settings.monitor_notify:
            return None
        telegram = getattr(self.container, "telegram", None)
        if telegram is None or not self.settings.tg_admin_ids:
            return None
        if self._notifier is None:
            from app.telegram.notifier import AdminNotifier

            self._notifier = AdminNotifier(
                telegram.bot, self.settings.tg_admin_ids, throttle_seconds=1800
            )
        return self._notifier

    async def _notify(self, text: str) -> None:
        """发运行事件通知（开关关闭/无 admin 时静默）。"""
        from app.telegram.notifier import notify_admins

        telegram = getattr(self.container, "telegram", None)
        if telegram is None or not self.settings.tg_admin_ids:
            return
        if not self.settings.monitor_notify:
            return
        await notify_admins(telegram.bot, self.settings.tg_admin_ids, text)

    async def _watchdog_loop(self) -> None:
        """连接状态监测：断连告警（节流）+ 重连恢复 + 持续断连升级。

        Telethon 自带断线重连，这里只负责把状态变化告诉 admin，
        避免监控静默失效而无人知晓。
        """
        was_connected = True
        fail_since: float | None = None  # 首次断连时刻
        while True:
            await asyncio.sleep(30)
            if self._client is None:
                continue
            connected = self._client.is_connected()
            if connected and not was_connected:
                notifier = self._get_notifier()
                if notifier is not None:
                    mins = round((time.time() - fail_since) / 60, 1) if fail_since else None
                    await notifier.resolve(
                        "monitor_disconnected",
                        "✅ 频道监控连接已恢复"
                        + (f"（断连约 {mins} 分钟）" if mins else ""),
                    )
                fail_since = None
            elif not connected and was_connected:
                fail_since = time.time()
                await self._notify("⚠️ 频道监控与 Telegram 的连接已断开，正在自动重连…")
            elif not connected and fail_since is not None:
                # 持续断连：每 30 分钟升级提醒（notifier 节流 30min 天然覆盖）
                notifier = self._get_notifier()
                if notifier is not None:
                    mins = int((time.time() - fail_since) / 60)
                    await notifier.alert(
                        "monitor_disconnected",
                        f"🔴 频道监控持续断连已 {mins} 分钟，自动重连未见效。\n"
                        f"可能 session 失效或网络故障：/mon 查看状态，"
                        f"必要时 /mon login 重新登录。",
                    )
            was_connected = connected

    # ------------------------------------------------------------------ #
    # 只读状态（/mon 展示用）
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def login_hint(self) -> str:
        return "/mon login（Bot 内交互式登录；或 python -m app.monitor.login CLI）"

    @property
    def login_active(self) -> bool:
        return self._login is not None

    @property
    def login_stage_desc(self) -> str:
        """登录进行中的阶段描述（/mon 状态展示用；无会话返回空串）。"""
        if self._login is None:
            return ""
        if time.time() > self._login.expires_at:
            return ""  # 已过期（下次访问时惰性清理）
        return {
            "phone": "等待手机号",
            "code": "等待验证码",
            "password": "等待两步密码",
        }.get(self._login.stage, self._login.stage)

    async def filters(self) -> FilterRules:
        rules = await self.store.list_filters()
        return FilterRules(
            include=[r.keyword for r in rules if r.kind == KIND_INCLUDE],
            exclude=[r.keyword for r in rules if r.kind == KIND_EXCLUDE],
        )

    async def batch_seconds(self) -> int:
        raw = await self.store.get_setting(KEY_BATCH)
        if raw is None:
            return max(0, self.settings.monitor_batch_seconds)
        try:
            return max(0, int(raw))
        except ValueError:
            return max(0, self.settings.monitor_batch_seconds)

    async def target_chat_id(self) -> str:
        """推送目标：/mon target 设置 > TG_CHAT_ID_ED2K > TG_CHAT_ID。"""
        t = await self.store.get_setting(KEY_TARGET)
        return t or self.settings.tg_chat_id_ed2k or self.settings.tg_chat_id

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> bool:
        """启动监控：session 已授权则直接运行；未授权转 NO_LOGIN（等 /mon login）。"""
        try:
            from telethon import events  # noqa: F401 — 仅探测依赖
        except ImportError:
            self.state = STATE_NO_API
            logger.error("telethon 未安装，频道监控不可用（pip install telethon）")
            return False
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            self.state = STATE_NO_API
            logger.warning("TG_API_ID / TG_API_HASH 未配置，频道监控不启动")
            return False

        try:
            client = await self._make_client()
        except Exception:
            self.state = STATE_NO_LOGIN
            logger.exception("监控客户端连接失败（%s）", self.login_hint)
            return False
        if not await client.is_user_authorized():
            self.state = STATE_NO_LOGIN
            logger.warning("监控账号未登录：请向 Bot 发送 /mon login 交互式登录")
            await client.disconnect()
            return False

        await self._setup(client)
        await self._post_login()
        return True

    async def _make_client(self):
        """创建 Telethon 客户端并连接（代理沿用 Bot 配置）。"""
        from telethon import TelegramClient

        client = TelegramClient(
            self.settings.monitor_session,
            self.settings.tg_api_id,
            self.settings.tg_api_hash,
            proxy=parse_proxy(self.settings.proxy_url),
        )
        await client.connect()
        return client

    async def _setup(self, client) -> None:
        """登录态就绪后的快速初始化：注册事件处理器 + 状态置运行。"""
        from telethon import events

        me = await client.get_me()
        logger.info(
            "频道监控已登录用户账号：%s（%s）",
            getattr(me, "first_name", "") or me.id,
            getattr(me, "phone", ""),
        )
        client.add_event_handler(self._on_new_message, events.NewMessage())
        self._client = client
        self.state = STATE_RUNNING
        # 连接监测（断连/重连通知）
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def _post_login(self) -> None:
        """慢速初始化：清理去重 + 加载频道 + 补扫（可后台执行）。"""
        await self._cleanup_seen()
        await self._load_channels()
        caught = await self._catchup()
        await self._notify(
            f"✅ 频道监控已就绪：{len(self._monitored)} 个频道"
            + (f"，补扫停机漏档 {caught} 条链接" if caught else "")
        )

    # ------------------------------------------------------------------ #
    # Bot 内交互式登录（/mon login → 手机号 → 验证码 → 两步密码）
    # ------------------------------------------------------------------ #
    async def login_stage(self, chat_id: int) -> str | None:
        """该 chat 的登录阶段（超时惰性清理）；无有效会话返回 None。"""
        flow = self._login
        if flow is None or flow.chat_id != chat_id:
            return None
        if time.time() > flow.expires_at:
            await self._login_discard("超时")
            return None
        return flow.stage

    async def login_start(self, chat_id: int, phone: str = "") -> str:
        """开始登录（/mon login [手机号]）；phone 为空则先等用户发送手机号。"""
        if self.is_running:
            return "✅ 监控已在运行，无需登录"
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            self.state = STATE_NO_API
            return "❌ 未配置 TG_API_ID / TG_API_HASH（.env 填写后重启容器）"

        await self._login_discard("新登录会话")  # 单实例：新登录覆盖旧会话
        flow = _LoginFlow(
            chat_id=chat_id,
            stage="phone",
            phone="",
            client=None,
            expires_at=time.time() + _LOGIN_TIMEOUT,
        )
        if not phone:
            self._login = flow
            return "📲 请发送监控账号的手机号（国际格式如 +8613800138000，国内 11 位亦可）"
        _, msg = await self._send_code(flow, phone)
        return msg

    async def login_phone(self, phone: str) -> tuple[bool, str]:
        """stage=phone：接收手机号 → 连接 + 发送验证码。"""
        flow = self._login
        if flow is None or flow.stage != "phone":
            return False, "❌ 登录会话不存在，请重新 /mon login"
        if time.time() > flow.expires_at:
            await self._login_discard("超时")
            return False, "❌ 登录已超时，请重新 /mon login"
        return await self._send_code(flow, phone)

    async def _send_code(self, flow: _LoginFlow, phone: str) -> tuple[bool, str]:
        """校验手机号格式 → 连接 → 请求验证码（进入 code 阶段）。"""
        from telethon.errors import FloodWaitError

        phone = re.sub(r"[\s\-()]", "", phone.strip())
        if _PHONE_CN_RE.fullmatch(phone):  # 国内 11 位裸号自动补 +86
            phone = "+86" + phone
        if not _PHONE_RE.fullmatch(phone):
            return False, "❌ 手机号格式：+国家码手机号（如 +8613800138000），请重发"

        try:
            client = await self._make_client()
        except Exception as exc:  # noqa: BLE001 — 网络异常回报给用户
            logger.warning("监控登录：连接 Telegram 失败：%s", exc)
            return False, f"❌ 连接 Telegram 失败：{exc}"
        try:
            await client.send_code_request(phone)
        except FloodWaitError as exc:
            await client.disconnect()
            return False, f"❌ 验证码请求过于频繁，请 {int(exc.seconds) + 5} 秒后重新 /mon login"
        except Exception as exc:  # noqa: BLE001 — 任意 API 错误回报给用户
            logger.warning("监控登录：发送验证码失败：%s", exc)
            await client.disconnect()
            return False, f"❌ 发送验证码失败：{exc}，请重发手机号"

        self._login = _LoginFlow(
            chat_id=flow.chat_id,
            stage="code",
            phone=phone,
            client=client,
            expires_at=time.time() + _LOGIN_TIMEOUT,
        )
        logger.info("监控登录：验证码已发送（chat=%s）", flow.chat_id)
        return True, "✅ 验证码已发送至该账号的 Telegram 客户端，请直接发送验证码（5 分钟内有效）"

    async def login_code(self, code: str) -> tuple[str, str]:
        """stage=code：验证码登录。返回 (status, msg)。

        status：ok=登录成功并启动监控 | password=需两步验证 | retry=输入有误可重试
                | error=会话失效需重新开始
        """
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        flow = self._login
        if flow is None or flow.stage != "code":
            return "error", "❌ 登录会话不存在，请重新 /mon login"
        if time.time() > flow.expires_at:
            await self._login_discard("超时")
            return "error", "❌ 登录已超时，请重新 /mon login"
        code = code.strip().replace(" ", "")
        if not code.isdigit():
            return "retry", "❌ 验证码应为数字，请重发"
        try:
            # 只传 code：传 phone 会触发重发验证码
            await flow.client.sign_in(code=code)
        except SessionPasswordNeededError:
            flow.stage = "password"
            return "password", "🔐 该账号已开启两步验证，请发送密码"
        except PhoneCodeInvalidError:
            return "retry", "❌ 验证码错误，请重发"
        except PhoneCodeExpiredError:
            await self._login_discard("验证码过期")
            return "error", "❌ 验证码已过期，请重新 /mon login"
        except Exception as exc:  # noqa: BLE001 — 任意 API 错误可重试
            logger.warning("监控登录：验证码登录失败：%s", exc)
            return "retry", f"❌ 登录失败：{exc}，请重发验证码"
        return await self._login_done()

    async def login_password(self, password: str) -> tuple[str, str]:
        """stage=password：两步验证密码登录（语义同 login_code）。"""
        from telethon.errors import PasswordHashInvalidError

        flow = self._login
        if flow is None or flow.stage != "password":
            return "error", "❌ 登录会话不存在，请重新 /mon login"
        if time.time() > flow.expires_at:
            await self._login_discard("超时")
            return "error", "❌ 登录已超时，请重新 /mon login"
        try:
            await flow.client.sign_in(password=password)
        except PasswordHashInvalidError:
            return "retry", "❌ 密码错误，请重发"
        except Exception as exc:  # noqa: BLE001 — 任意 API 错误可重试
            logger.warning("监控登录：密码登录失败：%s", exc)
            return "retry", f"❌ 登录失败：{exc}，请重发密码"
        return await self._login_done()

    async def login_cancel(self) -> str:
        if self._login is None:
            return "ℹ️ 当前没有进行中的登录"
        await self._login_discard("手动取消")
        return "✅ 已取消登录，敏感消息可自行删除"

    async def _login_done(self) -> tuple[str, str]:
        """登录成功：接管为监控客户端，慢速初始化转后台。"""
        flow, self._login = self._login, None
        logger.info("监控账号登录成功：%s（chat=%s）", flow.phone, flow.chat_id)
        await self._setup(flow.client)
        # 加载频道/补扫较慢，后台执行不阻塞回复
        asyncio.create_task(self._post_login())
        return (
            "ok",
            "✅ 登录成功，频道监控已启动 🎉\n"
            + f"session 已保存：{self.settings.monitor_session}\n"
            + "用 /mon add @频道 添加监控，/mon 查看状态",
        )

    async def _login_discard(self, reason: str) -> None:
        """废弃登录会话（覆盖/取消/超时）：断开未完成的客户端。"""
        flow, self._login = self._login, None
        if flow is None:
            return
        if flow.client is not None:
            try:
                await flow.client.disconnect()
            except Exception:
                logger.exception("废弃登录客户端断开失败")
        logger.info("登录会话已废弃（%s，chat=%s）", reason, flow.chat_id)

    async def stop(self) -> None:
        """停止监控：冲刷待推送批次 → 断开客户端。"""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        client = self._client
        if client is None:
            return
        self._client = None
        self.state = STATE_STOPPED
        for pend in self._pending.values():
            if pend.task and not pend.task.done():
                pend.task.cancel()
        try:
            for chat_id in list(self._pending):
                await self._flush(chat_id)
        except Exception:
            logger.exception("停止时冲刷待推送批次失败")
        try:
            await client.disconnect()
        except Exception:
            logger.exception("断开监控客户端失败")
        logger.info("频道监控已停止")

    # ------------------------------------------------------------------ #
    # 频道管理（/mon add / /mon del）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_ref(ref: str) -> str | int:
        """@xxx / t.me/xxx / t.me/c/123 / 纯数字 → Telethon 可解析形式。"""
        m = re.search(r"t\.me/(?:c/)?(\w+)", ref)
        if m:
            return int("-100" + m.group(1)) if "c/" in ref else m.group(1)
        ref = ref.strip().removeprefix("@")
        if ref.lstrip("-").isdigit():
            return int(ref)
        return ref

    async def add_channel(self, ref: str) -> tuple[bool, str]:
        """添加监控频道（自动加入，保证实时更新下发）。"""
        if self._client is None:
            return False, f"监控服务未运行（{self._state_hint()}）"
        ref = ref.strip()
        if not ref:
            return False, "用法：/mon add @频道用户名（或 t.me 链接 / chat_id）"
        try:
            entity = await self._client.get_entity(self._normalize_ref(ref))
        except ValueError:
            return False, f"无法解析频道：{ref}（确认是公开频道且名称正确）"
        except Exception as exc:  # noqa: BLE001
            return False, f"解析频道失败：{exc}"

        from telethon import utils

        chat_id = utils.get_peer_id(entity)  # marked id（与 event.chat_id 同形式）
        if getattr(entity, "megagroup", False):
            return False, "仅支持频道（channel），不支持群组"
        title = getattr(entity, "title", "") or str(chat_id)
        username = getattr(entity, "username", "") or ""
        await self._join(entity)
        await self.store.add_channel(chat_id, title, username)
        self._monitored[chat_id] = title
        return True, f"✅ 已添加监控频道：{title}（{chat_id}）"

    async def remove_channel(self, ref: str) -> tuple[bool, str]:
        """移除监控频道（支持 chat_id / @username / t.me 链接）。"""
        norm = self._normalize_ref(ref.strip())
        for ch in await self.store.list_channels():
            if ch.chat_id == norm or (isinstance(norm, str) and norm == ch.username):
                await self.store.remove_channel(ch.chat_id)
                self._monitored.pop(ch.chat_id, None)
                pend = self._pending.pop(ch.chat_id, None)
                if pend and pend.task and not pend.task.done():
                    pend.task.cancel()
                return True, f"✅ 已移除监控频道：{ch.title}（{ch.chat_id}）"
        return False, f"未找到监控频道：{ref}"

    def _state_hint(self) -> str:
        if self.state == STATE_NO_API:
            return "未配置 TG_API_ID/TG_API_HASH"
        if self.state == STATE_NO_LOGIN:
            return f"账号未登录，请运行 {self.login_hint}"
        if self.state == STATE_DISABLED:
            return "MONITOR_ENABLED=false"
        return "服务未启动"

    # ------------------------------------------------------------------ #
    # 内部：事件处理 / 聚合 / 推送
    # ------------------------------------------------------------------ #
    async def _on_new_message(self, event) -> None:
        """Telethon NewMessage 处理器（所有对话都会进来，先按监控集过滤）。"""
        try:
            chat_id = event.chat_id
            title = self._monitored.get(chat_id)
            if title is None:
                return
            msg = event.message
            new_items = await self._collect(msg.message or "")
            await self.store.set_last_msg_id(chat_id, msg.id)
            if not new_items:
                return
            logger.info("监控捕获 %d 条新 ed2k：%s（msg %s）", len(new_items), title, msg.id)

            pend = self._pending.setdefault(chat_id, _Pending(title=title))
            for item, h in new_items:
                pend.items.append(item)
                pend.hashes.append(h)
            if msg.date:
                pend.latest_ts = max(pend.latest_ts, msg.date.timestamp())

            window = await self.batch_seconds()
            if window <= 0:
                await self._flush(chat_id)
            elif pend.task is None or pend.task.done():
                pend.task = asyncio.create_task(self._flush_later(chat_id, window))
        except Exception:
            logger.exception("监控消息处理异常")

    async def _collect(self, text: str) -> list[tuple[LinkItem, str]]:
        """提取 → 验证 → 过滤 → 去重，返回 (链接, hash) 列表。"""
        if not text:
            return []
        rules = await self.filters()
        out: list[tuple[LinkItem, str]] = []
        for link in extract_ed2k(text):
            if not validate(link):
                continue
            item = parse_link(link)
            if item is None or not match_filters(item.filename, rules):
                continue
            h = link_hash(link)
            if h in self._seen_mem or await self.store.is_seen(h):
                continue
            self._seen_mem.add(h)  # 先占位关竞态；推送失败时回滚
            out.append((item, h))
        return out

    async def _flush_later(self, chat_id: int, window: int) -> None:
        try:
            await asyncio.sleep(window)
            await self._flush(chat_id)
        except asyncio.CancelledError:
            pass  # stop()/del 时由 stop() 冲刷兜底

    async def _flush(self, chat_id: int) -> None:
        pend = self._pending.pop(chat_id, None)
        if pend is None or not pend.items:
            return

        processor = getattr(self.container, "processor", None)
        if processor is None:
            # 降级：无 processor（如测试 stub）时退回合并纯文本推送
            text = render_batch(pend.title, pend.items, pend.latest_ts)
            if await self._push_text(text):
                await self.store.mark_seen(pend.hashes)
                logger.info("监控推送成功：%s（%d 条）", pend.title, len(pend.items))
            else:
                self._seen_mem -= set(pend.hashes)
                logger.error("监控推送失败（%d 条待重试）：%s", len(pend.items), pend.title)
            return

        # 卡片模式：逐链接走主链路（TMDB 匹配 → 海报卡片），
        # 与手动推送模板完全一致；单条失败不影响其余
        target = await self.target_chat_id()
        done: list[str] = []
        failed: list[str] = []
        for item, h in zip(pend.items, pend.hashes):
            if await self._push_card(processor, item, pend.title, pend.latest_ts, target):
                done.append(h)
            else:
                failed.append(h)
        if done:
            await self.store.mark_seen(done)
        if failed:
            # 失败：回滚进程内去重占位，不落库——链接再次出现/重启补扫会重试
            self._seen_mem -= set(failed)
            logger.error("监控推送失败（%d 条待重试）：%s", len(failed), pend.title)
        if done:
            logger.info("监控推送成功：%s（%d 条卡片）", pend.title, len(done))

    async def _push_card(self, processor, item: LinkItem, source: str, ts: float,
                         target: str) -> bool:
        """单链接推送：优先完整卡片（与手动推送一致），TMDB 未匹配回退纯文本。"""
        from app.core.link_parser import ParsedShare

        parsed = ParsedShare("ed2k", item.link)
        # 链路 trace：监控推送与手动推送共用 tid 派生规则（ed2k hash 前缀）
        with trace_id(make_trace_id(parsed)):
            try:
                r = await processor.process(parsed, chat_id=target or None)
                if r.ok:
                    await asyncio.sleep(_PUSH_INTERVAL)  # 限速，防 flood control
                    return True
                if r.dup:
                    # 已推送过（手动或此前监控）：视为完成，不重推不回退
                    logger.info("监控跳过已推送链接：%s", r.message)
                    return True
                logger.warning("监控卡片推送未成功（%s）：%s — 回退纯文本",
                               r.message, item.filename)
            except Exception:
                logger.exception("监控卡片推送异常：%s", item.filename)

            # 回退：TMDB 未匹配/卡片异常时仍以纯文本中继链接（不丢资源）
            text = render_batch(source, [item], ts)
            if await self._push_text(text):
                return True
            logger.error("监控回退纯文本推送失败：%s", item.filename)
            return False

    async def _push_text(self, text: str) -> bool:
        """经 Bot 推送到目标频道：flood 自动等待 + 3 次退避重试 + 全局串行限速。"""
        from telegram.constants import ParseMode

        pusher = self.container.pusher
        target = await self.target_chat_id()
        if pusher is None or pusher.bot is None:
            logger.error("Bot 推送器未就绪，监控消息暂不推送")
            return False
        if not target:
            logger.error("推送目标未配置（/mon target 或 TG_CHAT_ID_ED2K/TG_CHAT_ID）")
            return False
        async with self._push_lock:
            for attempt in range(1, _PUSH_RETRY + 1):
                try:
                    await _send_with_retry(
                        lambda: pusher.bot.send_message(
                            chat_id=target,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    )
                    await asyncio.sleep(_PUSH_INTERVAL)  # 限速，防 flood control
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("监控推送失败（第 %d/%d 次）：%s", attempt, _PUSH_RETRY, exc)
                    await asyncio.sleep(5 * attempt)
            return False

    # ------------------------------------------------------------------ #
    # 内部：启动期加载 / 补扫 / 清理
    # ------------------------------------------------------------------ #
    async def _load_channels(self) -> None:
        """加载监控频道：解析实体 → 加入（幂等）→ 缓存标题。"""
        for ch in await self.store.list_channels():
            entity = None
            for ref in (ch.chat_id, ch.username):
                if ref in ("", None):
                    continue
                try:
                    entity = await self._client.get_entity(ref)
                    break
                except (ValueError, TypeError):
                    continue
            if entity is None:
                logger.warning("监控频道不可达，暂跳过：%s（%s）", ch.title, ch.chat_id)
                continue
            await self._join(entity)
            title = getattr(entity, "title", "") or ch.title or str(ch.chat_id)
            self._monitored[ch.chat_id] = title
            logger.info("监控频道就绪：%s（%s）", title, ch.chat_id)

    async def _join(self, entity) -> None:
        """加入频道（幂等）：未加入的频道不会实时下发新消息。"""
        from telethon.errors import (
            ChannelPrivateError,
            ChannelPublicGroupNaError,
            UserBannedInChannelError,
        )
        from telethon.tl.functions.channels import JoinChannelRequest

        try:
            await self._client(JoinChannelRequest(entity))
        except (
            ChannelPrivateError,
            ChannelPublicGroupNaError,
            UserBannedInChannelError,
        ) as exc:
            logger.warning("加入频道失败（%s）：%s", getattr(entity, "title", entity), exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加入频道异常：%s", exc)

    async def _catchup(self) -> int:
        """补扫停机期间漏掉的消息（每频道 ≤_CATCHUP_LIMIT 条，聚合为一条推送）。

        返回捕获的链接总数（通知文案用）。
        """
        caught = 0
        for ch in await self.store.list_channels():
            if ch.chat_id not in self._monitored:
                continue  # 不可达频道
            try:
                pend = _Pending(title=self._monitored[ch.chat_id])
                max_id = 0
                async for msg in self._client.iter_messages(
                    ch.chat_id, limit=_CATCHUP_LIMIT, min_id=ch.last_msg_id, reverse=True
                ):
                    max_id = max(max_id, msg.id)
                    for item, h in await self._collect(msg.message or ""):
                        pend.items.append(item)
                        pend.hashes.append(h)
                    if msg.date:
                        pend.latest_ts = max(pend.latest_ts, msg.date.timestamp())
                if max_id:
                    await self.store.set_last_msg_id(ch.chat_id, max_id)
                if pend.items:
                    logger.info("补扫 %s：捕获 %d 条新 ed2k", pend.title, len(pend.items))
                    caught += len(pend.items)
                    self._pending[ch.chat_id] = pend
                    await self._flush(ch.chat_id)
            except ValueError:
                logger.warning("补扫失败（实体无法解析）：%s", ch.title)
            except Exception:
                logger.exception("补扫异常：%s", ch.title)
        return caught

    async def _cleanup_seen(self) -> None:
        try:
            n = await self.store.cleanup_seen(_SEEN_TTL_DAYS)
            if n:
                logger.info("已清理 %d 条过期去重记录（>%d 天）", n, _SEEN_TTL_DAYS)
        except Exception:
            logger.exception("去重记录清理失败")
