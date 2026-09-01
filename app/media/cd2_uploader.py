"""CD2 上传服务：目录C（CD2 本地挂载）→ CopyFile → 115 网盘。

链路：
1. 每 CD2_UPLOAD_INTERVAL_SECONDS 秒通过 CD2 gRPC 列目录C（CD2 侧路径）
2. 文件 (size, writeTime) 连续 2 轮无变化 → 稳定（防半截上传）
3. 稳定 → GetSubFiles 列 115 目标目录查重（同名 → 记完成，跳过）
4. 未重复 → CopyFile 提交跨云复制任务（ConflictPolicy=Skip）
5. 轮询 GetCopyTasks 按 sourcePath/destPath 追踪：
   - Completed → DeleteFile 删本地源 → 记完成 → 汇总日志
   - Failed    → 指数退避重试（1h→2h→…→24h 封顶），超 CD2_STUCK_DAYS 卡死告警
   - 传输中   → 记录进度（uploadedBytes/totalBytes）
6. 串行：同时只跑 1 个 copy 任务（避免挤占上行带宽）

重试状态持久化 data/cd2_state.json（重启不丢退避计数）。
dry-run（CD2_UPLOAD_DRY_RUN 默认 true）：只查重 + 出"将上传"日志，不提交任务。

注意：gRPC 调用是同步 IO，统一丢 executor 线程执行，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.db.state import StateStore, load_with_legacy
from app.media.service import retry_backoff_seconds
from app.telegram.notifier import render_progress_bar

logger = logging.getLogger(__name__)


def _fmt_dur(seconds: float) -> str:
    """时长友好显示（进度条 ETA/已传时长用）。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    mins = seconds / 60
    if mins < 60:
        return f"{mins:.0f} 分钟"
    return f"{mins // 60:.0f} 小时 {mins % 60:.0f} 分"


@dataclass
class Cd2UploadReport:
    """一轮 CD2 上传扫描结果。"""

    scanned: int = 0        # 目录C 文件数
    submitted: int = 0       # 本轮新提交 copy 任务
    dry_submitted: int = 0   # dry-run 模拟提交
    completed: int = 0       # 本轮确认完成（含删源）
    skipped: int = 0        # 115 已存在同名跳过
    failed: int = 0         # 本轮失败（退避）
    stuck: int = 0           # 超 STUCK_DAYS 告警
    active: int = 0          # 当前传输中任务数
    details: list[str] = field(default_factory=list)  # 本轮事件明细（admin 通知用）
    progress_notified: int = 0  # 已通过进度消息单独通知的事件数（提交/完成/失败）

    def summary(self) -> str:
        s = f"扫描 {self.scanned} 个文件"
        if self.completed:
            s += f"：✅ 上传完成 {self.completed}"
        if self.submitted:
            s += f" · 📤 新任务 {self.submitted}"
        if self.dry_submitted:
            s += f" · 🔍 [DRY-RUN] 模拟上传 {self.dry_submitted}"
        if self.active:
            s += f" · 🔄 传输中 {self.active}"
        if self.skipped:
            s += f" · ⏭️ 已存在跳过 {self.skipped}"
        if self.failed:
            s += f" · ⏳ 失败退避 {self.failed}"
        if self.stuck:
            s += f" · 🚨 卡死 {self.stuck}（人工检查）"
        return s


@dataclass
class _TaskInfo:
    """一个已提交 copy 任务的追踪信息。"""

    name: str
    src_path: str           # CD2 源路径
    dst_path: str           # CD2 目标目录
    size: int
    submitted_at: float
    uploaded_bytes: int = 0
    last_progress: float = 0.0  # 上次进度更新时间（防卡死判定）


