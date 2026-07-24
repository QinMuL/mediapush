"""日志查看：内存环形缓冲双源（决策记录第 5 条）。

读取 logging.memory_handler.recent()，按级别 / 关键词过滤，倒序展示。
"""
import re

from fastapi import APIRouter, Request

from app.core.logging import memory_handler
from app.web.templates import templates

router = APIRouter(prefix="/logs")

_LEVEL_RE = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]")


@router.get("")
async def logs_page(request: Request):
    level = (request.query_params.get("level") or "").upper().strip()
    q = (request.query_params.get("q") or "").strip()
    lines = memory_handler.recent(500)

    def _keep(line: str) -> bool:
        if level:
            m = _LEVEL_RE.search(line)
            if not m or m.group(1) != level:
                return False
        if q and q.lower() not in line.lower():
            return False
        return True

    filtered = [ln for ln in lines if _keep(ln)]
    filtered.reverse()  # 最新在上
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "lines": filtered,
            "level": level,
            "q": q,
            "levels": ["", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        },
    )
