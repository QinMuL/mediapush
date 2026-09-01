"""ed2k 流水线服务：目录B稳定检测 → ed2k 哈希 → 移入目录C。

链路：
1. 每 ED2K_INTERVAL_SECONDS 秒递归扫描目录B（30s/轮 × 3 轮 = 1.5 分钟稳定）
2. 文件 (size, mtime) 连续 ED2K_STABLE_ROUNDS 轮无变化 → 稳定（防半截哈希）
3. 稳定 → ed2k_hash_file 流式分块哈希（单进程串行，IO 瓶颈）
4. 成功 → 写 jsonl 记录 data/ed2k_results.jsonl + 连同字幕移入 C（相对路径不变）
5. 失败（哈希 IO / OSError）→ 指数退避重试（1h→2h→…→24h 封顶）
6. 同一文件超 ED2K_STUCK_DAYS 天仍失败 → warning 提示人工介入
7. B 子目录清空后删除空目录

重试状态持久化 data/ed2k_state.json（重启不丢退避计数）。
dry-run（ED2K_DRY_RUN 默认 true）：只哈希、写 jsonl、出"将移动到"日志，不实际移文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.db.state import StateStore, load_with_legacy
from app.media.ed2k import ed2k_hash_file, ed2k_uri
from app.media.service import (
    _TEMP_EXTS,
    _TEMP_TAILS,
    VIDEO_EXTS,
    fast_move,
    retry_backoff_seconds,
)

logger = logging.getLogger(__name__)

SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".sub")


def _is_temp_file(path: Path) -> bool:
    name = path.name
    if path.suffix.lower() in _TEMP_EXTS:
        return True
    return any(name.endswith(t) for t in _TEMP_TAILS)


def _pick_sidecars(video: Path) -> list[Path]:
    """同 stem 的字幕与 nfo/jpg/png 海报（Emby 刮削元数据）。"""
    out: list[Path] = []
    for ext in SUBTITLE_EXTS + (".nfo", ".jpg", ".jpeg", ".png"):
        if (p := video.with_suffix(ext)).exists():
            out.append(p)
    return out


@dataclass
class Ed2kReport:
    """一轮 ed2k 扫描结果。"""

    scanned: int = 0        # 候选视频
    stable: int = 0          # 稳定并处理
    hashed: int = 0          # 哈希成功
    dry_moved: int = 0       # dry-run 模拟移动
    moved: int = 0           # 实际移入 C（含伴生文件）
    failed: int = 0          # 本轮哈希失败（退避）
    conflict: int = 0        # 目标同名跳过
    stuck: int = 0           # 超 STUCK_DAYS 告警

    def summary(self) -> str:
        s = f"扫描 {self.scanned} 个视频：稳定 {self.stable} · 哈希 {self.hashed}"
        if self.moved:
            s += f" → ✅ 移入C {self.moved}"
        if self.dry_moved:
            s += f" → 🔍 [DRY-RUN] 模拟移动 {self.dry_moved}"
        if self.failed:
            s += f" · ⏳ 失败退避 {self.failed}"
        if self.conflict:
            s += f" · ⚠️ 同名跳过 {self.conflict}"
        if self.stuck:
            s += f" · 🚨 卡死 {self.stuck}（人工检查）"
        return s


class Ed2kService:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.interval = max(1.0, settings.ed2k_interval_seconds)
        self.input_dir = Path(settings.ed2k_input_dir)
        self.output_dir = Path(settings.ed2k_output_dir)
        self.results_file = Path("./data/ed2k_results.jsonl")
        # 统一状态存储（data/state.db，service=ed2k；旧 JSON 自动迁移）
        self._store = StateStore(getattr(settings, "state_db_path", "./data/state.db"))
        self._seen: dict[str, tuple[int, float]] = {}
        self._stable: dict[str, int] = {}
        self._retry_state: dict[str, dict] = {}
        self._busy: set[str] = set()  # 哈希进行中：防重复触发
        # DRY-RUN 已模拟处理的文件（内存级，重启清空）
        self._dry_done: set[str] = set()
        self._last_report: str | None = None  # 最近一轮汇总（/status 展示用）
        self._load_state()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        data = load_with_legacy(self._store, "ed2k", "./data/ed2k_state.json")
        self._retry_state = {
            k: v for k, v in data.items() if isinstance(v, dict)
        }

    def _save_state(self) -> None:
        self._store.save("ed2k", self._retry_state)

    def _append_result(self, record: dict) -> None:
        try:
            self.results_file.parent.mkdir(parents=True, exist_ok=True)
            with self.results_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("ed2k 结果追加失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 单轮扫描
    # ------------------------------------------------------------------ #
    async def run_once(self) -> Ed2kReport:
        report = Ed2kReport()
        if not self.input_dir.is_dir():
            return report
        files = [
            f for f in self.input_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS and not _is_temp_file(f)
        ]
        report.scanned = len(files)

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
        for key in list(self._seen):
            if key not in alive:
                self._seen.pop(key, None)
                self._stable.pop(key, None)

        now = time.time()
        moved_sources: list[Path] = []
        for f in files:
            key = str(f)
            if key in self._dry_done:
                continue
            if self._stable.get(key, 0) < self.settings.ed2k_stable_rounds:
                continue
            if key in self._busy:
                continue
            st = self._retry_state.get(key)
            if st is not None and st.get("next_retry", 0) > now:
                continue
            try:
                ok = await self._process(f, report, now)
            except Exception as exc:
                logger.error("ed2k 处理异常 %s：%s", f.name, exc, exc_info=exc)
                self._record_failure(f, f"未预期异常: {exc}", report, now)
                continue
            if ok:
                report.stable += 1
                moved_sources.append(f)

        self._save_state()
        for src in moved_sources:
            self._cleanup_empty_dirs(src.parent)
        return report

    # ------------------------------------------------------------------ #
    # 单文件处理
    # ------------------------------------------------------------------ #
    async def _process(self, f: Path, report: Ed2kReport, now: float) -> bool:
        key = str(f)
        self._busy.add(key)
        try:
            try:
                root, size = await ed2k_hash_file(f)
            except (OSError, ValueError) as exc:
                self._record_failure(f, f"哈希失败: {exc}", report, now)
                return True
            hex_hash = root.hex()
            uri = ed2k_uri(f.name, size, hex_hash)
            self._append_result({
                "path": str(f),
                "name": f.name,
                "size_bytes": size,
                "root_hash": hex_hash,
                "ed2k": uri,
                "at": int(now),
            })
            report.hashed += 1
            return self._move(f, report, uri)
        finally:
            self._busy.discard(key)

    def _move(self, f: Path, report: Ed2kReport, uri: str) -> bool:
        # C 保持 B 的目录结构（剧集片名夹/Sxx 原样复用）
        rel = f.relative_to(self.input_dir)
        dest = self.output_dir / rel
        if dest.exists():
            report.conflict += 1
            logger.warning("ed2k 跳过 %s：目标已存在 %s（不覆盖）", f.name, dest)
            return True
        sides = _pick_sidecars(f)
        if self.settings.ed2k_dry_run:
            report.dry_moved += 1
            logger.info(
                "[DRY-RUN] ed2k %s → %s（伴行 %d，链接=%s）",
                f.name, dest, len(sides), uri,
            )
            self._dry_done.add(str(f))
            return True
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fast_move(f, dest)
            for s in sides:
                side_dest = dest.with_suffix(s.suffix)
                if not side_dest.exists():
                    fast_move(s, side_dest)
            report.moved += 1 + len(sides)
        except OSError as exc:
            logger.error("ed2k 移动失败 %s → %s：%s", f.name, dest, exc)
            return False
        self._retry_state.pop(str(f), None)
        logger.info(
            "ed2k 完成 %s → %s（伴行 %d，链接=%s）", f.name, dest, len(sides), uri
        )
        return True

    def _record_failure(self, f: Path, reason: str,
                        report: Ed2kReport, now: float) -> None:
        key = str(f)
        st = self._retry_state.get(key, {"failures": 0, "first_seen": now})
        st["failures"] += 1
        st["next_retry"] = now + retry_backoff_seconds(st["failures"])
        if now - st.get("first_seen", now) > self.settings.ed2k_stuck_days * 86400:
            report.stuck += 1
            logger.warning(
                "ed2k 卡死告警：%s 失败 %.1f 天（%s）——建议人工检查",
                f.name, (now - st["first_seen"]) / 86400, reason,
            )
        self._retry_state[key] = st
        report.failed += 1
        backoff_h = retry_backoff_seconds(st["failures"]) / 3600
        logger.info(
            "ed2k 失败保留 %s（%s）→ %.1fh 后重试（第 %d 次）",
            f.name, reason, backoff_h, st["failures"],
        )

    def _cleanup_empty_dirs(self, start: Path) -> None:
        try:
            cur = start.resolve()
            root = self.input_dir.resolve()
        except OSError:
            return
        while cur != root and root in cur.parents:
            try:
                cur.rmdir()
                logger.info("ed2k 清理空目录：%s", cur)
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
            "ed2k 流水线启动：B=%s → C=%s（%s，%.0fs/轮 × %d 轮稳定）",
            self.input_dir, self.output_dir,
            "DRY-RUN 模拟" if self.settings.ed2k_dry_run else "实际移动",
            self.interval, self.settings.ed2k_stable_rounds,
        )
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                self._last_report = report.summary()
                if report.hashed or report.moved or report.dry_moved or report.conflict or report.stuck:
                    logger.info("ed2k 扫描：%s", report.summary())
            except Exception as exc:
                logger.error("ed2k 扫描轮异常：%s", exc, exc_info=exc)
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
        logger.info("ed2k 流水线已停止")
