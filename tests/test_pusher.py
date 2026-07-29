"""Pusher 渲染测试（单消息：caption 智能截断）。

- render_caption：海报下方 ≤1024，含标题区+文件清单模块+简介模块+链接，超限智能截断
- render_text：无海报完整版 ≤4096
"""

from app.parser.media_parser import AggregatedMedia
from app.providers.base import ShareFile
from app.telegram.pusher import (
    _format_ranges,
    _human_size,
    _merge_ranges,
    _quality_line,
    _render_files_block,
    _render_footer,
    _render_head,
    _render_quality_block,
    _render_season_block,
    _share_url,
    _tmdb_url,
    render_caption,
    render_text,
)


def _tv_details():
    return {
        "tmdb_id": 100,
        "media_type": "tv",
        "title": "三体",
        "original_title": "三体",
        "year": 2023,
        "release_date": "2023-01-15",
        "overview": "纳米材料科学家汪淼。" * 30,
        "poster_path": "/abc.jpg",
        "vote_average": 8.2,
        "vote_count": 1234,
        "status": "Ended",
        "genres": ["科幻", "剧情"],
        "number_of_seasons": 1,
        "number_of_episodes": 30,
        "seasons": [{"season": 1, "episode_count": 30, "name": "第一季"}],
        "cast": ["张鲁一", "于和伟"],
        "creators": ["滕华涛"],
        "countries": ["中国"],
    }


def _tv_media():
    return AggregatedMedia(
        title="三体", year=2023, media_type="tv", season=1, episode_start=1,
        episode_end=12, quality="4K / 2160P", source="WEB-DL",
        hdr="Dolby Vision", file_count=12, total_episodes=12,
    )


def _sample_files(n=4):
    files = [ShareFile(name="Season 01", is_dir=True)]
    files += [
        ShareFile(name=f"Three.Body.S01E0{i}.2160p.WEB-DL.mkv", size=8_000_000_000)
        for i in range(1, n)
    ]
    files.append(ShareFile(name="海报.jpg", size=1_200_000))
    return files


def _many_files(n=30):
    """构造 n 个文件，模拟大分享。"""
    return [
        ShareFile(
            name=f"Show.S01E{i:02d}.2160p.WEB-DL.DDP5.1.HDR10.mkv",
            size=8_000_000_000,
        )
        for i in range(1, n + 1)
    ]


# -------------------- caption 基础 -------------------- #
def test_caption_within_limit():
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert len(cap) <= 1024


def test_caption_has_files_block():
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert "<blockquote expandable>📁 分享内容（5 项）" in cap
    assert "</blockquote>" in cap
    assert "Three.Body.S01E01" in cap


def test_caption_has_overview_block():
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert "<blockquote expandable>📝 简介" in cap
    assert "纳米材料科学家汪淼" in cap


def test_caption_has_links():
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert "https://115.com/s/sw8k9m2?password=ab12" in cap
    assert "ab12" in cap
    # TMDB 详情改为卡片下方 inline button，不在 caption 文本中
    assert "themoviedb.org" not in cap


def test_caption_includes_password():
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert "ab12" in cap


def test_caption_html_escaping():
    details = _tv_details()
    details["title"] = "<script>x</script>"
    cap = render_caption(details, _tv_media(), "code1234", None, _sample_files())
    assert "<script>" not in cap
    assert "&lt;script&gt;" in cap


def test_caption_filename_escaping():
    """文件名含 HTML 特殊字符需转义。"""
    files = [ShareFile(name="<evil>.mkv", size=100)]
    cap = render_caption(_tv_details(), _tv_media(), "code1234", None, files)
    assert "&lt;evil&gt;.mkv" in cap


def test_caption_movie_label():
    """电影类型 caption 用「上映」+ 时长。"""
    details = {
        "tmdb_id": 1, "media_type": "movie", "title": "X", "year": 2000,
        "release_date": "2000-01-01", "overview": "", "poster_path": None,
        "vote_average": 7.0, "vote_count": 10, "status": "Released",
        "genres": ["动作"], "runtime": 120, "cast": [], "directors": [],
        "countries": [],
    }
    media = AggregatedMedia(title="X", year=2000, media_type="movie", quality="1080P")
    cap = render_caption(details, media, "abc12345", None, None)
    assert "120 分钟" in cap
    assert "上映" in cap


