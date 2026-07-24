"""分享列表：搜索 / 推送状态筛选 / 分页。"""
from fastapi import APIRouter, Request

from app.db import repository
from app.web.templates import templates

router = APIRouter(prefix="/shares")
PAGE_SIZE = 30


def _parse_bool(val: str | None) -> bool | None:
    if val == "1" or val == "true":
        return True
    if val == "0" or val == "false":
        return False
    return None


@router.get("")
async def shares_page(request: Request):
    container = request.app.state.container
    q = (request.query_params.get("q") or "").strip()
    pushed = _parse_bool(request.query_params.get("pushed"))
    page = max(1, int(request.query_params.get("page", "1")))
    offset = (page - 1) * PAGE_SIZE

    async with container.session_factory() as session:
        total = await repository.count_shares_filtered(session, q=q, pushed=pushed)
        shares = await repository.list_shares_filtered(
            session, q=q or None, pushed=pushed, limit=PAGE_SIZE, offset=offset
        )

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "shares.html",
        {
            "shares": shares,
            "q": q,
            "pushed": request.query_params.get("pushed", ""),
            "page": page,
            "pages": pages,
            "total": total,
        },
    )
