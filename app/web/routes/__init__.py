"""Web 路由聚合：dashboard / config / tasks / logs / shares / tmdb_cache / auth。

各子模块定义 router，此处合并为 web_router 供 mount_web 挂载。
container / scheduler 通过 request.app.state 访问（lifespan 中设置）。
"""
from fastapi import APIRouter

from app.web.routes import (
    auth as auth_routes,
)
from app.web.routes import (
    config as config_routes,
)
from app.web.routes import (
    dashboard as dashboard_routes,
)
from app.web.routes import (
    logs as logs_routes,
)
from app.web.routes import (
    shares as shares_routes,
)
from app.web.routes import (
    tasks as tasks_routes,
)
from app.web.routes import (
    tmdb_cache as tmdb_cache_routes,
)

web_router = APIRouter()
web_router.include_router(auth_routes.router)
web_router.include_router(dashboard_routes.router)
web_router.include_router(config_routes.router)
web_router.include_router(tasks_routes.router)
web_router.include_router(logs_routes.router)
web_router.include_router(shares_routes.router)
web_router.include_router(tmdb_cache_routes.router)