# -------------------- 智能截断 -------------------- #
def test_caption_truncates_when_many_files():
    """30 个文件 + 长简介：caption 仍 ≤1024。"""
    files = _many_files(30)
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", files)
    assert len(cap) <= 1024, f"caption 长度 {len(cap)} > 1024"
    # 应出现截断提示
    assert "已显示前" in cap
    # 标题区始终保留
    assert "三体" in cap
    # 115 链接始终保留（TMDB 改为按钮，不在文本中）
    assert "115.com" in cap
    assert "themoviedb.org" not in cap


def test_caption_truncates_overview_first():
    """超限时先截简介（简介应被截断，文件清单尽量全）。"""
    files = _sample_files(4)
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", files)
    # 4 个文件少，应能全部显示（无文件截断提示）
    assert "已显示前" not in cap or "已显示前 4 项" not in cap


def test_caption_no_files_still_has_overview():
    """无文件清单时，caption 仍含简介模块。"""
    cap = render_caption(_tv_details(), _tv_media(), "abc12345", None, None)
    assert "<blockquote expandable>📝 简介" in cap
    assert "分享内容" not in cap


def test_caption_always_has_head_and_footer():
    """无论内容多少，标题区和链接始终保留。"""
    files = _many_files(50)
    cap = render_caption(_tv_details(), _tv_media(), "sw8k9m2", "ab12", files)
    assert len(cap) <= 1024
    assert "三体" in cap  # 标题
    assert "8.2" in cap  # 评分
    assert "115.com/s/sw8k9m2" in cap  # 115 链接
    assert "themoviedb.org" not in cap  # TMDB 改为按钮，不在文本中


