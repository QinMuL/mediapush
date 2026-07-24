"""任务历史 + 手动触发 / 停止。

手动触发与定时任务共享 is_running 互斥（决策记录第 4 条）。
触发为后台 fire-and-forget（流水线耗时较长，不阻塞响应）。
"""
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.db import repository
from app.web.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks")


@router.get("")
async def tasks_page(request: Request):
    container = request.app.state.container
    page = max(1, int(request.query_params.get("page", "1")))
    page_size = 30
    async with container.session_factory() as session:
        logs = await repository.list_task_logs(session, limit=page_size * page)
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "logs": logs,
            "page": page,
            "pipeline_running": container.is_pipeline_running(),
        },
    )


async def _run_pipeline_bg(container, codes):
    try:
        result = await container.run_pipeline_once(codes, trigger="manual")
        logger.info(
            "手动触发 pipeline 完成：new=%s pushed=%s existing=%s",
            result.get("new"), result.get("pushed"), result.get("existing"),
        )
    except Exception:  # noqa: BLE001  后台任务异常不应影响 Web
        logger.exception("手动触发 pipeline 异常")


@router.post("/trigger")
async def tasks_trigger(request: Request):
    container = request.app.state.container
    if container.is_pipeline_running():
        return RedirectResponse("/tasks?err=running", status_code=303)
    codes = await container.get_monitored_shares()
    if not codes:
        return RedirectResponse("/tasks?err=no_shares", status_code=303)
    asyncio.create_task(_run_pipeline_bg(container, codes))
    return RedirectResponse("/tasks?ok=triggered", status_code=303)


@router.post("/stop")
async def tasks_stop(request: Request):
    container = request.app.state.container
    container.stop_pipeline()
    return RedirectResponse("/tasks?ok=stopped", status_code=303)