class Cd2UploaderService:
    """目录C → CD2 CopyFile → 115 上传服务。"""

    def __init__(self, container, settings) -> None:
        self.container = container   # 取 telegram bot 发 admin 通知（未 build 时为 None）
        self.settings = settings
        self.interval = max(5.0, settings.cd2_upload_interval_seconds)
        self.address = settings.cd2_address
        self.token = settings.cd2_token
        self.username = settings.cd2_username
        self.password = settings.cd2_password
        self.src_dir = settings.cd2_upload_src.rstrip("/")
        self.dst_dir = settings.cd2_upload_dst.rstrip("/")
        # 统一状态存储（data/state.db，service=cd2；旧 cd2_state.json 自动迁移）
        self._store = StateStore(getattr(settings, "state_db_path", "./data/state.db"))
        # DRY-RUN 已模拟处理的文件（内存级：dry 期间防重复出日志；
        # 不落盘——切回实际模式后这些文件正常上传，不被"已完成"污染）
        self._dry_done: set[str] = set()

        # 运行态
        self._jwt: str | None = None            # GetToken 缓存（token 模式无需）
        self._channel = None
        self._stub = None
        self._tasks: dict[str, _TaskInfo] = {}  # src_path → info（提交中任务）
        self._retry_state: dict[str, dict] = {}  # src_path → 退避状态
        self._completed: set[str] = set()         # 已完成/跳过的 src（去重）
        self._stable_seen: dict[str, str] = {}   # 稳定检测快照
        self._task: asyncio.Task | None = None
        # 列目录失败 warning 冷却（同路径 5min 内 1 条，防刷屏）
        self._list_dir_warn_cooldown: float = 300.0
        self._last_list_dir_warn_at: dict[str, float] = {}
        # 最近一轮汇总（/upload_status 与 /status 展示用）
        self._last_report: str | None = None
        # 任务进度消息（进度条通知）：每个 admin 一条，随轮次 edit 更新
        self._progress_msgs: list[tuple[int, int]] = []  # (chat_id, message_id)
        self._progress_src: str | None = None            # 进度消息跟随的任务 src
        self._progress_last_text: str = ""                # 防重复编辑（同文不 edit）

    # ------------------------------------------------------------------ #
    # gRPC 连接层（同步，统一走 executor）
    # ------------------------------------------------------------------ #
    @property
    def _auth_md(self) -> list[tuple[str, str]]:
        if self.token:
            return [("authorization", f"Bearer {self.token}")]
        if self._jwt:
            return [("authorization", f"Bearer {self._jwt}")]
        return []

    def _ensure_conn(self) -> bool:
        """建立 gRPC channel（幂等）。"""
        if self._stub is not None:
            return True
        try:
            import grpc

            from app.cd2 import clouddrive_pb2_grpc

            self._channel = grpc.insecure_channel(self.address)
            self._stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 gRPC 连接失败 %s：%s", self.address, exc)
            return False

    def _login(self) -> bool:
        """账号密码模式：GetToken 换 JWT（token 模式直接跳过）。"""
        if self.token:
            return True
        if self._jwt:
            return True
        try:
            pb2 = self._pb2()
            req = pb2.GetTokenRequest(
                userName=self.username, password=self.password
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

    # ------------------------------------------------------------------ #
    # 状态持久化
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        data = load_with_legacy(self._store, "cd2", "./data/cd2_state.json")
        self._retry_state = {
            k: v for k, v in (data.get("retry") or {}).items() if isinstance(v, dict)
        }
        self._completed = set(data.get("completed") or [])

    def _save_state(self) -> None:
        self._store.save(
            "cd2",
            {"retry": self._retry_state, "completed": list(self._completed)},
        )

    # ------------------------------------------------------------------ #
    # gRPC 操作封装（同步方法，由 executor 调用）
    # ------------------------------------------------------------------ #
    def _list_dir(self, path: str) -> list | None:
        """列目录；失败返回 None（区别于空目录 []）。"""
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

    def _submit_copy(self, src_path: str) -> bool:
        """提交 CopyFile 任务（Skip 冲突策略）。"""
        try:
            pb2 = self._pb2()
            req = pb2.CopyFileRequest(
                theFilePaths=[src_path],
                destPath=self.dst_dir,
                conflictPolicy=pb2.CopyFileRequest.Skip,
            )
            resp = self._stub.CopyFile(req, metadata=self._auth_md, timeout=30)
            return bool(resp.success)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 CopyFile 提交失败 %s：%s", src_path, exc)
            return False

    def _query_tasks(self) -> list | None:
        """GetCopyTasks 返回任务列表。"""
        try:
            from google.protobuf import empty_pb2

            resp = self._stub.GetCopyTasks(
                empty_pb2.Empty(), metadata=self._auth_md, timeout=30
            )
            return list(resp.copyTasks)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 GetCopyTasks 失败：%s", exc)
            return None

    def _delete_file(self, src_path: str) -> bool:
        """上传完成后删本地源文件。"""
        try:
            pb2 = self._pb2()
            req = pb2.FileRequest(path=src_path, forceRefresh=False)
            resp = self._stub.DeleteFile(req, metadata=self._auth_md, timeout=30)
            return bool(resp.success)
        except Exception as exc:  # noqa: BLE001
            logger.error("CD2 删除源文件失败 %s：%s", src_path, exc)
            return False

    # ------------------------------------------------------------------ #
    # 单轮扫描
    # ------------------------------------------------------------------ #
    async def run_once(self) -> Cd2UploadReport:
        report = Cd2UploadReport()
        loop = asyncio.get_running_loop()

        # 连接 + 登录
        def _conn() -> bool:
            return self._ensure_conn() and self._login()

        if not await loop.run_in_executor(None, _conn):
            return report

        # 1) 追踪已提交任务的进度（优先：可能已完成的先收尾）
        if self._tasks:
            await self._track_tasks(report)

        # 2) 列目录C
        files = await loop.run_in_executor(None, self._list_dir, self.src_dir)
        if files is None:
            now = time.time()
            last = self._last_list_dir_warn_at.get(self.src_dir, 0.0)
            if now - last >= self._list_dir_warn_cooldown:
                self._last_list_dir_warn_at[self.src_dir] = now
                logger.warning(
                    "CD2 源目录列取失败（返回 None）：%s 路径在 CD2 命名空间下可能不存在或 gRPC 中断——请检查 CD2_UPLOAD_SRC 配置与 CD2 服务状态（%.0f 秒内不再重复告警）",
                    self.src_dir, self._list_dir_warn_cooldown,
                )
            return report
        videos = [f for f in files if not f.isDirectory and f.size > 0]
        report.scanned = len(videos)

        now = time.time()
        names_dst: set[str] | None = None  # 115 目标目录文件名集（懒加载）

        # 3) 稳定检测 + 新任务提交（串行：有活跃任务时不提交新的）
        dry_run = bool(self.settings.cd2_upload_dry_run)
        for f in videos:
            src = f.fullPathName
            if src in self._completed or src in self._tasks:
                continue
            if dry_run and src in self._dry_done:
                continue  # dry 期间已模拟过（内存去重，防每轮刷日志）
            st = self._retry_state.get(src)
            if st is not None and st.get("next_retry", 0) > now:
                continue
            if self._tasks:  # 串行：等当前任务完成再提交下一个
                break
            # 稳定检测：连续 2 轮 (size, writeTime) 一致才稳
            snap = f"{f.size}:{f.writeTime.seconds if f.writeTime.seconds else 0}"
            if not self._is_stable(src, snap):
                continue
            # 查重（懒加载一次目标目录）
            if names_dst is None:
                dst_files = await loop.run_in_executor(
                    None, self._list_dir, self.dst_dir
                )
                names_dst = (
                    {x.name for x in dst_files if not x.isDirectory}
                    if dst_files is not None
                    else set()
                )
            if f.name in names_dst:
                self._completed.add(src)
                report.skipped += 1
                report.details.append(f"⏭️ {f.name}：115 目标已存在同名，跳过")
                logger.info("CD2 上传跳过 %s：115 目标已存在同名", f.name)
                continue
            # 提交
            if dry_run:
                report.dry_submitted += 1
                report.details.append(
                    f"🔍 [DRY-RUN] 将上传 {f.name}（{f.size / 1024**3:.2f}GB）"
                )
                logger.info(
                    "[DRY-RUN] CD2 将上传 %s（%.2fGB）→ %s",
                    f.name, f.size / 1024**3, self.dst_dir,
                )
                # 只记内存级去重（不落盘）：dry→real 切换后该文件正常上传，
                # 不会因 dry 期间被标"已完成"而跳过
                self._dry_done.add(src)
                continue
            ok = await loop.run_in_executor(None, self._submit_copy, src)
            if ok:
                self._tasks[src] = _TaskInfo(
                    name=f.name, src_path=src, dst_path=self.dst_dir,
                    size=f.size, submitted_at=now,
                )
                report.submitted += 1
                logger.info(
                    "CD2 上传任务已提交：%s（%.2fGB）→ %s",
                    f.name, f.size / 1024**3, self.dst_dir,
                )
                # 进度条消息（发送成功则不再进本轮汇总明细，避免同事件两条通知）
                if await self._send_progress_start(self._tasks[src]):
                    report.progress_notified += 1
                else:
                    report.details.append(
                        f"📤 新任务 {f.name}（{f.size / 1024**3:.2f}GB）"
                    )
            else:
                await self._record_failure(src, f.name, "CopyFile 提交失败", report, now)

        report.active = len(self._tasks)
        self._save_state()
        return report

    def _is_stable(self, src: str, snap: str) -> bool:
        """连续 2 轮（含当前）快照一致 → 稳定。"""
        prev = self._stable_seen.get(src)
        self._stable_seen[src] = snap
        return prev == snap

    # ------------------------------------------------------------------ #
    # 任务进度追踪
    # ------------------------------------------------------------------ #
    async def _track_tasks(self, report: Cd2UploadReport) -> None:
        loop = asyncio.get_running_loop()
        now = time.time()
        tasks = await loop.run_in_executor(None, self._query_tasks)
        if tasks is None:
            return
        by_src: dict[str, list] = {}
        for t in tasks:
            by_src.setdefault(t.sourcePath, []).append(t)

        for src, info in list(self._tasks.items()):
            matched = by_src.get(src) or []
            if not matched:
                # 单文件 copy 时 CD2 的 sourcePath 可能是父目录
                matched = [t for t in by_src.get(self.src_dir, [])
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
                        src, info.name, "任务消失且目标无此文件", report, now
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
                        src, info.name, f"CD2 任务失败: {err}", report, now
                    )
                else:
                    # 0 Pending / 1 Scanning / 2 Scanned：传输中
                    info.uploaded_bytes = t.uploadedBytes
                    info.last_progress = now
                    report.active += 1
                    await self._edit_task_progress(info, now)

    async def _finish(self, info: _TaskInfo, report: Cd2UploadReport, now: float) -> None:
        """任务完成：删源 + 状态清理 + 进度消息收尾 + 日志。"""
        loop = asyncio.get_running_loop()
        ok = loop.run_in_executor(None, self._delete_file, info.src_path)
        self._tasks.pop(info.src_path, None)
        self._retry_state.pop(info.src_path, None)
        self._completed.add(info.src_path)
        report.completed += 1
        took = now - info.submitted_at
        speed = info.size / max(0.1, took) / 1024**2
        note = "，疑似 115 秒传" if took < 60 else ""
        logger.info(
            "CD2 上传完成 %s（%.2fGB，%.1f 分钟，%.1f MB/s%s）",
            info.name, info.size / 1024**3, took / 60, speed, note,
        )
        # 进度消息收尾：编辑为完成态（该事件已单独通知，不再进汇总明细）
        if await self._end_progress(
            f"✅ CD2 上传完成 · {info.name}\n"
            f"[{render_progress_bar(100.0)}] 100%"
            f" · {info.size / 1024**3:.2f}GB · {took / 60:.1f} 分钟 · {speed:.1f} MB/s{note}",
            info.src_path,
        ):
            report.progress_notified += 1
        else:
            report.details.append(
                f"✅ {info.name}（{info.size / 1024**3:.2f}GB · {took / 60:.1f} 分钟 · {speed:.1f} MB/s{note}）"
            )
        if ok is None:
            logger.warning("CD2 上传完成但删源调度失败：%s", info.name)

    async def _record_failure(self, src: str, name: str, reason: str,
                               report: Cd2UploadReport, now: float) -> None:
        self._tasks.pop(src, None)
        st = self._retry_state.get(src, {"failures": 0, "first_seen": now})
        st["failures"] += 1
        st["next_retry"] = now + retry_backoff_seconds(st["failures"])
        if now - st.get("first_seen", now) > self.settings.cd2_stuck_days * 86400:
            report.stuck += 1
            logger.warning(
                "CD2 上传卡死告警：%s 失败 %.1f 天（%s）——建议人工检查",
                name, (now - st["first_seen"]) / 86400, reason,
            )
        self._retry_state[src] = st
        report.failed += 1
        backoff_h = retry_backoff_seconds(st["failures"]) / 3600
        logger.info(
            "CD2 上传失败 %s（%s）→ %.1fh 后重试（第 %d 次）",
            name, reason, backoff_h, st["failures"],
        )
        # 进度消息收尾：编辑为失败态（该事件已单独通知，不再进汇总明细）
        if await self._end_progress(
            f"⚠️ CD2 上传失败 · {name}\n{reason} → {backoff_h:.1f}h 后重试"
            f"（第 {st['failures']} 次）",
            src,
        ):
            report.progress_notified += 1
        else:
            report.details.append(
                f"⏳ 失败 {name}（第 {st['failures']} 次：{reason}，{backoff_h:.1f}h 后重试）"
            )

    # ------------------------------------------------------------------ #
    # 进度条消息（任务提交 → 传输中随轮编辑 → 完成/失败收尾）
    # ------------------------------------------------------------------ #
    def _progress_bot(self):
        """进度消息可用的 raw bot（CD2_REPORT_ADMIN 开 + TG 已 build + 有 admin）。"""
        if not getattr(self.settings, "cd2_report_admin", True):
            return None
        tg = getattr(self.container, "telegram", None) if self.container else None
        bot = getattr(tg, "bot", None) if tg is not None else None
        if bot is None or not (getattr(self.settings, "tg_admin_ids", None) or []):
            return None
        return bot

    async def _send_progress_start(self, info: _TaskInfo) -> bool:
        """任务提交时给每个 admin 发进度条消息（至少送达一个才算成功）。"""
        bot = self._progress_bot()
        if bot is None:
            return False
        text = (
            f"📤 CD2 上传开始 · {info.name}\n"
            f"[{render_progress_bar(0.0)}] 0%"
            f" · 0.00/{info.size / 1024**3:.2f}GB"
        )
        msgs: list[tuple[int, int]] = []
        for uid in self.settings.tg_admin_ids:
            try:
                m = await bot.send_message(chat_id=uid, text=text)
                msgs.append((uid, m.message_id))
            except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
                logger.warning("CD2 进度消息发送 admin %s 失败：%s", uid, exc)
        if not msgs:
            return False
        self._progress_msgs = msgs
        self._progress_src = info.src_path
        self._progress_last_text = text
        return True

    async def _edit_progress(self, text: str) -> None:
        """编辑进度消息（同文跳过防 Message is not modified；编辑失败的剔除防循环报错）。"""
        if not self._progress_msgs or text == self._progress_last_text:
            return
        bot = self._progress_bot()
        if bot is None:
            return
        alive: list[tuple[int, int]] = []
        for cid, mid in self._progress_msgs:
            try:
                await bot.edit_message_text(chat_id=cid, message_id=mid, text=text)
                alive.append((cid, mid))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CD2 进度消息编辑失败（%s）：%s", cid, exc)
        self._progress_msgs = alive
        self._progress_last_text = text

    async def _end_progress(self, text: str, src: str) -> bool:
        """任务终态收尾：把进度消息编辑为完成/失败态并清空进度状态。"""
        if not self._progress_msgs or self._progress_src != src:
            return False
        await self._edit_progress(text)
        self._progress_msgs = []
        self._progress_src = None
        self._progress_last_text = ""
        return True

    async def _edit_task_progress(self, info: _TaskInfo, now: float) -> None:
        """传输中随轮更新进度条；CD2 单文件传输不报字节时降级显示已传时长。"""
        if not self._progress_msgs or self._progress_src != info.src_path:
            return
        elapsed = max(0.0, now - info.submitted_at)
        if info.uploaded_bytes > 0 and info.size > 0:
            pct = min(100.0, info.uploaded_bytes / info.size * 100)
            speed = info.uploaded_bytes / max(0.1, elapsed)
            eta = (info.size - info.uploaded_bytes) / max(1.0, speed)
            await self._edit_progress(
                f"📤 CD2 上传中 · {info.name}\n"
                f"[{render_progress_bar(pct)}] {pct:.0f}%"
                f" · {info.uploaded_bytes / 1024**3:.2f}/{info.size / 1024**3:.2f}GB"
                f" · {speed / 1024**2:.1f} MB/s · 剩余 {_fmt_dur(eta)}"
            )
        else:
            await self._edit_progress(
                f"📤 CD2 上传中 · {info.name}\n"
                f"[{render_progress_bar(0.0)}] 传输中（CD2 单文件不报字节进度）"
                f" · 已 {_fmt_dur(elapsed)}"
            )

    # ------------------------------------------------------------------ #
    # admin 通知（本轮有动作才发，空轮不打扰）
    # ------------------------------------------------------------------ #
    async def _send_report(self, report: Cd2UploadReport) -> None:
        """把本轮汇总 + 明细发给 TG_ADMIN_IDS；发送失败只记日志，不中断循环。

        已通过进度条消息单独通知的事件（提交/完成/失败）不再重复发汇总；
        stuck 告警始终发送。
        """
        if not getattr(self.settings, "cd2_report_admin", True):
            return
        action_count = (report.completed + report.submitted + report.dry_submitted
                        + report.skipped + report.failed)
        if action_count - report.progress_notified <= 0 and not report.stuck:
            return
        tg = getattr(self.container, "telegram", None) if self.container else None
        if tg is None:
            return
        from app.telegram.notifier import format_round_report

        text = format_round_report(
            "📤", "CD2 上传汇总", report.summary(), report.details,
            dry_run=self.settings.cd2_upload_dry_run,
        )
        for uid in list(getattr(self.settings, "tg_admin_ids", []) or []):
            try:
                await tg.send_message(chat_id=uid, text=text)
            except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
                logger.warning("CD2 上传汇总发送 admin %s 失败：%s", uid, exc)

    # ------------------------------------------------------------------ #
    # /upload_status 查询
    # ------------------------------------------------------------------ #
    def status_lines(self) -> list[str]:
        lines = [
            f"📤 CD2 上传：{self.src_dir} → {self.dst_dir}",
            f"周期 {self.interval:.0f}s ｜ DRY_RUN {self.settings.cd2_upload_dry_run}",
            f"已完成 {len(self._completed)} 个 ｜ 退避中 {len(self._retry_state)}",
        ]
        now = time.time()
        for info in list(self._tasks.values())[:5]:
            pct = info.uploaded_bytes / max(1, info.size) * 100
            mins = (now - info.submitted_at) / 60
            lines.append(
                f"🔄 {info.name}：{pct:.0f}%"
                f"（{info.uploaded_bytes / 1024**3:.2f}/{info.size / 1024**3:.2f}GB"
                f"，{mins:.0f} 分钟）"
            )
        if self._retry_state:
            oldest = min(v.get("first_seen", 0) for v in self._retry_state.values())
            if oldest:
                lines.append(f"最老失败：{(time.time() - oldest) / 3600:.1f}h 前")
        if self._last_report:
            lines.append(f"最近一轮：{self._last_report}")
        return lines

    # ------------------------------------------------------------------ #
    # 后台循环
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._load_state()
        logger.info(
            "CD2 上传服务启动：%s → %s（%s，%.0fs/轮）",
            self.src_dir, self.dst_dir,
            "DRY-RUN 模拟" if self.settings.cd2_upload_dry_run else "实际上传",
            self.interval,
        )
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                self._last_report = report.summary()
                if (
                    report.completed or report.submitted or report.dry_submitted
                    or report.skipped or report.failed or report.stuck
                ):
                    logger.info("CD2 上传扫描：%s", report.summary())
                    await self._send_report(report)
            except Exception as exc:
                logger.error("CD2 上传扫描轮异常：%s", exc, exc_info=exc)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001,S110 - 关闭失败不阻断退出
                pass
            self._channel = None
            self._stub = None
        self._save_state()
