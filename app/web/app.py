"""FastAPI Web 装配：挂载路由 / 静态资源 / 鉴权中间件。

mount_web 在 app 构造期（main.py 模块级）调用，早于任何请求；
鉴权中间件在请求期读取 request.app.state.container（lifespan 中设置），
无需在 lifespan 注册中间件，规避 Starlette 中间件栈时序问题。
"""
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.web.auth import SESSION_COOKIE, is_authed
from app.web.routes import web_router

logger = logging.getLogger(__name__)

# 公开路径（无需登录）
_PUBLIC_PATHS = {"/login", "/health", "/"}
_STATIC_PREFIX = "/static/"


def mount_web(app: FastAPI) -> None:
    app.include_router(web_router)

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_STATIC_PREFIX):
            return await call_next(request)

        container = getattr(request.app.state, "container", None)
        # container 未就绪（启动早期）放行，由具体路由处理缺省
        if container is None:
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if await is_authed(container.session_factory, token):
            return await call_next(request)

        # 浏览器请求重定向到登录页；非 HTML（API）返回 401
        accept = request.headers.get("accept", "")
        if "text/html" in accept or request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "未登录"}, status_code=401)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request, exc):  # noqa: BLE001
        # HTTPException 有专属处理器，不会被此处捕获
        logger.exception("Web 处理异常: %s %s", request.method, request.url.path)
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(
                "<h1>服务器内部错误</h1><p>请查看日志页了解详情。</p>",
                status_code=500,
            )
        return JSONResponse({"detail": "服务器内部错误"}, status_code=500)
