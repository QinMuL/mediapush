"""ed2k 结果推送服务：追读 data/ed2k_results.jsonl → ShareProcessor 频道卡片。

ed2k_service 把哈希成功的资源记录为 JSONL 每行：
    {"path":..., "name":..., "size_bytes":..., "root_hash":..., "ed2k":..., "at":...}

本服务每 ED2K_PUSH_INTERVAL_SECONDS 秒追读增量（offset 字节位置持久化，重启不丢）：
- 每条 `ed2k` URL → `ParsedShare("ed2k", url)` → `container.processor.process()`
  processor 内部已完成：ed2k 解析 → 文件名聚合 → TMDB 匹配 → Pusher 卡片推送
  → `TG_CHAT_ID_ED2K` 频道 → cache 去重标记
- process 返回 False（TMDB 未匹配 / 推送失败…）：1h→2h→…→24h 指数退避重试
- dry-run（ED2K_PUSH_DRY_RUN 默认 true）：只日志"将推送 <ed2k URL>"，不调用 process

工程化约定与前两个服务一致：
- 状态持久化 `data/ed2k_push_state.json`（offset + 每条退避状态）
- 卡死超 STUCK_DAYS → warning 人工介入
- 去重由 `cache.is_pushed` 保证（ed2k URL 为 code）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.link_parser import ParsedShare
from app.media.service import retry_backoff_seconds

logger = logging.getLogger(__name__)


@dataclass
class PushReport:
    read: int = 0           # 本轮新 JSONL 行数
    pushed: int = 0         # 推送成功
    dry_pushed: int = 0     # dry-run 模拟推送
    skipped_dup: int = 0    # processor 命中去重（已推送过）
    failed: int = 0         # 本轮失败（退避）
    stuck: int = 0          # 超 STUCK_DAYS 告警

    def summary(self) -> str:
        s = f"读取 {self.read} 条新记录：推送 {self.pushed}"
        if self.dry_pushed:
            s += f" · 🔍 [DRY-RUN] 模拟推送 {self.dry_pushed}"
        if self.skipped_dup:
            s += f" · ⏭️ 已推过去重 {self.skipped_dup}"
        if self.failed:
            s += f" · ⏳ 失败退避 {self.failed}"
        if self.stuck:
            s += f" · 🚨 卡死 {self.stuck}"
        return s


class Ed2kPusherService:
    def __init__(self, container, settings) -> None:
        self.container = container
        self.settings = settings
        self.interval = max(1.0, settings.ed2k_push_interval_seconds)
        self.results_file = Path("./data/ed2k_results.jsonl")
        self.state_file = Path("./data/ed2k_push_state.json")
        # {"_offset": int, "<ed2k_url>": {failures, next_retry, first_seen}}
        self._state: dict = {"_offset": 0}
        self._load_state()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._state = data
            if "_offset" not in self._state:
                self._state["_offset"] = 0
        except FileNotFoundError:
            self._state = {"_offset": 0}
        except (OSError, ValueError) as exc:
            logger.warning("ed2k 推送状态加载失败（按空启动）：%s", exc)
            self._state = {"_offset": 0}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("ed2k 推送状态保存失败：%s", exc)

    # ------------------------------------------------------------------ #
    def _read_new_lines(self) -> list[dict]:
        if not self.results_file.is_file():
            return []
        try:
            size = self.results_file.stat().st_size
        except OSError:
            return []
        offset = self._state.get("_offset", 0)
        if size < offset:
            offset = 0  # 文件被截断/轮转（清了日志）→ 从头读
        records: list[dict] = []
        with self.results_file.open("r", encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError as exc:
                    logger.warning("ed2k 结果 JSONL 行解析失败：%s：%s", exc, line[:80])
            self._state["_offset"] = f.tell()
        return records

    # ------------------------------------------------------------------ #
    async def run_once(self) -> PushReport:
        # state_file/results_file 可能在 __init__ 之后被赋值（测试/配置变更场景）
        if not getattr(self, "_state_file_bound", None) or self._state_file_bound != str(self.state_file):
            self._state = {"_offset": 0}
            self._load_state()
            self._state_file_bound = str(self.state_file)
        report = PushReport()
        processor = getattr(self.container, "processor", None)
        if processor is None:
            self._save_state()
            return report

        # 1) 从 JSONL 读新行
        records = self._read_new_lines()
        report.read = len(records)

        # 2) 从 state 里扫到期的失败项再重推（按 URL 唯一 key）
        now = time.time()
        retry_records: list[dict] = []
        for key, st in list(self._state.items()):
            if key.startswith("_"):
                continue
            next_retry = float(st.get("next_retry", 0) or 0)
            if next_retry > now:
                continue
            retry_records.append({
                "ed2k": key,
                "name": st.get("name") or key,
                "size_bytes": st.get("size_bytes") or 0,
                "_retry": True,
            })
        # 去重：JSONL 新行里有的 URL，优先用新行，不再从 retry 取
        seen_from_new = {r.get("ed2k") for r in records if r.get("ed2k")}
        retry_records = [r for r in retry_records if r["ed2k"] not in seen_from_new]
        all_records = records + retry_records

        if not all_records:
            self._save_state()
            return report

        for rec in all_records:
            url = rec.get("ed2k")
            name = rec.get("name") or url
            if not url:
                logger.warning("ed2k 结果缺 ed2k 字段：%s", rec)
                continue
            st_key = url
            st = self._state.get(st_key)
            if st is not None and float(st.get("next_retry", 0) or 0) > now:
                continue
            if st is None:
                self._state[st_key] = {
                    "failures": 0,
                    "first_seen": now,
                    "name": name,
                    "size_bytes": rec.get("size_bytes") or 0,
                }
            else:
                st.setdefault("name", name)
                st.setdefault("size_bytes", rec.get("size_bytes") or 0)
                st.setdefault("first_seen", now)

            if self.settings.ed2k_push_dry_run:
                report.dry_pushed += 1
                logger.info("[DRY-RUN] 推送 ed2k -> %s", name)
                self._state.pop(st_key, None)
                continue

            try:
                parsed = ParsedShare(provider="ed2k", code=url, password=None)
                res = await processor.process(parsed)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(st_key, name, f"未预期异常: {exc}", report, now)
                continue

            if res.dup:
                report.skipped_dup += 1
                logger.info("ed2k 已推送过，跳过：%s", name)
                self._state.pop(st_key, None)
                continue
            if res.ok:
                report.pushed += 1
                logger.info("ed2k 推送成功：%s", name)
                self._state.pop(st_key, None)
                continue
            self._record_failure(st_key, name, res.message or "推送失败", report, now)

        self._save_state()
        return report

    def _record_failure(self, key: str, name: str, reason: str,
                        report: PushReport, now: float) -> None:
        st = self._state.get(key, {"failures": 0, "first_seen": now})
        st["failures"] += 1
        st["next_retry"] = now + retry_backoff_seconds(st["failures"])
        if now - st.get("first_seen", now) > self.settings.ed2k_push_stuck_days * 86400:
            report.stuck += 1
            logger.warning(
                "ed2k 推送卡死告警：%s 失败 %.1f 天（%s）——建议人工检查",
                name, (now - st["first_seen"]) / 86400, reason,
            )
        self._state[key] = st
        report.failed += 1
        backoff_h = retry_backoff_seconds(st["failures"]) / 3600
        logger.info(
            "ed2k 推送失败 %s（%s）→ %.1fh 后重试（第 %d 次）",
            name, reason, backoff_h, st["failures"],
        )

    # ------------------------------------------------------------------ #
    def status_text(self) -> str:
        """给 /ed2k_status 命令用的状态文本：offset、pending、卡死、最近汇总。"""
        offset = self._state.get("_offset", 0)
        file_size = 0
        try:
            if self.results_file.is_file():
                file_size = self.results_file.stat().st_size
        except OSError:
            pass
        pending = 0
        stuck = 0
        worst_hours = 0.0
        now = _time_now_proxy()
        worst_key = ""
        for key, st in list(self._state.items()):
            if key.startswith("_"):
                continue
            pending += 1
            age_h = max(0.0, (now - float(st.get("first_seen", now))) / 3600.0)
            if age_h > worst_hours:
                worst_hours = age_h
                worst_key = key
            if now - float(st.get("first_seen", now)) > self.settings.ed2k_push_stuck_days * 86400:
                stuck += 1
        progress_pct = (offset / file_size * 100.0) if file_size else 0.0
        lines = [
            "📤 ed2k 推送状态",
            f"• 追读文件：{self.results_file}",
            f"• 进度：{offset}/{file_size} bytes（{progress_pct:.1f}%）",
            f"• Pending：{pending} 条未推/退避中（🚨 卡死 {stuck}）",
            f"• 周期：{self.interval:.0f}s ｜ DRY_RUN：{self.settings.ed2k_push_dry_run}",
        ]
        if worst_key:
            snippet = worst_key if len(worst_key) <= 60 else worst_key[:57] + "..."
            lines.append(f"• 最老待推：{worst_hours:.1f}h 前 {snippet}")
        last = getattr(self, "_last_report", None)
        if last:
            lines.append(f"• 最近一轮：{last}")
        return chr(10).join(lines)

    async def _send_report(self, report: PushReport) -> None:
        """把本轮汇总发 admin / 目标频道（按配置）。发送失败只记日志，不中断循环。"""
        if not (self.settings.ed2k_push_report_admin or self.settings.ed2k_push_report_channel):
            return
        # 有任何有效动作（read/pushed/...）才发；空轮只在有 pending / 卡死时发
        has_action = bool(
            report.read or report.pushed or report.dry_pushed
            or report.skipped_dup or report.failed or report.stuck
        )
        if not has_action and not report.stuck:
            return
        tg = getattr(self.container, "telegram", None)
        if tg is None:
            return
        header = (
            "🔍 [DRY-RUN] ed2k 推送汇总"
            if self.settings.ed2k_push_dry_run
            else "📤 ed2k 推送汇总"
        )
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"<b>{header}</b>  <i>{ts}</i>" + chr(10) + report.summary()
        targets: list[int | str] = []
        if self.settings.ed2k_push_report_admin:
            targets = list(getattr(self.settings, "tg_admin_ids", []) or [])
        if self.settings.ed2k_push_report_channel:
            cid = getattr(self.settings, "tg_chat_id_ed2k", None) or getattr(
                self.settings, "tg_chat_id", None
            )
            if cid:
                targets.append(cid)
        for cid in targets:
            try:
                await tg.send_message(chat_id=cid, text=text, parse_mode="HTML")
            except Exception as exc:  # noqa: BLE001
                logger.warning("ed2k 推送汇总 %s 发送失败：%s", cid, exc)


def _time_now_proxy() -> float:
    import time as _t
    return _t.time()

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info(
            "ed2k 推送启动：追读 %s（%s，每 %.0fs 扫）",
            self.results_file,
            "DRY-RUN 模拟" if self.settings.ed2k_push_dry_run else "实际推送",
            self.interval,
        )
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                self._last_report = report.summary()
                if (report.read or report.pushed or report.dry_pushed
                        or report.skipped_dup or report.failed or report.stuck):
                    logger.info("ed2k 推送：%s", report.summary())
                    await self._send_report(report)
            except Exception as exc:
                logger.error("ed2k 推送轮异常：%s", exc, exc_info=exc)
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
        logger.info("ed2k 推送已停止")
