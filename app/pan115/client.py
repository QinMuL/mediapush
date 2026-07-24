"""115 网盘客户端封装。

旧项目约束（见 ARCHITECTURE.md 第 8 节 p115client 兼容性）：
- p115client 装坏时不应拖垮整个服务：顶部容错导入，P115Client/tool 函数失败时置 None
- tool 模块函数逐个容错导入（fs_files_iter 新名优先）
- user_id 为 cached property，直接访问
- login_another_app(replace=True) 返回新 P115Client 实例，relogin 后需重建以维持 webapi 端点
- share_info / fs_info（非 _app 后缀）
- share_iterdir_walk 第三位置参数为 receive_code，app='web'
- 405 为永久错误，不重试
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.core.retry import retry_async

logger = logging.getLogger(__name__)

# ---- 容错导入：p115client 装坏时不让整条 import 链崩溃 ----
try:
    from p115client import P115Client as _P115Client
except Exception:  # noqa: BLE001  容错导入
    _P115Client = None

# tool 模块函数逐个容错导入（新版可能移除/重命名）
try:
    from p115client.tool import share_iterdir_walk as _share_iterdir_walk
except Exception:  # noqa: BLE001
    _share_iterdir_walk = None

try:
    from p115client.tool import fs_files_iter as _fs_files_iter  # 新名优先
except Exception:  # noqa: BLE001
    _fs_files_iter = None
    try:
        from p115client.tool import iter_fs_files as _fs_files_iter  # 回退旧名
    except Exception:  # noqa: BLE001
        _fs_files_iter = None


class Pan115Error(Exception):
    """115 业务异常基类。"""


class Pan115PermanentError(Pan115Error):
    """永久性错误（如 405），不应重试。"""


def is_p115client_available() -> bool:
    return _P115Client is not None


def _is_transient(err: Exception) -> bool:
    """非 Pan115PermanentError 视为瞬时（超时/连接/限流），可重试；405 等永久错误不重试。"""
    return not isinstance(err, Pan115PermanentError)


class Pan115Client:
    """P115Client 封装：cookie 管理、reset 重建、分享操作。

    所有 IO 方法为 async，便于 mock 与单测。
    """

    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._client = self._build(cookie) if cookie else None

    def _build(self, cookie: str):
        if _P115Client is None:
            raise Pan115Error("p115client 未安装或导入失败")
        # p115client 构造参数为 cookies=（非 cookie=）
        return _P115Client(cookies=cookie)

    @property
    def client(self):
        """底层 P115Client 实例（已登录）。"""
        if self._client is None:
            raise Pan115Error("未配置 115 cookie")
        return self._client

    @property
    def user_id(self):
        """cached property，直接访问（旧项目约束：需显式 uid 时用此值）。"""
        return self.client.user_id

    @property
    def cookie(self) -> str:
        return self._cookie

    async def reset(self, cookie: str) -> None:
        """更新 cookie 后重建 client（维持 webapi 端点）。

        对应 container.reset_pan115()，为 async，需 await。
        """
        await self.close()
        self._cookie = cookie
        self._client = self._build(cookie) if cookie else None

    async def relogin_another_app(self, app: str = "alipaymini") -> None:
        """通过 login_another_app 重新登录。

        replace=True 返回新 P115Client 实例（不是 cookie 字符串），
        需用它替换旧 client 以维持 webapi 端点。
        """
        old = self._client
        new_client = await self.client.login_another_app(
            app=app, replace=True, async_=True
        )
        if old is not None:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = new_client
        # 新实例的 cookies 由 login_another_app 内部维护；self._cookie 仅作展示用，
        # P115Client 无 .cookie 字符串属性（.cookies 是 CookieJar），故保留旧值。

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def check_health(self) -> bool:
        """轻量健康检查：调用 user_info 验证 cookie 仍有效。

        user_info 需显式传 uid（self.user_id，cached property 从 cookie 解析，不联网）。
        任何异常视为不健康（cookie 失效 / 网络问题 / 接口变更）。
        """
        if self._client is None:
            return False
        try:
            await self._client.user_info(
                {"user_id": self.user_id}, async_=True
            )
            return True
        except Exception:  # noqa: BLE001  健康检查不应抛出
            return False

    # ---- 分享操作 ----
    async def share_info(self, share_code: str, receive_code: str = "") -> dict:
        """分享元信息（share_info，非 share_info_app）。瞬时错误指数退避重试。"""
        payload = {"share_code": share_code, "receive_code": receive_code}

        async def _call():
            try:
                return await self.client.share_info(payload, async_=True)
            except Exception as e:
                self._raise_if_405(e, share_code)
                raise

        return await retry_async(
            _call, retries=3, base_delay=2.0,
            is_transient=_is_transient, label=f"share_info({share_code})",
        )

    async def iter_share_files(
        self, share_code: str, receive_code: str = ""
    ) -> AsyncIterator[dict]:
        """遍历分享文件（share_iterdir_walk, app='web', receive_code 第三位置参数）。

        share_snap 已废弃（405），统一用 share_iterdir_walk。
        重试策略：仅在尚未产出任何文件前重试（避免重复推送）；
        永久错误（405）不重试。
        """
        if _share_iterdir_walk is None:
            raise Pan115Error("p115client.tool.share_iterdir_walk 不可用")
        retries = 3
        for attempt in range(1, retries + 1):
            it = _share_iterdir_walk(
                self.client, share_code, receive_code, app="web", async_=True
            )
            yielded = False
            try:
                async for _depth, _dirs, files in it:
                    for f in files:
                        yielded = True
                        yield f
                return  # 正常完成
            except Pan115PermanentError:
                raise  # 永久错误不重试
            except Exception as e:
                if yielded or attempt >= retries:
                    raise  # 已产出后不重试（避免重复），或重试耗尽
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                logger.warning(
                    "iter_share_files(%s) 第 %d/%d 次失败：%r，%.1fs 后重试",
                    share_code, attempt, retries, e, delay,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _raise_if_405(err: Exception, share_code: str) -> None:
        """405 为永久错误，转 Pan115PermanentError（不重试）。"""
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 405:
            raise Pan115PermanentError(f"405 永久错误: share_code={share_code}") from err
