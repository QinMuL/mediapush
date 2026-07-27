"""115 网盘 Provider（封装 p115client）。

核心：读取**别人分享**的链接内容走 115 的匿名 web 接口（app='web'），
p115client.tool.share_iterdir_walk 的 client 参数可传 None —— 不登录即可获取数据，
访问码（receive_code）即是门禁。因此**无需 115 cookie 即可解析人工发送的分享链接**。

cookie 降级为可选：仅用于 /status 健康检查（验证自有账号有效性），与读取流程无关。

落实前序硬约束：
- tool.share_iterdir_walk(client, code, receive_code, app='web', async_=True)
  产出 (cid, dirs, files)，receive_code 第三位置必须传（访问码），app='web'
- p115client 在方法内懒导入（装坏不拖垮 bot）
- user_info(uid, async_=True) 健康检查需显式 uid（从 cookie 解析 UID）
- 115 默认不走代理（走代理易触发风控）
"""

from __future__ import annotations

import logging
import re

from app.providers.base import BaseShareProvider, ShareFile
from app.providers.exceptions import Pan115Error

logger = logging.getLogger(__name__)

_UID_RE = re.compile(r"UID=(\d+)", re.IGNORECASE)


def _uid_from_cookie(cookie: str) -> int | None:
    m = _UID_RE.search(cookie or "")
    return int(m.group(1)) if m else None


class Pan115Provider(BaseShareProvider):
    name = "115"

    def __init__(
        self,
        cookie: str = "",
        *,
        use_proxy: bool = False,
        proxy_url: str = "",
    ) -> None:
        # cookie 可选：仅用于健康检查；读取分享走匿名 web，不需要 cookie
        self.cookie = (cookie or "").strip()
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url
        if use_proxy:
            logger.warning("PAN115_USE_PROXY=true 但 p115client 代理未接线，115 仍走直连")
        self._client = None  # type: ignore[assignment]
        self._uid = _uid_from_cookie(self.cookie)

    # ------------------------------------------------------------------ #
    def _build_client(self):
        """懒构造 P115Client（仅健康检查用；读取分享不需要）。无 cookie 返回 None。"""
        if not self.cookie:
            return None
        if self._client is None:
            try:
                from p115client.client import P115Client
            except Exception as exc:
                raise Pan115Error(f"p115client 导入失败：{exc}") from exc
            try:
                self._client = P115Client(cookies=self.cookie, app="web")
            except Exception as exc:
                raise Pan115Error(f"P115Client 构造失败：{exc}") from exc
        return self._client

    # ------------------------------------------------------------------ #
    async def list_share(self, code: str, password: str | None) -> list[ShareFile]:
        """读取分享内容（递归扁平化）。

        匿名 web 读取，无需 cookie：
        - code: 分享码（或完整链接，share_iterdir_walk 内部支持链接）
        - password: 访问码（receive_code），无则空串
        """
        receive_code = password or ""
        try:
            from p115client.tool import share_iterdir_walk
        except Exception as exc:
            raise Pan115Error(f"p115client.tool 导入失败：{exc}") from exc

        out: list[ShareFile] = []
        try:
            # client=None：匿名 web 读取（app='web' 支持不登录）
            ait = share_iterdir_walk(
                None, code, receive_code, app="web", async_=True
            )
            async for _cid, dirs, files in ait:  # (cid, dirs, files)
                for d in dirs or []:
                    out.append(self._to_share_file(d, is_dir=True))
                for f in files or []:
                    out.append(self._to_share_file(f, is_dir=False))
        except Pan115Error:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "password" in msg or "receive" in msg or "访问码" in msg:
                raise Pan115Error("访问码错误或分享需要访问码", code=code) from exc
            if "not exist" in msg or "不存在" in msg or "失效" in msg or "expired" in msg:
                raise Pan115Error("分享不存在或已失效", code=code) from exc
            raise Pan115Error(f"读取 115 分享失败：{exc}", code=code) from exc

        if not out:
            raise Pan115Error("分享内容为空（可能访问码错误或分享无文件）", code=code)
        return out

    @staticmethod
    def _to_share_file(d: dict, *, is_dir: bool) -> ShareFile:
        return ShareFile(
            name=str(d.get("name") or d.get("file_name") or ""),
            size=int(d.get("size") or d.get("file_size") or 0),
            is_dir=bool(d.get("is_dir", is_dir)),
            sha1=(str(d["sha1"]) if d.get("sha1") else None),
        )

    # ------------------------------------------------------------------ #
    async def check_health(self) -> bool | None:
        """健康检查（验证自有 cookie）。

        - 无 cookie：返回 None（匿名读取可用，只是无法验证账号）
        - 有 cookie：返回 True/False
        """
        if not self.cookie:
            return None
        client = self._build_client()
        if client is None:
            return None
        if not self._uid:
            logger.warning("无法从 cookie 解析 UID，跳过健康检查")
            return False
        try:
            data = await client.user_info(self._uid, async_=True)
            return bool(data and data.get("data"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("115 健康检查失败：%s", exc)
            return False
