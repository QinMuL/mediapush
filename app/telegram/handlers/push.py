"""推送链路：/115 直推、裸链接自动处理、多链接聚合批处理。

PushCoordinator 收编原先散落的模块级并发状态：
- 处理中 60s 去重（防同一链接并发双推）
- 批量串行锁（防多消息并发加剧 TG flood）
- 多链接聚合缓冲（TG 长消息拆分时 3s 窗口合并）
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import Update
from telegram.ext import ContextTypes

from app.core.link_parser import ParsedShare, parse_share, parse_shares
from app.logging_config import make_trace_id, trace_id
from app.telegram.edit_session import EditState
from app.telegram.handlers.common import (
    _DENY_TEXT,
    Pan115Error,
    _container,
    _edit,
    _get_session,
    _is_admin,
)
from app.telegram.handlers.edit_flow import _handle_quality_input
from app.telegram.handlers.monitor_cmds import _handle_login_input

logger = logging.getLogger(__name__)

_AGGREGATE_WINDOW = 3  # 聚合窗口（秒）


class PushCoordinator:
    """推送协调器：处理中去重 + 批量串行 + 多链接聚合缓冲。"""

    # 处理中 60s 去重（借 P115-Share）：网络读取慢时，重复发送的同一链接直接跳过，
    # 防止并发处理绕过 pushed 去重造成双推。TTL 兜底防泄漏（进程内内存态）。
    _PROCESSING_TTL = 60.0

    def __init__(self) -> None:
        self.batch_lock = asyncio.Lock()  # 串行化批量推送，避免多消息并发加剧 TG flood
        self._processing: dict[str, float] = {}  # "provider:code" -> 标记时刻（monotonic）
        self._pending_shares: list[ParsedShare] = []
        self._pending_timer: asyncio.Task | None = None
        self._pending_update: Update | None = None
        self._pending_context = None

    # ---------------- 处理中去重 ---------------- #
    @staticmethod
    def _processing_key(parsed) -> str:
        return f"{parsed.provider}:{parsed.code}"

    def is_processing(self, parsed) -> bool:
        """该链接是否正在处理（60s 内）。顺手清理过期标记。"""
        now = time.monotonic()
        for k, t in list(self._processing.items()):
            if now - t >= self._PROCESSING_TTL:
                self._processing.pop(k, None)
        key = self._processing_key(parsed)
        started = self._processing.get(key)
        return started is not None and now - started < self._PROCESSING_TTL

    def mark_processing(self, parsed) -> None:
        self._processing[self._processing_key(parsed)] = time.monotonic()

    def unmark_processing(self, parsed) -> None:
        self._processing.pop(self._processing_key(parsed), None)

    # ---------------- 聚合缓冲 ---------------- #
    def has_pending(self) -> bool:
        return bool(self._pending_shares)

    async def aggregate(self, update: Update, context, shares: list[ParsedShare]) -> None:
        """多链接或聚合中 → 缓冲聚合（窗口到期由 _flush 统一批处理）。"""
        self._pending_shares.extend(shares)
        if self._pending_update is None:
            self._pending_update = update
            self._pending_context = context
        logger.info("聚合 +%d（累计 %d）", len(shares), len(self._pending_shares))
        if self._pending_timer is not None and not self._pending_timer.done():
            self._pending_timer.cancel()
        self._pending_timer = asyncio.create_task(self._flush_pending())

    async def _flush_pending(self) -> None:
        """聚合窗口到期：跨消息去重后批量推送。"""
        await asyncio.sleep(_AGGREGATE_WINDOW)
        shares = self._pending_shares
        update, context = self._pending_update, self._pending_context
        self._pending_shares = []
        self._pending_update = None
        self._pending_context = None
        self._pending_timer = None
        seen: set[tuple[str, str]] = set()
        unique: list[ParsedShare] = []
        for p in shares:
            k = (p.provider, p.code)
            if k in seen:
                continue
            seen.add(k)
            unique.append(p)
        logger.info("聚合完成 %d 个链接，开始批处理", len(unique))
        try:
            await self.process_batch(update, context, unique)
        except Exception:
            logger.exception("聚合批处理失败")

    # ---------------- 推送入口 ---------------- #
    async def process(self, update: Update, context, parsed) -> None:
        """单链接直推（含处理中去重 + 链路 trace）。"""
        container = _container(context)
        # 处理中 60s 去重：同一链接并发处理会造成双推（读取慢时用户易重发）
        if self.is_processing(parsed):
            await update.message.reply_text("⏳ 该链接正在处理中，请稍候（勿在 1 分钟内重复发送）")
            return
        self.mark_processing(parsed)
        # 链路 trace：prepare/读取/TMDB/推送全程日志带 [tid=xxx]，grep 即拉全链路
        with trace_id(make_trace_id(parsed)):
            try:
                await _process_locked(update, context, container, parsed)
            finally:
                self.unmark_processing(parsed)

    async def process_batch(self, update: Update, context, shares) -> None:
        """多链接串行处理：逐个推送，单条汇总消息实时更新，失败继续。"""
        container = _container(context)
        shares = sorted(shares, key=_episode_sort_key)
        total = len(shares)
        placeholder = await update.message.reply_text(
            f"⏳ 正在处理 {total} 个链接，逐个推送中 ..."
        )
        lines: list[str] = []
        done = 0
        async with self.batch_lock:  # 串行化批量，避免多消息并发推送加剧 flood
            for parsed in shares:
                done += 1
                # 处理中 60s 去重：跳过批外正在处理的同一链接（防双推）
                if self.is_processing(parsed):
                    lines.append(f"⏭️ {_short_id(parsed)}（正在处理中，跳过）")
                    await _edit(
                        placeholder,
                        _build_batch_summary(f"⏳ 处理中 ({done}/{total})", lines),
                    )
                    continue
                self.mark_processing(parsed)
                # 链路 trace：批内逐条独立 tid，日志交错也可按条 grep
                with trace_id(make_trace_id(parsed)):
                    try:
                        try:
                            result = await container.processor.process(parsed)
                        except Pan115Error as exc:
                            lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
                        except Exception as exc:
                            uid = update.effective_user.id if update.effective_user else None
                            logger.exception("处理分享失败：user=%s", uid)
                            lines.append(f"⚠️ {_short_id(parsed)}：{exc}".replace("\n", " "))
                        else:
                            lines.append(_summarize_line(parsed, result))
                    finally:
                        self.unmark_processing(parsed)
                await _edit(
                    placeholder,
                    _build_batch_summary(f"⏳ 处理中 ({done}/{total})", lines),
                )
                if done < total:
                    await asyncio.sleep(2)  # 限速避免 TG 频道 flood control

        ok_count = sum(1 for ln in lines if ln.startswith("✅"))
        skip_count = sum(1 for ln in lines if ln.startswith("⏭️"))
        fail_count = sum(1 for ln in lines if ln.startswith("⚠️"))
        header = f"✅ 完成 {ok_count}/{total}"
        if skip_count:
            header += f"，⏭️ 跳过 {skip_count}"
        if fail_count:
            header += f"，⚠️ 失败 {fail_count}"
        await _edit(placeholder, _build_batch_summary(header, lines))


# 模块级单例（bot 进程内唯一）
coordinator = PushCoordinator()


async def cmd_115(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        await update.message.reply_text(_DENY_TEXT)
        return
    text = " ".join(context.args) if context.args else (update.message.text or "")
    parsed = parse_share(text)
    if not parsed:
        await update.message.reply_text("❌ 无法识别链接，请发送 115 分享链接、8+ 位裸码或 ed2k 链接。")
        return
    logger.info("收到 /115 命令：%s", parsed.provider)
    await coordinator.process(update, context, parsed)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """裸链接消息自动触发处理（单链接直推 / 多链接聚合批处理）。

    单链接且无聚合中 → 立即直推（保持原 UX）。
    多链接或聚合中 → 缓冲聚合：TG 会把超长消息拆成多条，此处等 3s 合并
    成一个批量，统一按集数排序推送，避免拆分破坏顺序。
    顶部优先处理编辑模式 AWAITING_QUALITY 状态，其次监控登录输入流。
    """
    if not _is_admin(update, context):
        return
    monitor = getattr(_container(context), "monitor", None)
    if monitor is not None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        stage = await monitor.login_stage(chat_id)
        if stage is not None:
            await _handle_login_input(update, context, monitor, stage)
            return
    session = _get_session(context)
    if session is not None and session.state == EditState.AWAITING_QUALITY:
        await _handle_quality_input(update, context, session)
        return
    text = update.message.text or ""
    shares = parse_shares(text)
    if not shares:
        return
    uid = update.effective_user.id if update.effective_user else "?"
    # 单链接且无聚合中 → 立即直推
    if len(shares) == 1 and not coordinator.has_pending():
        logger.info("收到链接：%s（user=%s）", shares[0].provider, uid)
        await coordinator.process(update, context, shares[0])
        return
    # 多链接或聚合中 → 缓冲聚合
    logger.info("收到链接：%d 个（user=%s，进入聚合）", len(shares), uid)
    await coordinator.aggregate(update, context, shares)


# ---------------------------------------------------------------------- #
# 单链接处理
# ---------------------------------------------------------------------- #
async def _process_locked(update: Update, context, container, parsed) -> None:
    # ed2k 链接很长，placeholder 用简短文案；115 显示分享码
    if parsed.provider == "ed2k":
        loading = "⏳ 正在解析 ed2k 资源 ..."
    else:
        loading = f"⏳ 正在读取分享 `{parsed.code}` ..."
    placeholder = await update.message.reply_text(loading, parse_mode="Markdown")
    try:
        result = await container.processor.process(parsed)
    except Pan115Error as exc:
        await _edit(placeholder, f"❌ 115 错误：{exc}")
        return
    except Exception as exc:
        logger.exception("处理分享失败")
        await _edit(placeholder, f"❌ 处理失败：{exc}")
        return

    if result.ok:
        await _edit(
            placeholder,
            f"✅ {result.message}\n"
            f"📁 文件 {result.file_count} · 🎬 {result.title}"
            + (f" ({result.year})" if result.year else ""),
        )
    else:
        await _edit(placeholder, f"⚠️ {result.message}")


# ---------------------------------------------------------------------- #
# 多链接批处理
# ---------------------------------------------------------------------- #
def _short_id(parsed) -> str:
    """汇总行短标识（无标题时兜底）。"""
    return "ed2k 资源" if parsed.provider == "ed2k" else parsed.code


def _summarize_line(parsed, result) -> str:
    """单条结果 → 汇总行（单行，去换行防排版错乱）。"""
    msg = (result.message or "").replace("\n", " ")
    if result.ok:
        title = (result.title or _short_id(parsed)).replace("\n", " ")
        year = f" ({result.year})" if result.year else ""
        return f"✅ {title}{year}"
    if "已推送" in msg:
        title = (result.title or _short_id(parsed)).replace("\n", " ")
        return f"⏭️ {title}（已推送，跳过）"
    return f"⚠️ {_short_id(parsed)}：{msg}"


def _build_batch_summary(header: str, lines: list[str]) -> str:
    """组装批处理汇总，超 4096 截断。"""
    body = "\n".join(lines)
    msg = f"{header}\n\n{body}"
    if len(msg) <= 4000:
        return msg
    kept: list[str] = []
    total = len(header) + 2
    for line in lines:
        if total + len(line) + 1 > 3990:
            break
        kept.append(line)
        total += len(line) + 1
    return f"{header}\n\n" + "\n".join(kept) + "\n…（更多已截断）"


_EP_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def _episode_sort_key(parsed) -> tuple:
    """按季集排序 ed2k 链接：从文件名提取 SxxExx；无法提取的排最后（保持原序）。

    115 分享码不含文件名，无法预排序，统一排后并保持原文顺序。
    """
    if parsed.provider == "ed2k":
        parts = parsed.code.split("|")
        if len(parts) > 2:
            m = _EP_RE.search(parts[2])
            if m:
                return (0, int(m.group(1)), int(m.group(2)))
    return (1, 0, 0)
