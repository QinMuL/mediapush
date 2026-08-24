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
from dataclasses import dataclass, field
from urllib.parse import urlparse

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

    # ------------------------------------------------------------------ #
    # 只读状态（/mon 展示用）
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def login_hint(self) -> str:
        return f"python -m app.monitor.login（session：{self.settings.monitor_session}）"

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
        try:
            from telethon import TelegramClient, events
        except ImportError:
            self.state = STATE_NO_API
            logger.error("telethon 未安装，频道监控不可用（pip install telethon）")
            return False
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            self.state = STATE_NO_API
            logger.warning("TG_API_ID / TG_API_HASH 未配置，频道监控不启动")
            return False

        client = TelegramClient(
            self.settings.monitor_session,
            self.settings.tg_api_id,
            self.settings.tg_api_hash,
            proxy=parse_proxy(self.settings.proxy_url),
        )
        await client.connect()
        if not await client.is_user_authorized():
            self.state = STATE_NO_LOGIN
            logger.error("监控账号未登录，请运行 %s", self.login_hint)
            await client.disconnect()
            return False

        me = await client.get_me()
        logger.info(
            "频道监控已登录用户账号：%s（%s）",
            getattr(me, "first_name", "") or me.id,
            getattr(me, "phone", ""),
        )

        client.add_event_handler(self._on_new_message, events.NewMessage())
        self._client = client
        self.state = STATE_RUNNING

        await self._cleanup_seen()
        await self._load_channels()
        await self._catchup()
        return True

    async def stop(self) -> None:
        """停止监控：冲刷待推送批次 → 断开客户端。"""
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
        text = render_batch(pend.title, pend.items, pend.latest_ts)
        if await self._push_text(text):
            await self.store.mark_seen(pend.hashes)
            logger.info("监控推送成功：%s（%d 条）", pend.title, len(pend.items))
        else:
            # 失败：回滚进程内去重占位，不落库——链接再次出现/重启补扫会重试
            self._seen_mem -= set(pend.hashes)
            logger.error("监控推送失败（%d 条待重试）：%s", len(pend.items), pend.title)

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

    async def _catchup(self) -> None:
        """补扫停机期间漏掉的消息（每频道 ≤_CATCHUP_LIMIT 条，聚合为一条推送）。"""
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
                    self._pending[ch.chat_id] = pend
                    await self._flush(ch.chat_id)
            except ValueError:
                logger.warning("补扫失败（实体无法解析）：%s", ch.title)
            except Exception:
                logger.exception("补扫异常：%s", ch.title)

    async def _cleanup_seen(self) -> None:
        try:
            n = await self.store.cleanup_seen(_SEEN_TTL_DAYS)
            if n:
                logger.info("已清理 %d 条过期去重记录（>%d 天）", n, _SEEN_TTL_DAYS)
        except Exception:
            logger.exception("去重记录清理失败")
