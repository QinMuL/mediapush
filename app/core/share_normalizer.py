"""分享前目录结构标准化。

在 ShareWatcher._share_and_push() 之前调用，把待分享资源整理成
Emby/Jellyfin 标准目录结构：

    片名 (年份) {tmdb-ID}/
    ├── 01/          ← 季目录（零填充两位）
    │   └── S01E01.mkv
    └── 02/

四种场景：
  A. 已有季子目录 → 重命名根目录 + 季子目录
  B. 剧集散集文件 → 重命名根目录 + 按季号建子目录 + 移入文件
  C. 电影文件夹   → 重命名根目录
  D. 监控目录散文件 → 建资源根目录 + 季目录 + 移入文件

幂等：已是标准结构的目录不重复处理。
失败容错：标准化失败不阻断分享创建，降级为用原结构建分享。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.parser.media_parser import parse_filename

logger = logging.getLogger(__name__)

# 季目录名匹配（Season 1 / S01 / 第1季 / 第一季 → 01）
_SEASON_DIR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^Season\s*(\d{1,2})$", re.IGNORECASE), "en"),
    (re.compile(r"^S(\d{1,2})$", re.IGNORECASE), "en"),
    (re.compile(r"^第\s*(\d{1,2})\s*季$"), "zh"),
    (re.compile(r"^第([一二三四五六七八九十]+)季$"), "zh_cn"),
]

# 中文数字映射
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str) -> int | None:
    """中文数字转 int（支持一到十、十一…十九、二十…九十九）。"""
    if not s:
        return None
    if len(s) == 1:
        return _CN_NUM.get(s)
    if s.startswith("十"):
        rest = s[1:]
        return 10 + (_CN_NUM.get(rest, 0) if rest else 0)
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 0)
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


def parse_season_dir(name: str) -> int | None:
    """从目录名解析季号。返回 int 季号或 None。

    >>> parse_season_dir("Season 1")
    1
    >>> parse_season_dir("S02")
    2
    >>> parse_season_dir("第3季")
    3
    >>> parse_season_dir("第一季")
    1
    >>> parse_season_dir("01")
    1
    >>> parse_season_dir("Extras")
    None
    """
    # 已是零填充格式（01, 02...）
    m = re.match(r"^0*(\d{1,2})$", name)
    if m and 1 <= int(m.group(1)) <= 99:
        return int(m.group(1))

    for pattern, kind in _SEASON_DIR_PATTERNS:
        m = pattern.match(name)
        if m:
            raw = m.group(1)
            if kind == "zh_cn":
                return _cn_to_int(raw)
            return int(raw)
    return None


def format_season_dir(season: int) -> str:
    """季号 → 零填充两位目录名。"""
    return f"{season:02d}"


def build_resource_name(title: str, year: int | None, tmdb_id: int | None,
                        media_type: str) -> str:
    """构建标准资源目录名：片名 (年份) {tmdb-ID}。

    >>> build_resource_name("葬送的芙莉莲", 2026, 246389, "tv")
    '葬送的芙莉莲 (2026) {tmdb-246389}'
    >>> build_resource_name("沙丘", None, 693134, "movie")
    '沙丘 {tmdb-693134}'
    """
    parts: list[str] = [title.strip()]
    if year:
        parts.append(f"({year})")
    if tmdb_id:
        parts.append(f"{{tmdb-{tmdb_id}}}")
    return " ".join(parts)


def _has_tmdb_tag(name: str) -> bool:
    """检查目录名是否已含 {tmdb-xxx} 标注（幂等判断用）。"""
    return bool(re.search(r"\{tmdb-\d+\}", name, re.IGNORECASE))


@dataclass
class NormalizeResult:
    """标准化结果。"""
    fid: int               # 标准化后的 fid（可能变了：建了新目录）
    name: str              # 标准化后的名称（用于日志）
    changed: bool          # 是否有实际变更
    actions: list[str]     # 变更明细（日志/通知用）


class ShareNormalizer:
    """分享前目录结构标准化器。

    依赖 pan115 provider（list_dir/fs_rename/fs_move/fs_makedirs）和
    tmdb client（search_best/get_details）。
    """

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.dry_run = bool(getattr(settings, "share_normalize_dry_run", True))

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "share_normalize_enabled", False))

    async def normalize(self, pan115, fid: int, name: str, is_dir: bool,
                        parent_cid: int) -> NormalizeResult:
        """标准化单个资源。

        Args:
            pan115: Pan115 provider 实例
            fid: 资源 fid（目录或文件）
            name: 资源名
            is_dir: 是否目录
            parent_cid: 父目录 cid（散文件建目录时需要）

        Returns:
            NormalizeResult，含标准化后的 fid 和变更明细
        """
        if not self.enabled:
            return NormalizeResult(fid=fid, name=name, changed=False, actions=[])

        try:
            if is_dir:
                return await self._normalize_folder(pan115, fid, name)
            # 散文件：建资源目录包裹
            return await self._wrap_single_file(pan115, fid, name, parent_cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("目录标准化失败（%s），降级用原结构：%s", name, exc)
            return NormalizeResult(fid=fid, name=name, changed=False, actions=[])

    # ------------------------------------------------------------------ #
    # 场景 A/B/C：已有目录
    # ------------------------------------------------------------------ #
    async def _normalize_folder(self, pan115, fid: int,
                                name: str) -> NormalizeResult:
        """标准化资源目录。"""
        actions: list[str] = []
        items = await pan115.list_dir(fid, nf=0)

        # 探测内容类型
        subdirs = [it for it in items if it["is_dir"]]
        files = [it for it in items if not it["is_dir"]]

        # 尝试从目录名/文件名解析媒体信息
        media = self._detect_media(name, items)
        if media is None:
            logger.debug("目录标准化跳过（无法识别媒体信息）：%s", name)
            return NormalizeResult(fid=fid, name=name, changed=False, actions=[])

        title, year, media_type, tmdb_id = media

        # TMDB 匹配（如果目录名没有 {tmdb-xxx} 标注）
        if tmdb_id is None:
            tmdb_id = await self._tmdb_match(title, year, media_type)
            if tmdb_id is not None:
                # 取 TMDB 详情修正标题/年份
                details = await self._tmdb_details(tmdb_id, media_type)
                if details:
                    title = details.get("title") or title
                    year = details.get("year") or year

        # 重命名根目录
        new_name = build_resource_name(title, year, tmdb_id, media_type)
        root_changed = False
        if new_name != name:
            if not self.dry_run:
                await pan115.fs_rename(fid, new_name)
            root_changed = True
            tag = "[DRY-RUN] " if self.dry_run else ""
            actions.append(f"{tag}重命名目录：{name} → {new_name}")
        elif _has_tmdb_tag(name):
            # 已是标准名，检查季目录
            pass

        # 处理季子目录
        season_changed = await self._handle_seasons(
            pan115, fid, subdirs, files, actions
        )

        return NormalizeResult(
            fid=fid,
            name=new_name if root_changed else name,
            changed=root_changed or season_changed,
            actions=actions,
        )

    # ------------------------------------------------------------------ #
    # 季目录处理
    # ------------------------------------------------------------------ #
    async def _handle_seasons(
        self, pan115, parent_fid: int,
        subdirs: list[dict], files: list[dict],
        actions: list[str],
    ) -> bool:
        """处理季目录：A）重命名已有季子目录；B）为散集文件建季目录。

        Returns: 是否有变更。
        """
        changed = False

        # A. 已有季子目录 → 重命名
        season_dirs: list[tuple[dict, int]] = []  # (item, season_num)
        non_season_dirs: list[dict] = []
        for d in subdirs:
            sn = parse_season_dir(d["name"])
            if sn is not None:
                season_dirs.append((d, sn))
            else:
                non_season_dirs.append(d)

        for item, sn in season_dirs:
            target = format_season_dir(sn)
            if item["name"] == target:
                continue  # 已是标准名
            if not self.dry_run:
                await pan115.fs_rename(item["fid"], target)
            changed = True
            tag = "[DRY-RUN] " if self.dry_run else ""
            actions.append(
                f"{tag}季目录重命名：{item['name']} → {target}"
            )

        # B. 散集文件（无季子目录但有 SxxExx 文件）→ 按季号建子目录
        if not season_dirs and files:
            season_files: dict[int, list[dict]] = {}  # season → files
            for f in files:
                parsed = parse_filename(f["name"])
                sn = parsed.season
                if sn is not None:
                    season_files.setdefault(sn, []).append(f)

            for sn, sfiles in sorted(season_files.items()):
                target = format_season_dir(sn)
                if self.dry_run:
                    actions.append(
                        f"[DRY-RUN] 建季目录 {target}/，移入 {len(sfiles)} 个文件"
                    )
                    changed = True
                    continue
                # 建季目录
                # fs_makedirs 需要完整路径，但这里只能用相对名
                # 改用 list_dir 检查是否已存在，不存在则在 parent 下建
                # 115 的 fs_makedirs 接受路径，我们在 parent 下用路径拼接
                # 但我们没有 parent 的路径——改用 fid 方式：
                # 115 API fs_mkdirs 需要 pid + names
                # p115client 有 fs_mkdirs(pid, names) 方法
                season_cid = await self._makedirs_under(pan115, parent_fid, target)
                for f in sfiles:
                    await pan115.fs_move(f["fid"], season_cid)
                    await asyncio.sleep(0.5)  # 115 移动异步，给间隔
                actions.append(
                    f"建季目录 {target}/，移入 {len(sfiles)} 个文件"
                )
                changed = True

        return changed

    # ------------------------------------------------------------------ #
    # 场景 D：监控目录下的散文件
    # ------------------------------------------------------------------ #
    async def _wrap_single_file(
        self, pan115, fid: int, name: str, parent_cid: int,
    ) -> NormalizeResult:
        """散文件 → 建资源根目录 + 季目录 → 移入。"""
        actions: list[str] = []

        parsed = parse_filename(name)
        title = parsed.title
        year = parsed.year
        media_type = parsed.media_type

        tmdb_id = await self._tmdb_match(title, year, media_type)
        if tmdb_id is not None:
            details = await self._tmdb_details(tmdb_id, media_type)
            if details:
                title = details.get("title") or title
                year = details.get("year") or year

        root_name = build_resource_name(title, year, tmdb_id, media_type)

        if self.dry_run:
            tag = "[DRY-RUN] "
            actions.append(f"{tag}建资源目录：{root_name}/")
            if parsed.season is not None:
                target = format_season_dir(parsed.season)
                actions.append(f"{tag}建季目录：{root_name}/{target}/")
                actions.append(f"{tag}移入文件：{name}")
            else:
                actions.append(f"{tag}移入文件：{name}")
            return NormalizeResult(
                fid=fid,  # dry-run 不建目录，返回原 fid
                name=root_name,
                changed=True,
                actions=actions,
            )

        # 实际执行：建资源根目录
        root_cid = await self._makedirs_under(pan115, parent_cid, root_name)

        # 剧集：建季目录
        if parsed.season is not None:
            season_name = format_season_dir(parsed.season)
            season_cid = await self._makedirs_under(pan115, root_cid, season_name)
            await pan115.fs_move(fid, season_cid)
            actions.append(
                f"建资源目录 {root_name}/{season_name}/，移入 {name}"
            )
        else:
            # 电影：直接移入根目录
            await pan115.fs_move(fid, root_cid)
            actions.append(f"建资源目录 {root_name}/，移入 {name}")

        # 返回新建的资源根目录 fid（后续建分享用这个 fid）
        return NormalizeResult(
            fid=root_cid,
            name=root_name,
            changed=True,
            actions=actions,
        )

    # ------------------------------------------------------------------ #
    # 媒体信息探测
    # ------------------------------------------------------------------ #
    def _detect_media(
        self, dir_name: str, items: list[dict],
    ) -> tuple[str, int | None, str, int | None] | None:
        """从目录名和子项探测媒体信息。

        Returns: (title, year, media_type, tmdb_id) 或 None
        """
        # 先从目录名提取 {tmdb-xxx}
        from app.parser.media_parser import extract_tmdb_id
        tmdb_id = extract_tmdb_id([dir_name]) or extract_tmdb_id(
            [it["name"] for it in items]
        )

        # 解析目录名
        parsed = parse_filename(dir_name)
        title = parsed.title
        year = parsed.year
        media_type = parsed.media_type

        # 如果目录名无季信息，从子项补充
        if media_type == "movie":
            # 检查子目录/文件是否有季信息
            for it in items:
                p = parse_filename(it["name"])
                if p.season is not None:
                    media_type = "tv"
                    break
            # 多文件也可能是剧集
            video_count = sum(
                1 for it in items
                if not it["is_dir"] and any(
                    it["name"].lower().endswith(ext)
                    for ext in (".mkv", ".mp4", ".avi", ".ts", ".mov")
                )
            )
            if video_count > 1:
                media_type = "tv"

        if not title or title == dir_name:
            # 目录名解析不出标题，尝试从子项
            for it in items:
                p = parse_filename(it["name"])
                if p.title and p.title != it["name"]:
                    title = p.title
                    if p.year:
                        year = p.year
                    if p.media_type == "tv":
                        media_type = "tv"
                    break

        if not title:
            return None

        return (title, year, media_type, tmdb_id)

    # ------------------------------------------------------------------ #
    # TMDB 匹配
    # ------------------------------------------------------------------ #
    async def _tmdb_match(
        self, title: str, year: int | None, media_type: str,
    ) -> int | None:
        """调 TMDB search_best 返回 tmdb_id 或 None。"""
        tmdb = getattr(self.container, "tmdb", None)
        if tmdb is None or not getattr(self.settings, "tmdb_api_key", ""):
            return None
        try:
            result = await tmdb.search_best(title, year, media_type)
            if result is not None:
                return int(result[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("TMDB 匹配失败（%s）：%s", title, exc)
        return None

    async def _tmdb_details(
        self, tmdb_id: int, media_type: str,
    ) -> dict | None:
        """取 TMDB 详情（标题/年份修正用）。"""
        tmdb = getattr(self.container, "tmdb", None)
        if tmdb is None:
            return None
        try:
            return await tmdb.get_details(tmdb_id, media_type)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    # 115 目录创建辅助
    # ------------------------------------------------------------------ #
    async def _makedirs_under(
        self, pan115, parent_cid: int, name: str,
    ) -> int:
        """在 parent_cid 下创建子目录，返回 cid。

        幂等：已存在则返回现有 cid。
        """
        # 先检查是否已存在
        items = await pan115.list_dir(parent_cid, nf=1)
        for it in items:
            if it["name"].lower() == name.lower():
                return it["fid"]
        # 创建
        from p115client.client import check_response
        client = pan115._login_client()
        resp = await pan115._call_with_margin(
            lambda: client.fs_mkdirs(
                {"pid": parent_cid, "names": [name], "ignore_warn": 1},
                async_=True,
            ),
            label="fs_mkdirs",
        )
        try:
            check_response(resp)
        except Exception as exc:
            from app.providers.exceptions import Pan115Error
            raise Pan115Error(f"建目录失败（{name}）：{exc}") from exc
        # 解析返回的 cid
        data = resp.get("data") or resp
        cid = data.get("file_id") or data.get("cid") or data.get("id")
        if cid is None and isinstance(data, list) and data:
            cid = data[0].get("file_id") or data[0].get("cid")
        if cid is None:
            # 重新列目录获取
            items = await pan115.list_dir(parent_cid, nf=1)
            for it in items:
                if it["name"].lower() == name.lower():
                    return it["fid"]
            raise RuntimeError(f"建目录后无法获取 cid：{name}")
        return int(cid)
