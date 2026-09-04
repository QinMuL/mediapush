"""统一媒体流水线：目录A → 重命名 → B 资源库 → 哈希/推卡片 → CD2 上传 115。

方案二整合（替代原四段服务 organizer/mover/pusher/cd2_uploader）：
四段原各自独立扫描目录（用文件系统模拟进程内消息队列），本服务在同一次
轮询里完成全部阶段，消灭了 3 个冗余同步点：
- B 侧/C 侧重复稳定判定 → 只保留 A 侧（下载完成检测）一处
- B→C 目录搬运 → 不存在（B 即资源库兼 CD2 上传源）
- pusher 的 JSONL offset 状态机 → 账本（dict）直接驱动

单轮流程（run_once）：
1. _track_tasks   追踪进行中的 CD2 上传（完成删源 / 失败退避 / 进度条）
2. _scan_a        扫描 A：稳定 → 体积守门 → TMDB 重命名 → 移入 B → 哈希记账
3. _scan_b        对账 B：未知文件（重启恢复/迁移/哈希失败重试）→ 哈希记账
4. _push_pending  账本中未推送的 ed2k → processor 推频道卡片
5. _submit_next   无活跃上传任务时：查重 115 目标 → 提交 CopyFile（串行）

阶段开关（dry 语义统一，/reload 热切换即时生效）：
- PIPELINE_RENAME_DRY_RUN  ① 只出"拟移动"日志（仍真实调 TMDB）
- PIPELINE_PUSH_DRY_RUN    ② 只出"将推送"日志（哈希/JSONL 仍真实）
- PIPELINE_UPLOAD_DRY_RUN  ③ 只出"将上传"日志（仍真实查重 115 目标）

状态（data/state.db，service=pipeline）：
- failures：FailureTracker，键 "rename:路径" / "hash:路径" / "push:路径" / "upload:路径"
- completed：已上传完成/查重跳过的路径（防重复提交）
- pushed：已推送（或查重跳过）的路径
JSONL（data/ed2k_results.jsonl）保留为追加式审计账本：path → ed2k 记录。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.link_parser import ParsedShare
from app.core.service_base import (
    AdminProgressMessages,
    FailureTracker,
    PollingService,
    retry_backoff_seconds,
)
from app.db.state import StateStore
from app.ed2k.hasher import ed2k_hash_file, ed2k_uri
from app.media.namer import NamingResult, analyze_file, sanitize_name
from app.telegram.notifier import render_progress_bar

logger = logging.getLogger(__name__)

# 推送限速间隔（秒）：连续推卡片避免 TG 频道 flood control（测试可调 0 加速）
_PUSH_GAP = 2.0

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv",
              ".webm", ".ts", ".m2ts", ".m4v", ".mpg", ".mpeg", ".rmvb"}
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".sub")
# 上传伴行文件：字幕 + 刮削元数据（与视频一起 CopyFile，完成后一起删源）
SIDECAR_EXTS = SUBTITLE_EXTS + (".nfo", ".jpg", ".jpeg", ".png")
# 下载器/同步工具的临时文件：扩展名匹配或 !qb 尾缀
_TEMP_EXTS = {".tmp", ".part", ".crdownload", ".download", ".downloading"}
_TEMP_TAILS = (".!qb",)


def fast_move(src: Path, dst: Path) -> None:
    """快速移动：同文件系统 rename（瞬时），跨挂载点 hardlink+unlink（瞬时）。

    Docker bind-mount A/B 为独立挂载点时 os.rename 会 EXDEV，
    但底层是同一物理文件系统 → os.link 硬链接可用且瞬时。
    """
    import errno
    try:
        os.rename(str(src), str(dst))
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    os.link(str(src), str(dst))
    os.unlink(str(src))


def is_temp_file(path: Path) -> bool:
    name = path.name
    if path.suffix.lower() in _TEMP_EXTS:
        return True
    return any(name.endswith(t) for t in _TEMP_TAILS)


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def pick_subtitles(video: Path) -> list[Path]:
    """同 stem 的字幕文件（Emby 刮削要求字幕与视频同名；重命名伴行）。"""
    return [s for ext in SUBTITLE_EXTS if (s := video.with_suffix(ext)).exists()]


def pick_upload_sidecars(video: Path) -> list[Path]:
    """上传伴行：字幕 + nfo/海报（与视频同 stem，存在于 B）。"""
    out: list[Path] = []
    for ext in SIDECAR_EXTS:
        if (p := video.with_suffix(ext)).exists():
            out.append(p)
    return out


def _fmt_dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    mins = seconds / 60
    if mins < 60:
        return f"{mins:.0f} 分钟"
    return f"{mins // 60:.0f} 小时 {mins % 60:.0f} 分"


def _remove_file(path: Path) -> None:
    """删文件（清洗成功后删 A 原件；失败仅日志）。"""
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删除文件失败 %s：%s", path, exc)


@dataclass
class PipelineReport:
    """一轮流水线结果。"""

    scanned: int = 0          # A 候选视频
    renamed: int = 0          # ① 实际重命名移入 B
    dry_renamed: int = 0      # ① 模拟移动
    hashed: int = 0           # ② 哈希成功（记账）
    pushed: int = 0           # ② 推卡片成功
    dry_pushed: int = 0       # ② 模拟推送
    skipped_dup: int = 0      # ② 已推过去重
    submitted: int = 0        # ③ 新提交上传任务
    dry_submitted: int = 0    # ③ 模拟上传
    upload_skipped: int = 0   # ③ 115 目标已存在同名跳过
    completed: int = 0        # ③ 上传完成（含删源）
    low_conf: int = 0         # ① 低置信保留（退避）
    conflict: int = 0         # ① B 目标同名跳过
    failed: int = 0           # 本轮失败（哈希/推送/上传，退避）
    stuck: int = 0            # 卡死告警
    active: int = 0           # 传输中任务数
    progress_notified: int = 0  # 已由进度条单独通知的事件数
    # 分组明细（grouped_details 按 ShareWatcher 同款分组渲染；条目已格式化，
    # 仅收"进度条未覆盖"的事件——新任务/完成/失败走进度条时不重复进汇总）
    pushed_titles: list[str] = field(default_factory=list)      # ✅ 推送成功
    dry_push_names: list[str] = field(default_factory=list)     # 🔍 模拟推送
    dup_names: list[str] = field(default_factory=list)          # ⏭️ 已推送过
    upload_done_lines: list[str] = field(default_factory=list)  # ✅ 上传完成
    upload_new_lines: list[str] = field(default_factory=list)   # 📤 新上传任务
    upload_dry_lines: list[str] = field(default_factory=list)   # 🔍 将上传
    upload_skip_lines: list[str] = field(default_factory=list)  # ⏭️ 115 已存在跳过
    fail_lines: list[str] = field(default_factory=list)         # ⏳ 失败（全阶段）
    cleaned_lines: list[str] = field(default_factory=list)      # 🧹 已清洗
    clean_dry_lines: list[str] = field(default_factory=list)    # 🧹 检测到（未清洗）
    cleaned_count: int = 0
    clean_checked: int = 0      # 闸门实际检测过的文件数（含干净，可见性用）

    def summary(self) -> str:
        s = f"A 扫描 {self.scanned}"
        if self.renamed or self.dry_renamed:
            tag = "🔍 [DRY-RUN] " if not self.renamed and self.dry_renamed else ""
            s += f"：{tag}重命名 {self.renamed or self.dry_renamed}"
        if self.cleaned_count or self.clean_dry_lines:
            if self.cleaned_count:
                s += f" · 🧹 清洗 {self.cleaned_count}"
            else:
                s += f" · 🧹 检测到垃圾 {len(self.clean_dry_lines)}"
        elif self.clean_checked:
            # 闸门跑了但全干净（可见性：区分"启用+干净"与"未启用"）
            s += f" · 🧹 检测 {self.clean_checked}（干净）"
        if self.hashed:
            s += f" · 哈希 {self.hashed}"
        if self.pushed or self.dry_pushed or self.skipped_dup:
            s += f" · 推送 {self.pushed or self.dry_pushed}"
            if self.dry_pushed and not self.pushed:
                s = s.replace("· 推送 ", "· 🔍 模拟推送 ")
            if self.skipped_dup:
                s += f"（去重 {self.skipped_dup}）"
        if self.completed or self.submitted or self.dry_submitted or self.upload_skipped:
            s += f" · 上传完成 {self.completed}"
            if self.submitted:
                s += f"/新任务 {self.submitted}"
            if self.dry_submitted and not self.submitted:
                s += f"（🔍 模拟 {self.dry_submitted}）"
            if self.upload_skipped:
                s += f"（已存在跳过 {self.upload_skipped}）"
        if self.low_conf:
            s += f" · ⏳ 低置信 {self.low_conf}"
        if self.conflict:
            s += f" · ⚠️ 同名跳过 {self.conflict}"
        if self.failed:
            s += f" · ⏳ 失败退避 {self.failed}"
        if self.stuck:
            s += f" · 🚨 卡死 {self.stuck}（人工检查）"
        return s

    def has_events(self) -> bool:
        return bool(
            self.renamed or self.dry_renamed or self.hashed
            or self.pushed or self.dry_pushed or self.skipped_dup
            or self.submitted or self.dry_submitted or self.upload_skipped
            or self.completed or self.low_conf or self.conflict
            or self.failed or self.stuck
            or self.cleaned_count or self.clean_dry_lines
        )

    def grouped_details(self) -> list[str]:
        """分组明细（ShareWatcher notify_admin 同款风格，两处格式保持一致）：

        组标题带计数 + 缩进条目（  • xxx）+ 组间空行；空组不渲染。
        """
        groups: list[tuple[str, list[str]]] = [
            ("🧹 已清洗", self.cleaned_lines),
            ("🧹 检测到垃圾", self.clean_dry_lines),
            ("✅ 推送成功", self.pushed_titles),
            ("🔍 模拟推送", self.dry_push_names),
            ("⏭️ 已推送过", self.dup_names),
            ("✅ 上传完成", self.upload_done_lines),
            ("📤 新上传任务", self.upload_new_lines),
            ("🔍 将上传", self.upload_dry_lines),
            ("⏭️ 115 已存在跳过", self.upload_skip_lines),
            ("⏳ 失败", self.fail_lines),
        ]
        out: list[str] = []
        for title, items in groups:
            if not items:
                continue
            if out:
                out.append("")  # 组间空行
            out.append(f"{title}（{len(items)}）")
            out.extend(f"  • {it}" for it in items)
        return out


@dataclass
class _TaskInfo:
    """一个已提交 CD2 copy 任务的追踪信息（键=本地 B 路径）。

    进度字段说明：CD2 的 CopyTask 只有文件级粒度——uploadedFiles 每完成
    一个文件 +1、uploadedBytes 相应跳变；单文件传输期间字节不更新
    （gRPC 接口上限，进度条按文件数/已传时长显示）。
    """

    name: str
    src_path: str           # 本地 B 路径
    cd2_src: str            # CD2 命名空间路径
    dst_path: str           # CD2 目标目录
    size: int
    submitted_at: float
    uploaded_bytes: int = 0
    last_progress: float = 0.0
    status: int = 0             # CD2 TaskStatus（0 Pending/1 Scanning/2 Scanned）
    uploaded_files: int = 0
    total_files: int = 0


class PipelineService(PollingService):
    """统一媒体流水线：run_once 单轮跑完全部阶段；循环骨架见 PollingService。"""

    name = "pipeline"
    log_prefix = "媒体流水线"

    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(1.0, settings.pipeline_interval_seconds)
        self.input_dir = Path(settings.pipeline_input_dir)        # A：下载落地
        self.library_dir = Path(settings.pipeline_library_dir)    # B：资源库（CD2 上传源）
        self.cd2_src = settings.cd2_upload_src.rstrip("/")        # B 的 CD2 视图
        self.cd2_dst = settings.cd2_upload_dst.rstrip("/")
        self.results_file = Path("./data/ed2k_results.jsonl")
        # 状态存储（data/state.db，service=pipeline）
        self._store = StateStore(getattr(settings, "state_db_path", "./data/state.db"))
        # 阶段退避：键 "rename:路径"/"hash:路径"/"push:路径"/"upload:路径"
        self._failures = FailureTracker(max(1.0, settings.pipeline_stuck_days))
        self._completed: set[str] = set()   # 已上传/查重跳过（本地路径）
        self._pushed: set[str] = set()      # 已推卡片/查重跳过（本地路径）
        # 账本：本地 B 路径 → JSONL 记录（哈希完成即入账，驱动推送/上传）
        self._ledger: dict[str, dict] = {}

        # A 侧稳定性追踪：(size, mtime) 快照 + 连续稳定轮数
        self._seen: dict[str, tuple[int, float]] = {}
        self._stable: dict[str, int] = {}
        self._small_warned: set[str] = set()
        # B 对账稳定性（重启恢复/迁移文件，2 轮快照一致才哈希）
        self._b_seen: dict[str, tuple[int, float]] = {}

        # dry 模拟去重（内存级：切实际后这些条目立即正常处理）
        self._dry_renamed: set[str] = set()
        self._dry_pushed: set[str] = set()
        self._dry_submitted: set[str] = set()

        # CD2 gRPC 运行态（同步调用统一走 executor）
        self._jwt: str | None = None
        self._channel = None
        self._stub = None
        self._tasks: dict[str, _TaskInfo] = {}   # 本地路径 → info
        # 上传任务进度条（每 admin 一条，随轮实时编辑；CD2 仅文件级进度）
        self._progress = AdminProgressMessages(
            self._progress_bot,
            lambda: getattr(self.settings, "tg_admin_ids", None) or [],
        )
        self._progress_src: str | None = None

    # ------------------------------------------------------------------ #
    # 状态持久化 / 账本
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        data = self._store.load("pipeline") or {}
        self._failures.load(data.get("failures") or {})
        self._completed = set(data.get("completed") or [])
        self._pushed = set(data.get("pushed") or [])
        # 账本从 JSONL 重建：只保留仍存在于 B 的路径（历史已删源的条目丢弃）
        if self.results_file.is_file():
            try:
                with self.results_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        p = rec.get("path") or ""
                        # 只保留仍存在于 B 的路径（已删源的历史条目丢弃）
                        if p and p not in self._ledger and Path(p).is_file():
                            self._ledger[p] = rec
            except OSError as exc:
                logger.warning("流水线账本读取失败：%s", exc)

    def _save_state(self) -> None:
        self._store.save("pipeline", {
            "failures": self._failures.dump(),
            "completed": list(self._completed),
            "pushed": list(self._pushed),
        })

    def _append_result(self, record: dict) -> None:
        try:
            self.results_file.parent.mkdir(parents=True, exist_ok=True)
            with self.results_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("流水线 JSONL 追加失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 单轮编排
    # ------------------------------------------------------------------ #
    async def run_once(self) -> PipelineReport:
        report = PipelineReport()
        self._failures.stuck_days = max(1.0, self.settings.pipeline_stuck_days)
        loop = asyncio.get_running_loop()

        # 1) 追踪进行中的上传任务
        if self._tasks:
            await self._track_tasks(report, loop)

        # 2) 扫描 A：稳定 → 守门 → 重命名 → 移入 B → 哈希记账
        await self._scan_a(report)

        # 3) 对账 B：未知文件（重启/迁移/哈希失败重试）→ 哈希记账
        await self._scan_b(report)

        # 4) 推卡片：账本中未推送的
        await self._push_pending(report)

        # 5) 串行上传：无活跃任务时取下一个就绪文件
        await self._submit_next(report, loop)

        report.active = len(self._tasks)
        self._save_state()
        return report

    # ------------------------------------------------------------------ #
    # ① A 扫描：稳定 → 体积守门 → TMDB 重命名 → 移入 B → 哈希
    # ------------------------------------------------------------------ #
    async def _scan_a(self, report: PipelineReport) -> None:
        if not self.input_dir.is_dir():
            return
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
        for key in list(self._seen):
            if key not in alive:
                self._seen.pop(key, None)
                self._stable.pop(key, None)
                self._small_warned.discard(key)

        now = time.time()
        moved_sources: list[Path] = []   # A 内源文件（移入 B 后清理其空父目录）
        processed = 0
        batch_max = max(1, int(getattr(self.settings, "pipeline_batch_max", 5)))
        stable_rounds = max(1, int(self.settings.pipeline_stable_rounds))
        dry = bool(self.settings.pipeline_rename_dry_run)
        for f in files:
            if processed >= batch_max:
                break
            key = str(f)
            # dry 模拟过的文件仅在本轮仍处 dry 时跳过（与全链路切换语义一致）
            if dry and key in self._dry_renamed:
                continue
            if self._stable.get(key, 0) < stable_rounds:
                continue
            # mtime 静默年龄守门：下载器（TG 分片/flood wait 等）停顿可骗过
            # 30s 快照对比，但骗不过"最近 N 分钟内仍有写入"——mtime 随写
            # 持续更新，写完才静止。rename 走半截文件会中断下载器后续写入
            # （NAS 实发：E01/E02 半截 1.06/0.93GB 上传 115）。
            min_age = max(
                0.0, getattr(self.settings, "pipeline_min_age_minutes", 5.0) * 60
            )
            try:
                if now - f.stat().st_mtime < min_age:
                    continue
            except OSError:
                continue
            # 体积守门：下载失败留下的 0 字节/残缺占位文件（空文件天然"稳定"）
            min_bytes = getattr(self.settings, "pipeline_min_size_mb", 10.0) * 1024 * 1024
            try:
                if f.stat().st_size < min_bytes:
                    if key not in self._small_warned:
                        self._small_warned.add(key)
                        report.low_conf += 1
                        logger.warning(
                            "流水线拦截疑似残缺文件 %s（%.1f MB < %.0f MB 阈值，"
                            "疑似下载失败占位；若确认下载完整请手动处理）",
                            f.name, f.stat().st_size / 1048576, min_bytes / 1048576,
                        )
                    continue
            except OSError:
                continue
            if not self._failures.due(f"rename:{key}", now):
                continue
            try:
                dest = await self._rename_move(f, report, now, dry)
            except Exception as exc:
                logger.error("流水线重命名异常 %s：%s", f.name, exc, exc_info=exc)
                continue
            if dest is None:
                continue
            processed += 1
            if dry:
                continue
            moved_sources.append(f)
            # 移入 B 即刻哈希（fast_move 原子完成，无需再稳定判定）
            await self._hash_to_ledger(dest, report, now)

        for src in moved_sources:
            if not src.exists():
                self._cleanup_empty_dirs(src.parent)

    async def _rename_move(self, f: Path, report: PipelineReport,
                           now: float, dry: bool) -> Path | None:
        """TMDB 分析 → 高置信重命名移入 B；低置信退避。返回 B 内目标路径。"""
        tmdb = self.container.tmdb
        if tmdb is None:
            logger.warning("流水线跳过 %s：TMDB 不可用", f.name)
            return None
        result = await analyze_file(str(f), tmdb)
        if not (result.high_confidence and result.proposed):
            return self._hold(f, result, report, now)
        dest = self.library_dir / sanitize_name(result.proposed)
        if dest.exists():
            report.conflict += 1
            logger.warning("流水线跳过 %s：B 已存在同名 %s（不覆盖）", f.name, dest)
            return None
        subs = pick_subtitles(f)
        if dry:
            report.dry_renamed += 1
            self._dry_renamed.add(str(f))
            logger.info("[DRY-RUN] %s → %s（字幕伴行 %d 个）", f.name, dest, len(subs))
            return dest  # 返回拟移动目标（计入单轮批量；不实际移动）
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not await self._maybe_clean(f, dest, report):
                return None  # 清洗失败已记退避；原件保留 A 待重试
            for s in subs:
                fast_move(s, dest.with_suffix(s.suffix))
            report.renamed += 1
        except OSError as exc:
            logger.error("流水线移动失败 %s → %s：%s", f.name, dest, exc)
            return None
        self._failures.clear(f"rename:{f}")
        logger.info("流水线重命名 %s → %s（字幕伴行 %d 个）", f.name, dest, len(subs))
        return dest

    async def _maybe_clean(self, f: Path, dest: Path,
                           report: PipelineReport) -> bool:
        """移入 B 前的元数据清洗闸门：干净→fast_move；脏→remux 清洗直落 B。

        返回 False = 本次流转失败（已记退避，原件保留 A）。
        - PIPELINE_CLEAN_ENABLED=false：原样 fast_move（零开销）
        - PIPELINE_CLEAN_DRY_RUN=true：只检测报告，文件照常 fast_move 进 B
        - 检测失败（ffprobe 不可用等）：按"干净"降级 fast_move，不阻塞流水线
        """
        if not getattr(self.settings, "pipeline_clean_enabled", False):
            fast_move(f, dest)
            return True
        from app.media import cleaner

        rpt = await cleaner.inspect(str(f))
        if rpt is None:
            logger.warning("元数据检测失败（按原样移动）：%s", f.name)
            fast_move(f, dest)
            return True
        report.clean_checked += 1
        if not rpt.has_junk:
            fast_move(f, dest)
            return True
        # 命中垃圾
        line = f"{f.name}：{rpt.summary()}"
        if getattr(self.settings, "pipeline_clean_dry_run", True):
            report.clean_dry_lines.append(f"{line}（未清洗，CLEAN_DRY_RUN）")
            logger.info("[DRY-RUN] 元数据清洗 %s", line)
            fast_move(f, dest)
            return True
        try:
            await cleaner.clean(str(f), str(dest), rpt)
        except cleaner.CleanError as exc:
            logger.error("元数据清洗失败 %s：%s（原件保留待重试）", f.name, exc)
            self._failures.record(f"rename:{f}", time.time())
            report.failed += 1
            return False
        report.cleaned_lines.append(line)
        report.cleaned_count += 1
        _remove_file(f)  # 清洗成功删 A 原件（B 已是干净版）
        return True

    def _hold(self, f: Path, result: NamingResult,
              report: PipelineReport, now: float) -> None:
        """低置信：原地保留 A，指数退避后重试。"""
        key = str(f)
        is_stuck = self._failures.record(f"rename:{key}", now)
        st = self._failures.get(f"rename:{key}") or {}
        if is_stuck:
            report.stuck += 1
            logger.warning(
                "流水线卡死告警：%s 低置信 %.1f 天（原因：%s）——建议人工检查",
                f.name, (now - st.get("first_seen", now)) / 86400,
                "；".join(result.reasons),
            )
        report.low_conf += 1
        backoff_h = retry_backoff_seconds(st.get("failures", 1)) / 3600
        logger.info(
            "流水线低置信保留 %s（原因：%s）→ %.1fh 后重试（第 %d 次）",
            f.name, "；".join(result.reasons), backoff_h, st.get("failures", 1),
        )

    # ------------------------------------------------------------------ #
    # A 空目录清理：文件全部流转后删除空的子目录
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
                logger.info("流水线清理空目录：%s", cur)
            except OSError:
                return
            cur = cur.parent

    # ------------------------------------------------------------------ #
    # ② 哈希记账（A 移入 B 后立即调用；scan_b 重试同一路径）
    # ------------------------------------------------------------------ #
    async def _hash_to_ledger(self, dest: Path, report: PipelineReport,
                              now: float) -> bool:
        key = str(dest)
        try:
            # 哈希闭环：前后双 stat——TG 下载器分片停顿可骗过 30s 稳定判定，
            # 半截文件 rename 进 B 后下载器仍凭句柄继续写 B 路径 inode；
            # 前后 size 不一致 = 文件仍在写入，哈希作废等 _scan_b 下轮重试
            size_before = dest.stat().st_size
            root, size = await ed2k_hash_file(dest)
            size_after = dest.stat().st_size
        except (OSError, ValueError) as exc:
            await self._record_failure("hash", key, dest.name, f"哈希失败: {exc}", report, now)
            return False
        if size_before != size_after or size != size_after:
            logger.warning(
                "哈希期间文件仍在写入（%d→%d→%d），作废本次哈希：%s",
                size_before, size, size_after, dest.name,
            )
            return False  # 不入账、不记退避；_scan_b 快照稳定后自动重哈希
        rec = {
            "path": key,
            "name": dest.name,
            "size_bytes": size,
            "root_hash": root.hex(),
            "ed2k": ed2k_uri(dest.name, size, root.hex()),
            "at": int(now),
        }
        self._append_result(rec)
        self._ledger[key] = rec
        self._failures.clear(f"hash:{key}")
        report.hashed += 1
        logger.info("流水线哈希完成：%s（%s）", dest.name, rec["ed2k"][:60])
        return True

    async def _scan_b(self, report: PipelineReport) -> None:
        """对账 B：不在账本/已完成/上传中的视频 → 2 轮快照稳定后哈希。

        覆盖三类场景：容器重启（_ledger 内存态重建）、旧链路迁移
        （C 目录内容移入 B）、哈希失败退避重试。
        """
        if not self.library_dir.is_dir():
            return
        now = time.time()
        videos = [
            f for f in self.library_dir.rglob("*")
            if f.is_file() and is_video_file(f) and not is_temp_file(f)
        ]
        alive = set()
        for f in videos:
            key = str(f)
            if key in self._ledger or key in self._completed or key in self._tasks:
                continue
            alive.add(key)
            try:
                stat = f.stat()
            except OSError:
                continue
            snap = (stat.st_size, stat.st_mtime)
            if self._b_seen.get(key) != snap:
                self._b_seen[key] = snap
                continue  # 首见：下轮快照一致才哈希
            # 体积守门（与 A 侧同阈值）：0 字节/残缺占位不对账哈希
            min_bytes = getattr(self.settings, "pipeline_min_size_mb", 10.0) * 1024 * 1024
            if stat.st_size < min_bytes:
                continue
            if not self._failures.due(f"hash:{key}", now):
                continue
            await self._hash_to_ledger(f, report, now)
        for key in list(self._b_seen):
            if key not in alive:
                self._b_seen.pop(key, None)

    # ------------------------------------------------------------------ #
    # ② 推卡片：账本未推送 → processor（TMDB 匹配 + 频道卡片 + 去重标记）
    # ------------------------------------------------------------------ #
    async def _push_pending(self, report: PipelineReport) -> None:
        processor = getattr(self.container, "processor", None)
        if processor is None:
            return
        dry = bool(self.settings.pipeline_push_dry_run)
        now = time.time()
        pushed_this_round = 0
        for key in list(self._ledger):
            if key in self._pushed:
                continue
            if dry and key in self._dry_pushed:
                continue
            if not self._failures.due(f"push:{key}", now):
                continue
            rec = self._ledger[key]
            url, name = rec.get("ed2k", ""), rec.get("name") or key
            if not url:
                continue
            if dry:
                report.dry_pushed += 1
                self._dry_pushed.add(key)
                report.dry_push_names.append(name)
                logger.info("[DRY-RUN] 推送 ed2k -> %s", name)
                continue
            try:
                res = await processor.process(ParsedShare(provider="ed2k", code=url))
            except Exception as exc:  # noqa: BLE001
                await self._record_failure("push", key, name, f"未预期异常: {exc}", report, now)
                continue
            if res.dup:
                report.skipped_dup += 1
                logger.info("流水线已推送过，跳过：%s", name)
                report.dup_names.append(name)
                self._pushed.add(key)
                self._failures.clear(f"push:{key}")
            elif res.ok:
                report.pushed += 1
                title = (getattr(res, "title", "") or "").strip() or name
                logger.info("流水线推送成功：%s", name)
                report.pushed_titles.append(title)
                self._pushed.add(key)
                self._failures.clear(f"push:{key}")
            else:
                await self._record_failure("push", key, name, res.message or "推送失败", report, now)
            pushed_this_round += 1
            if pushed_this_round >= 5:
                break  # 单轮推送上限（防一轮几十张卡片刷爆频道）
            await asyncio.sleep(_PUSH_GAP)

    # ------------------------------------------------------------------ #
    # ③ 串行上传：账本就绪 → 查重 115 目标 → CopyFile（视频+伴行）
    # ------------------------------------------------------------------ #
    async def _submit_next(self, report: PipelineReport, loop) -> None:
        if self._tasks:  # 串行：等当前任务完成再提交下一个
            return
        dry = bool(self.settings.pipeline_upload_dry_run)
        now = time.time()
        for key in sorted(self._ledger):
            if key in self._completed or key in self._tasks:
                continue
            if dry and key in self._dry_submitted:
                continue
            if not self._failures.due(f"upload:{key}", now):
                continue
            src = Path(key)
            if not src.is_file():
                continue  # 已被删（异常路径），下轮对账清理
            # 上传前复核：哈希后文件仍在增长（稳定判定被下载停顿骗过的
            # 残余场景）→ 作废账本重新哈希，绝不把半截文件传上 115
            try:
                cur_size = src.stat().st_size
                rec_size = self._ledger[key]["size_bytes"]
                if cur_size != rec_size:
                    logger.warning(
                        "上传前复核发现文件已变化（哈希 %d → 当前 %d），"
                        "作废账本重新哈希：%s",
                        rec_size, cur_size, src.name,
                    )
                    self._ledger.pop(key, None)
                    continue
            except OSError:
                continue
            # 连接 + 登录（幂等；失败只影响上传阶段，不阻塞其他阶段）
            if not await loop.run_in_executor(
                None, lambda: self._ensure_conn() and self._login()
            ):
                return
            # 查重：115 目标已存在同名 → 本地源冗余，删源后记完成跳过
            # （也是 _finish 删源失败的兜底重试入口）
            names_dst = await self._dst_names(loop)
            if names_dst is not None and src.name in names_dst:
                if dry:
                    # 模拟模式不动文件：仅记完成（查重本身是真实动作，沿用旧语义）
                    self._completed.add(key)
                    report.upload_skipped += 1
                    report.upload_skip_lines.append(src.name)
                    logger.info("流水线上传跳过 %s：115 目标已存在同名", src.name)
                    continue
                deleted = True
                for p in [src] + pick_upload_sidecars(src):
                    cd2_p = self._cd2_path(str(p))
                    ok = await loop.run_in_executor(
                        None, lambda q=cd2_p: self._delete_file(q)
                    )
                    if p == src:
                        deleted = ok
                if deleted:
                    self._completed.add(key)
                    report.upload_skipped += 1
                    report.upload_skip_lines.append(src.name)
                    logger.info(
                        "流水线上传跳过 %s：115 目标已存在同名（本地源已删）", src.name
                    )
                else:
                    await self._record_failure(
                        "upload", key, src.name,
                        "115 已存在但删除本地源失败", report, now,
                    )
                continue
            sidecars = pick_upload_sidecars(src)
            if dry:
                report.dry_submitted += 1
                self._dry_submitted.add(key)
                report.upload_dry_lines.append(
                    f"{src.name}（{src.stat().st_size / 1024**3:.2f}GB）"
                )
                logger.info(
                    "[DRY-RUN] 流水线将上传 %s（%.2fGB，伴行 %d）→ %s",
                    src.name, src.stat().st_size / 1024**3, len(sidecars), self.cd2_dst,
                )
                continue
            paths = [self._cd2_path(key)] + [self._cd2_path(str(s)) for s in sidecars]
            ok = await loop.run_in_executor(
                None, lambda p=paths: self._submit_copy(p)
            )
            if ok:
                info = _TaskInfo(
                    name=src.name, src_path=key, cd2_src=paths[0],
                    dst_path=self.cd2_dst, size=src.stat().st_size, submitted_at=now,
                )
                self._tasks[key] = info
                report.submitted += 1
                logger.info(
                    "流水线上传任务已提交：%s（%.2fGB，伴行 %d）→ %s",
                    src.name, info.size / 1024**3, len(sidecars), self.cd2_dst,
                )
                if await self._send_progress_start(info):
                    report.progress_notified += 1
                else:
                    report.upload_new_lines.append(
                        f"{src.name}（{info.size / 1024**3:.2f}GB）"
                    )
            else:
                await self._record_failure(
                    "upload", key, src.name, "CopyFile 提交失败", report, now
                )
            return  # 每轮至多提交一个（串行）

    async def _dst_names(self, loop) -> set[str] | None:
        """115 目标目录文件名集（查重用）；失败返回 None（跳过查重放行）。"""
        files = await loop.run_in_executor(None, self._list_dir, self.cd2_dst)
        if files is None:
            return None
        return {f.name for f in files if not f.isDirectory}

    def _cd2_path(self, local_path: str) -> str:
        """本地 B 路径 → CD2 命名空间路径（前缀映射）。"""
        try:
            rel = os.path.relpath(local_path, str(self.library_dir))
        except ValueError:
            return local_path
        if rel.startswith(".."):
            return local_path
        return f"{self.cd2_src}/{rel}"

    def _local_path(self, cd2_path: str) -> str:
        """CD2 路径 → 本地 B 路径（重启恢复用）。"""
        prefix = self.cd2_src + "/"
        if cd2_path.startswith(prefix):
            return str(self.library_dir / cd2_path[len(prefix):])
        return cd2_path

    # ------------------------------------------------------------------ #
    # 上传任务追踪（完成删源 / 失败退避 / 进度条）
    # ------------------------------------------------------------------ #
    async def _track_tasks(self, report: PipelineReport, loop) -> None:
        now = time.time()
        tasks = await loop.run_in_executor(None, self._query_tasks)
        if tasks is None:
            return
        by_src: dict[str, list] = {}
        for t in tasks:
            by_src.setdefault(t.sourcePath, []).append(t)

        for key, info in list(self._tasks.items()):
            matched = by_src.get(info.cd2_src) or []
            if not matched:
                # 单文件 copy 时 CD2 的 sourcePath 可能是父目录（B 根）
                matched = [t for t in by_src.get(self.cd2_src, [])
                           if t.destPath == info.dst_path]
            if not matched:
                # 刚提交 (<3 分钟) 等下一轮；否则查目标目录确认结果
                if now - info.submitted_at < 180:
                    continue
                dst_files = await loop.run_in_executor(
                    None, self._list_dir, info.dst_path
                )
                if dst_files is not None and any(
                    x.name == info.name and not x.isDirectory for x in dst_files
                ):
                    await self._finish(info, report, now)
                else:
                    await self._record_failure(
                        "upload", key, info.name, "任务消失且目标无此文件", report, now
                    )
                continue
            for t in matched:
                status = t.status
                if status == 3:  # Completed
                    await self._finish(info, report, now)
                elif status == 4:  # Failed
                    err = "; ".join(
                        f"{e.path}: {e.error}" for e in list(t.errors)[:3]
                    ) or "未知错误"
                    await self._record_failure(
                        "upload", key, info.name, f"CD2 任务失败: {err}", report, now
                    )
                else:
                    # 0 Pending / 1 Scanning / 2 Scanned：传输中
                    info.status = status
                    info.uploaded_bytes = t.uploadedBytes
                    info.uploaded_files = t.uploadedFiles
                    info.total_files = t.totalFiles
                    info.last_progress = now
                    report.active += 1
                    await self._edit_task_progress(info, now)

    async def _finish(self, info: _TaskInfo, report: PipelineReport, now: float) -> None:
        """任务完成：删源（视频+伴行）+ 状态清理 + 进度收尾。"""
        loop = asyncio.get_running_loop()
        # 删源：视频 + 伴行（此时文件仍在 B，按 stem 重找）。
        # 必须经 _cd2_path 转命名空间路径——本地路径直传 CD2 会 NOT_FOUND
        src = Path(info.src_path)
        deleted = True
        for p in [src] + pick_upload_sidecars(src):
            cd2_p = self._cd2_path(str(p))
            ok = await loop.run_in_executor(
                None, lambda q=cd2_p: self._delete_file(q)
            )
            if p == src:
                deleted = ok  # 视频删失败为门槛；伴行失败仅日志（孤儿字幕无碍）
        if not deleted:
            # 删源失败不记 completed（防 B 静默残留）：退避重试，
            # 下轮查重发现 115 已存在 → 走跳过路径兜底删源
            await self._record_failure(
                "upload", info.src_path, info.name,
                "上传完成但删除本地源失败", report, now,
            )
            return
        self._tasks.pop(info.src_path, None)
        self._failures.clear(f"upload:{info.src_path}")
        self._completed.add(info.src_path)
        report.completed += 1
        took = now - info.submitted_at
        speed = info.size / max(0.1, took) / 1024**2
        note = "，疑似 115 秒传" if took < 60 else ""
        logger.info(
            "流水线上传完成 %s（%.2fGB，%.1f 分钟，%.1f MB/s%s）",
            info.name, info.size / 1024**3, took / 60, speed, note,
        )
        if await self._end_progress(
            f"{self._pheader('✅', 'CD2 上传完成', info.submitted_at)}\n"
            f"📁 {info.name}（{info.size / 1024**3:.2f}GB · {took / 60:.1f} 分钟"
            f" · {speed:.1f} MB/s{note}）\n"
            f"[{render_progress_bar(100.0)}] 100%",
            info.src_path,
        ):
            report.progress_notified += 1
        else:
            report.upload_done_lines.append(
                f"{info.name}（{info.size / 1024**3:.2f}GB · {took / 60:.1f} 分钟）"
            )

    # 阶段中文标签（失败明细行前缀）
    _STAGE_LABEL = {"rename": "重命名", "hash": "哈希", "push": "推送", "upload": "上传"}

    async def _record_failure(self, stage: str, key: str, name: str, reason: str,
                              report: PipelineReport, now: float) -> None:
        full_key = f"{stage}:{key}"
        is_stuck = self._failures.record(full_key, now)
        st = self._failures.get(full_key) or {}
        if is_stuck:
            report.stuck += 1
            logger.warning(
                "流水线卡死告警：%s %s 失败 %.1f 天（%s）——建议人工检查",
                stage, name, (now - st.get("first_seen", now)) / 86400, reason,
            )
        report.failed += 1
        backoff_h = retry_backoff_seconds(st.get("failures", 1)) / 3600
        logger.info(
            "流水线%s失败 %s（%s）→ %.1fh 后重试（第 %d 次）",
            stage, name, reason, backoff_h, st.get("failures", 1),
        )
        label = self._STAGE_LABEL.get(stage, stage)
        fail_line = f"{label} {name}：{reason}（第 {st.get('failures', 1)} 次）"
        if stage == "upload":
            info = self._tasks.get(key)
            self._tasks.pop(key, None)
            ts = info.submitted_at if info is not None else now
            if await self._end_progress(
                f"{self._pheader('⚠️', 'CD2 上传失败', ts)}\n"
                f"📁 {name}\n"
                f"{reason} → {backoff_h:.1f}h 后重试（第 {st.get('failures', 1)} 次）",
                key,
            ):
                report.progress_notified += 1
            else:
                report.fail_lines.append(fail_line)  # 进度条未覆盖 → 进汇总
        else:
            report.fail_lines.append(fail_line)

    # ------------------------------------------------------------------ #
    # CD2 gRPC 层（同步，统一走 executor）
    # ------------------------------------------------------------------ #
    @property
    def _auth_md(self) -> list[tuple[str, str]]:
        if self.settings.cd2_token:
            return [("authorization", f"Bearer {self.settings.cd2_token}")]
        if self._jwt:
            return [("authorization", f"Bearer {self._jwt}")]
        return []

    def _ensure_conn(self) -> bool:
        if self._stub is not None:
            return True
        try:
            import grpc

            from app.cd2 import clouddrive_pb2_grpc

            self._channel = grpc.insecure_channel(self.settings.cd2_address)
            self._stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 gRPC 连接失败 %s：%s", self.settings.cd2_address, exc)
            return False

    def _login(self) -> bool:
        if self.settings.cd2_token or self._jwt:
            return True
        try:
            pb2 = self._pb2()
            req = pb2.GetTokenRequest(
                userName=self.settings.cd2_username, password=self.settings.cd2_password
            )
            resp = self._stub.GetToken(req, timeout=10)
            if resp.success:
                self._jwt = resp.token
                return True
            logger.error("CD2 登录失败：%s", resp.errorMessage)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 登录异常：%s", exc)
            return False

    def _pb2(self):
        from app.cd2 import clouddrive_pb2

        return clouddrive_pb2

    def _list_dir(self, path: str) -> list | None:
        try:
            pb2 = self._pb2()
            req = pb2.ListSubFileRequest(path=path, forceRefresh=True)
            files: list = []
            for resp in self._stub.GetSubFiles(req, metadata=self._auth_md, timeout=60):
                files.extend(resp.subFiles)
            return files
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 列目录失败 %s：%s", path, exc)
            return None

    def _submit_copy(self, cd2_paths: list[str]) -> bool:
        try:
            pb2 = self._pb2()
            req = pb2.CopyFileRequest(
                theFilePaths=cd2_paths,
                destPath=self.cd2_dst,
                conflictPolicy=pb2.CopyFileRequest.Skip,
            )
            resp = self._stub.CopyFile(req, metadata=self._auth_md, timeout=30)
            return bool(resp.success)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 CopyFile 提交失败 %s：%s", cd2_paths, exc)
            return False

    def _query_tasks(self) -> list | None:
        try:
            from google.protobuf import empty_pb2

            resp = self._stub.GetCopyTasks(
                empty_pb2.Empty(), metadata=self._auth_md, timeout=30
            )
            return list(resp.copyTasks)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 GetCopyTasks 失败：%s", exc)
            return None

    def _delete_file(self, cd2_path: str) -> bool:
        try:
            pb2 = self._pb2()
            req = pb2.FileRequest(path=cd2_path, forceRefresh=False)
            resp = self._stub.DeleteFile(req, metadata=self._auth_md, timeout=30)
            return bool(resp.success)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 删除源文件失败 %s：%s", cd2_path, exc)
            return False

    # ------------------------------------------------------------------ #
    # 重启恢复：从 CD2 GetCopyTasks 重建进行中任务追踪
    # ------------------------------------------------------------------ #
    async def _recover_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = await loop.run_in_executor(None, self._query_tasks)
        if not tasks:
            return
        now = time.time()
        recovered = 0
        for t in tasks:
            if t.destPath != self.cd2_dst:
                continue
            if t.status in (3, 4):  # Completed/Failed：由下轮对账收尾/重新提交
                continue
            st_time = getattr(t, "startTime", None)
            submitted_at = st_time.seconds if getattr(st_time, "seconds", 0) else now
            local = self._local_path(t.sourcePath)
            if local in self._ledger and Path(local).is_file():
                info = _TaskInfo(
                    name=Path(local).name, src_path=local, cd2_src=t.sourcePath,
                    dst_path=self.cd2_dst,
                    size=Path(local).stat().st_size, submitted_at=submitted_at,
                    uploaded_bytes=t.uploadedBytes,
                )
            elif t.sourcePath == self.cd2_src:
                # 目录级任务（Scanning 期 totalBytes=0）：占位堵串行闸门，完成不删源
                info = _TaskInfo(
                    name="（重启恢复·待扫描）", src_path=self.cd2_src, cd2_src=t.sourcePath,
                    dst_path=self.cd2_dst, size=t.totalBytes, submitted_at=submitted_at,
                    uploaded_bytes=t.uploadedBytes,
                )
                logger.warning(
                    "流水线重启恢复：目录级任务无法定位文件（totalBytes=%d），"
                    "已建占位追踪防重复提交", t.totalBytes,
                )
            else:
                # 按大小匹配账本内文件
                match = next(
                    (k for k, r in self._ledger.items()
                     if Path(k).is_file() and Path(k).stat().st_size == t.totalBytes),
                    None,
                )
                if match is None:
                    continue
                info = _TaskInfo(
                    name=Path(match).name, src_path=match, cd2_src=t.sourcePath,
                    dst_path=self.cd2_dst, size=t.totalBytes, submitted_at=submitted_at,
                    uploaded_bytes=t.uploadedBytes,
                )
            self._tasks[info.src_path] = info
            recovered += 1
            logger.info(
                "流水线重启恢复任务：%s（%.2fGB，CD2 报告 %.2fGB 已传）",
                info.name, info.size / 1024**3, info.uploaded_bytes / 1024**3,
            )
        if recovered:
            first = next(iter(self._tasks.values()), None)
            if first is not None and first.src_path != self.cd2_src:
                await self._send_progress_start(first)

    # ------------------------------------------------------------------ #
    # 上传进度条消息（AdminProgressMessages 收发，这里只管渲染格式）
    # 格式与轮汇总 format_round_report 统一：「图标 标题 · 开始时间」头 + 正文行
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pheader(icon: str, title: str, ts: float) -> str:
        """进度消息头：与轮汇总同款「图标 标题 · MM-DD HH:MM」（时间=任务开始时刻，
        全生命周期不变，作为该消息的锚点）。"""
        return f"{icon} {title} · {time.strftime('%m-%d %H:%M', time.localtime(ts))}"

    def _progress_bot(self):
        if not getattr(self.settings, "pipeline_report_admin", True):
            return None
        tg = getattr(self.container, "telegram", None) if self.container else None
        bot = getattr(tg, "bot", None) if tg is not None else None
        if bot is None or not (getattr(self.settings, "tg_admin_ids", None) or []):
            return None
        return bot

    async def _send_progress_start(self, info: _TaskInfo) -> bool:
        text = (
            f"{self._pheader('📤', 'CD2 上传开始', info.submitted_at)}\n"
            f"📁 {info.name}（{info.size / 1024**3:.2f}GB）\n"
            f"[{render_progress_bar(0.0)}] 0%"
            f" · 0.00/{info.size / 1024**3:.2f}GB"
        )
        ok = await self._progress.send(text)
        if ok:
            self._progress_src = info.src_path
        return ok

    async def _end_progress(self, text: str, src: str) -> bool:
        if not self._progress.active or self._progress_src != src:
            return False
        await self._progress.finalize(text)
        self._progress_src = None
        return True

    async def _edit_task_progress(self, info: _TaskInfo, now: float) -> None:
        """传输中随轮（默认 10s）实时编辑进度条。

        CD2 仅文件级进度：uploadedBytes 只在整文件完成时跳变。
        - 有字节进度（多文件任务，视频已完成）→ 百分比 + 速度 + 剩余时间
        - 无字节进度（单文件传输中）→ 任务状态文字 + 文件进度 + 已传时长
          （每轮更新，至少能看出任务活着、没卡死）
        """
        if not self._progress.active or self._progress_src != info.src_path:
            return
        elapsed = max(0.0, now - info.submitted_at)
        head = (
            f"{self._pheader('📤', 'CD2 上传中', info.submitted_at)}\n"
            f"📁 {info.name}（{info.size / 1024**3:.2f}GB）"
        )
        status_txt = {0: "排队中", 1: "准备中"}.get(info.status, "传输中")

        if info.uploaded_bytes > 0 and info.size > 0:
            pct = min(100.0, info.uploaded_bytes / info.size * 100)
            speed = info.uploaded_bytes / max(0.1, elapsed)
            eta = (info.size - info.uploaded_bytes) / max(1.0, speed)
            await self._progress.edit(
                f"{head}\n"
                f"[{render_progress_bar(pct)}] {pct:.0f}% · {status_txt}"
                f" · {info.uploaded_bytes / 1024**3:.2f}/{info.size / 1024**3:.2f}GB"
                f" · {speed / 1024**2:.1f} MB/s · 剩余 {_fmt_dur(eta)}"
            )
            return

        files_txt = (
            f" · 文件 {info.uploaded_files}/{info.total_files}"
            if info.total_files > 1 else ""
        )
        await self._progress.edit(
            f"{head}\n"
            f"[{render_progress_bar(0.0)}] {status_txt}{files_txt}"
            f" · 已 {_fmt_dur(elapsed)}（CD2 单文件不报字节进度）"
        )

    # ------------------------------------------------------------------ #
    # admin 轮汇总（有动作才发，空轮不打扰）
    # ------------------------------------------------------------------ #
    async def _send_report(self, report: PipelineReport) -> None:
        """轮汇总：分组明细（与目录监控 notify_admin 同款格式，风格统一）。"""
        if not getattr(self.settings, "pipeline_report_admin", True):
            return
        action_count = (
            report.renamed + report.hashed + report.pushed + report.submitted
            + report.dry_renamed + report.dry_pushed + report.dry_submitted
            + report.completed + report.upload_skipped + report.failed
        )
        if action_count - report.progress_notified <= 0 and not report.stuck:
            return
        tg = getattr(self.container, "telegram", None) if self.container else None
        if tg is None:
            return
        from app.telegram.notifier import format_round_report

        dry_any = (
            self.settings.pipeline_rename_dry_run
            or self.settings.pipeline_push_dry_run
            or self.settings.pipeline_upload_dry_run
        )
        text = format_round_report(
            "🎬", "媒体流水线汇总", report.summary(),
            report.grouped_details(),
            dry_run=dry_any,
        )
        for uid in list(getattr(self.settings, "tg_admin_ids", []) or []):
            try:
                await tg.send_message(chat_id=uid, text=text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("流水线汇总发送 admin %s 失败：%s", uid, exc)

    # ------------------------------------------------------------------ #
    # 状态查询（/status /ed2k_status /upload_status）
    # ------------------------------------------------------------------ #
    def _push_pending_count(self) -> int:
        dry = bool(self.settings.pipeline_push_dry_run)
        now = time.time()
        n = 0
        for key in self._ledger:
            if key in self._pushed or key in self._completed:
                continue
            if dry and key in self._dry_pushed:
                continue
            if not self._failures.due(f"push:{key}", now):
                continue
            n += 1
        return n

    def overview_lines(self) -> list[str]:
        """/status 流水线块：三阶段一行一个 + 概要。"""
        now = time.time()
        a_queue = sum(
            1 for k in self._seen
            if self._stable.get(k, 0) >= self.settings.pipeline_stable_rounds
        )
        failure_keys = list(self._failures.dump())
        clean_note = ""
        if getattr(self.settings, "pipeline_clean_enabled", False):
            clean_note = (
                " · 清洗模拟" if getattr(self.settings, "pipeline_clean_dry_run", True)
                else " · 清洗实际"
            )
        lines = [
            f"① A→B 重命名：{'模拟' if self.settings.pipeline_rename_dry_run else '实际'}{clean_note}"
            f" · A 待处理 {a_queue} · 低置信退避 "
            f"{sum(1 for k in failure_keys if k.startswith('rename:'))}",
            f"② B→卡片：{'模拟' if self.settings.pipeline_push_dry_run else '实际'}"
            f" · 待推 {self._push_pending_count()} · 账本 {len(self._ledger)} 条",
            f"③ B→115：{'模拟' if self.settings.pipeline_upload_dry_run else '实际'}"
            f" · 已完成 {len(self._completed)} · 退避 "
            f"{sum(1 for k in failure_keys if k.startswith('upload:'))}",
            f"   A={self.input_dir} → B={self.library_dir}",
            f"   CD2：{self.cd2_src} → {self.cd2_dst}",
        ]
        for info in list(self._tasks.values())[:3]:
            pct = info.uploaded_bytes / max(1, info.size) * 100
            mins = (now - info.submitted_at) / 60
            lines.append(
                f"   🔄 传输中：{info.name} {pct:.0f}%（{mins:.0f} 分钟）"
            )
        if self._last_report:
            lines.append(f"   最近一轮：{self._last_report}")
        return lines

    def status_push_text(self) -> str:
        """/ed2k_status：推送侧详情（pending/卡死/最近汇总）。"""
        now = time.time()
        pending: list[tuple[float, str, dict]] = []
        for key in self._ledger:
            if key in self._pushed:
                continue
            st = self._failures.get(f"push:{key}")
            if st is None:
                pending.append((0.0, key, {}))
            else:
                pending.append((
                    max(0.0, (now - float(st.get("first_seen", now))) / 3600.0),
                    key, st,
                ))
        pending.sort(key=lambda x: x[0], reverse=True)
        stuck_days = max(1.0, self.settings.pipeline_stuck_days)
        lines = [
            "📤 流水线推送状态",
            f"• 账本：{len(self._ledger)} 条 · 已推 {len(self._pushed)}",
            f"• 周期：{self.interval:.0f}s ｜ 推送 DRY_RUN：{self.settings.pipeline_push_dry_run}",
        ]
        if pending:
            n = 8
            lines.append(f"• Pending Top {min(n, len(pending))}（按等待时长倒序）：")
            for age_h, key, st in pending[:n]:
                f = st.get("failures", 0)
                nr = float(st.get("next_retry", 0) or 0)
                left = max(0.0, (nr - now) / 3600.0)
                name = key.rsplit("/", 1)[-1]
                snippet = name if len(name) <= 72 else name[:69] + "..."
                flag = " 🚨" if age_h / 24.0 >= stuck_days else ""
                lines.append(f"  • {age_h:.1f}h｜失败{f}｜下次{left:.1f}h{flag}｜{snippet}")
            if len(pending) > n:
                lines.append(f"  （剩 {len(pending) - n} 条未列出）")
        if self._last_report:
            lines.append(f"• 最近一轮：{self._last_report}")
        return "\n".join(lines)

    def status_upload_text(self) -> str:
        """/upload_status：上传侧详情（任务/退避/最近汇总）。"""
        now = time.time()
        lines = [
            "📤 流水线上传状态",
            f"• B={self.library_dir}（CD2 {self.cd2_src}）→ {self.cd2_dst}",
            f"• 周期 {self.interval:.0f}s ｜ 上传 DRY_RUN：{self.settings.pipeline_upload_dry_run}",
            f"• 已完成 {len(self._completed)} 个",
        ]
        for info in list(self._tasks.values())[:5]:
            pct = info.uploaded_bytes / max(1, info.size) * 100
            mins = (now - info.submitted_at) / 60
            lines.append(
                f"• 🔄 {info.name}：{pct:.0f}%"
                f"（{info.uploaded_bytes / 1024**3:.2f}/{info.size / 1024**3:.2f}GB，{mins:.0f} 分钟）"
            )
        retries = [
            (k[len("upload:"):], st) for k, st in self._failures.dump().items()
            if k.startswith("upload:")
        ]
        if retries:
            stuck_days = max(1.0, self.settings.pipeline_stuck_days)
            retries.sort(
                key=lambda x: x[1].get("first_seen", 0), reverse=True
            )
            lines.append(f"• 失败退避 Top {min(8, len(retries))}（按等待时长倒序）：")
            for key, st in retries[:8]:
                age_h = max(0.0, (now - float(st.get("first_seen", now))) / 3600.0)
                f = st.get("failures", 0)
                nr = float(st.get("next_retry", 0) or 0)
                left = max(0.0, (nr - now) / 3600.0)
                name = key.rsplit("/", 1)[-1]
                snippet = name if len(name) <= 72 else name[:69] + "..."
                flag = " 🚨" if age_h / 24.0 >= stuck_days else ""
                lines.append(f"  • {age_h:.1f}h｜失败{f}｜下次{left:.1f}h{flag}｜{snippet}")
        if self._last_report:
            lines.append(f"• 最近一轮：{self._last_report}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 生命周期钩子（循环骨架见 PollingService；start 覆盖以加重启恢复）
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._load_state()
        self._on_start()
        # 重启恢复：CD2 侧仍在跑的任务重新纳入追踪（防重复提交 + 进度续显）
        try:
            loop = asyncio.get_running_loop()
            if await loop.run_in_executor(
                None, lambda: self._ensure_conn() and self._login()
            ):
                await self._recover_tasks()
            else:
                logger.warning("流水线重启任务恢复跳过：CD2 gRPC 连接/登录失败")
        except Exception as exc:  # noqa: BLE001 - 恢复失败不阻断启动
            logger.warning("流水线重启任务恢复失败（下轮对账将收尾）：%s", exc)
        self._task = asyncio.create_task(self._run_loop())

    async def after_round(self, report) -> None:
        if report.has_events():
            self.log.info("%s：%s", self.log_prefix, report.summary())
            await self._send_report(report)

    def _on_start(self) -> None:
        clean_mode = (
            "关闭" if not self.settings.pipeline_clean_enabled
            else "模拟" if self.settings.pipeline_clean_dry_run else "实际"
        )
        self.log.info(
            "媒体流水线启动：A=%s → B=%s（重命名%s · 推送%s · 上传%s · 清洗%s，%.0fs/轮）",
            self.input_dir, self.library_dir,
            "模拟" if self.settings.pipeline_rename_dry_run else "实际",
            "模拟" if self.settings.pipeline_push_dry_run else "实际",
            "模拟" if self.settings.pipeline_upload_dry_run else "实际",
            clean_mode,
            self.interval,
        )

    def on_stopped(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001,S110 - 关闭失败不阻断退出
                pass
            self._channel = None
            self._stub = None
        self._save_state()
        self.log.info("媒体流水线已停止")
