"""分享链接解析（通用入口，按 provider 路由）。

115 三形态：
- https://115.com/s/{code}?password={pwd}
- https://115.com/s/{code}?{pwd}（尾 token）
- 8+ 字符裸码
- 链接未带访问码时，从消息正文提取（"访问码：xxxx" / "提取码:xxxx" / "密码：xxxx"）

ed2k 单文件链接：
- ed2k://|file|<文件名>|<字节数>|<hash>|...|/
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 115 分享 URL：支持 115.com / 115cdn.com 等域名；捕获 share_code 与 ? 后的 query 串
_115_URL_RE = re.compile(r"115(?:cdn)?\.com/s/([A-Za-z0-9_-]+)(?:\?(?P<query>[^ \n]*))?", re.IGNORECASE)
_115_PWD_RE = re.compile(r"password=([A-Za-z0-9_-]+)", re.IGNORECASE)
# 尾 token：URL ? 后无 = 的短串（访问码，4-12 字符）
_TAIL_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
# 正文访问码：关键词 + 冒号/等号（全半角）+ 4-12 位字母数字（借 P115-Share 的提示词模式）
_ACCESS_CODE_RE = re.compile(
    r"(?:访问码|提取码|密码)\s*[：:=＝]\s*([A-Za-z0-9]{4,12})",
)
# 裸码：8+ 字符（避免误匹配 hello 等普通词）
_BARE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")

# ed2k 单文件链接：ed2k://|file|<name>|<size>|<hash>|...|/
# 捕获 文件名 / 字节数 / hash；group(0) 为完整 URL（作为 provider 解析输入与去重 key）
_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|[^ ]*?/",
    re.IGNORECASE,
)


@dataclass
class ParsedShare:
    provider: str  # "115" | "ed2k"
    code: str
    password: str | None = None


def _from_115_match(m: re.Match) -> ParsedShare:
    """从 115 URL 正则匹配构造 ParsedShare。"""
    code = m.group(1)
    query = m.group("query") or ""
    pwd: str | None = None
    pm = _115_PWD_RE.search(query)
    if pm:
        pwd = pm.group(1)
    elif query and _TAIL_TOKEN_RE.fullmatch(query):
        # 尾 token 形式：?abcd1234（无 = 的短访问码）
        pwd = query
    return ParsedShare("115", code, pwd)


def _fill_body_password(shares: list[ParsedShare], text: str) -> None:
    """链接未带访问码时，用正文提取的访问码填充（原地）。

    仅 115 需要（ed2k 无访问码）；URL 自带 password/尾 token 的不覆盖。
    正文懒提取：只有存在待填充项时才扫描一次。
    """
    if not any(p.provider == "115" and p.password is None for p in shares):
        return
    m = _ACCESS_CODE_RE.search(text)
    if not m:
        return
    pwd = m.group(1)
    for p in shares:
        if p.provider == "115" and p.password is None:
            p.password = pwd


def parse_shares(text: str) -> list[ParsedShare]:
    """提取文本中所有 115 + ed2k 链接，按出现顺序返回（去重）。

    用于多链接批处理：一条消息含多个链接时全部识别。
    不含裸码（裸码为单链接概念，由 parse_share 处理）。
    """
    if not text:
        return []
    found: list[tuple[int, ParsedShare]] = []
    for m in _ED2K_RE.finditer(text):
        found.append((m.start(), ParsedShare("ed2k", m.group(0), None)))
    for m in _115_URL_RE.finditer(text):
        found.append((m.start(), _from_115_match(m)))
    found.sort(key=lambda x: x[0])
    seen: set[tuple[str, str]] = set()
    result: list[ParsedShare] = []
    for _, p in found:
        key = (p.provider, p.code)
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    _fill_body_password(result, text)
    return result


def parse_share(text: str) -> ParsedShare | None:
    """从任意文本中解析分享链接/裸码（取第一个）。无法识别返回 None。"""
    if not text:
        return None

    # ed2k 链接（优先于裸码匹配，避免长串误判）
    m = _ED2K_RE.search(text)
    if m:
        return ParsedShare("ed2k", m.group(0), None)

    m = _115_URL_RE.search(text)
    if m:
        p = _from_115_match(m)
        _fill_body_password([p], text)
        return p

    # 裸码（取首个 token）
    first = text.strip().split()[0] if text.strip() else ""
    if _BARE_CODE_RE.fullmatch(first):
        p = ParsedShare("115", first, None)
        _fill_body_password([p], text)
        return p

    return None
