"""Container.reload_config / refresh_cookie_file 测试：/reload 热加载语义。"""

from app.config import Settings
from app.core.container import Container
from app.core.rate_limiter import AdaptiveLimiter


class _FakePan115:
    def __init__(self, cookie=""):
        self.cookie = cookie
        self.updated: list = []

    def update_cookie(self, cookie):
        self.updated.append(cookie)
        self.cookie = cookie


class _FakeInspector:
    def __init__(self):
        self.interval = 6.0


# ---------------------- refresh_cookie_file ---------------------- #
def test_refresh_cookie_file_updates_on_change(tmp_path):
    f = tmp_path / "cookie.txt"
    f.write_text("UID=9;CID=8;", encoding="utf-8")
    pan115 = _FakePan115("UID=1;CID=2;")
    c = Container(Settings())
    c.pan115 = pan115
    c.settings.pan115_cookie_file = str(f)

    assert c.refresh_cookie_file() is True
    assert pan115.cookie == "UID=9;CID=8;"

    # 内容未变 → 不重复更新
    assert c.refresh_cookie_file() is False
    assert len(pan115.updated) == 1


def test_refresh_cookie_file_no_file_or_empty(tmp_path):
    c = Container(Settings())
    c.pan115 = _FakePan115()
    c.settings.pan115_cookie_file = ""  # 未配置
    assert c.refresh_cookie_file() is False

    empty = tmp_path / "empty.txt"
    empty.write_text("   ", encoding="utf-8")
    c.settings.pan115_cookie_file = str(empty)
    assert c.refresh_cookie_file() is False  # 空内容不动


# ---------------------- reload_config ---------------------- #
def test_reload_config_hot_and_restart_split():
    old = Settings()
    new = Settings()
    new.inspect_interval_hours = 3.0          # 热加载（含服务缓存同步）
    new.share_watch_interval_minutes = 5.0    # 热加载
    new.pan115_request_interval = 2.5         # 热加载（限速器）
    new.tg_bot_token = "abc"                 # 需重启
    new.tmdb_api_key = "k"                    # 需重启

    c = Container(old)
    c.inspector = _FakeInspector()
    c.pan115_limiter = AdaptiveLimiter(1.0)

    hot, restart = c.reload_config(new)

    assert set(hot) == {
        "inspect_interval_hours", "share_watch_interval_minutes", "pan115_request_interval"
    }
    assert set(restart) == {"tg_bot_token", "tmdb_api_key"}

    # settings 原地更新（服务持同一引用即刻可见）
    assert old is c.settings
    assert old.inspect_interval_hours == 3.0
    # 需重启项不落地（保持运行现状，等容器重启由新配置接管）
    assert old.tg_bot_token == ""
    assert old.tmdb_api_key == ""

    # 服务缓存派生值同步
    assert c.inspector.interval == 3.0
    assert c.pan115_limiter.base_interval == 2.5


def test_reload_config_no_change():
    s = Settings()
    c = Container(s)
    c.pan115 = None
    hot, restart = c.reload_config(Settings())
    assert hot == []
    assert restart == []


def test_reload_config_env_cookie_hot_updates_provider():
    """PAN115_COOKIE 直配变化 → provider.update_cookie 同步（client 重置）。"""
    old = Settings()
    new = Settings()
    new.pan115_cookie = "UID=9;CID=8;SEID=x"

    c = Container(old)
    c.pan115 = _FakePan115("UID=1;CID=2;")

    hot, _restart = c.reload_config(new)

    assert "pan115_cookie" in hot
    assert c.pan115.cookie == "UID=9;CID=8;SEID=x"
    assert old.pan115_cookie == "UID=9;CID=8;SEID=x"
