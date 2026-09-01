"""代理环境变量策略测试：NO_PROXY 保护 115 直连，TMDB 可用系统代理。"""

import os


def test_no_proxy_set_for_115_domains(monkeypatch):
    """setup_proxy_env 后 NO_PROXY 包含 115 域名，且不清除 HTTP_PROXY/HTTPS_PROXY。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    from app.main import setup_proxy_env

    setup_proxy_env()

    no_proxy = os.environ.get("NO_PROXY", "")
    assert "115.com" in no_proxy

    assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7897"
    assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7897"


def test_no_proxy_preserves_existing(monkeypatch):
    """已有 NO_PROXY 时追加 115 域名，不覆盖用户配置。"""
    monkeypatch.setenv("NO_PROXY", "example.com,foo.bar")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")

    from app.main import setup_proxy_env

    setup_proxy_env()

    no_proxy = os.environ.get("NO_PROXY", "")
    assert "example.com" in no_proxy
    assert "foo.bar" in no_proxy
    assert "115.com" in no_proxy
