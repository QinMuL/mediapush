"""监控消息处理（纯函数，无 IO，便于单测）。

职责链：提取 ed2k → 验证格式 → 关键词过滤 → 渲染推送文本。
- 提取复用 link_parser.parse_shares（保持与 Bot 主链路同一套正则语义）
- 过滤规则：include（命中才推）/ exclude（命中即丢），关键词对文件名不区分大小写
- 渲染：来源频道 + 北京时间戳 + 链接（<code> 明文块，沿用主链路 ed2k 展示约定）
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.link_parser import parse_shares

_TZ = ZoneInfo("Asia/Shanghai")
_MAX_LINKS = 20  # 单条推送最多展示的链接数（超出仅计数，防超长消息）

# 字段拆解：ed2k://|file|<name>|<size>|<hash>|...
_ED2K_FIELDS_RE = re.compile(
    r"^ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|",
    re.IGNORECASE,
)
# 完整性校验：必须以 / 收尾（标准 ed2k://|file|...|/ 形态）
_ED2K_VALID_RE = re.compile(
    r"^ed2k://\|file\|[^|]+\|\d+\|[0-9A-Fa-f]{32}\|.*?/$",
    re.IGNORECASE,
)


@dataclass
class LinkItem:
    link: str  # 完整 ed2k URL（去重 key / 展示内容）
    filename: str
    size: int  # 字节


@dataclass
class FilterRules:
    include: list[str]  # 命中任一才推送（空 = 不限）
    exclude: list[str]  # 命中任一即丢弃


def extract_ed2k(text: str) -> list[str]:
    """提取文本中全部 ed2k 链接（按出现顺序，去重）。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in parse_shares(text):
        if p.provider == "ed2k" and p.code not in seen:
            seen.add(p.code)
            out.append(p.code)
    return out


def validate(link: str) -> bool:
    """格式完整性验证：文件名/字节数/32 位 hash 齐全且以 / 收尾。"""
    return bool(_ED2K_VALID_RE.match(link))


def parse_link(link: str) -> LinkItem | None:
    """拆解链接字段；格式非法返回 None。"""
    m = _ED2K_FIELDS_RE.match(link)
    if not m:
        return None
    return LinkItem(link=link, filename=m.group(1), size=int(m.group(2)))


def human_size(n: float) -> str:
    """二进制（1024）单位，2 位小数——沿用主链路容量口径。"""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


def match_filters(filename: str, rules: FilterRules) -> bool:
    """关键词过滤：exclude 优先否决；配置了 include 时必须命中其一。"""
    low = filename.lower()
    if any(k.lower() in low for k in rules.exclude):
        return False
    if not rules.include:
        return True
    return any(k.lower() in low for k in rules.include)


def format_ts(ts: float) -> str:
    """时间戳 → 北京时间字符串。"""
    return datetime.fromtimestamp(ts, _TZ).strftime("%Y-%m-%d %H:%M:%S")


def render_batch(source_title: str, items: list[LinkItem], latest_ts: float) -> str:
    """渲染推送消息（Telegram HTML）。

    结构：📡 监控头 / 📺 来源 / 🕐 时间 / 🔗 链接列表（序号 + 文件名（大小） + <code> 链接）。
    """
    lines = [
        "📡 频道监控",
        f"📺 来源：{html.escape(source_title)}",
        f"🕐 时间：{format_ts(latest_ts)}",
        f"🔗 ed2k 链接（{len(items)} 条）：",
    ]
    for i, item in enumerate(items[:_MAX_LINKS], 1):
        lines.append(f"{i}. {html.escape(item.filename)}（{human_size(item.size)}）")
        lines.append(f"<code>{html.escape(item.link)}</code>")
    if len(items) > _MAX_LINKS:
        lines.append(f"… 共 {len(items)} 条，仅显示前 {_MAX_LINKS} 条")
    return "\n".join(lines)


def process_text(
    text: str,
    source_title: str,
    rules: FilterRules,
    *,
    latest_ts: float | None = None,
) -> tuple[str, list[LinkItem]]:
    """完整处理链：提取 → 验证 → 过滤 → 渲染。

    返回 (推送文本, 通过的链接列表)；无有效链接时文本为空串。
    latest_ts 缺省用当前时间。
    """
    ts = latest_ts if latest_ts is not None else datetime.now(UTC).timestamp()
    items: list[LinkItem] = []
    seen: set[str] = set()
    for link in extract_ed2k(text):
        if link in seen or not validate(link):
            continue
        item = parse_link(link)
        if item is None or not match_filters(item.filename, rules):
            continue
        seen.add(link)
        items.append(item)
    if not items:
        return "", []
    return render_batch(source_title, items, ts), items
