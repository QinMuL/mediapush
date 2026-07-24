"""TMDB 缓存管理：列表 + 刷新（等价 /refresh <tmdb_id>）。"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.db import repository
from app.web.templates import templates

router = APIRouter(prefix="/tmdb_cache")
PAGE_SIZE = 30


@router.get("")
async def tmdb_cache_page(request: Request):
    container = request.app.state.container
    page = max(1, int(request.query_params.get("page", "1")))
    offset = (page - 1) * PAGE_SIZE
    async with container.session_factory() as session:
        total = await repository.count_tmdb_cache(session)
        caches = await repository.list_tmdb_cache(
            session, limit=PAGE_SIZE, offset=offset
        )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "tmdb_cache.html",
        {
            "caches": caches,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


@router.post("/{tmdb_id}/refresh")
async def tmdb_cache_refresh(request: Request, tmdb_id: int):
    container = request.app.state.container
    await container.refresh_tmdb(tmdb_id)
    return RedirectResponse("/tmdb_cache?ok=refreshed", status_code=303)
