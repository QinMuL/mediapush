"""Web 管理后台测试：鉴权、中间件、dashboard/config/tasks/logs/shares/tmdb_cache。

用真实文件 sqlite（factory）+ FakeContainer（mock 服务方法）+ httpx ASGITransport。
ASGITransport 不跑 lifespan，故手动建表、写配置、设置 app.state。
"""
import tempfile
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models, repository
from app.db.base import Base
from app.web.app import mount_web
from app.web.auth import (
    SESSION_COOKIE,
    bootstrap_secrets,
    create_session_token,
    is_authed,
    verify_password,
    verify_session_token,
)
from app.web.routes.config import CONFIG_FIELDS


# ---- 公共 Fake 组件 ----
class FakeScheduler:
    def __init__(self):
        self.update_calls: list[int] = []
        self.last_pipeline_execution = None

    async def update_interval(self, n):
        self.update_calls.append(n)
        return (max(1, round(120 / n)), min(max(n * 60, 180), 900))


class FakeContainer:
    """真实 session_factory + 可 mock 的服务方法。"""

    def __init__(self, factory):
        self.session_factory = factory
        self._pipeline_running = False
        self._monitored = [("c1", "p1")]
        self.run_calls: list = []
        self.refresh_calls: list = []
        self.on_config_calls: list = []
        self.stop_called = False

    def is_pipeline_running(self):
        return self._pipeline_running

    def stop_pipeline(self):
        self.stop_called = True

    async def get_monitored_shares(self):
        return self._monitored

    async def run_pipeline_once(self, codes, trigger="manual"):
        self.run_calls.append((list(codes), trigger))
        return {"new": 1, "pushed": 1, "existing": 0}

    async def refresh_tmdb(self, tmdb_id):
        self.refresh_calls.append(tmdb_id)
        return 1

    async def on_config_changed(self, key):
        self.on_config_calls.append(key)

    async def get_status(self):
        from sqlalchemy import func, select

        async with self.session_factory() as s:
            unpushed = (
                await s.execute(
                    select(func.count())
                    .select_from(models.Share)
                    .where(models.Share.pushed.is_(False))
                )
            ).scalar() or 0
            cfg = await repository.get_all_config(s)
        ch = {
            "tg_token": bool(cfg.get("tg_bot_token")),
            "pan115_cookie": bool(cfg.get("pan115_cookie")),
            "tmdb_key": bool(cfg.get("tmdb_api_key")),
            "proxy": (cfg.get("proxy_enabled") or "").lower() in {"1", "true", "yes", "on"},
            "schedule_interval": cfg.get("schedule_interval", "?"),
        }
        return {
            "bot_running": False,
            "pipeline_running": self._pipeline_running,
            "unpushed": unpushed,
            "config_health": ch,
        }


# ---- fixtures ----
@pytest.fixture
async def factory():
    db_path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        await repository.ensure_default_config(s)
        await repository.set_config(s, "admin_password", "testpass")
        await repository.set_config(s, "web_secret", "test-secret-key")
    yield f
    await engine.dispose()


async def _build(factory, container=None, scheduler=None):
    app = FastAPI()
    mount_web(app)

    # 复刻 main.py 的公开 JSON 端点，供公开路径测试
    @app.get("/health")
    async def _health():
        return {"status": "ok"}

    @app.get("/")
    async def _index():
        return {"app": "mediapush", "version": "0.1.0"}

    app.state.container = container or FakeContainer(factory)
    app.state.scheduler = scheduler or FakeScheduler()
    return app


async def _client(app, cookie=None):
    transport = ASGITransport(app=app)
    headers = {}
    if cookie:
        # 直接走 Cookie 头，规避 httpx 在 ASGITransport 下的 cookie 域匹配问题
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def _auth_cookie(factory):
    return await create_session_token(factory, {"authed": True})


