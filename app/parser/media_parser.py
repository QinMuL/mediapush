"""文件名解析：guessit + 噪音清洗 → MediaData。

承接前序经验：
- 噪音词（声道/帧率/色深/HQ/HD/FINE）前置清洗避免干扰 TMDB 标题匹配
- 音频标签支持 AAC2.0/DTS5.1
- extract_season_episode 第三返回值是"集跨度"，episode_end = ep + ep_span - 1
- total_episodes 优先用 TMDB，回退文件名聚合
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.providers.base import ShareFile


@dataclass
class MediaData:
    title: str = ""
    year: int | None = None
    media_type: str = "movie"  # "movie" | "tv"
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    quality: str = ""
    source: str = ""
    hdr: str = ""
    quality_info: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class AggregatedMedia:
    title: str = ""
    year: int | None = None
    media_type: str = "movie"
    season: int | None = None
    seasons: list[int] = field(default_factory=list)  # 所有季号（去重排序）
    season_episodes: dict[int, list[int]] = field(default_factory=dict)  # 按文件名季分组的集号
    episode_start: int | None = None
    episode_end: int | None = None
    quality: str = ""
    source: str = ""
    hdr: str = ""
    quality_info: list[str] = field(default_factory=list)
    file_count: int = 0
    total_episodes: int | None = None
    tmdb_id: int | None = None  # 文件名/目录名标注的 TMDB ID（{tmdb-XXX}）


# TMDB ID 标注：{tmdb-1311031}（媒体管理工具/分享者标注，最可靠的匹配来源）
_TMDB_ID_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def extract_tmdb_id(names: list[str]) -> int | None:
    """从名称列表中提取第一个 {tmdb-XXX} 标注。无则 None。"""
    for name in names:
        m = _TMDB_ID_RE.search(name)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------- #
# 噪音清洗
# ---------------------------------------------------------------------- #
_SEP_RE = re.compile(r"[._\-\(\)\[\]]")
_FPS_RE = re.compile(r"\b\d{1,2}(?:[.,]\d+)?\s*fps\b", re.IGNORECASE)
_BITDEPTH_RE = re.compile(r"\b\d{1,2}\s*-?\s*bit\b", re.IGNORECASE)
_NOISE_TAG_RE = re.compile(r"\b(?:HQ|HD|FINE|高清)\b", re.IGNORECASE)
_AUDIO_TAG_RE = re.compile(
    r"\b(?:AAC|EAC3|AC3|DTS|DTS-?HD|TrueHD|Atmos|FLAC|DDP?|DD)\d*(?:[.,]\d+)?\b",
    re.IGNORECASE,
)
_CHANNEL_RE = re.compile(r"\b\d\.\d\b")


def clean_name(name: str) -> str:
    """去扩展名、分隔符归一为空格、清洗噪音词。"""
    base = name.rsplit(".", 1)[0] if "." in name else name
    s = _SEP_RE.sub(" ", base)
    s = _FPS_RE.sub(" ", s)
    s = _BITDEPTH_RE.sub(" ", s)
    s = _AUDIO_TAG_RE.sub(" ", s)
    s = _CHANNEL_RE.sub(" ", s)
    s = _NOISE_TAG_RE.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


# ---------------------------------------------------------------------- #
# 季 / 集
# ---------------------------------------------------------------------- #
# 范围：S01E01-E12 / S01E01~E12 / S01E01E12（要求显式分隔符避免单集误匹配）
# 集号支持 4 位（航海王等长剧集数 >1000）
_EP_RANGE_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})\s*[-~E]+\s*(\d{1,4})", re.IGNORECASE)
# 单集：S01E01（不依赖 \b，避免 S0 紧邻 1 时边界失效）
_SE_EP_RE = re.compile(r"S(\d{1,2})\s*E(\d{1,4})", re.IGNORECASE)
_SEASON_ONLY_RE = re.compile(r"S(?:eason\s*)?(\d{1,2})\b", re.IGNORECASE)


def extract_season_episode(text: str) -> tuple[int | None, int | None, int]:
    """返回 (season, ep_start, ep_span)。无则 None/None/0。

    第三返回值是"集跨度"（多少集）；episode_end = ep_start + ep_span - 1。
    """
    rng = _EP_RANGE_RE.search(text)
    if rng:
        s, a, b = int(rng.group(1)), int(rng.group(2)), int(rng.group(3))
        return s, a, max(1, b - a + 1)

    m = _SE_EP_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), 1

    sm = _SEASON_ONLY_RE.search(text)
    if sm:
        return int(sm.group(1)), None, 0

    return None, None, 0


# ---------------------------------------------------------------------- #
# 画质 / 来源 / HDR
# ---------------------------------------------------------------------- #
def get_quality(text: str) -> str:
    t = text.lower()
    if "2160p" in t or "4k" in t or "uhd" in t:
        return "4K / 2160P"
    if "1080p" in t:
        return "1080P"
    if "720p" in t:
        return "720P"
    if "480p" in t:
        return "480P"
    return ""


def get_source(text: str) -> str:
    t = text.lower()
    rules = [
        (r"\bremux\b", "REMUX"),
        (r"\bblu-?ray\b|\bbd\b", "BluRay"),
        (r"\bweb-?dl\b|\bwebrip\b|\bweb\b", "WEB-DL"),
        (r"\bhdtv\b", "HDTV"),
        (r"\bdvdrip\b|\bdvd\b", "DVD"),
    ]
    for pat, label in rules:
        if re.search(pat, t):
            return label
    return ""


def get_hdr(text: str) -> str:
    t = text.lower()
    tags: list[str] = []
    if "dolby vision" in t or "dovi" in t or "dv " in t or ".dv" in t:
        tags.append("Dolby Vision")
    if "hdr10+" in t:
        tags.append("HDR10+")
    elif "hdr10" in t:
        tags.append("HDR10")
    elif "hdr" in t:
        tags.append("HDR")
    if "sdr" in t and not tags:
        tags.append("SDR")
    return " / ".join(tags)


# ---------------------------------------------------------------------- #
# 画质详细信息（分辨率/平台/来源/DV版本/编码/色深/帧率/音频）
# ---------------------------------------------------------------------- #
_PLATFORM_MAP = {
    "NF": "Netflix", "NETFLIX": "Netflix",
    "MAX": "Max", "HBOMAX": "Max", "HBO": "HBO",
    "DISNEY": "Disney+", "DISNEY+": "Disney+",
    "HULU": "Hulu",
    "AMZN": "Amazon", "AMAZON": "Amazon",
    "ATVP": "Apple TV+", "APPLETV": "Apple TV+", "APPLETV+": "Apple TV+",
    "PCOK": "Peacock", "PEACOCK": "Peacock",
    "PMTP": "Paramount+", "PARAMOUNT": "Paramount+", "PARAMOUNT+": "Paramount+",
    "CR": "Crunchyroll", "CRUNCHYROLL": "Crunchyroll",
}
_PLATFORM_RE = re.compile(
    r"\b(NF|Netflix|Max|HBOMax|HBO|Disney\+?|Hulu|AMZN|Amazon|ATVP|AppleTV\+?"
    r"|PCOK|Peacock|PMTP|Paramount\+?|CR|Crunchyroll)\b",
    re.IGNORECASE,
)
_DOVI_RE = re.compile(r"\b(?:DoVi|Dolby\s*Vision|DV)\s*P(\d+)\b", re.IGNORECASE)
_CODEC_MAP = {
    "H265": "H.265", "H.265": "H.265", "HEVC": "H.265", "X265": "H.265",
    "H264": "H.264", "H.264": "H.264", "AVC": "H.264", "X264": "H.264",
    "H266": "H.266", "H.266": "H.266", "VVC": "H.266",
}
_CODEC_RE = re.compile(r"\b(H\.?26[456]|HEVC|AVC|VVC|X26[456])\b", re.IGNORECASE)
_BITDEPTH_RE = re.compile(r"\b(\d+)[\-\s]?bit\b", re.IGNORECASE)
_FPS_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*fps\b", re.IGNORECASE)
_AUDIO_MAP = {
    "TRUEHD": "TrueHD", "DTSHD": "DTS-HD", "DTS": "DTS",
    "DDP": "DDP", "DD+": "DDP", "EAC3": "DDP", "EAC-3": "DDP",
    "AC3": "AC3", "AC-3": "AC3", "AAC": "AAC", "FLAC": "FLAC",
    "ATMOS": "Atmos", "DD": "DD",
}
_AUDIO_RE = re.compile(
    r"\b(TrueHD|DTS-HD|DTS|DDP|DD\+|E-?AC-?3|AC-?3|AAC|FLAC|Atmos|DD)\b"
    r"[\s.]*(\d(?:\.\d)?)?",
    re.IGNORECASE,
)


def get_quality_info(text: str) -> list[str]:
    """从文件名提取画质详细信息列表（用 | 分隔展示）。

    提取：分辨率 | 平台 | 来源 | DV版本 | 编码 | 色深 | 帧率 | 音频
    """
    parts: list[str] = []
    tl = text.lower()

    # 1. 分辨率
    if "2160p" in tl or "4k" in tl or "uhd" in tl:
        parts.append("4K")
    elif "1080p" in tl:
        parts.append("1080P")
    elif "720p" in tl:
        parts.append("720P")
    elif "480p" in tl:
        parts.append("480P")

    # 2. 来源平台
    m = _PLATFORM_RE.search(text)
    if m:
        key = m.group(1).upper().replace("+", "+")
        platform = _PLATFORM_MAP.get(key, m.group(1))
        if platform not in parts:
            parts.append(platform)

    # 3. 发布类型
    if re.search(r"\bremux\b", tl):
        if "BluRay" not in parts:
            parts.append("BluRay")
        parts.append("REMUX")
    elif re.search(r"\bblu-?ray\b|\bbd\b", tl):
        parts.append("BluRay")
    elif re.search(r"\bweb-?dl\b|\bwebrip\b", tl):
        parts.append("WEB-DL")
    elif re.search(r"\bhdtv\b", tl):
        parts.append("HDTV")

    # 4. HDR / 杜比视界（P5/P7/P8 分别显示）
    dovi = _DOVI_RE.search(text)
    if dovi:
        parts.append(f"DoVi P{dovi.group(1)}")
    elif "hdr10+" in tl:
        parts.append("HDR10+")
    elif "hdr10" in tl:
        parts.append("HDR10")
    elif "hdr" in tl:
        parts.append("HDR")
    elif "sdr" in tl:
        parts.append("SDR")

    # 5. 编码
    m = _CODEC_RE.search(text)
    if m:
        key = m.group(1).upper().replace(".", "")
        codec = _CODEC_MAP.get(key, m.group(1))
        parts.append(codec)

    # 6. 色深
    m = _BITDEPTH_RE.search(text)
    if m:
        parts.append(f"{m.group(1)}-bit")

    # 7. 帧率
    m = _FPS_RE.search(text)
    if m:
        parts.append(f"{m.group(1)}fps")

    # 8. 音频
    m = _AUDIO_RE.search(text)
    if m:
        key = m.group(1).upper().replace("-", "").replace(".", "").replace("+", "")
        audio_name = _AUDIO_MAP.get(key, m.group(1))
        channels = m.group(2)
        parts.append(f"{audio_name} {channels}" if channels else audio_name)

    return parts


# ---------------------------------------------------------------------- #
# 单文件解析
# ---------------------------------------------------------------------- #
def parse_filename(name: str) -> MediaData:
    cleaned = clean_name(name)
    try:
        from guessit import guessit

        g = guessit(cleaned, {"expected_title": [], "type": "auto"})
    except Exception:  # noqa: BLE001 - guessit 可选/失败时退化
        g = {}

    def _first(v):
        if isinstance(v, list):
            return v[0] if v else None
        return v

    title = _first(g.get("title")) or cleaned
    if isinstance(title, (list, tuple)):
        title = title[0] if title else cleaned
    title = str(title).strip()

    year = _first(g.get("year"))
    year = int(year) if year else None

    # 季集检测在原始文件名上做（cleaned 已把 '-' 替换为空格，
    # 会破坏 S01E01-E12 范围）
    season, ep, ep_span = extract_season_episode(name)
    g_season = _first(g.get("season"))
    g_ep = _first(g.get("episode"))
    if season is None and g_season:
        season = int(g_season)
    if ep is None and g_ep:
        ep = int(g_ep)
        ep_span = 1

    media_type = "tv" if (season is not None or ep is not None) else "movie"

    return MediaData(
        title=title,
        year=year,
        media_type=media_type,
        season=season,
        episode=ep,
        episode_end=(ep + ep_span - 1) if ep is not None else None,
        quality=get_quality(name),
        source=get_source(name),
        hdr=get_hdr(name),
        quality_info=get_quality_info(name),
        raw=name,
    )


# ---------------------------------------------------------------------- #
# 分享聚合
# ---------------------------------------------------------------------- #
_QUALITY_RANK = {"4K / 2160P": 4, "1080P": 3, "720P": 2, "480P": 1, "": 0}


def analyze_share(files: list[ShareFile]) -> AggregatedMedia | None:
    """从扁平文件列表聚合出代表性媒体信息。无可用文件返回 None。"""
    videos = [f for f in files if f.is_video]
    dirs = [f for f in files if f.is_dir]
    candidates = videos if videos else dirs[:1]
    if not candidates:
        return None

    parsed = [parse_filename(f.name) for f in candidates]

    title = Counter(p.title for p in parsed if p.title).most_common(1)
    title = title[0][0] if title else parsed[0].title

    years = [p.year for p in parsed if p.year]
    year = years[0] if years else None

    has_tv = any(p.media_type == "tv" for p in parsed)
    media_type = "tv" if (has_tv or len(videos) > 1) else "movie"

    # 季号聚合：从视频文件名 + 目录名（如 "Season 4"）解析所有季
    all_seasons: list[int] = []
    for p in parsed:
        if p.season is not None:
            all_seasons.append(p.season)
    for d in dirs:
        ds = parse_filename(d.name).season
        if ds is not None:
            all_seasons.append(ds)
    seasons = sorted(set(all_seasons))
    season = seasons[0] if seasons else None

    ep_starts = [p.episode for p in parsed if p.episode is not None]
    ep_ends = [p.episode_end for p in parsed if p.episode_end is not None]
    ep_start = min(ep_starts) if ep_starts else None
    ep_end = max(ep_ends) if ep_ends else None

    # 按文件名 season 分组集号（用于按季渲染集数范围）
    season_episodes: dict[int, list[int]] = {}
    for p in parsed:
        if p.season is not None and p.episode is not None:
            season_episodes.setdefault(p.season, []).append(p.episode)

    best = max(parsed, key=lambda p: _QUALITY_RANK.get(p.quality, 0))
    quality, source, hdr = best.quality, best.source, best.hdr
    quality_info = best.quality_info

    total_eps = len({p.episode for p in parsed if p.episode is not None}) or None

    # TMDB ID 标注：优先从目录名提取（分享者/媒体工具标注，最可靠），再从文件名
    tmdb_id = extract_tmdb_id([d.name for d in dirs]) or extract_tmdb_id(
        [f.name for f in files]
    )

    return AggregatedMedia(
        title=title,
        year=year,
        media_type=media_type,
        season=season,
        seasons=seasons,
        season_episodes=season_episodes,
        episode_start=ep_start,
        episode_end=ep_end,
        quality=quality,
        source=source,
        hdr=hdr,
        quality_info=quality_info,
        file_count=len(videos),
        total_episodes=total_eps,
        tmdb_id=tmdb_id,
    )