# -------------------- text（无海报完整版） -------------------- #
def test_text_within_limit():
    txt = render_text(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert len(txt) <= 4096


def test_text_has_all_blocks():
    """无海报完整版含标题区+文件清单+简介+链接。"""
    txt = render_text(_tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files())
    assert "三体" in txt  # 标题
    assert "<blockquote expandable>📁 分享内容" in txt
    assert "<blockquote expandable>📝 简介" in txt
    assert "115.com" in txt
    # TMDB 详情改为卡片下方 inline button，不在 text 文本中
    assert "themoviedb.org" not in txt


def test_text_no_password_when_absent():
    txt = render_text(_tv_details(), _tv_media(), "abc12345", None, _sample_files())
    assert "访问码" not in txt
    assert "https://115.com/s/abc12345" in txt


def test_text_html_escaping_in_filename():
    files = [ShareFile(name="<evil>.mkv", size=100)]
    txt = render_text(_tv_details(), _tv_media(), "code1234", None, files)
    assert "&lt;evil&gt;.mkv" in txt


# -------------------- 辅助函数 -------------------- #
def test_tmdb_url_tv():
    assert _tmdb_url({"media_type": "tv", "tmdb_id": 5}) == "https://www.themoviedb.org/tv/5"


def test_share_url():
    assert _share_url("abc", None) == "https://115.com/s/abc"
    assert _share_url("abc", "p") == "https://115.com/s/abc?password=p"


def test_quality_line():
    m = AggregatedMedia(quality="4K / 2160P", hdr="HDR10", source="BluRay")
    assert _quality_line(m) == "4K / 2160P HDR10 BluRay"


def test_season_block_single_season():
    """单季 TV：🗂️ 季集行 + 📋 集数行 + ⚙️ 状态行。"""
    media = AggregatedMedia(
        title="兵自风中来", year=2026, media_type="tv", season=1,
        episode_start=1, episode_end=4, quality="4K / 2160P",
        source="WEB-DL", hdr="HDR10", file_count=4,
    )
    details = {
        "media_type": "tv", "status": "Returning Series",
        "seasons": [{"season": 1, "episode_count": 20}],
    }
    block = _render_season_block(details, media)
    assert "<blockquote>" in block
    assert "🗂️ 季集：S01 共20集（4个文件）" in block
    assert "📋 集数：S01 E01-E04" in block
    assert "⚙️ 状态：连载中" in block


def test_season_block_multi_season_file_named():
    """多季 TV（文件名有多季）：按季分组渲染集数范围。"""
    media = AggregatedMedia(
        title="女清洁工", year=2022, media_type="tv", season=4,
        seasons=[1, 2, 3, 4],
        season_episodes={1: [1, 2, 3], 2: [1, 2, 3], 3: [1, 2, 3], 4: [1, 2, 3]},
        episode_start=1, episode_end=3,
        quality="1080P", source="WEB-DL", hdr="SDR", file_count=12,
    )
    details = {
        "media_type": "tv", "number_of_seasons": 4, "number_of_episodes": 46,
        "seasons": [{"season": 4, "episode_count": 12}],
    }
    block = _render_season_block(details, media)
    assert "🗂️ 季集：S01-S04 共46集（12个文件）" in block
    assert "S01 E01-E03" in block
    assert "S02 E01-E03" in block
    assert " | " in block  # 季间分隔符


def test_season_block_one_piece_reallocate():
    """航海王场景：文件名只有 S01Exxx（全局集号），但 TMDB 有多季。
    用 TMDB 季范围重新分配集号到对应季。
    """
    # 文件名解析出 season=1, episode=1..100（全局集号）
    media = AggregatedMedia(
        title="航海王", year=1999, media_type="tv", season=1,
        seasons=[1, 2, 3],
        season_episodes={1: list(range(1, 101))},  # 100 集，全标为 S01
        episode_start=1, episode_end=100,
        quality="4K / 2160P", source="WEB-DL", file_count=100,
    )
    # TMDB: S01=61集, S02=16集(E62-E77), S03=23集(E78-E100)
    details = {
        "media_type": "tv", "number_of_seasons": 3, "number_of_episodes": 100,
        "seasons": [
            {"season": 1, "episode_count": 61},
            {"season": 2, "episode_count": 16},
            {"season": 3, "episode_count": 23},
        ],
    }
    block = _render_season_block(details, media)
    assert "🗂️ 季集：S01-S03 共100集（100个文件）" in block
    # 集数行应按 TMDB 季重新分配
    assert "S01 E01-E61" in block
    assert "S02 E62-E77" in block
    assert "S03 E78-E100" in block


def test_season_block_missing_episodes():
    """缺集场景：用顿号分隔不连续的集号。"""
    media = AggregatedMedia(
        title="X", year=2026, media_type="tv", season=1,
        seasons=[1],
        season_episodes={1: [2, 3, 4, 5, 7, 10, 11, 12]},
        episode_start=2, episode_end=12,
        file_count=8,
    )
    details = {"media_type": "tv", "seasons": [{"season": 1, "episode_count": 12}]}
    block = _render_season_block(details, media)
    assert "S01 E02-E05、E07、E10-E12" in block


def test_merge_ranges():
    assert _merge_ranges([]) == []
    assert _merge_ranges([1, 2, 3]) == [(1, 3)]
    assert _merge_ranges([1, 3, 5]) == [(1, 1), (3, 3), (5, 5)]
    assert _merge_ranges([2, 3, 4, 7, 10, 11, 12]) == [(2, 4), (7, 7), (10, 12)]
    assert _format_ranges([(2, 4), (7, 7), (10, 12)]) == "E02-E04、E07、E10-E12"


def test_season_block_status_mapping():
    """TMDB 状态映射为中文。"""
    media = AggregatedMedia(
        title="X", year=2026, media_type="tv", season=1,
        episode_start=1, episode_end=10, file_count=10,
    )
    for raw, zh in [
        ("Returning Series", "连载中"),
        ("Ended", "已完结"),
        ("Canceled", "已取消"),
        ("In Production", "制作中"),
        ("Planned", "计划中"),
        ("Pilot", "试播集"),
    ]:
        details = {
            "media_type": "tv", "status": raw,
            "seasons": [{"season": 1, "episode_count": 10}],
        }
        block = _render_season_block(details, media)
        assert f"⚙️ 状态：{zh}" in block, f"{raw} → {zh} 失败"


def test_season_block_status_unknown_passthrough():
    """未知状态原文透传。"""
    media = AggregatedMedia(
        title="X", year=2026, media_type="tv", season=1,
        episode_start=1, episode_end=10, file_count=10,
    )
    details = {
        "media_type": "tv", "status": "Some New Status",
        "seasons": [{"season": 1, "episode_count": 10}],
    }
    block = _render_season_block(details, media)
    assert "⚙️ 状态：Some New Status" in block


def test_season_block_no_status():
    """无 status 字段时不显示状态行。"""
    media = AggregatedMedia(
        title="X", year=2026, media_type="tv", season=1,
        episode_start=1, episode_end=10, file_count=10,
    )
    details = {"media_type": "tv", "seasons": [{"season": 1, "episode_count": 10}]}
    block = _render_season_block(details, media)
    assert "⚙️ 状态" not in block


def test_season_block_movie_empty():
    """电影：season_block 为空（时长在 head）。"""
    media = AggregatedMedia(title="X", year=2020, media_type="movie")
    details = {"media_type": "movie"}
    assert _render_season_block(details, media) == ""


def test_season_block_single_episode():
    """单集分享：E04（无范围）。"""
    media = AggregatedMedia(
        title="X", year=2026, media_type="tv", season=1,
        episode_start=4, episode_end=4, file_count=1,
    )
    details = {"media_type": "tv", "seasons": [{"season": 1, "episode_count": 20}]}
    block = _render_season_block(details, media)
    assert "📋 集数：S01 E04" in block
    assert "E04-E04" not in block


def test_caption_multi_season_has_season_block():
    """多季 caption 含季集模块。"""
    media = AggregatedMedia(
        title="女清洁工", year=2022, media_type="tv", season=4,
        seasons=[1, 2, 3, 4], file_count=46,
    )
    details = {
        "tmdb_id": 125282, "media_type": "tv", "title": "女清洁工", "year": 2022,
        "release_date": "2022-01-03", "overview": "女主是机智的医生。",
        "poster_path": "/x.jpg", "vote_average": 7.4, "vote_count": 253,
        "status": "Ended", "genres": ["犯罪", "剧情"],
        "number_of_seasons": 4, "number_of_episodes": 46,
    }
    cap = render_caption(details, media, "abc12345", None, None)
    assert "🗂️ 季集：S01-S04 共46集（46个文件）" in cap
    assert len(cap) <= 1024


def test_human_size():
    assert _human_size(0) == ""
    # 二进制：1 GB = 1024^3 = 1,073,741,824 字节
    assert _human_size(8_589_934_592) == "8.00 GB"  # 8 * 1024^3
    assert _human_size(1_048_576) == "1.00 MB"  # 1024^2
    assert _human_size(95_000) == "92.77 KB"
    assert _human_size(500) == "500 B"


def test_render_files_block_natural_sort():
    """文件清单自然排序：Season 1 → 2 → 3 → ... → 10 → 11（非字典序 1,10,11,2）。"""
    files = [
        ShareFile(name="Season 10", is_dir=True),
        ShareFile(name="Season 2", is_dir=True),
        ShareFile(name="航海王 (1999) {tmdb-37854}", is_dir=True),
        ShareFile(name="Season 1", is_dir=True),
        ShareFile(name="Season 23", is_dir=True),
        ShareFile(name="One Piece.S01E61.mkv", size=1_000_000_000),
        ShareFile(name="One Piece.S01E02.mkv", size=1_000_000_000),
    ]
    block = _render_files_block(files)
    # 航海王目录排第一（普通目录在 Season 前）
    assert block.index("航海王") < block.index("Season 1")
    # Season 按数字排序
    assert block.index("Season 1") < block.index("Season 2")
    assert block.index("Season 2") < block.index("Season 10")
    assert block.index("Season 10") < block.index("Season 23")
    # 视频文件按集号排序
    assert block.index("S01E02") < block.index("S01E61")


def test_render_files_block_tmdb_parent_first():
    """带 {tmdb-xxx} 的父目录排最前（优先于数字名子目录）。"""
    files = [
        ShareFile(name="01", is_dir=True),
        ShareFile(name="金特务：本色回归 (2026) {tmdb-296206}", is_dir=True),
        ShareFile(name="02", is_dir=True),
    ]
    block = _render_files_block(files)
    # tmdb 父目录在 01/02 之前（用 📁 前缀避免误匹配年份里的数字）
    assert block.index("📁 金特务") < block.index("📁 01")
    assert block.index("📁 金特务") < block.index("📁 02")
    # 01 < 02（自然排序）
    assert block.index("📁 01") < block.index("📁 02")


def test_render_files_block_max_items():
    """max_items 限制显示前 N 项 + 截断提示。"""
    files = _many_files(10)
    b = _render_files_block(files, max_items=3)
    assert "📁 分享内容（10 项）" in b
    assert "Show.S01E01" in b
    assert "Show.S01E03" in b
    assert "Show.S01E04" not in b  # 第 4 项不显示
    assert "已显示前 3 项" in b


def test_render_head_new_info_fields():
    """标题区含新信息项：评分/年份/地区/类型/画质/主演/体积/标签。"""
    media = AggregatedMedia(
        title="京城奇探", year=2025, media_type="tv", season=1,
        episode_start=1, episode_end=10, quality="4K / 2160P",
        source="WEB-DL", hdr="HDR10", file_count=10,
    )
    details = {
        "media_type": "tv", "title": "京城奇探", "year": 2025,
        "vote_average": 8.5, "vote_count": 1234,
        "genres": ["剧情", "悬疑"], "countries": ["CN"],
        "cast": ["张三", "李四", "王五"],
        "release_date": "2025-01-01", "status": "Returning Series",
    }
    files = [ShareFile(name=f"E{i}.mkv", size=1_000_000_000) for i in range(10)]
    head = _render_head(details, media, files)
    assert "✨ 评分：8.5 · 1234票" in head
    assert "⏳ 年份：2025" in head
    assert "🌐 地区：🇨🇳 中国大陆" in head
    assert "🔮 类型：剧情 / 悬疑" in head
    assert "🎭 主演：张三 / 李四 / 王五" in head
    assert "💾 体积：" in head  # 10 GB
    assert "🏷️ 标签：#京城奇探" in head
    assert "📅 首播 2025-01-01" in head


def test_quality_block_html_module():
    """画质信息是独立 <blockquote> 模块，含完整 quality_info。"""
    from app.parser.media_parser import get_quality_info

    media = AggregatedMedia(
        title="名校的阶梯", year=2024, media_type="tv", season=1,
        quality_info=get_quality_info(
            "名校的阶梯.2024.S01E06.2160p.Netflix.WEB-DL.DoVi P5.H.265.10-bit.23.976fps.DDP 5.1-ADWeb.mkv"
        ),
    )
    block = _render_quality_block(media)
    assert block.startswith("<blockquote>") and block.endswith("</blockquote>")
    assert "DoVi P5" in block
    assert "Netflix" in block
    assert "H.265" in block


def test_render_head_and_footer():
    head = _render_head(_tv_details(), _tv_media())
    footer = _render_footer("abc", "p1")
    assert "三体" in head
    assert "8.2" in head
    assert "115.com/s/abc?password=p1" in footer
    # TMDB 详情改为卡片下方 inline button，footer 仅含 115 网盘模块
    assert "themoviedb.org" not in footer
    assert "p1" in footer  # 访问码包含在 115 URL 中


# -------------------- 标签 hashtag 清洗 -------------------- #
def test_tag_name_strips_symbols():
    """片名含引号/括号/冒号/连字符等符号需清洗为 hashtag 安全名。"""
    from app.telegram.pusher import _tag_name

    assert _tag_name('喜欢上"欠欠"的你') == "喜欢上欠欠的你"
    assert _tag_name("宾虚 (1959)") == "宾虚1959"
    assert _tag_name("Dr. STONE") == "DrSTONE"
    assert _tag_name("龙族：前传") == "龙族前传"
    assert _tag_name("X (2022) - 2160p") == "X20222160p"
    assert _tag_name("") == ""
    assert _tag_name("航海王") == "航海王"


def test_head_tag_clean_for_quoted_title():
    """含引号标题渲染后 hashtag 不含引号。"""
    details = {
        "tmdb_id": 1, "media_type": "tv", "title": '喜欢上"欠欠"的你',
        "year": 2026, "release_date": "2026-01-01", "overview": "",
        "poster_path": None, "vote_average": 0, "vote_count": 0,
        "status": "Returning Series", "genres": [], "cast": [],
        "countries": [], "seasons": [{"season": 1, "episode_count": 10}],
    }
    media = AggregatedMedia(title='喜欢上"欠欠"的你', year=2026, media_type="tv", season=1)
    head = _render_head(details, media)
    assert "🏷️ 标签：#喜欢上欠欠的你" in head
    assert '"' not in head.split("🏷️")[1]  # 标签行不含引号


# -------------------- ed2k footer / 渲染 -------------------- #
_ED2K_URL = (
    "ed2k://|file|宾虚 (1959) - 2160p.BluRay REMUX.DoVi P7.H.265.10-bit.23.976fps.TrueHD 7.1-WF.mkv"
    "|135915637476|3E874DEBD5E4A7AF8B1EEE7F41E7DD51|/"
)


def test_ed2k_footer_block():
    """ed2k footer：🔗 ed2k 资源 + 明文完整 URL，无访问码行。"""
    footer = _render_footer(_ED2K_URL, None, provider="ed2k")
    assert footer.startswith("<blockquote>") and footer.endswith("</blockquote>")
    assert "ed2k 资源" in footer
    assert _ED2K_URL in footer  # 完整 URL 明文展示
    assert "访问码" not in footer
    assert "115.com" not in footer


def test_ed2k_caption_uses_ed2k_footer():
    """ed2k caption 含 ed2k 资源模块、资源文件标题；无 115 链接。"""
    details = {
        "tmdb_id": 1, "media_type": "movie", "title": "宾虚", "year": 1959,
        "release_date": "1959-11-18", "overview": "宾虚的故事。",
        "poster_path": None, "vote_average": 8.2, "vote_count": 100,
        "status": "Released", "genres": ["剧情"], "runtime": 212,
        "cast": [], "countries": ["US"],
    }
    media = AggregatedMedia(title="宾虚", year=1959, media_type="movie", quality="4K / 2160P")
    files = [ShareFile(name="宾虚 (1959) - 2160p.mkv", size=135915637476)]
    cap = render_caption(details, media, _ED2K_URL, None, files, provider="ed2k")
    assert "ed2k 资源" in cap
    assert _ED2K_URL in cap
    assert "资源文件（1 项）" in cap  # ed2k 用「资源文件」标题
    assert "115.com" not in cap
    assert "访问码" not in cap
    assert "themoviedb.org" not in cap  # TMDB 走按钮


def test_ed2k_text_render():
    """ed2k 无海报纯文本渲染含 ed2k 资源模块。"""
    details = {
        "tmdb_id": 1, "media_type": "movie", "title": "宾虚", "year": 1959,
        "release_date": "1959-11-18", "overview": "宾虚的故事。",
        "poster_path": None, "vote_average": 8.2, "vote_count": 100,
        "status": "Released", "genres": ["剧情"], "runtime": 212,
        "cast": [], "countries": ["US"],
    }
    media = AggregatedMedia(title="宾虚", year=1959, media_type="movie", quality="4K / 2160P")
    files = [ShareFile(name="宾虚 (1959) - 2160p.mkv", size=135915637476)]
    txt = render_text(details, media, _ED2K_URL, None, files, provider="ed2k")
    assert len(txt) <= 4096
    assert "ed2k 资源" in txt
    assert _ED2K_URL in txt
    assert "126.58 GB" in txt  # 135915637476 / 1024^3 ≈ 126.58 GB（二进制）


# -------------------- S00 特别篇 -------------------- #
def test_season_block_s00_special():
    """S00 特别篇：显示「特别篇」，用 TMDB season 0 的集数，不回退整剧总集数。"""
    media = AggregatedMedia(
        title="妖精的尾巴", year=2009, media_type="tv", season=0,
        seasons=[0],
        season_episodes={0: [1]},
        episode_start=1, episode_end=1,
        quality="4K / 2160P", source="WEB-DL", file_count=1,
    )
    details = {
        "media_type": "tv", "status": "Ended",
        "number_of_seasons": 9, "number_of_episodes": 328,
        "seasons": [
            {"season": 0, "episode_count": 32, "name": "特别篇"},
            {"season": 1, "episode_count": 48, "name": "第一季"},
        ],
    }
    block = _render_season_block(details, media)
    # 季范围显示「S00 特别篇」而非「S00」
    assert "S00 特别篇" in block
    assert "S00 共328集" not in block  # 不回退整剧总集数
    # 用 season 0 的 episode_count
    assert "共32集（1个文件）" in block
    # 集数行保持 S00，不被重分配到 S01
    assert "📋 集数：S00 E01" in block
    assert "S01 E01" not in block


def test_season_block_s00_no_tmdb_season0():
    """S00 文件但 TMDB 无 season 0 数据：不回退整剧总集数，仅显示文件数。"""
    media = AggregatedMedia(
        title="妖精的尾巴", year=2009, media_type="tv", season=0,
        seasons=[0],
        season_episodes={0: [1]},
        episode_start=1, episode_end=1,
        file_count=1,
    )
    # 旧缓存场景：TMDB 数据里没有 season 0（曾被跳过）
    details = {
        "media_type": "tv", "status": "Ended",
        "number_of_seasons": 9, "number_of_episodes": 328,
        "seasons": [{"season": 1, "episode_count": 48, "name": "第一季"}],
    }
    block = _render_season_block(details, media)
    assert "S00 特别篇" in block
    assert "共328集" not in block  # 不回退
    # 无 TMDB season 0 集数 → 只显示文件数
    assert "S00 特别篇（1个文件）" in block
    assert "📋 集数：S00 E01" in block


# -------------------- 编辑模式：画质模块覆写（精品/推荐语） -------------------- #
def test_quality_block_premium_and_extra():
    """精品标记 + 自动画质 + 推荐语同处一个 blockquote，顺序：精品→画质→推荐语。"""
    from app.parser.media_parser import get_quality_info

    media = AggregatedMedia(
        title="X", media_type="movie",
        quality_info=get_quality_info("X.2024.2160p.WEB-DL.DoVi P7.H.265.10-bit.mkv"),
    )
    block = _render_quality_block(
        media, quality_extra="原盘内封中字 · 国配音轨", is_premium=True
    )
    assert block.startswith("<blockquote>") and block.endswith("</blockquote>")
    assert "💎 精品资源" in block
    assert "💿 画质：" in block
    assert "DoVi P7" in block  # 自动 8 维度保留
    assert "📝 原盘内封中字 · 国配音轨" in block
    assert block.index("💎 精品资源") < block.index("💿 画质：")
    assert block.index("💿 画质：") < block.index("📝")


def test_quality_block_default_unchanged():
    """不传新参时与改造前一致（回归保护）。"""
    media = AggregatedMedia(quality="4K / 2160P", source="WEB-DL", hdr="HDR10")
    block = _render_quality_block(media)
    assert block == "<blockquote>💿 画质：4K / 2160P HDR10 WEB-DL</blockquote>"


def test_quality_block_empty_when_nothing():
    """无画质/无精品/无推荐语时返回空串（与原行为一致）。"""
    media = AggregatedMedia(media_type="movie")  # quality_info 空，quality 空
    assert _render_quality_block(media) == ""


def test_quality_block_only_premium():
    """仅精品标记（无画质信息）：仍输出 blockquote 含 💎 行。"""
    media = AggregatedMedia(media_type="movie")
    block = _render_quality_block(media, is_premium=True)
    assert "💎 精品资源" in block
    assert "💿 画质：" not in block
    assert block.startswith("<blockquote>")


def test_quality_block_extra_escaped():
    """推荐语 HTML 特殊字符转义防注入。"""
    media = AggregatedMedia(quality="1080P")
    block = _render_quality_block(media, quality_extra="<b>bold</b> & <i>")
    assert "<b>bold</b>" not in block
    assert "&lt;b&gt;bold&lt;/b&gt;" in block
    assert "&amp;" in block


def test_quality_block_extra_truncated():
    """推荐语超长截断（≤ _QUALITY_EXTRA_LIMIT + 省略号）。"""
    from app.telegram.pusher import _QUALITY_EXTRA_LIMIT

    media = AggregatedMedia(quality="1080P")
    long_text = "语" * (_QUALITY_EXTRA_LIMIT + 50)
    block = _render_quality_block(media, quality_extra=long_text)
    assert "…" in block
    assert ("语" * (_QUALITY_EXTRA_LIMIT + 50)) not in block


def test_render_caption_passes_extra():
    """caption 透传 quality_extra/is_premium 到画质模块。"""
    cap = render_caption(
        _tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files(),
        quality_extra="精品推荐语", is_premium=True,
    )
    assert "💎 精品资源" in cap
    assert "📝 精品推荐语" in cap
    assert len(cap) <= 1024


def test_render_text_passes_extra():
    """无海报 text 透传 quality_extra/is_premium。"""
    txt = render_text(
        _tv_details(), _tv_media(), "sw8k9m2", "ab12", _sample_files(),
        quality_extra="精品推荐语", is_premium=True,
    )
    assert "💎 精品资源" in txt
    assert "📝 精品推荐语" in txt
