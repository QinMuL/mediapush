"""推送卡片渲染 + 发送（单消息）。

- 有海报：send_photo + caption（≤1024，智能截断）
- 无海报：send_message + text（≤4096，完整不截断）

caption 内容：标题区 + 文件清单模块 + 简介模块 + 链接
超 1024 时按优先级截断：先截简介 → 再减文件项数 → 去简介 → 去文件清单。
两个可展开模块用 <blockquote expandable>，默认折叠点击展开。
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from app.parser.media_parser import AggregatedMedia
from app.providers.base import ShareFile

logger = logging.getLogger(__name__)

_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096
_ELLIPSIS = "…"

TMDB_SITE = "https://www.themoviedb.org"

# ISO 3166-1 国家代码 → (国旗 emoji, 中文名)
_COUNTRY_MAP = {
    "US": ("🇺🇸", "美国"), "CN": ("🇨🇳", "中国大陆"), "HK": ("🇭🇰", "中国香港"),
    "TW": ("🇹🇼", "中国台湾"), "JP": ("🇯🇵", "日本"), "KR": ("🇰🇷", "韩国"),
    "GB": ("🇬🇧", "英国"), "FR": ("🇫🇷", "法国"), "DE": ("🇩🇪", "德国"),
    "IN": ("🇮🇳", "印度"), "CA": ("🇨🇦", "加拿大"), "AU": ("🇦🇺", "澳大利亚"),
    "ES": ("🇪🇸", "西班牙"), "IT": ("🇮🇹", "意大利"), "RU": ("🇷🇺", "俄罗斯"),
    "TH": ("🇹🇭", "泰国"), "MX": ("🇲🇽", "墨西哥"), "BR": ("🇧🇷", "巴西"),
    "NL": ("🇳🇱", "荷兰"), "SE": ("🇸🇪", "瑞典"), "DK": ("🇩🇰", "丹麦"),
    "NO": ("🇳🇴", "挪威"), "FI": ("🇫🇮", "芬兰"), "PL": ("🇵🇱", "波兰"),
    "TR": ("🇹🇷", "土耳其"), "AE": ("🇦🇪", "阿联酋"), "SA": ("🇸🇦", "沙特"),
    "EG": ("🇪🇬", "埃及"), "ZA": ("🇿🇦", "南非"), "NG": ("🇳🇬", "尼日利亚"),
    "AR": ("🇦🇷", "阿根廷"), "CL": ("🇨🇱", "智利"), "CO": ("🇨🇴", "哥伦比亚"),
    "BE": ("🇧🇪", "比利时"), "CH": ("🇨🇭", "瑞士"), "AT": ("🇦🇹", "奥地利"),
    "IE": ("🇮🇪", "爱尔兰"), "PT": ("🇵🇹", "葡萄牙"), "GR": ("🇬🇷", "希腊"),
    "CZ": ("🇨🇿", "捷克"), "HU": ("🇭🇺", "匈牙利"), "RO": ("🇷🇴", "罗马尼亚"),
    "IL": ("🇮🇱", "以色列"), "ID": ("🇮🇩", "印度尼西亚"), "MY": ("🇲🇾", "马来西亚"),
    "SG": ("🇸🇬", "新加坡"), "PH": ("🇵🇭", "菲律宾"), "VN": ("🇻🇳", "越南"),
    "PK": ("🇵🇰", "巴基斯坦"), "BD": ("🇧🇩", "孟加拉国"), "IR": ("🇮🇷", "伊朗"),
    "KZ": ("🇰🇿", "哈萨克斯坦"), "UA": ("🇺🇦", "乌克兰"), "IS": ("🇮🇸", "冰岛"),
    "NZ": ("🇳🇿", "新西兰"), "PE": ("🇵🇪", "秘鲁"), "VE": ("🇻🇪", "委内瑞拉"),
    "JO": ("🇯🇴", "约旦"), "LB": ("🇱🇧", "黎巴嫩"), "IQ": ("🇮🇶", "伊拉克"),
    "KH": ("🇰🇭", "柬埔寨"), "MM": ("🇲🇲", "缅甸"), "LK": ("🇱🇰", "斯里兰卡"),
}

# TMDB 状态 → 中文
_STATUS_MAP = {
    "Returning Series": "连载中",
    "Ended": "已完结",
    "Canceled": "已取消",
    "In Production": "制作中",
    "Planned": "计划中",
    "Pilot": "试播集",
    "Released": "已上映",
    "Rumored": "传闻中",
    "Post Production": "后期制作中",
}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(_ELLIPSIS))] + _ELLIPSIS


def _human_size(n: int) -> str:
    """字节数 → 人类可读（GB/MB/KB，二进制 1G=1024M，与网盘/Windows 一致）。0 或目录返回空串。"""
    if n <= 0:
        return ""
    units = [("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]
    for label, factor in units:
        if n >= factor:
            return f"{n / factor:.2f} {label}"
    return f"{n} B"


# hashtag 清洗：仅保留单词字符（字母/数字/下划线，含 CJK），去引号/括号/冒号/连字符等
# Telegram 的 # 标签遇到标点会断开，需清洗否则 #喜欢上"欠欠"的你 只识别成 #喜欢上
_TAG_CLEAN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tag_name(title: str) -> str:
    """片名 → hashtag 安全名：去空格与所有非单词字符。"""
    return _TAG_CLEAN_RE.sub("", title or "")


def _quality_line(media: AggregatedMedia) -> str:
    """画质信息：优先用详细 quality_info（分辨率|平台|来源|DV版本|编码|色深|帧率|音频）。"""
    info = getattr(media, "quality_info", None) or []
    if info:
        return " | ".join(info)
    # 兜底：用旧的 quality/source/hdr
    parts = [p for p in (media.quality, media.hdr, media.source) if p]
    return " ".join(parts) or "未知"


# ---------------------------------------------------------------------- #
# 季集范围合并辅助
# ---------------------------------------------------------------------- #
def _merge_ranges(eps: list[int]) -> list[tuple[int, int]]:
    """连续集号合并成范围。返回 [(start, end), ...]。"""
    if not eps:
        return []
    eps = sorted(set(eps))
    ranges = [(eps[0], eps[0])]
    for ep in eps[1:]:
        if ep == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], ep)
        else:
            ranges.append((ep, ep))
    return ranges


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    """范围列表格式化：E01-E05、E07、E10-E12"""
    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(f"E{start:02d}")
        else:
            parts.append(f"E{start:02d}-E{end:02d}")
    return "、".join(parts)


def _format_season_episodes(season_episodes: dict[int, list[int]]) -> str:
    """格式化每季集数范围：S01 E01-E61 | S02 E62-E77 | ..."""
    parts = []
    for season in sorted(season_episodes.keys()):
        eps = season_episodes[season]
        ranges = _merge_ranges(eps)
        parts.append(f"S{season:02d} {_format_ranges(ranges)}")
    return " | ".join(parts)


def _reallocate_by_tmdb(
    all_eps: list[int], tmdb_seasons: list[dict]
) -> dict[int, list[int]]:
    """用 TMDB 季的 episode_count 计算每季集号范围，把全局集号分配到对应季。

    适用场景：文件名只有 S01Exxx（全局集号），但 TMDB 有多季。
    """
    season_ranges: dict[int, tuple[int, int]] = {}
    ep_start = 1
    for s in sorted(tmdb_seasons, key=lambda x: x.get("season") or 0):
        season = s.get("season")
        ec = s.get("episode_count") or 0
        if season is not None and season > 0 and ec > 0:
            season_ranges[season] = (ep_start, ep_start + ec - 1)
            ep_start += ec
    if not season_ranges:
        return {}
    result: dict[int, list[int]] = {}
    for ep in all_eps:
        for season, (lo, hi) in season_ranges.items():
            if lo <= ep <= hi:
                result.setdefault(season, []).append(ep)
                break
    return result


# 推荐语/精品说明最大长度（caption ≤1024 限额兜底，无海报路径 ≤4096 余量大）
_QUALITY_EXTRA_LIMIT = 200


def _render_quality_block(
    media: AggregatedMedia,
    *,
    quality_extra: str = "",
    is_premium: bool = False,
) -> str:
    """画质模块：<blockquote>：可选 💎精品 + 💿画质（自动 8 维度）+ 📝推荐语。

    - is_premium：追加「💎 精品资源」行（普通/精品资源视觉区分）
    - 自动 quality_info（_quality_line）保留，作为「💿 画质：...」行
    - quality_extra：用户手动追加的推荐语/精品说明，esc + 截断防注入与超长
    三者同处一个 blockquote；全空返回 ""（与原行为一致，自动直推路径不受影响）。
    """
    lines: list[str] = []
    if is_premium:
        lines.append("💎 精品资源")
    q = _quality_line(media)
    if q and q != "未知":
        lines.append(f"💿 画质：{_esc(q)}")
    if quality_extra:
        lines.append(f"📝 {_esc(_truncate(quality_extra.strip(), _QUALITY_EXTRA_LIMIT))}")
    if not lines:
        return ""
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _render_season_block(details: dict, media: AggregatedMedia) -> str:
    """季集信息 HTML 模块（<blockquote>，默认展开）。

    TV 两行：
      🗂️ 季集：S01 共20集（4个文件）           ← TMDB 总集数 + 实际文件数
      📋 集数：S01 E01-E04 | S02 E05-E08 | ...  ← 按季实际集数范围
    多季：S01-S23 共1181集（1166个文件） / S01 E01-E61 | S02 E62-E77 | ...
    缺集：S01 E02-E05、E07、E10-E12 | ...
    电影：返回空（时长在 head 里）
    """
    if details["media_type"] != "tv":
        return ""

    seasons_list = getattr(media, "seasons", None) or []
    actual = media.file_count  # 视频文件数（不含文件夹）
    file_season_eps = getattr(media, "season_episodes", None) or {}
    tmdb_seasons = details.get("seasons", [])

    # 季范围字符串
    if len(seasons_list) > 1:
        season_str = f"S{seasons_list[0]:02d}-S{seasons_list[-1]:02d}"
    elif media.season is not None:
        # season 0 = 特别篇/specials
        season_str = "S00 特别篇" if media.season == 0 else f"S{media.season:02d}"
    elif seasons_list:
        season_str = f"S{seasons_list[0]:02d}"
    else:
        season_str = ""

    # TMDB 总集数：只统计分享中包含的季，而非整剧总集数
    if len(seasons_list) > 1:
        # 多季：TMDB 覆盖分享中所有季时精确统计，否则回退整剧总集数
        season_set = set(seasons_list)
        tmdb_season_nums = {s.get("season") for s in tmdb_seasons}
        if season_set <= tmdb_season_nums:
            tmdb_eps = sum(
                s.get("episode_count") or 0
                for s in tmdb_seasons
                if s.get("season") in season_set
            )
        else:
            tmdb_eps = details.get("number_of_episodes") or 0
    elif media.season is None:
        tmdb_eps = details.get("number_of_episodes") or 0
    else:
        tmdb_eps = 0
        for s in tmdb_seasons:
            if s.get("season") == media.season:
                tmdb_eps = s.get("episode_count") or 0
                break
        # S00（特别篇）无 TMDB 数据时不回退整剧总集数（避免误显 328 集）
        if not tmdb_eps and media.season != 0:
            tmdb_eps = details.get("number_of_episodes") or 0

    lines: list[str] = []
    # 季集行
    if season_str:
        if tmdb_eps:
            lines.append(f"🗂️ 季集：{season_str} 共{tmdb_eps}集（{actual}个文件）")
        elif actual:
            lines.append(f"🗂️ 季集：{season_str}（{actual}个文件）")
        else:
            lines.append(f"🗂️ 季集：{season_str}")

    # 集数行：按季渲染实际集数范围
    final_eps = file_season_eps
    # 仅 S01（可能默认标注）+ 全局集号才重分配：文件名只有 S01Exxx 但 TMDB 有多季，
    # 用 TMDB 季范围把全局集号拆分到各季。S02+ 显式标注，集号是按季的，不重分配。
    # S00（特别篇）也不参与重分配。
    if (
        len(file_season_eps) == 1
        and 0 not in file_season_eps
        and 1 in file_season_eps
        and len(tmdb_seasons) > 1
    ):
        all_eps = next(iter(file_season_eps.values()))
        reallocated = _reallocate_by_tmdb(all_eps, tmdb_seasons)
        if reallocated:
            final_eps = reallocated

    if final_eps:
        ep_str = _format_season_episodes(final_eps)
        if ep_str:
            lines.append(f"📋 集数：{ep_str}")
    elif media.episode_start is not None:
        # 兜底：用整体集数范围
        if media.episode_end and media.episode_end > media.episode_start:
            ep_range = f"E{media.episode_start:02d}-E{media.episode_end:02d}"
        else:
            ep_range = f"E{media.episode_start:02d}"
        prefix = f"{season_str} " if season_str else ""
        lines.append(f"📋 集数：{prefix}{ep_range}")

    # 状态行：TMDB 网站状态（连载中/已完结/...）；未映射值转义防 HTML 注入
    status_raw = details.get("status") or ""
    if status_raw:
        status_zh = _STATUS_MAP.get(status_raw, status_raw)
        lines.append(f"⚙️ 状态：{_esc(status_zh)}")

    if not lines:
        return ""
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _tmdb_url(details: dict) -> str:
    kind = "movie" if details["media_type"] == "movie" else "tv"
    return f"{TMDB_SITE}/{kind}/{details['tmdb_id']}"


def _share_url(code: str, password: str | None) -> str:
    url = f"https://115.com/s/{code}"
    if password:
        url += f"?password={password}"
    return url


# ---------------------------------------------------------------------- #
# 内容片段
# ---------------------------------------------------------------------- #
def _render_head(
    details: dict, media: AggregatedMedia, files: list[ShareFile] | None = None
) -> str:
    """标题区：标题 + 信息行（评分/年份/地区/类型/画质/主演/体积/标签/日期/时长）。"""
    title = _esc(details.get("title") or media.title)
    is_movie = (details.get("media_type") or media.media_type) == "movie"
    icon_label = "📽️ 电影" if is_movie else "🎞️ 剧集"
    lines = [f"{icon_label}：<b>{title}</b>"]

    # TMDB ID（配套下方 📚 TMDB 详情按钮，便于溯源）
    tmdb_id = details.get("tmdb_id")
    if tmdb_id:
        lines.append(f"🆔 TMDB：{tmdb_id}")

    # 评分
    vote = details.get("vote_average") or 0
    vcount = details.get("vote_count") or 0
    if vote:
        vote_s = f"✨ 评分：{vote}"
        if vcount:
            vote_s += f" · {vcount}票"
        lines.append(vote_s)

    # 年份
    year = details.get("year") or media.year or ""
    if year:
        lines.append(f"⏳ 年份：{year}")

    # 地区（ISO 代码 → 国旗+中文）
    countries = details.get("countries") or []
    if countries:
        parts = []
        for code in countries[:3]:
            flag, zh = _COUNTRY_MAP.get(code, ("", code))
            parts.append(f"{flag} {zh}" if flag else zh)
        lines.append(f"🌐 地区：{' / '.join(parts)}")

    # 类型
    genres = " / ".join(details.get("genres", [])[:3])
    if genres:
        lines.append(f"🔮 类型：{_esc(genres)}")

    # 画质（单独成模块，见 _render_quality_block）

    # 主演
    cast = details.get("cast") or []
    if cast:
        lines.append(f"🎭 主演：{_esc(' / '.join(cast[:5]))}")

    # 体积（所有文件大小总和，不含目录）
    if files:
        total_size = sum(f.size for f in files if not f.is_dir and f.size)
        if total_size:
            lines.append(f"💾 体积：{_human_size(total_size)}")

    # 标签（片名作为 hashtag，去所有非单词字符防断开）
    tag_name = _tag_name(details.get("title") or media.title or "")
    if tag_name:
        lines.append(f"🏷️ 标签：#{_esc(tag_name)}")

    # 日期
    rd = details.get("release_date")
    if rd:
        label = "上映" if details["media_type"] == "movie" else "首播"
        lines.append(f"📅 {label} {_esc(rd)}")

    # 电影时长
    if details["media_type"] == "movie":
        rt = details.get("runtime")
        if rt:
            lines.append(f"⏱️ 时长：{_esc(rt)} 分钟")

    return "\n".join(lines)


def _render_footer(code: str, password: str | None, provider: str = "115") -> str:
    """链接区：115 网盘 / ed2k 资源 模块（明文链接）。TMDB 详情做成卡片下方 inline button。

    - 115：code 为分享码，拼访问码成完整 URL
    - ed2k：code 即完整 ed2k URL，直接展示（无访问码）
    """
    if provider == "ed2k":
        return f"<blockquote>\U0001F517 ed2k 资源\n<code>{_esc(code)}</code></blockquote>"
    share = _esc(_share_url(code, password))
    return f"<blockquote>\U0001F517 115 网盘\n<code>{share}</code></blockquote>"


# ---------------------------------------------------------------------- #
# 文件清单自然排序
# ---------------------------------------------------------------------- #
_NATURAL_SPLIT = re.compile(r"(\d+)")
_SEASON_DIR_RE = re.compile(r"^season\s*(\d+)$", re.IGNORECASE)
_TMDB_DIR_RE = re.compile(r"\{tmdb-\d+\}", re.IGNORECASE)


def _natural_key(name: str) -> list:
    """自然排序 key：数字按数值比较，其他按字符串。"""
    return [
        int(t) if t.isdigit() else t.lower() for t in _NATURAL_SPLIT.split(name)
    ]


def _file_sort_key(f: ShareFile) -> tuple:
    """排序 key：目录在前 → TMDB 父目录 → 普通目录 → Season 目录按季号 → 文件。"""
    name = f.name.strip()
    dir_first = 0 if f.is_dir else 1
    # 带 {tmdb-xxx} 的是分享根目录/父目录，排最前
    if _TMDB_DIR_RE.search(name):
        return (dir_first, -1, _natural_key(name))
    m = _SEASON_DIR_RE.match(name)
    if m:
        # Season 目录排普通目录之后，按季号数字排序
        return (dir_first, 1, int(m.group(1)))
    return (dir_first, 0, _natural_key(name))


def _render_files_block(
    files: list[ShareFile],
    *,
    max_items: int | None = None,
    provider: str = "115",
    files_sorted: list[ShareFile] | None = None,
) -> str:
    """文件清单可展开模块。max_items 限制显示前 N 项。

    files_sorted：调用方已排序的列表（截断搜索复用，避免大分享重复排序）。
    """
    files_sorted = files_sorted if files_sorted is not None else sorted(files, key=_file_sort_key)
    shown = files_sorted if max_items is None else files_sorted[:max_items]
    title = "📁 资源文件" if provider == "ed2k" else "📁 分享内容"
    lines = [f"<blockquote expandable>{title}（{len(files)} 项） · 点击展开"]
    for f in shown:
        icon = "📁" if f.is_dir else "📄"
        line = f"{icon} {_esc(f.name)}"
        if not f.is_dir and f.size:
            line += f"  ·  {_human_size(f.size)}"
        lines.append(line)
    if max_items is not None and max_items < len(files):
        lines.append(f"… 共 {len(files)} 项，已显示前 {max_items} 项")
    lines.append("</blockquote>")
    return "\n".join(lines)


def _render_overview_block(overview: str, *, limit: int | None = None) -> str:
    """简介可展开模块。limit 截断字数。"""
    text = _truncate(overview, limit) if limit else overview
    return f"<blockquote expandable>📝 简介 · 点击展开\n{text}\n</blockquote>"


# ---------------------------------------------------------------------- #
# 智能截断（caption ≤1024）
# ---------------------------------------------------------------------- #
def _fit_caption(
    head: str,
    quality_block: str,
    season_block: str,
    footer: str,
    files: list[ShareFile] | None,
    overview: str,
    limit: int = _CAPTION_LIMIT,
    provider: str = "115",
) -> str:
    """组装 caption，超限按优先级截断。

    固定部分（必保）：head + quality_block + season_block + footer
    可变部分（按优先级截断）：文件清单 → 简介

    性能：排序一次复用；减文件项数用二分查找（caption 长度随项数单调
    不减），1166 文件从线性重试 ~3.6s 降到毫秒级，不再阻塞事件循环。
    """
    SEP = "\n\n"

    def build(fb: str, ob: str) -> str:
        parts = [head]
        if quality_block:
            parts.append(quality_block)
        if season_block:
            parts.append(season_block)
        if fb:
            parts.append(fb)
        if ob:
            parts.append(ob)
        parts.append(footer)
        return SEP.join(parts)

    # 排序一次：完整渲染与截断二分复用
    files_sorted = sorted(files, key=_file_sort_key) if files else None

    def render_fb(n: int | None = None) -> str:
        return _render_files_block(
            files, max_items=n, provider=provider, files_sorted=files_sorted
        )

    fb = render_fb() if files else ""
    ob = _render_overview_block(overview) if overview else ""

    # 1. 完整
    body = build(fb, ob)
    if len(body) <= limit:
        return body

    # 2. 简介截断到 200
    if overview:
        ob = _render_overview_block(overview, limit=200)
        body = build(fb, ob)
        if len(body) <= limit:
            return body

    # 3. 减文件项数：二分找最大 n∈[1, len] 使 caption ≤ limit
    if files:
        fb = ""  # 兜底：n=1 仍超限时去掉整个文件清单
        if len(build(render_fb(1), ob)) <= limit:
            lo, hi = 1, len(files)
            while lo <= hi:
                mid = (lo + hi) // 2
                fb_mid = render_fb(mid)
                if len(build(fb_mid, ob)) <= limit:
                    fb = fb_mid
                    lo = mid + 1
                else:
                    hi = mid - 1
        body = build(fb, ob)
        if len(body) <= limit:
            return body

    # 4. 去简介
    if overview:
        body = build(fb, "")
        if len(body) <= limit:
            return body

    # 5. 兜底（head + quality_block + season_block + footer）
    parts = [head]
    if quality_block:
        parts.append(quality_block)
    if season_block:
        parts.append(season_block)
    parts.append(footer)
    return SEP.join(parts)


# ---------------------------------------------------------------------- #
# 渲染入口
# ---------------------------------------------------------------------- #
def render_caption(
    details: dict,
    media: AggregatedMedia,
    code: str,
    password: str | None,
    files: list[ShareFile] | None = None,
    provider: str = "115",
    *,
    quality_extra: str = "",
    is_premium: bool = False,
) -> str:
    """海报下方 caption（≤1024）：标题区 + 季集模块 + 文件清单模块 + 简介模块 + 链接。

    超限智能截断。quality_extra/is_premium 透传给画质模块（编辑模式用）。
    """
    head = _render_head(details, media, files)
    quality_block = _render_quality_block(
        media, quality_extra=quality_extra, is_premium=is_premium
    )
    season_block = _render_season_block(details, media)
    footer = _render_footer(code, password, provider)
    overview = _esc(details.get("overview") or "")
    return _fit_caption(
        head, quality_block, season_block, footer, files, overview, _CAPTION_LIMIT, provider
    )


def render_text(
    details: dict,
    media: AggregatedMedia,
    code: str,
    password: str | None,
    files: list[ShareFile] | None = None,
    provider: str = "115",
    *,
    quality_extra: str = "",
    is_premium: bool = False,
) -> str:
    """无海报时的完整消息（≤4096）：标题区 + 画质模块 + 季集模块 + 文件清单模块 + 简介模块 + 链接。"""
    head = _render_head(details, media, files)
    quality_block = _render_quality_block(
        media, quality_extra=quality_extra, is_premium=is_premium
    )
    season_block = _render_season_block(details, media)
    footer = _render_footer(code, password, provider)
    overview = _esc(details.get("overview") or "")

    blocks: list[str] = []
    if quality_block:
        blocks.append(quality_block)
    if season_block:
        blocks.append(season_block)
    if files:
        blocks.append(_render_files_block(files, provider=provider))
    if overview:
        blocks.append(_render_overview_block(overview))

    body = "\n\n".join([head] + blocks + [footer])
    return _truncate(body, _TEXT_LIMIT)


# ---------------------------------------------------------------------- #
async def _send_with_retry(sender):
    """发送消息，遇 RetryAfter（flood control）等待后重试，最多 3 次。"""
    from telegram.error import RetryAfter

    for attempt in range(3):
        try:
            return await sender()
        except RetryAfter as exc:
            if attempt == 2:
                raise
            logger.warning("Flood control，%ss 后重试（第 %d 次）", exc.retry_after, attempt + 1)
            await asyncio.sleep(exc.retry_after + 1)


class Pusher:
    def __init__(
        self,
        bot,
        chat_id: str,
        chat_id_115: str = "",
        chat_id_ed2k: str = "",
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id  # 默认频道（未分流时回退）
        self.chat_id_115 = chat_id_115
        self.chat_id_ed2k = chat_id_ed2k

    def _select_chat_id(self, provider: str) -> str:
        """按 provider 分流频道：ed2k → ed2k 频道，115/其他 → 115 频道；未配置回退默认。"""
        if provider == "ed2k":
            return self.chat_id_ed2k or self.chat_id
        return self.chat_id_115 or self.chat_id

    async def push_share(
        self,
        details: dict,
        media: AggregatedMedia,
        code: str,
        password: str | None,
        files: list[ShareFile] | None = None,
        provider: str = "115",
        *,
        quality_extra: str = "",
        is_premium: bool = False,
        chat_id: str | None = None,
    ) -> tuple[bool, str, int | None, str]:
        """推送卡片到频道（单消息）。

        有海报：send_photo + caption（≤1024，智能截断）
        无海报或发送失败：send_message + text（≤4096，完整）
        quality_extra/is_premium 透传给渲染（编辑模式精品标记/推荐语）。
        chat_id 覆盖默认分流目标（频道监控用；None 按 provider 分流）。
        返回 (ok, message, message_id, chat_id)：后两者为频道消息引用，
        供失效巡检撤卡（delete_message）与 mark_pushed 存档。
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from app.tmdb.client import TMDBHelper

        chat_id = chat_id or self._select_chat_id(provider)
        image_url = TMDBHelper.image_url(details)
        tmdb_url = _tmdb_url(details)
        reply_markup = None
        if tmdb_url:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📚 TMDB 详情", url=tmdb_url)]]
            )

        if image_url:
            caption = render_caption(
                details, media, code, password, files, provider,
                quality_extra=quality_extra, is_premium=is_premium,
            )
            try:
                sent = await _send_with_retry(lambda: self.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                ))
                logger.info("推送 → 频道 %s（海报+详情）", chat_id)
                return True, "已推送（海报+详情）", _msg_id(sent), _chat_id_str(sent, chat_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("send_photo 失败，回退 send_message：%s", exc)

        # 无海报或海报发送失败：send_message（≤4096）
        text = render_text(
            details, media, code, password, files, provider,
            quality_extra=quality_extra, is_premium=is_premium,
        )
        sent = await _send_with_retry(lambda: self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=reply_markup,
        ))
        logger.info("推送 → 频道 %s（纯文本+详情）", chat_id)
        return True, "已推送（纯文本+详情）", _msg_id(sent), _chat_id_str(sent, chat_id)


def _msg_id(sent: object) -> int | None:
    """从发送返回的 Message 取 message_id（测试桩无此属性时 None）。"""
    return getattr(sent, "message_id", None)


def _chat_id_str(sent: object, fallback: str) -> str:
    """从发送返回的 Message 取 chat.id（测试桩无此属性时回退目标 chat_id）。"""
    chat = getattr(sent, "chat", None)
    cid = getattr(chat, "id", None)
    return str(cid) if cid is not None else str(fallback)
