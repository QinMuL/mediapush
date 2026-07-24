"""文件名解析。

遵守旧项目踩坑约束（见 ARCHITECTURE.md 第 8 节"数据/解析"）：
- 噪音词（声道/帧率/色深/HDR）前置清理，避免干扰标题与季集提取
- 音频标签支持 AAC2.0/DTS5.1 等：模式 \\bAAC\\d*(?:\\.\\d+)?\\b
- 清理非正式画质标记 HQ/HD/FINE，避免干扰 TMDB 标题匹配
- ed2k 链接非贪婪匹配到 |/
- extract_season_episode 第三返回值是"集跨度"（多少集），
  episode_end 语义为"结束集号"，需 ep_end = ep + ep_span - 1
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MediaData:
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None  # 起始集
    episode_span: int = 1  # 集跨度（多少集）
    episode_end: int | None = None  # 结束集号 = episode + episode_span - 1
    quality: str = ""
    audio: str = ""
    total_episodes: int | None = None  # 文件名推断的总集数（如 E01-12）
    is_whole_season: bool = False  # 整季文件夹（如 S01，无具体集号）


# ---- 噪音词（前置清理）----
# 声道、帧率、色深、HDR/SDR：会干扰标题与季集提取
_NOISE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b\d+(?:\.\d+)?\s*(?:ch|channels?)\b",  # 2CH / 2.0ch
        r"\b\d{1,2}(?:\.\d+)?\s*(?:fps|frame)",  # 23.976fps
        r"\b(?:23\.?976|29\.97|24|25|30|48|50|60|120)\s*fps\b",
        r"\b\d+\s*bit\b",  # 10bit
        r"\b(?:hdr10\+?|hlg|dolby\s*vision|dovi|dv)\b",
        r"\b(?:sdr|hdr)\b",
        r"\b(?:x264|x265|h264|h265|hevc|avc|xvid|divx)\b",  # 编码
        r"\b(?:web-dl|webrip|webdl|bluray|blu-ray|bdrip|brrip|remux|hdtv)\b",  # 来源
    ]
]

# ---- 画质 ----
# 正式画质标记（保留）
_QUALITY_PATTERNS = [
    re.compile(r"\b8k\b", re.IGNORECASE),
    re.compile(r"\b2160p\b", re.IGNORECASE),
    re.compile(r"\b1080[pi]\b", re.IGNORECASE),
    re.compile(r"\b720p\b", re.IGNORECASE),
    re.compile(r"\b480p\b", re.IGNORECASE),
    re.compile(r"\b4k\b", re.IGNORECASE),
]
# 非正式画质标记（清理，避免干扰 TMDB 标题匹配）
_INFORMAL_QUALITY = re.compile(r"\b(?:HQ|HD|FINE|UHD)\b")


def clean_noise(name: str) -> str:
    """前置清理噪音词（声道/帧率/色深/编码/来源）。"""
    for pat in _NOISE_PATTERNS:
        name = pat.sub(" ", name)
    return name


def extract_quality(name: str) -> str:
    """提取正式画质（8K/2160p/1080p/720p/480p/4K）；非正式 HQ/HD/FINE 不计。"""
    for pat in _QUALITY_PATTERNS:
        m = pat.search(name)
        if m:
            q = m.group(0)
            # 规范化 1080i -> 1080p 表述统一为原样保留
            return q.upper() if q.lower() in ("4k", "8k") else q.lower()
    return ""


def clean_informal_quality(name: str) -> str:
    """清理非正式画质标记 HQ/HD/FINE，避免干扰 TMDB 标题匹配。"""
    return _INFORMAL_QUALITY.sub(" ", name)


# ---- 音频 ----
# 支持 AAC2.0/DTS5.1/AC3/EAC3/TrueHD/Atmos/FLAC/DDP/DD5.1 等
_AUDIO_PATTERNS = [
    re.compile(r"\bAAC\d*(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bDTS-?HD(?:\s*MA)?\b", re.IGNORECASE),
    re.compile(r"\bDTS\d?(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bE?AC-?3\b", re.IGNORECASE),
    re.compile(r"\bTrueHD\s*Atmos\b", re.IGNORECASE),
    re.compile(r"\bTrueHD\b", re.IGNORECASE),
    re.compile(r"\bAtmos\b", re.IGNORECASE),
    re.compile(r"\bDDP\d?(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bDD\d?(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bFLAC\b", re.IGNORECASE),
]


def extract_audio(name: str) -> str:
    """提取音频标签（取第一个匹配，支持 AAC2.0/DTS5.1 等）。"""
    for pat in _AUDIO_PATTERNS:
        m = pat.search(name)
        if m:
            return m.group(0)
    return ""


# ---- 年份 ----
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def extract_year(name: str) -> int | None:
    m = _YEAR_RE.search(name)
    return int(m.group(1)) if m else None


# ---- 季 / 集 ----
# 返回 (season, episode, episode_span)；episode_span 是"集跨度"（多少集）
# S01E02-05 → (1, 2, 4)
_SE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # S01E02-05 / S01E02E05 / S01E02
    (re.compile(r"S(\d{1,2})\s*E(\d{1,3})(?:\s*[-Ee]+\s*(\d{1,3}))?", re.IGNORECASE), "se"),
    # 第1季第02集 / 第1季第02-05集
    (
        re.compile(
            r"第\s*(\d{1,2})\s*[季部]\s*第\s*(\d{1,3})\s*(?:[-~]+\s*(\d{1,3}))?\s*集",
        ),
        "cn_se",
    ),
    # 第02集 / 第02-05集
    (re.compile(r"第\s*(\d{1,3})\s*(?:[-~]+\s*(\d{1,3}))?\s*集"), "cn_ep"),
    # EP02-05 / E02-05 / EP02
    (re.compile(r"\bE(?:P)?(\d{1,3})(?:\s*[-~]+\s*(\d{1,3}))?", re.IGNORECASE), "ep"),
    # S01（整季，无集号）—— 必须放最后，且不能紧跟 E
    (re.compile(r"S(\d{1,2})(?!\s*[Ee]\d)", re.IGNORECASE), "season_only"),
    # 第1季（仅季）
    (re.compile(r"第\s*(\d{1,2})\s*[季部]"), "cn_season_only"),
]


def extract_season_episode(name: str) -> tuple[int | None, int | None, int | None]:
    """提取 (season, episode, episode_span)。

    episode_span 是"集跨度"（多少集），不是结束集号。
    整季文件夹（如 S01 无集号）返回 (season, None, None)。
    """
    season: int | None = None
    episode: int | None = None
    ep_span: int | None = None

    for pat, kind in _SE_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        groups = m.groups()
        if kind == "se":
            season = int(groups[0])
            episode = int(groups[1])
            ep_span = _span(groups[1], groups[2])
            break
        if kind == "cn_se":
            season = int(groups[0])
            episode = int(groups[1])
            ep_span = _span(groups[1], groups[2])
            break
        if kind == "cn_ep":
            episode = int(groups[0])
            ep_span = _span(groups[0], groups[1])
            break
        if kind == "ep":
            episode = int(groups[0])
            ep_span = _span(groups[0], groups[1])
            break
        if kind == "season_only":
            if season is None:
                season = int(groups[0])
            continue
        if kind == "cn_season_only":
            if season is None:
                season = int(groups[0])
            continue

    return season, episode, ep_span


def _span(start_str: str | None, end_str: str | None) -> int:
    """根据起止集号计算集跨度。无 end 则跨度 1。"""
    if not start_str:
        return 1
    if not end_str:
        return 1
    start, end = int(start_str), int(end_str)
    if end < start:
        return 1
    return end - start + 1


# ---- ed2k ----
# 非贪婪匹配到 |/，正确处理文件名中的空格
_ED2K_RE = re.compile(r"ed2k://.*?\|/", re.IGNORECASE)


def parse_ed2k(text: str) -> list[str]:
    """提取所有 ed2k 链接（非贪婪到 |/）。"""
    return _ED2K_RE.findall(text)


# ---- 标题清理 ----
# 季集/年份/画质/音频等标记从文件名移除后，剩余即标题
_SEASON_EP_CLEAN = re.compile(
    r"(?:S\d{1,2}\s*E\d{1,3}(?:\s*[-Ee]+\s*\d{1,3})?|"
    r"第\s*\d{1,2}\s*[季部]\s*第\s*\d{1,3}\s*(?:[-~]+\s*\d{1,3})?\s*集|"
    r"第\s*\d{1,3}\s*(?:[-~]+\s*\d{1,3})?\s*集|"
    r"E(?:P)?\d{1,3}(?:\s*[-~]+\s*\d{1,3})?|"
    r"S\d{1,2}|第\s*\d{1,2}\s*[季部])",
    re.IGNORECASE,
)


def extract_title(name: str) -> str:
    """从文件名提取标题：清理噪音、画质、音频、季集、年份、分隔符。"""
    name = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", name)  # 剥离文件扩展名
    name = clean_noise(name)
    name = clean_informal_quality(name)
    # 移除画质/音频标记
    for pat in _QUALITY_PATTERNS:
        name = pat.sub(" ", name)
    for pat in _AUDIO_PATTERNS:
        name = pat.sub(" ", name)
    # 移除季集
    name = _SEASON_EP_CLEAN.sub(" ", name)
    # 移除年份
    name = _YEAR_RE.sub(" ", name)
    # 规范化分隔符：把 . _ - 当空格
    name = re.sub(r"[._\-\u3010\u3011\u300a\u300b()\[\]【】《》]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_filename(filename: str) -> MediaData:
    """解析文件名，返回 MediaData。"""
    name = clean_noise(filename)
    name = clean_informal_quality(name)

    season, episode, ep_span = extract_season_episode(filename)
    year = extract_year(filename)
    quality = extract_quality(filename)
    audio = extract_audio(filename)
    title = extract_title(filename)

    is_whole_season = season is not None and episode is None

    # 集跨度语义：episode_end = episode + ep_span - 1
    episode_end: int | None = None
    total_episodes: int | None = None
    if episode is not None and ep_span is not None:
        episode_end = episode + ep_span - 1
        if ep_span > 1:
            total_episodes = ep_span  # E01-12 推断总集数 12（供 TMDB 回退）

    return MediaData(
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_span=ep_span if ep_span is not None else 1,
        episode_end=episode_end,
        quality=quality,
        audio=audio,
        total_episodes=total_episodes,
        is_whole_season=is_whole_season,
    )