def _build_form(cfg, **overrides):
    form = {}
    for key, _label, ftype, _group, _secret in CONFIG_FIELDS:
        if key in overrides:
            form[key] = overrides[key]
            continue
        val = cfg.get(key, "")
        form[key] = "on" if (ftype == "checkbox" and val == "true") else val
    return form


# ============ auth 单元 ============
async def test_bootstrap_secrets_generates_when_empty(factory):
    async with factory() as s:
        await repository.set_config(s, "admin_password", "")
        await repository.set_config(s, "web_secret", "")
    await bootstrap_secrets(factory)
    async with factory() as s:
        assert await repository.get_config(s, "admin_password", "") != ""
        assert await repository.get_config(s, "web_secret", "") != ""


async def test_bootstrap_secrets_idempotent(factory):
    await bootstrap_secrets(factory)
    async with factory() as s:
        pw1 = await repository.get_config(s, "admin_password")
    await bootstrap_secrets(factory)
    async with factory() as s:
        pw2 = await repository.get_config(s, "admin_password")
    assert pw1 == pw2  # 已存在不覆盖


async def test_verify_password(factory):
    assert await verify_password(factory, "testpass") is True
    assert await verify_password(factory, "wrong") is False


async def test_session_token_roundtrip(factory):
    token = await create_session_token(factory, {"authed": True})
    assert await is_authed(factory, token) is True
    assert await is_authed(factory, None) is False
    assert await verify_session_token(factory, "garbage") is None


async def test_session_token_rejects_wrong_secret(factory):
    token = await create_session_token(factory, {"authed": True})
    async with factory() as s:
        await repository.set_config(s, "web_secret", "changed-secret")
    assert await verify_session_token(factory, token) is None  # 密钥变更失效


# ============ 中间件 ============
async def test_public_paths_no_auth(factory):
    app = await _build(factory)
    async with await _client(app) as c:
        r = await c.get("/health")
        assert r.status_code == 200
        r = await c.get("/")
        assert r.status_code == 200
        r = await c.get("/login")
        assert r.status_code == 200


