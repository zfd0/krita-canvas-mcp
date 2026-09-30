"""公共工具转发：所有 MCP 工具统一经 call() 转发到 Krita 插件，
并自动登记动作历史（供 get_action_history 使用）。"""
import json
import time

from ..bridge import bridge
from ..envelope import err, ok
from ..errors import KritaError

from . import session_store

# 不做历史登记的只读观测方法（避免噪音）
_QUIET_METHODS = {
    "get_canvas_snapshot", "get_document_info", "get_node_tree",
    "get_view_state", "list_documents", "get_krita_info",
    "get_snapshot", "wait_for_done",
}


def call(method: str, params: dict | None = None, *,
         quiet: bool = False) -> str:
    """转发一次 RPC 调用并记入动作历史。
    返回 MCP 工具约定的 JSON 字符串（envelope）。
    """
    params = params or {}
    t0 = time.time()
    try:
        data = bridge.call(method, params)
        elapsed = int((time.time() - t0) * 1000)
        if method not in _QUIET_METHODS and not quiet:
            session_store.store.record_action(
                method, session_store.store.action_digest(params),
                True, elapsed,
                result_snippet=json.dumps(data, ensure_ascii=False))
        return ok(data)
    except KritaError as e:
        elapsed = int((time.time() - t0) * 1000)
        if method not in _QUIET_METHODS and not quiet:
            session_store.store.record_action(
                method, session_store.store.action_digest(params),
                False, elapsed)
        return err(e)