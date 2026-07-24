"""启动配置：仅从环境变量加载 Web 进程启动必需的最小集。

其余业务配置（TG token、115 cookie、TMDB key、代理、调度参数等）
全部在 Web 管理后台维护、持久化在 DB app_config 表。
详见 ARCHITECTURE.md 第 9 节。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False)

    # 仅最小启动集（docker-compose.yml environment 注入，非敏感）
    database_url: str = "sqlite+aiosqlite:///data/mediapush.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8088
    # 日志文件路径（固定，可 env 覆盖）
    log_file: str = "data/mediapush.log"


settings = Settings()
