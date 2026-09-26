import json
from typing import Any

from .errors import KritaError


def ok(data: Any = None, meta: dict | None = None) -> str:
    """成功返回。MCP 工具应 return 本函数结果（JSON 字符串）。"""
    payload = {"ok": True, "data": data if data is not None else {}}
    if meta:
        payload["meta"] = meta
    return json.dumps(payload, ensure_ascii=False)


def err(exc: KritaError) -> str:
    """失败返回。约定：MCP isError 恒为 false，错误信息在 JSON 里。"""
    return json.dumps(exc.to_dict(), ensure_ascii=False)