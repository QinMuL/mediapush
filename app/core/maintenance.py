"""一键重置服务（/reset 确认）：停服务 → 清存储 → 重建重启。

保留（配置）：.env、115 cookie 文件、监控频道（monitor.db）、
/dir add 的监控目录（share_dirs 表）。
清空（数据）：TMDB 缓存、推送去重历史、分享登记、
统一状态存储（state.db）、ed2k_results.jsonl、全部日志。

原实现位于 app/telegram/handlers.py 的 _reset_all_data（UI 层下沉至 core）。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ResetService:
    """一键清空业务数据并重启服务。"""

    def __init__(self, container) -> None:
        self.container = container

    async def _stop_quietly(self, svc) -> None:
        """停后台服务：失败只记日志（不阻断清理流程）。"""
        if svc is None:
            return
        try:
            await svc.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("/reset 停止服务 %s 失败：%s", type(svc).__name__, exc)

    async def run(self) -> list[str]:
        """执行清空，返回人可读的摘要行列表。"""
        from app.db.state import StateStore
        from app.logging_config import purge_log_files

        container = self.container
        s = container.settings

        # 1) 停后台服务（必须先停：内存态会把已清空的数据写回 DB）
        for svc in (
            container.pipeline,
            container.share_watcher,
            container.inspector,
        ):
            await self._stop_quietly(svc)

        summary: list[str] = []

        # 2) 清业务数据库（share_dirs 为用户配置，保留）
        if container.cache is not None:
            counts = await container.cache.clear_all()
            summary.append(
                "数据库：tmdb_cache {tmdb_cache} · pushed_shares {pushed_shares} "
                "· shared_items {shared_items} 行已清".format(**counts)
            )

        # 3) 清统一状态存储（data/state.db 全部服务行）
        StateStore(s.state_db_path).clear()
        summary.append(f"状态存储：{s.state_db_path} 已清空")

        # 4) 删散落数据文件（ed2k 结果 JSONL + 旧状态 JSON 残留/迁移备份）
        data_dir = Path(s.state_db_path).resolve().parent
        removed = 0
        for pattern in ("ed2k_results.jsonl", "*_state.json", "*.migrated"):
            for p in data_dir.glob(pattern):
                try:
                    p.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("/reset 删除文件失败 %s：%s", p, exc)
        if removed:
            summary.append(f"数据文件：{removed} 个已删除（ed2k 结果/旧状态残留）")

        # 5) 清日志（当前 + 全部归档，随后重建 handler）
        removed_logs = purge_log_files(
            s.log_level,
            use_color=s.log_color,
            log_file=s.log_file,
            log_media_file=s.log_media_file,
            log_max_bytes=s.log_max_bytes,
            log_retention_days=s.log_retention_days,
        )
        summary.append(f"日志：{len(removed_logs)} 个文件已清空（含归档）")

        # 6) 重建统一流水线（全新实例 = 全新内存态）并重启
        restarted: list[str] = []
        if s.pipeline_enabled:
            from app.pipeline.service import PipelineService

            container.pipeline = PipelineService(container, s)
            await container.pipeline.start()
            restarted.append("媒体流水线")
        # 巡检/目录监控无内存态缓存，直接重启原实例
        if container.inspector is not None:
            await container.inspector.start()
            restarted.append("分享巡检")
        if container.share_watcher is not None:
            await container.share_watcher.start()
            restarted.append("目录监控")
        summary.append("已重启：" + " · ".join(restarted))
        logger.warning("/reset 数据清空完成：%s", "；".join(summary))
        return summary
