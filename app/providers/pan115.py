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

目录监控→建分享（share_send + duration=-1 永久，见 create_share）：
- 需登录 cookie（创建分享接口不支持匿名）；无 cookie 时相关方法抛 Pan115Error

借 P115-Share（github.com/ListeningLTG/P115-Share）的经验：
- margin 限速识别：115 风控返回 {"margin": N}（无 state/data，仅剩余秒数），
  check_response 因缺 state 而放行，随后 resp["data"]["count"] 抛 KeyError ——
  故先拿 share_snap 原始响应自行识别，margin 时等 N 秒渐进重试
- 快照渐进重试：分享刚创建时 115 返回"正在生成文件快照"，等待后重查
- 状态语义（get_share_status 同款）：share_state 0=审核中 7=失效；
  errno 4100009/4100010=已取消；have_vio_file=1=违规
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.providers.base import BaseShareProvider, ShareFile
from app.providers.exceptions import Pan115Error

logger = logging.getLogger(__name__)

_UID_RE = re.compile(r"UID=(\d+)", re.IGNORECASE)


def _uid_from_cookie(cookie: str) -> int | None:
    m = _UID_RE.search(cookie or "")
    return int(m.group(1)) if m else None


def _is_margin_response(resp: object) -> bool:
    """识别 115 margin 限速响应 {"margin": N}（无 state/data/count，仅秒数）。"""
    return (
        isinstance(resp, dict)
        and "margin" in resp
        and "data" not in resp
        and "state" not in resp
        and "count" not in resp
    )


@dataclass
class ShareStatus:
    """分享状态（share_snap 预检结果，用于巡检与读取前校验）。

    访问码语义（errno 实测）：
    - 4100012「请输入访问码」：分享存在，仅缺访问码 → need_code=True（活着）
    - 4100008「访问码错误」：分享存在，但传入的码不对（多半被改）→ code_changed=True
    两类均非失效；但 list_share 深读仍需正确访问码。
    """

    state: int | None = None  # share_state：0=审核中 1=正常 7=失效（其他未知）
    snapshotting: bool = False  # 正在生成文件快照（等待后可恢复）
    violating: bool = False  # have_vio_file=1 违规
    need_code: bool = False  # 分享存在但需访问码（未存档/无法深读）
    code_changed: bool = False  # 存档的访问码已失效（分享还活着，卡片旧码失效）
    title: str = ""
    message: str = ""  # 非空表示不可读（原因）

    @property
    def readable(self) -> bool:
        return not self.message


