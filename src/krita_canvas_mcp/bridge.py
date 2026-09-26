"""MCP Server ↔ Krita 插件的 HTTP 桥。

插件在 127.0.0.1:5678/rpc 起一个 JSON-RPC 风格的接口，
请求体 {"method": "...", "params": {...}}，
响应体 {"ok": bool, "data": {...}} 或 {"ok": false, "err_code": "...", "message": "..."}。
"""
import httpx

from .errors import ErrCode, KritaError

DEFAULT_ENDPOINT = "http://127.0.0.1:5678/rpc"
DEFAULT_TIMEOUT = 60.0  # 导出大画布可能慢


class KritaBridge:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = DEFAULT_TIMEOUT):
        self.endpoint = endpoint
        self._client = httpx.Client(timeout=timeout)

    def call(self, method: str, params: dict | None = None) -> dict:
        try:
            resp = self._client.post(
                self.endpoint,
                json={"method": method, "params": params or {}},
            )
        except httpx.ConnectError as e:
            raise KritaError(
                ErrCode.KRITA_NOT_CONNECTED,
                f"无法连接 Krita 插件 ({self.endpoint})；"
                f"请确认 Krita 已打开且插件已启用。{e}",
            )
        except httpx.TimeoutException:
            raise KritaError(
                ErrCode.TIMEOUT,
                f"调用 {method} 超时（>{self._client.timeout.read}s）",
            )

        if resp.status_code != 200:
            raise KritaError(
                ErrCode.IO_ERROR,
                f"Krita 插件返回 HTTP {resp.status_code}: {resp.text[:200]}",
            )

        try:
            body = resp.json()
        except ValueError:
            raise KritaError(ErrCode.IO_ERROR, "Krita 插件返回非 JSON")

        if not body.get("ok", False):
            code_str = body.get("err_code", "IO_ERROR")
            try:
                code = ErrCode(code_str)
            except ValueError:
                code = ErrCode.IO_ERROR
            raise KritaError(code, body.get("message", "unknown error"))

        return body.get("data", {})


# 单例，所有工具共享
bridge = KritaBridge()