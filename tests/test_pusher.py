"""pusher 卡片渲染测试（纯函数，无 IO）。"""
from app.db import models
from app.telegram.pusher import (
    Pusher,
    poster_url,
    render_caption,
    render_text,
)


def _media(**kw):
    """构造 Media 实例（不入库，仅读属性）。"""
    base = dict(
        id=1, tmdb_id=42, media_type="tv", title="测试剧", original_title="Test",
        year=2024, season=1, episode_start=2, episode_end=5, total_episodes=12,
        quality="1080p", audio="AAC2.0", overview="这是剧情简介。" * 5,
        poster_path="/abc.png",
    )
    base.update(kw)
    return models.Media(**base)


def _share(**kw):
    base = dict(
        id=1, share_code="abc12345", share_password="pwd123",
        title="测试剧", status="", create_time="",
        file_count=4, size=1000,
    )
    base.update(kw)
    return models.Share(**base)


# ---- 完整文本卡片 ----
def test_render_text_tv_with_episodes():
    text = render_text(_share(), _media())
    assert "<b>测试剧</b>" in text
    assert "(2024)" in text
    assert "剧集" in text
    assert "第 1 季 E02-05" in text
    assert "总集数</b>：12" in text
    assert "1080p" in text and "AAC2.0" in text
    assert "115.com/s/abc12345" in text
    assert "<code>pwd123</code>" in text
    assert "文件数</b>：4" in text
    assert "<blockquote>" in text


def test_render_text_movie():
    media = _media(media_type="movie", season=None, episode_start=None,
                   episode_end=None, total_episodes=None, title="测试电影")
    text = render_text(_share(title="测试电影"), media)
    assert "电影" in text
    # 电影无季集信息
    assert "集数</b>" not in text
    assert "总集数" not in text


def test_render_text_whole_season():
    media = _media(episode_start=None, episode_end=None, season=2)
    text = render_text(_share(), media)
    assert "第 2 季 全季" in text


def test_render_text_no_media():
    text = render_text(_share(), None)
    # 无 media 时用 share.title
    assert "<b>测试剧</b>" in text
    assert "115.com/s/abc12345" in text
    assert "规格" not in text


def test_render_text_no_password():
    text = render_text(_share(share_password=""), _media())
    assert "访问码" not in text


def test_render_text_overview_truncated():
    long_ov = "长" * 800
    media = _media(overview=long_ov)
    text = render_text(_share(), media)
    assert "…" in text
    assert len(text) <= 4096


def test_render_text_html_escaped():
    media = _media(title="<script>x</script>")
    text = render_text(_share(), media)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# ---- 紧凑 caption ----
def test_render_caption_compact():
    cap = render_caption(_share(), _media())
    assert "<b>测试剧</b>" in cap
    assert "第 1 季 E02-05" in cap
    assert "115.com/s/abc12345" in cap
    assert "<blockquote>" not in cap  # caption 无简介
    assert len(cap) <= 1024


def test_render_caption_under_limit():
    media = _media(overview="x" * 2000)
    cap = render_caption(_share(), media)
    assert len(cap) <= 1024


# ---- poster url ----
def test_poster_url_present():
    assert poster_url(_media()) == "https://image.tmdb.org/t/p/w500/abc.png"


def test_poster_url_absent():
    assert poster_url(None) is None
    assert poster_url(_media(poster_path="")) is None


# ---- push_share IO（mock telegram_service）----
async def test_push_share_with_poster():
    sent = {}

    class FakeTG:
        async def send_photo(self, chat_id, photo, caption):
            sent["photo"] = photo
            sent["caption"] = caption
            sent["chat"] = chat_id

        async def send_message(self, chat_id, text):
            sent["text"] = text

    p = Pusher(FakeTG(), "chat123")
    ok = await p.push_share(_share(), _media())
    assert ok is True
    assert sent["chat"] == "chat123"
    assert "image.tmdb.org" in sent["photo"]
    assert "<b>测试剧</b>" in sent["caption"]


async def test_push_share_without_poster():
    sent = {}

    class FakeTG:
        async def send_photo(self, chat_id, photo, caption):
            sent["photo"] = caption

        async def send_message(self, chat_id, text):
            sent["text"] = text

    p = Pusher(FakeTG(), "chat123")
    ok = await p.push_share(_share(), _media(poster_path=""))
    assert ok is True
    assert "text" in sent and "photo" not in sent


async def test_push_share_failure_returns_false():
    class FakeTG:
        async def send_photo(self, *a, **k):
            raise RuntimeError("network")

        async def send_message(self, *a, **k):
            raise RuntimeError("network")

    p = Pusher(FakeTG(), "chat123")
    ok = await p.push_share(_share(), _media())
    assert ok is False  # 失败不抛，返回 False
