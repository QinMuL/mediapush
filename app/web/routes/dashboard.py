"""仪表盘：今日推送/新增、最近任务、健康状态、调度信息。"""
from fastapi import APIRouter, Request

from app.db import repository
from app.web.auth import now_utc_naive
from app.web.templates import templates

router = APIRouter()


@router.get("/dashboard")
async def dashboard(request: Request):
    container = request.app.state.container
    scheduler = getattr(request.app.state, "scheduler", None)

    status = await container.get_status()

    today = now_utc_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    async with container.session_factory() as session:
        new_today = await repository.count_shares_since(session, "created_at", today)
        pushed_today = await repository.count_shares_since(
            session, "pushed_at", today
        )
        recent_tasks = await repository.list_task_logs(session, limit=10)

    last_run = None
    if scheduler is not None and scheduler.last_pipeline_execution is not None:
        last_run = scheduler.last_pipeline_execution.strftime("%Y-%m-%d %H:%M:%S")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "status": status,
            "new_today": new_today,
            "pushed_today": pushed_today,
            "recent_tasks": recent_tasks,
            "last_run": last_run,
        },
    )
