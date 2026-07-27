"""网盘相关异常。

Pan115Error 独立定义（不依赖 p115client），p115client 装坏时
handlers 仍可正常导入此异常，保留 except 分支语义。
"""


class Pan115Error(Exception):
    """115 网盘操作异常（鉴权失败、分享失效、风控等）。"""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