async def test_protected_redirects_when_unauthed(factory):
    app = await _build(factory)
    async with await _client(app) as c:
        r = await c.get("/dashboard", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


async def test_protected_ok_when_authed(factory):
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/dashboard")
        assert r.status_code == 200
        assert "仪表盘" in r.text


# ============ 登录 ============
async def test_login_wrong_password(factory):
    app = await _build(factory)
    async with await _client(app) as c:
        r = await c.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert r.status_code == 401
        assert "密码错误" in r.text


async def test_login_correct_sets_cookie(factory):
    app = await _build(factory)
    async with await _client(app) as c:
        r = await c.post(
            "/login", data={"password": "testpass"}, follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard"
        assert SESSION_COOKIE in r.cookies


async def test_logout_clears_cookie(factory):
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.post("/logout", follow_redirects=False)
        assert r.status_code == 303


# ============ dashboard ============
async def test_dashboard_shows_stats(factory):
    async with factory() as s:
        await repository.add_share(
            s, models.Share(share_code="A1", share_password="", title="Show A", pushed=True)
        )
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/dashboard")
        assert r.status_code == 200
        assert "Show A" not in r.text  # 仪表盘不列分享
        assert "今日新增" in r.text


# ============ config ============
async def test_config_page_renders(factory):
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/config")
        assert r.status_code == 200
        assert "配置管理" in r.text
        assert "调度间隔" in r.text


async def test_config_save_schedule_interval_triggers_update(factory):
    sched = FakeScheduler()
    app = await _build(factory, scheduler=sched)
    cookie = await _auth_cookie(factory)
    async with factory() as s:
        cfg = await repository.get_all_config(s)
    form = _build_form(cfg, schedule_interval="10")
    async with await _client(app, cookie=cookie) as c:
        r = await c.post("/config", data=form, follow_redirects=False)
        assert r.status_code == 200
        assert "间隔已联动" in r.text
    assert sched.update_calls == [10]
    async with factory() as s:
        assert await repository.get_config(s, "schedule_interval") == "10"


async def test_config_secret_empty_no_change(factory):
    cont = FakeContainer(factory)
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with factory() as s:
        await repository.set_config(s, "pan115_cookie", "UID=1; CID=2;")
        cfg = await repository.get_all_config(s)
    # 不带 pan115_cookie（build_form 用当前值 → new==old 跳过）
    form = _build_form(cfg)
    async with await _client(app, cookie=cookie) as c:
        await c.post("/config", data=form)
    assert "pan115_cookie" not in cont.on_config_calls  # 未变更不分发
    async with factory() as s:
        assert await repository.get_config(s, "pan115_cookie") == "UID=1; CID=2;"


async def test_config_secret_change_triggers_rebuild(factory):
    cont = FakeContainer(factory)
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with factory() as s:
        cfg = await repository.get_all_config(s)
    form = _build_form(cfg, pan115_cookie="UID=9; CID=9;")
    async with await _client(app, cookie=cookie) as c:
        await c.post("/config", data=form)
    assert "pan115_cookie" in cont.on_config_calls
    async with factory() as s:
        assert await repository.get_config(s, "pan115_cookie") == "UID=9; CID=9;"


# ============ tasks ============
async def test_tasks_page(factory):
    async with factory() as s:
        await repository.create_task_log(
            s, models.TaskLog(task_type="pipeline", status="success", trigger="manual")
        )
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/tasks")
        assert r.status_code == 200
        assert "手动触发" in r.text


async def test_tasks_trigger_running_redirects(factory):
    cont = FakeContainer(factory)
    cont._pipeline_running = True
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.post("/tasks/trigger", follow_redirects=False)
        assert r.status_code == 303
        assert "err=running" in r.headers["location"]
    assert cont.run_calls == []


async def test_tasks_trigger_no_shares(factory):
    cont = FakeContainer(factory)
    cont._monitored = []
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.post("/tasks/trigger", follow_redirects=False)
        assert r.status_code == 303
        assert "err=no_shares" in r.headers["location"]


async def test_tasks_stop(factory):
    cont = FakeContainer(factory)
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.post("/tasks/stop", follow_redirects=False)
        assert r.status_code == 303
    assert cont.stop_called is True


# ============ logs ============
async def test_logs_page(factory):
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/logs")
        assert r.status_code == 200
        assert "日志" in r.text
        r2 = await c.get("/logs", params={"level": "ERROR"})
        assert r2.status_code == 200


# ============ shares ============
async def test_shares_page_filter(factory):
    async with factory() as s:
        await repository.add_share(
            s, models.Share(share_code="CODE1", share_password="", title="Dune", pushed=True)
        )
        await repository.add_share(
            s, models.Share(share_code="CODE2", share_password="", title="Matrix", pushed=False)
        )
    app = await _build(factory)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/shares", params={"q": "Dune"})
        assert r.status_code == 200
        assert "Dune" in r.text
        assert "Matrix" not in r.text
        r2 = await c.get("/shares", params={"pushed": "0"})
        assert "Matrix" in r2.text


# ============ tmdb_cache ============
async def test_tmdb_cache_page_and_refresh(factory):
    from app.db.models import _now

    async with factory() as s:
        s.add(models.TmdbCache(
            tmdb_id=123, media_type="tv", data={"name": "Test"},
            ongoing=True, cached_at=_now(), expires_at=_now() + timedelta(days=3),
        ))
        await s.commit()
    cont = FakeContainer(factory)
    app = await _build(factory, container=cont)
    cookie = await _auth_cookie(factory)
    async with await _client(app, cookie=cookie) as c:
        r = await c.get("/tmdb_cache")
        assert r.status_code == 200
        assert "123" in r.text
        r2 = await c.post("/tmdb_cache/123/refresh", follow_redirects=False)
        assert r2.status_code == 303
    assert cont.refresh_calls == [123]
