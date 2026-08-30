"""本地目录监控服务：目录A稳定性检测 → namer 重命名 → 移入目录B。

链路（与用户确认的设计）：
1. 每 LOCAL_MEDIA_INTERVAL_SECONDS 秒递归扫描目录A（含子目录）
2. 文件 (size, mtime) 连续 LOCAL_MEDIA_STABLE_ROUNDS 轮无变化 → 判定稳定
   （防网盘同步/下载器半截文件；排除 .tmp/.part/.!qb 等临时文件）
3. 稳定文件交 namer.analyze_file 分析：
   - 高置信：拟名 + 目标路径 → 移入目录B
     电影平铺：B/片名 (年份) - 质量.mkv
     剧集分夹：B/片名 (年份)/Sxx/片名.年份.SxxEyy.第zz集...mkv
   - 低置信：原地保留，指数退避后重试（1h→2h→…→24h 封顶）
4. 字幕伴行：同 stem 的 .srt/.ass/.ssa/.sub 跟随视频改名移动（Emby 配对）
5. 同名冲突：目标已存在则跳过+告警，绝不覆盖
6. A 子目录清空后删除空目录（保持源目录整洁）
7. 同一文件重试超 LOCAL_MEDIA_STUCK_DAYS 天仍低置信 → warning 提示人工介入

重试状态持久化 data/local_media_state.json（重启不丢退避计数）。
dry-run（LOCAL_MEDIA_DRY_RUN 默认 true）：只输出"将移动到"日志，不实际动文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.media.namer import NamingResult, analyze_file, sanitize_name

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv",
              ".webm", ".ts", ".m2ts", ".m4v", ".mpg", ".mpeg", ".rmvb"}
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".sub")
# 下载器/同步工具的临时文件：扩展名匹配或 !qb 尾缀
_TEMP_EXTS = {".tmp", ".part", ".crdownload", ".download", ".downloading"}
_TEMP_TAILS = (".!qb",)

# 重试退避：1h 起指数翻倍，24h 封顶
_RETRY_BASE_SECONDS = 3600
_RETRY_CAP_SECONDS = 24 * 3600


def retry_backoff_seconds(failures: int) -> int:
    """低置信重试退避：failures=1→1h, 2→2h, 3→4h … 封顶 24h。"""
    if failures <= 0:
        return 0
    return min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (1 << (failures - 1)))


def is_temp_file(path: Path) -> bool:
    """下载器/同步工具临时文件判定（不稳定文件，不参与分析）。"""
    name = path.name
    if path.suffix.lower() in _TEMP_EXTS:
        return True
    return any(name.endswith(t) for t in _TEMP_TAILS)


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def pick_subtitles(video: Path) -> list[Path]:
    """同 stem 的字幕文件（Emby 刮削要求字幕与视频同名）。"""
    return [s for ext in SUBTITLE_EXTS if (s := video.with_suffix(ext)).exists()]


def build_dest_dir(result: NamingResult, output_dir: Path) -> Path:
    """目标目录布局：电影平铺；剧集按"片名 (年份)/Sxx/"（均为净化后路径）。"""
    details = result.details or {}
    title = sanitize_name(details.get("title") or result.parsed.title or "未命名")
    year = details.get("year") or result.parsed.year
    if result.parsed.media_type != "tv":
        return output_dir
    season = result.parsed.season if result.parsed.season is not None else 1
    folder = f"{title} ({year})" if year else title
    return output_dir / sanitize_name(folder) / f"S{season:02d}"


@dataclass
class LocalMediaReport:
    """一轮本地目录扫描结果。"""

    scanned: int = 0        # 候选视频文件数
    stable: int = 0          # 本轮达到稳定并处理的文件数
    moved: int = 0           # 实际移动（含字幕伴行）
    dry_moved: int = 0       # dry-run 模拟移动
    low_conf: int = 0        # 低置信保留（退避重试）
    conflict: int = 0        # 目标同名跳过
    stuck: int = 0           # 卡死告警（超 STUCK_DAYS 仍低置信）

    def summary(self) -> str:
        s = f"扫描 {self.scanned} 个视频：稳定 {self.stable}"
        if self.moved:
            s += f" → ✅ 移动 {self.moved}"
        if self.dry_moved:
            s += f" → 🔍 [DRY-RUN] 模拟移动 {self.dry_moved}"
        if self.low_conf:
            s += f" · ⏳ 低置信保留 {self.low_conf}（退避重试）"
        if self.conflict:
            s += f" · ⚠️ 同名跳过 {self.conflict}"
        if self.stuck:
            s += f" · 🚨 卡死 {self.stuck}（人工检查）"
        return s


class LocalMediaService:
    """本地媒体目录监控：run_once 单轮；start/stop 后台循环。"""

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(1.0, settings.local_media_interval_seconds)
        self.input_dir = Path(settings.local_media_input_dir)
        self.output_dir = Path(settings.local_media_output_dir)
        self.state_file = Path("./data/local_media_state.json")
        # 稳定性追踪：(size, mtime) 快照 + 连续稳定轮数
        self._seen: dict[str, tuple[int, float]] = {}
        self._stable: dict[str, int] = {}
        # 低置信重试状态：path → {failures, next_retry, first_seen}
        self._retry_state: dict[str, dict] = {}
        # DRY-RUN 已模拟处理的文件（内存级，重启清空；防同一文件无限循环）
        self._dry_done: set[str] = set()
        self._load_state()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 状态持久化
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._retry_state = {
                k: v for k, v in data.items() if isinstance(v, dict)
            }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("本地媒体重试状态加载失败（按空启动）：%s", exc)

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self._retry_state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("本地媒体重试状态保存失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 单轮扫描
    # ------------------------------------------------------------------ #
    async def run_once(self) -> LocalMediaReport:
        report = LocalMediaReport()
        if not self.input_dir.is_dir():
            return report
        files = [
            f for f in self.input_dir.rglob("*")
            if f.is_file() and is_video_file(f) and not is_temp_file(f)
        ]
        report.scanned = len(files)

        # 稳定性判定：本轮 stat 与上轮一致 → 稳定计数 +1，否则重置
        alive = set()
        for f in files:
            key = str(f)
            alive.add(key)
            try:
                stat = f.stat()
            except OSError:
                continue
            snap = (stat.st_size, stat.st_mtime)
            if self._seen.get(key) == snap:
                self._stable[key] = self._stable.get(key, 0) + 1
            else:
                self._seen[key] = snap
                self._stable[key] = 1
        for key in list(self._seen):  # 清理已消失文件
            if key not in alive:
                self._seen.pop(key, None)
                self._stable.pop(key, None)

        now = time.time()
        moved_sources: list[Path] = []
        for f in files:
            key = str(f)
            if key in self._dry_done:
                continue
            if self._stable.get(key, 0) < self.settings.local_media_stable_rounds:
                continue
            if not self._retry_due(key, now):
                continue
            try:
                ok = await self._process(f, report, now)
            except Exception as exc:
                logger.error("本地媒体处理异常 %s：%s", f.name, exc, exc_info=exc)
                continue
            # 处理过（无论成败）本轮不再重复分析
            self._seen.pop(key, None)
            self._stable.pop(key, None)
            if ok:
                report.stable += 1
                moved_sources.append(f)

        self._save_state()
        for src in moved_sources:
            self._cleanup_empty_dirs(src.parent)
        return report

    def _retry_due(self, key: str, now: float) -> bool:
        """无状态=首轮即试；有状态=退避到期才试（防打爆 TMDB）。"""
        st = self._retry_state.get(key)
        return st is None or st.get("next_retry", 0) <= now

    # ------------------------------------------------------------------ #
    # 单文件处理：高置信移动 / 低置信退避
    # ------------------------------------------------------------------ #
    async def _process(self, f: Path, report: LocalMediaReport, now: float) -> bool:
        """返回是否完成流转（True=已移动或模拟移动）。"""
        tmdb = self.container.tmdb
        if tmdb is None:
            logger.warning("本地媒体跳过 %s：TMDB 不可用", f.name)
            return False
        result = await analyze_file(str(f), tmdb)
        if result.high_confidence and result.proposed:
            return self._move(f, result, report)
        return self._hold(f, result, report, now)

    def _move(self, f: Path, result: NamingResult, report: LocalMediaReport) -> bool:
        dest_dir = build_dest_dir(result, self.output_dir)
        dest = dest_dir / sanitize_name(result.proposed)
        if dest.exists():
            report.conflict += 1
            logger.warning("本地媒体跳过 %s：目标已存在 %s（不覆盖）", f.name, dest)
            return False
        subs = pick_subtitles(f)
        if self.settings.local_media_dry_run:
            report.dry_moved += 1
            logger.info("[DRY-RUN] %s → %s（字幕伴行 %d 个）", f.name, dest, len(subs))
            self._dry_done.add(str(f))
            return True
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
            for s in subs:
                shutil.move(str(s), str(dest.with_suffix(s.suffix)))
            report.moved += 1 + len(subs)
        except OSError as exc:
            logger.error("本地媒体移动失败 %s → %s：%s", f.name, dest, exc)
            return False
        self._retry_state.pop(str(f), None)  # 成功即清退避状态
        logger.info("本地媒体移动 %s → %s（字幕伴行 %d 个）", f.name, dest, len(subs))
        return True

    def _hold(self, f: Path, result: NamingResult,
              report: LocalMediaReport, now: float) -> bool:
        """低置信：原地保留，记录退避状态。"""
        key = str(f)
        st = self._retry_state.get(key, {"failures": 0, "first_seen": now})
        st["failures"] += 1
        st["next_retry"] = now + retry_backoff_seconds(st["failures"])
        stuck_days = self.settings.local_media_stuck_days
        if now - st.get("first_seen", now) > stuck_days * 86400:
            report.stuck += 1
            logger.warning(
                "本地媒体卡死告警：%s 低置信 %.1f 天（原因：%s）——建议人工检查",
                f.name, (now - st["first_seen"]) / 86400, "；".join(result.reasons),
            )
        self._retry_state[key] = st
        report.low_conf += 1
        backoff_h = retry_backoff_seconds(st["failures"]) / 3600
        logger.info(
            "本地媒体低置信保留 %s（原因：%s）→ %.1fh 后重试（第 %d 次）",
            f.name, "；".join(result.reasons), backoff_h, st["failures"],
        )
        return False

    # ------------------------------------------------------------------ #
    # 空目录清理：A 内文件全部流转后，删除空的子目录
    # ------------------------------------------------------------------ #
    def _cleanup_empty_dirs(self, start: Path) -> None:
        try:
            cur = start.resolve()
            root = self.input_dir.resolve()
        except OSError:
            return
        while cur != root and root in cur.parents:
            try:
                cur.rmdir()  # 仅空目录可删，非空抛错即停
                logger.info("本地媒体清理空目录：%s", cur)
            except OSError:
                return
            cur = cur.parent

    # ------------------------------------------------------------------ #
    # 后台循环
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info(
            "本地媒体监控启动：A=%s → B=%s（%s，%.0fs/轮 × %d 轮稳定）",
            self.input_dir, self.output_dir,
            "DRY-RUN 模拟" if self.settings.local_media_dry_run else "实际移动",
            self.interval, self.settings.local_media_stable_rounds,
        )
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                if report.scanned or report.moved or report.dry_moved:
                    logger.info("本地媒体扫描：%s", report.summary())
            except Exception as exc:
                logger.error("本地媒体扫描轮异常：%s", exc, exc_info=exc)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._save_state()
        logger.info("本地媒体监控已停止")
