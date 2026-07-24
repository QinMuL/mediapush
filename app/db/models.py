"""ORM 模型：share / media / tmdb_cache / task_log / app_config。

字段对应 ARCHITECTURE.md 第 6 节。
"""
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> datetime:
    """当前 UTC 时间（naive，存 SQLite）。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Share(Base):
    __tablename__ = "share"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    share_code: Mapped[str] = mapped_column(String(64))
    share_password: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    # status / create_time 来自 115 接口，必须显式转字符串（旧项目约束）
    status: Mapped[str] = mapped_column(String(64), default="")
    create_time: Mapped[str] = mapped_column(String(64), default="")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    raw_files: Mapped[dict] = mapped_column(JSON, default=dict)
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media.id"), nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    media: Mapped["Media | None"] = relationship(back_populates="shares")


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[str] = mapped_column(String(16), default="tv")
    title: Mapped[str] = mapped_column(String(512), default="")
    original_title: Mapped[str] = mapped_column(String(512), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # total_episodes: 优先 TMDB number_of_episodes，回退文件名（旧项目约束）
    total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[str] = mapped_column(String(32), default="")
    audio: Mapped[str] = mapped_column(String(32), default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    poster_path: Mapped[str] = mapped_column(String(256), default="")

    shares: Mapped[list["Share"]] = relationship(back_populates="media")


class TmdbCache(Base):
    __tablename__ = "tmdb_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[str] = mapped_column(String(16), default="tv")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    # ongoing 决定过期天数：连载中 3 天 / 完结 30 天（旧项目约束）
    ongoing: Mapped[bool] = mapped_column(Boolean, default=False)
    cached_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class TaskLog(Base):
    __tablename__ = "task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32))  # pipeline/full_scan/health/manual
    # running/success/failed/cancelled
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares_new: Mapped[int] = mapped_column(Integer, default=0)
    shares_pushed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(String(16), default="scheduler")


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