class Pan115Provider(BaseShareProvider):
    name = "115"

    # margin/快照渐进重试（借 P115-Share）
    _SNAP_MAX_RETRY = 3  # 渐进重试总次数
    _MARGIN_WAIT_CAP = 30.0  # margin 等待上限（秒），防 115 返回超大值
    _SNAP_WAIT = 3.0  # 快照生成中基础等待（3s/6s/9s 渐进退避）

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
        self._anon = None  # type: ignore[assignment]  # 匿名 web client（读分享/查状态）
        self._uid = _uid_from_cookie(self.cookie)

    # ------------------------------------------------------------------ #
    def _anon_client(self):
        """懒构造匿名 P115Client（app='web'，无需登录），构造一次复用。"""
        if self._anon is None:
            from p115client.client import P115Client

            self._anon = P115Client("", app="web")
        return self._anon

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
    async def _share_snap_raw(self, code: str, receive_code: str) -> dict:
        """匿名调 share_snap（cid=0 仅拿状态），带 margin/快照渐进重试。

        - margin 响应：等待 min(margin, cap) 秒后重试
        - "正在生成文件快照"：渐进退避（3s/6s/9s）后重查
        - 其余原样返回（含 state=False 的失败响应，由调用方 check_response 分类）
        - 重试耗尽仍 margin/快照中 → 抛 Pan115Error
        """
        try:
            from p115client.util import share_extract_payload
        except Exception as exc:
            raise Pan115Error(f"p115client.util 导入失败：{exc}") from exc
        payload = dict(share_extract_payload(code))
        payload["receive_code"] = receive_code or payload.get("receive_code") or ""
        client = self._anon_client()

        for attempt in range(1, self._SNAP_MAX_RETRY + 1):
            resp = await client.share_snap(
                {"cid": 0, "limit": 1, "offset": 0, **payload},
                async_=True,
            )
            if _is_margin_response(resp):
                wait = min(float(resp.get("margin", 5) or 5), self._MARGIN_WAIT_CAP)
                logger.warning(
                    "115 margin 限速：等待 %.0fs 后重试（%d/%d）",
                    wait, attempt, self._SNAP_MAX_RETRY,
                )
                await asyncio.sleep(wait)
                continue
            if "正在生成文件快照" in str(resp):
                wait = self._SNAP_WAIT * attempt
                logger.info(
                    "分享快照生成中，等待 %.0fs 后重查（%d/%d）",
                    wait, attempt, self._SNAP_MAX_RETRY,
                )
                await asyncio.sleep(wait)
                continue
            return resp
        raise Pan115Error("115 限速或快照生成中，多次重试后仍失败", code=code)

    # ------------------------------------------------------------------ #
    async def check_share_status(self, code: str, password: str | None) -> ShareStatus:
        """检查分享状态（单次快照，不递归读列表）。

        借 P115-Share get_share_status 的状态语义：
        - share_state 0=审核中 7=失效；errno 4100009/4100010=已取消
        - "正在生成文件快照"=快照中（渐进重试内已消化，仍中则报快照中）
        - have_vio_file=1=违规
        网络异常向上抛 Pan115Error（由巡检器决定重试）。
        """
        receive_code = password or ""
        try:
            from p115client.client import check_response
        except Exception as exc:
            raise Pan115Error(f"p115client 导入失败：{exc}") from exc

        resp = await self._share_snap_raw(code, receive_code)
        # 访问码类先判（state=False 但分享存在，不能走失效分类）：
        # 4100012 请输入访问码（匿名/未存档）；4100008 访问码错误（存的码被改）
        if resp.get("state") is False and (resp.get("errno") or resp.get("errNo")) in (
            4100008, 4100012,
        ):
            if (resp.get("errno") or resp.get("errNo")) == 4100008:
                return ShareStatus(need_code=True, code_changed=True)
            return ShareStatus(need_code=True)
        try:
            check_response(resp)
        except Exception as exc:  # noqa: BLE001 - P115OSError 分类
            msg = str(exc)
            if "正在生成文件快照" in msg:
                return ShareStatus(state=0, snapshotting=True, message="分享正在生成文件快照")
            if "4100009" in msg or "4100010" in msg or "链接已失效" in msg or "分享已取消" in msg:
                return ShareStatus(state=7, message="分享已失效或被取消")
            if "password" in msg.lower() or "receive" in msg.lower() or "访问码" in msg:
                return ShareStatus(message="访问码错误或分享需要访问码")
            if "not exist" in msg.lower() or "不存在" in msg or "失效" in msg:
                return ShareStatus(state=7, message="分享不存在或已失效")
            return ShareStatus(message=f"分享状态异常：{exc}")

        data = resp.get("data") or {}
        info = data.get("shareinfo") or data.get("share_info") or {}
        state = data.get("share_state", info.get("share_state", info.get("status")))
        try:
            state = int(state) if state is not None else None
        except (TypeError, ValueError):
            state = None
        snapshotting = "正在生成文件快照" in str(resp)
        violating = str(info.get("have_vio_file") or "") == "1"
        title = str(info.get("share_title") or "")

        if snapshotting:
            return ShareStatus(state=state, snapshotting=True, message="分享正在生成文件快照")
        if state == 7:
            return ShareStatus(state=7, message="分享已失效")
        if state == 0:
            return ShareStatus(state=0, message="分享审核中")
        if violating:
            return ShareStatus(state=state, violating=True, message="分享含违规文件")
        return ShareStatus(state=state, snapshotting=False, violating=False, title=title)

    # ------------------------------------------------------------------ #
    async def list_share(self, code: str, password: str | None) -> list[ShareFile]:
        """读取分享内容（递归扁平化）。

        匿名 web 读取，无需 cookie：
        - code: 分享码（或完整链接，share_iterdir_walk 内部支持链接）
        - password: 访问码（receive_code），无则空串

        读取前先预检（check_share_status）：margin 渐进重试 + 失效/访问码/审核
        提前给出明确错误，避免 iterdir 黑盒里只能看到 KeyError('data')。
        """
        # 预检（A2/A3）：不可读时给出明确原因，直接抛
        status = await self.check_share_status(code, password)
        if not status.readable:
            raise Pan115Error(status.message, code=code)

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
        except KeyError as exc:
            # check_response 对 margin 响应放行 → resp["data"] KeyError（读取途中被限速）
            if str(exc) == "'data'":
                raise Pan115Error("115 限速（margin），读取中断，请稍后重试", code=code) from exc
            raise Pan115Error(f"读取 115 分享失败：{exc!r}", code=code) from exc
        except Exception as exc:
            msg = str(exc).lower()
            if "password" in msg or "receive" in msg or "访问码" in msg:
                raise Pan115Error("访问码错误或分享需要访问码", code=code) from exc
            if "not exist" in msg or "不存在" in msg or "失效" in msg or "expired" in msg:
                raise Pan115Error("分享不存在或已失效", code=code) from exc
            if "margin" in msg:
                raise Pan115Error("115 限速（margin），请稍后重试", code=code) from exc
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
    def update_cookie(self, cookie: str) -> None:
        """热更新 cookie（文件化支持）：重置客户端与 UID，下次健康检查用新值。"""
        self.cookie = (cookie or "").strip()
        self._client = None
        self._uid = _uid_from_cookie(self.cookie)

    # ------------------------------------------------------------------ #
    # 目录监控 → 创建永久分享（需登录 cookie）
    # ------------------------------------------------------------------ #
    def _login_client(self):
        """登录态 client（建分享/列自己网盘必须）；无 cookie 抛错。"""
        client = self._build_client()
        if client is None:
            raise Pan115Error("需要 115 cookie（PAN115_COOKIE / PAN115_COOKIE_FILE）才能创建分享")
        return client

    async def _call_with_margin(self, coro_fn, *, label: str, max_retry: int = 3):
        """调用 115 接口，margin 限速响应等待后重试（渐进，cap 30s）。

        margin 响应 {"margin": N} 无 state/data（check_response 放行后 data 取值炸），
        参照 P115-Share：等 N 秒重试，耗尽抛 Pan115Error。
        """
        import asyncio as _aio

        for attempt in range(1, max_retry + 1):
            resp = await coro_fn()
            if _is_margin_response(resp):
                wait = min(float(resp.get("margin", 5) or 5), self._MARGIN_WAIT_CAP)
                logger.warning(
                    "115 %s margin 限速：等待 %.0fs 重试（%d/%d）",
                    label, wait, attempt, max_retry,
                )
                await _aio.sleep(wait)
                continue
            return resp
        raise Pan115Error(f"115 {label} 限速，多次重试后仍失败")

    async def list_dir(self, cid: int = 0) -> list[dict]:
        """列自己网盘目录的**子目录**（fs_files + nf=1 仅目录，自动翻页）。

        返回 [{fid, name, size}]（均为目录），需登录 cookie。
        webapi 响应目录条目无 fid（id 在 cid 键）——p115client overview_attr 同款判定。
        """
        from p115client.client import check_response

        client = self._login_client()
        items: list[dict] = []
        offset = 0
        limit = 1000
        while True:
            resp = await self._call_with_margin(
                lambda off=offset: client.fs_files(
                    {"cid": cid, "limit": limit, "offset": off,
                     "nf": 1, "asc": 1, "o": "file_name"},
                    async_=True,
                ),
                label="fs_files",
            )
            try:
                check_response(resp)
            except Exception as exc:
                raise Pan115Error(f"列目录失败：{exc}", code=str(cid)) from exc
            data = resp.get("data") or {}
            batch = data.get("list") or []
            for it in batch:
                # webapi 格式：目录无 "fid" 键，目录 id 在 "cid"；文件 id 在 "fid"
                is_dir = "fid" not in it
                fid = int(it.get("cid") if is_dir else it.get("fid") or 0)
                items.append({
                    "fid": fid,
                    "name": str(it.get("n") or it.get("file_name") or ""),
                    "is_dir": is_dir,
                    "size": int(it.get("s") or it.get("file_size") or 0),
                })
            count = int(data.get("count") or 0)
            # 按实际拉取条数递增（limit=1000 时 offset+=limit 会一页跳过 count）
            offset += len(batch)
            if offset >= count or not batch:
                break
        return items

    async def resolve_path(self, path: str) -> int:
        """网盘路径 → cid（逐级下钻）。如 /媒体/新剧 → 逐段匹配目录名。

        大小写不敏感精确匹配；找不到抛 Pan115Error。需登录 cookie。
        """
        self._login_client()  # 校验 cookie 存在（列目录需登录态）
        parts = [p for p in (path or "").strip("/").split("/") if p]
        cid = 0
        walked: list[str] = []
        for part in parts:
            items = await self.list_dir(cid)
            hit = next(
                (it for it in items if it["is_dir"] and it["name"].lower() == part.lower()),
                None,
            )
            if hit is None:
                raise Pan115Error(
                    f"网盘目录不存在：/{'/'.join(walked + [part])}"
                    "（检查路径拼写，目录监控只支持已存在的目录）"
                )
            cid = hit["fid"]
            walked.append(part)
        return cid

    async def create_share(self, file_ids: int | str) -> tuple[str, str]:
        """创建**永久**分享，返回 (share_code, receive_code)。需登录 cookie。

        share_send 建分享（margin 渐进重试）→ share_update(duration=-1) 设永久
        （P115-Share 同款配方）。失败抛 Pan115Error。
        """
        from p115client.client import check_response

        client = self._login_client()
        fids = str(file_ids)

        resp = await self._call_with_margin(
            lambda: client.share_send(
                {"file_ids": fids, "ignore_warn": 1}, async_=True
            ),
            label="share_send",
        )
        try:
            check_response(resp)
        except Exception as exc:
            raise Pan115Error(f"创建分享失败：{exc}") from exc
        data = resp.get("data") or {}
        share_code = str(data.get("share_code") or "")
        receive_code = str(data.get("receive_code") or data.get("recv_code") or "")
        if not share_code:
            raise Pan115Error("创建分享失败：响应缺少 share_code")

        # 永久化（失败仅告警：默认分享也有较长有效期，下一轮巡检不受影响）
        try:
            upd = await self._call_with_margin(
                lambda: client.share_update(
                    {"share_code": share_code, "share_duration": -1}, async_=True
                ),
                label="share_update",
            )
            check_response(upd)
        except Exception as exc:  # noqa: BLE001
            logger.warning("分享设为永久失败（保留默认有效期）：%s", exc)
        return share_code, receive_code

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
