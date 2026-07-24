"""MediaPush 进程入口。

启动顺序（ARCHITECTURE.md 第 3.2 节）：
  配置 → 日志 → 建表 → 生成密钥 → Container（组装服务）→ bot polling → scheduler → uvicorn serve

关闭顺序（第 3.3 节）：scheduler → bot.stop → reset_all(container.close) → close_db。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.core.config import settings
from app.core.container import Container
from app.core.logging import setup_logging
from app.db.base import async_session, close_db, init_db
from app.scheduler import SchedulerService
from app.web.app import mount_web
from app.web.auth import bootstrap_secrets

# 进程启动第一时间配置日志
setup_logging(log_file=settings.log_file, level="INFO")
logger = logging.getLogger("mediapush")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MediaPush 启动中...")
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    await init_db()
    logger.info("数据库初始化完成")
    await bootstrap_secrets(async_session)

    container = await Container.create()
    app.state.container = container
    await container.start_bot()
    scheduler = SchedulerService(container)
    app.state.scheduler = scheduler
    await scheduler.start()
    yield
    # lifecycle 关闭：scheduler → bot.stop → reset_all(container.close) → close_db
    logger.info("MediaPush 关闭中...")
    await scheduler.stop()
    await container.close()
    await close_db()
    logger.info("MediaPush 已关闭")


app = FastAPI(title="MediaPush", version="0.1.0", lifespan=lifespan)
# Web 管理后台（路由 / 静态 / 鉴权中间件）；构造期挂载，早于任何请求
mount_web(app)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index():
    return {"app": "mediapush", "version": "0.1.0"}

