"""命名引擎：文件名解析 + TMDB 硬门槛匹配 + 实测标签 → 统一模板命名。

用户约定的硬标准：
- 有片名有年份：片名和年份都匹配上 TMDB 才放行重命名（剧集另需 SxxExx）
- 有片名无年份：允许 TMDB 查补年份（标题匹配候选唯一才放行）
- 低置信度不予继续流程，原地保留等待下轮重试（新片 TMDB 数据滞后会补全）

模板（刮削向，变量缺失时连同分隔点一起折叠）：
- 电影：{{title}} ({{year}}) - {{pix}}.{{platform}}.{{web_source}}.
  {{resource_type}}.{{effect}}.{{video_encode}}.{{bit_depth}}.{{frame_rate}}.
  {{audio_encode}}-{{team}}{{ext}}
- 剧集：{{title}}.{{year}}.SxxEyy.第zz集.{{pix}}.{{platform}}.{{web_source}}.
  {{resource_type}}.{{effect}}.{{video_encode}}.{{bit_depth}}.{{frame_rate}}.
  {{audio_encode}}-{{team}}{{ext}}

变量来源分工：
- 文件名（guessit）：title 候选 / year / SxxExx / web_source / 发布组 / 播放平台
- ffprobe 实测（probe.py）：分辨率 / HDR 效果 / 视频编码 / 色深 / 帧率 / 音频编码
- TMDB（zh-CN）：规范 title / year（无年份时查补）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.media.probe import ProbeTags, probe_file
from app.parser.media_parser import (
    MediaData,
    clean_release_group,
    extract_platform,
    is_platform_token,
    parse_filename,
)
from app.tmdb.client import TMDBHelper

logger = logging.getLogger(__name__)


@dataclass
class NamingResult:
    """命名分析结果。"""

    parsed: MediaData
    probe: ProbeTags | None = None
    details: dict | None = None        # TMDB 详情（命中时）
    reasons: list[str] = field(default_factory=list)  # 低置信原因（空=高置信）
    proposed: str = ""                  # 高置信时：拟命名（含扩展名）
    preview: str = ""                   # 低置信时：仅参考预览（用原解析）

    @property
    def high_confidence(self) -> bool:
        return not self.reasons and self.details is not None


# ---------------------------------------------------------------------- #
# 文件名预处理：leading SxxExx（如 "S02E08.One.Hundred.Years..."）
# ---------------------------------------------------------------------- #
_LEADING_SE_RE = re.compile(r"^s\d{1,2}e\d{1,4}[\s._\-]+", re.IGNORECASE)
# 浏览器重复下载后缀 "...(1)"（clean 后残留孤立 "1" 干扰 guessit）
_DUP_SUFFIX_RE = re.compile(r"[\s._\-(]1[\s._\)]*$", re.IGNORECASE)


def parse_for_naming(name: str) -> MediaData:
    """面向命名的解析：处理 leading SxxExx / 重复下载后缀等命名场景特例。"""
    stripped = _DUP_SUFFIX_RE.sub("", name) or name
    base = parse_filename(stripped)

    # 标题在 SxxExx 之后（如 "S02E08.One.Hundred.Years.of.Solitude..."）：
    # guessit 对这种顺序经常解析不出正确标题，剥掉前缀重解析标题
    lead_stripped = _LEADING_SE_RE.sub("", stripped)
    if lead_stripped != stripped:
        reparsed = parse_filename(lead_stripped)
        if reparsed.title and reparsed.title != base.title:
            base.title = reparsed.title

    # 剧集有集号无季号 → 默认 S1（命名圈惯例）
    if base.media_type == "tv" and base.episode is not None and base.season is None:
        base.season = 1

    # 发布组清洗（内部处理多 token 误判："4Audios HDVWEB" → "HDVWEB"）；
    # 平台名被 guessit 误判为发布组时剔除（"1080p.friDay.WEB-DL" 的 friDay
    # 已作为平台保留在质量段，尾部不再重复）
    group = clean_release_group(base.release_group)
    base.release_group = "" if is_platform_token(group) else group
    return base


# ---------------------------------------------------------------------- #
# TMDB 硬门槛匹配
# ---------------------------------------------------------------------- #
_TITLE_STRIP_RE = re.compile(r"[\s:：!！.·\-_''\"()（）]")


def _norm_title(s: str) -> str:
    return _TITLE_STRIP_RE.sub("", (s or "").lower())


def _title_match(query: str, candidate_titles: list[str]) -> bool:
    """标题匹配：归一化后相等或包含（≥4 字符防短词误匹配）。"""
    q = _norm_title(query)
    if not q:
        return False
    for t in candidate_titles:
        c = _norm_title(t)
        if not c:
            continue
        if q == c or (len(q) >= 4 and q in c) or (len(c) >= 4 and c in q):
            return True
    return False


def _cand_year(c: dict) -> int | None:
    d = c.get("release_date") or c.get("first_air_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


def _kind(c: dict) -> str:
    return "movie" if "title" in c else "tv"


async def match_tmdb(tmdb: TMDBHelper, parsed: MediaData) -> tuple[dict | None, list[str]]:
    """硬门槛匹配。返回 (details, 低置信原因)；原因空且 details 非空 = 高置信。

    硬标准：片名+年份都匹配；剧集另需 SxxExx。
    - 有片名有年份：年份必须匹配（电影严格相等；剧集资源年份可晚于 TMDB
      首播年，在播剧如 Lioness S03 在 2026 播出、首播 2023），不匹配不放行
    - 有片名无年份：允许 TMDB 查补（标题匹配候选唯一才放行，多个同名
      候选无法消歧时不放行）

    英文名场景（Sharp Turns → 藏锋）两轮搜索：
    1. zh-CN：标题/原名匹配 + 原名方括号中文标注（"[藏锋].Sharp.Turns..."）
    2. en-US：文件名是英文时，TMDB 中文结果标题对不上，换英文标题再匹配
    """
    reasons: list[str] = []
    if not parsed.title:
        return None, ["未解析出片名"]
    if parsed.media_type == "tv" and parsed.episode is None:
        reasons.append("剧集无 SxxExx 季集信息")

    queries = _search_queries(parsed)
    matched, err = await _search_round(tmdb, parsed, queries)
    if not matched and err is None:
        # 中文结果匹配不上 → 英文查询词走 en-US 搜索（标题是英文的 TMDB 条目）
        ascii_queries = [q for q in queries if q.isascii() and q.strip()]
        if ascii_queries:
            matched, err = await _search_round(tmdb, parsed, ascii_queries, language="en-US")

    if err is not None:
        return None, reasons + [err]
    if not matched:
        if parsed.year:
            return None, reasons + ["片名/年份与 TMDB 不匹配"]
        return None, reasons + ["TMDB 查补年份失败：片名不匹配"]

    best = _pick_best(matched, parsed)
    if best is None:
        return None, reasons + ["TMDB 查补年份失败：多个同名候选无法消歧"]

    try:
        details = await tmdb.get_details(int(best["id"]), parsed.media_type)
    except Exception as exc:  # noqa: BLE001
        return None, reasons + [f"TMDB 详情失败：{exc}"]
    return details, reasons


def _pick_best(matched: list[dict], parsed: MediaData) -> dict | None:
    """从匹配候选选出唯一条目。

    - 无年份（查补场景）：多候选无法消歧 → None
    - 有年份：多命中剧集取首播最晚（同名在播/最近版本）
    """
    if len(matched) == 1:
        return matched[0]
    if parsed.year is None:
        return None
    if parsed.media_type == "tv":
        return max(matched, key=lambda c: _cand_year(c) or 0)
    return matched[0]


# 原名方括号内的中文标注："[藏锋].Sharp.Turns.S01E01..." → "藏锋"
_BRACKET_CJK_RE = re.compile(r"[\[【]([\u4e00-\u9fff][\u4e00-\u9fff·0-9]{1,30})[\]】]")


def _uhd_tag(parsed: MediaData) -> str:
    """原文件名有独立 UHD token 时保留（"2160p.UHD.BluRay" 原盘惯例）。"""
    return "UHD" if re.search(r"(?i)\buhd\b", parsed.raw) else ""


# ---------------------------------------------------------------------- #
# 文件系统净化（Windows 非法字符 / 超长截断）
# ---------------------------------------------------------------------- #
# Windows 禁止的字符（/ \ 不会出现在单文件名里，防御性一并处理）
_ILLEGAL_FS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOT_RE = re.compile(r"[ .]+$")


def sanitize_name(name: str, max_len: int = 180) -> str:
    """净化文件名/目录名为文件系统合法形式。

    - `:` → 全角 `：`（"Mission: Impossible" → "Mission：Impossible"，
      保留语义；TMDB 中文标题本就用全角冒号，不受影响）
    - 其余非法字符（<>"/\\|?* 控制字符）→ 删除
    - 末尾的点/空格（Windows 路径保留名陷阱）→ 剥离
    - 超长截断 stem 保扩展名（NTFS 260 路径限制留余量）
    """
    cleaned = name.replace(":", "：")
    cleaned = _ILLEGAL_FS_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _TRAILING_DOT_RE.sub("", cleaned).rstrip()
    if len(cleaned) > max_len:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 6:  # 有扩展名：截 stem 保 ext
            cleaned = f"{stem[: max_len - len(ext) - 1].rstrip()}.{ext}"
        else:
            cleaned = cleaned[:max_len].rstrip()
        cleaned = _TRAILING_DOT_RE.sub("", cleaned)
    return cleaned or "unnamed"


def _search_queries(parsed: MediaData) -> list[str]:
    """搜索查询词：解析标题 + 原名中的中文方括号标注（去重）。"""
    queries = [parsed.title]
    for m in _BRACKET_CJK_RE.finditer(parsed.raw):
        if m.group(1) not in queries:
            queries.append(m.group(1))
    return queries


async def _search_round(
    tmdb: TMDBHelper, parsed: MediaData, queries: list[str], *, language: str | None = None
) -> tuple[list[dict], str | None]:
    """一轮搜索：聚合全部查询词的候选 → 硬门槛过滤。

    返回 (匹配候选列表, 错误消息或 None)。搜索异常时 err 非空；
    候选为空/标题不匹配时返回 ([], None)，由调用方决定是否进入下一轮。
    """
    candidates: list[dict] = []
    seen: set[int] = set()
    try:
        for q in queries:
            res = await tmdb.search(q, parsed.year, parsed.media_type, language=language)
            for c in res or []:
                cid = int(c.get("id", 0) or 0)
                if cid and cid not in seen:
                    seen.add(cid)
                    candidates.append(c)
    except Exception as exc:  # noqa: BLE001 - 网络/API 错误统一降级
        return [], f"TMDB 搜索失败：{exc}"
    if not candidates:
        return [], "TMDB 无搜索结果"
    return _filter_candidates(candidates, parsed, queries), None


def _filter_candidates(
    candidates: list[dict], parsed: MediaData, queries: list[str]
) -> list[dict]:
    """硬门槛过滤：类型 + 年份 + 标题匹配。"""
    matched: list[dict] = []
    for c in candidates:
        if _kind(c) != parsed.media_type:
            continue
        cy = _cand_year(c)
        if parsed.year and cy:
            if parsed.media_type == "movie":
                if cy != parsed.year:
                    continue
            elif cy > parsed.year:
                # 剧集：首播不得晚于资源年份
                continue
        titles = [
            c.get("title") or c.get("name") or "",
            c.get("original_title") or c.get("original_name") or "",
        ]
        if any(_title_match(q, titles) for q in queries):
            matched.append(c)
    return matched


# ---------------------------------------------------------------------- #
# 模板渲染
# ---------------------------------------------------------------------- #
def _web_source(parsed: MediaData) -> str:
    """来源标签：REMUX 必来自原盘（WEB 无 remux 场景）。"""
    if "remux" in parsed.raw.lower():
        return "BluRay"
    return parsed.source


def _resource_type(parsed: MediaData) -> str:
    return "REMUX" if "remux" in parsed.raw.lower() else ""


def render_name(parsed: MediaData, probe: ProbeTags | None, details: dict | None) -> str:
    """渲染统一模板命名。变量缺失时连同分隔点折叠。"""
    ext = Path(parsed.raw).suffix if "." in parsed.raw else ".mkv"
    probe = probe or ProbeTags()
    title = (details or {}).get("title") or parsed.title
    year = (details or {}).get("year") or parsed.year

    quality = ".".join(
        p for p in (
            probe.resolution,
            _uhd_tag(parsed),
            extract_platform(parsed.raw),
            _web_source(parsed),
            _resource_type(parsed),
            probe.effect,
            probe.video_encode,
            probe.bit_depth,
            probe.frame_rate,
            probe.audio_encode,
        ) if p
    )
    team = parsed.release_group
    tail = f"-{team}" if team else ""

    if parsed.media_type == "movie":
        head = f"{title} ({year})" if year else title
        name = f"{head} - {quality}" if quality else head
    else:
        season = parsed.season if parsed.season is not None else 1
        ep = parsed.episode if parsed.episode is not None else 0
        se = f"S{season:02d}E{ep:02d}"
        parts = [title] + ([str(year)] if year else []) + [se, f"第{ep:02d}集"]
        head = ".".join(parts)
        name = f"{head}.{quality}" if quality else head
    return f"{name}{tail}{ext}"


# ---------------------------------------------------------------------- #
# 编排：解析 → 探测 → 匹配 → 渲染
# ---------------------------------------------------------------------- #
async def analyze_file(path: str, tmdb: TMDBHelper) -> NamingResult:
    """单文件命名分析（不落盘）。"""
    parsed = parse_for_naming(Path(path).name)
    probe = await probe_file(path)
    details, reasons = await match_tmdb(tmdb, parsed)

    result = NamingResult(parsed=parsed, probe=probe, details=details, reasons=reasons)
    if result.high_confidence:
        result.proposed = render_name(parsed, probe, details)
    else:
        # 低置信也渲染预览（用原解析信息），供人工核对差在哪
        result.preview = render_name(parsed, probe, None)
    return result
