from app.core.link_parser import parse_share


def test_url_with_password_param():
    p = parse_share("https://115.com/s/abc123de?password=xyz99")
    assert p is not None
    assert p.provider == "115"
    assert p.code == "abc123de"
    assert p.password == "xyz99"


def test_url_with_tail_token():
    p = parse_share("https://115.com/s/abc123de?xyz99")
    assert p is not None
    assert p.code == "abc123de"
    assert p.password == "xyz99"


def test_url_without_password():
    p = parse_share("https://115.com/s/abc123de")
    assert p is not None
    assert p.code == "abc123de"
    assert p.password is None


def test_url_115cdn_domain():
    """115cdn.com 域名也要识别（实际分享链接常见）。"""
    p = parse_share("https://115cdn.com/s/swso4bo3hib?password=ofe1#")
    assert p is not None
    assert p.provider == "115"
    assert p.code == "swso4bo3hib"
    assert p.password == "ofe1"  # 尾部 # 不影响


def test_url_115cdn_tail_token():
    p = parse_share("https://115cdn.com/s/swso4bo3hib?ofe1")
    assert p is not None
    assert p.code == "swso4bo3hib"
    assert p.password == "ofe1"


def test_bare_code_8plus():
    p = parse_share("abc12345")
    assert p is not None
    assert p.code == "abc12345"
    assert p.password is None


def test_short_word_not_bare_code():
    assert parse_share("hello") is None
    assert parse_share("abc1") is None  # < 8 chars


def test_url_inside_command_text():
    p = parse_share("/115 https://115.com/s/sw8k9m2?password=ab12 extra")
    assert p is not None
    assert p.code == "sw8k9m2"
    assert p.password == "ab12"


def test_empty_and_garbage():
    assert parse_share("") is None
    assert parse_share("随便一句话") is None


# -------------------- ed2k -------------------- #
_ED2K_SAMPLE = (
    "ed2k://|file|宾虚 (1959) - 2160p.BluRay REMUX.DoVi P7.H.265.10-bit.23.976fps.TrueHD 7.1-WF.mkv"
    "|135915637476|3E874DEBD5E4A7AF8B1EEE7F41E7DD51|/"
)


def test_ed2k_link_recognized():
    p = parse_share(_ED2K_SAMPLE)
    assert p is not None
    assert p.provider == "ed2k"
    assert p.code == _ED2K_SAMPLE  # 完整 URL 作为 code
    assert p.password is None


def test_ed2k_link_embedded_in_text():
    p = parse_share(f"看看这个 {_ED2K_SAMPLE} 谢谢")
    assert p is not None
    assert p.provider == "ed2k"
    assert p.code == _ED2K_SAMPLE


def test_ed2k_does_not_break_115():
    """ed2k 检测不影响 115 链接识别。"""
    p = parse_share("https://115.com/s/sw8k9m2?password=ab12")
    assert p is not None
    assert p.provider == "115"
    assert p.code == "sw8k9m2"


def test_ed2k_invalid_hash_rejected():
    """hash 非 32 位 hex 不是合法 ed2k 链接。"""
    assert parse_share("ed2k://|file|x.mkv|100|deadbeef|/") is None


# -------------------- parse_shares 多链接 -------------------- #
from app.core.link_parser import parse_shares

_MULTI_MSG = """
https://115cdn.com/s/swslhhz3nu1?password=c0a6#
怒火救援 (2026) {tmdb-223386}
复制这段内容可在115-Desktop中打开！

https://115cdn.com/s/swssucu3nu1?password=red2#
航海王.1999.S23E1167.第1167集.2160p.SDR.H.265.23.976fps.AAC 2.0.mp4
复制这段内容可在115-Desktop中打开！

https://115cdn.com/s/swssims3nu1?password=t349#
昨夜将至 (2026) {tmdb-291947}
复制这段内容可在115-Desktop中打开！

ed2k://|file|宾虚 (1959) - 2160p.BluRay REMUX.DoVi P7.H.265.10-bit.mkv|135915637476|3E874DEBD5E4A7AF8B1EEE7F41E7DD51|/
"""


def test_parse_shares_extracts_all_in_order():
    shares = parse_shares(_MULTI_MSG)
    assert len(shares) == 4
    # 前三个 115 按出现顺序
    assert shares[0].provider == "115" and shares[0].code == "swslhhz3nu1"
    assert shares[0].password == "c0a6"
    assert shares[1].code == "swssucu3nu1" and shares[1].password == "red2"
    assert shares[2].code == "swssims3nu1" and shares[2].password == "t349"
    # ed2k 排在其后（按文本位置）
    assert shares[3].provider == "ed2k"
    assert shares[3].code.startswith("ed2k://")


def test_parse_shares_dedup():
    """同一链接重复出现只保留一次。"""
    txt = "https://115.com/s/abc12345?password=p1 https://115.com/s/abc12345?password=p1"
    shares = parse_shares(txt)
    assert len(shares) == 1


def test_parse_shares_empty():
    assert parse_shares("") == []
    assert parse_shares("没有链接的普通文本") == []


def test_parse_shares_single_equals_parse_share_for_url():
    """单链接时 parse_shares 第一个应与 parse_share 一致。"""
    txt = "https://115.com/s/sw8k9m2?password=ab12"
    shares = parse_shares(txt)
    single = parse_share(txt)
    assert len(shares) == 1
    assert shares[0].provider == single.provider
    assert shares[0].code == single.code
    assert shares[0].password == single.password


# -------------------- A1 正文访问码提取 -------------------- #
def test_body_password_fullwidth_colon():
    """链接未带访问码时，从正文"访问码：xxxx"提取。"""
    p = parse_share("https://115.com/s/abc123de\n访问码：w9k2")
    assert p is not None
    assert p.password == "w9k2"


def test_body_password_halfwidth_colon_and_spacer():
    p = parse_share("https://115.com/s/abc123de\n提取码: w9k2")
    assert p is not None
    assert p.password == "w9k2"


def test_body_password_keyword_mi_ma():
    p = parse_share("https://115.com/s/abc123de\n密码：w9k2")
    assert p is not None
    assert p.password == "w9k2"


def test_body_password_url_param_wins():
    """URL 自带 password 时正文提示不覆盖。"""
    p = parse_share("https://115.com/s/abc123de?password=xyz99\n访问码：w9k2")
    assert p is not None
    assert p.password == "xyz99"


def test_body_password_no_keyword_no_fill():
    """正文无访问码提示则保持 None（P115-Share 同款关键词集）。"""
    p = parse_share("https://115.com/s/abc123de\n复制这段内容可在115-Desktop中打开！")
    assert p is not None
    assert p.password is None


def test_body_password_bare_code():
    """裸码 + 正文访问码也能填充。"""
    p = parse_share("abc12345\n密码：w9k2")
    assert p is not None
    assert p.code == "abc12345"
    assert p.password == "w9k2"


def test_body_password_multi_shares_fill_all():
    """多链接消息中，无密码的链接统一用正文访问码填充。"""
    txt = (
        "https://115.com/s/aaaa1111\n"
        "https://115.com/s/bbbb2222\n"
        "访问码：w9k2"
    )
    shares = parse_shares(txt)
    assert len(shares) == 2
    assert shares[0].password == "w9k2"
    assert shares[1].password == "w9k2"


def test_body_password_ed2k_untouched():
    """ed2k 链接不受正文访问码影响（无访问码概念）。"""
    txt = f"{_ED2K_SAMPLE}\n访问码：w9k2"
    shares = parse_shares(txt)
    assert len(shares) == 1
    assert shares[0].provider == "ed2k"
    assert shares[0].password is None
