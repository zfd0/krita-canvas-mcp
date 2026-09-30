"""MCP 进程内会话状态存储（线程安全）。

有状态工具（get_target_image/diff_with_target/get_action_history/
get_session_state/abort_session/extract_palette 等）共享此单例。
Agent 闭环(agent/)持有自己的状态，与本存储互相独立。
"""
from __future__ import annotations

import base64
import threading

import numpy as np
from PIL import Image


class SessionStore:
    """会话记忆：目标图、动作历史、调色板、进度快照。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.target_path: str | None = None
        self.target_arr: np.ndarray | None = None      # RGB uint8 (画布尺寸对齐)
        self.target_b64: str | None = None
        self.target_size: tuple | None = None          # 原始 (w, h)
        self.history: list[dict] = []                  # 动作记录
        self.iteration: int = 0
        self.stage: str = "A"
        self.palette: list[str] | None = None
        self.baseline_hash: str | None = None
        self.last_snapshot_hash: str | None = None
        self.covered_series: list[float] = []          # 进度序列(停滞/进度用)
        self.aborted: bool = False

    # ------------------------------------------------------------ 基础读写

    def record_action(self, method: str, params_digest: dict, ok: bool,
                      elapsed_ms: int, result_snippet: str = "") -> None:
        """登记一次工具调用（供 get_action_history）。"""
        with self._lock:
            self.iteration += 1
            self.history.append({
                "iter": self.iteration, "stage": self.stage,
                "action": method, "params_digest": params_digest,
                "ok": ok, "elapsed_ms": elapsed_ms,
                "result": result_snippet[:200],
            })

    def snapshot(self) -> dict:
        """当前会话状态摘要。"""
        with self._lock:
            return {
                "iteration": self.iteration, "stage": self.stage,
                "target_path": self.target_path,
                "target_size": list(self.target_size or []),
                "history_count": len(self.history),
                "palette": self.palette,
                "aborted": self.aborted,
            }

    def action_digest(self, params: dict) -> dict:
        """工具参数 → 轻量摘要（历史展示用，剔除大字段）。"""
        d = {}
        for k in ("tool", "op", "node_type", "name", "mode", "steps"):
            if k in params and not isinstance(params[k], (dict, list)):
                d[k] = params[k]
        if "points" in params:
            d["points_count"] = len(params.get("points") or [])
        if "image_b64" in params:
            d["image_b64"] = f"<{len(params['image_b64']) // 1024}KB>"
        if "color" in params:
            d["color"] = params["color"]
        if "foreground" in params:
            d["color"] = params["foreground"]
        return d

    # ------------------------------------------------------------ 目标图

    def set_target(self, path: str, work_size: tuple) -> dict:
        """加载目标图并缓存（等比缩放到 work_size 供 diff 对齐）。"""
        img = Image.open(path).convert("RGB")
        orig = img.size
        if img.size != work_size:
            img = img.resize(work_size)
        arr = np.asarray(img, dtype=np.uint8)
        with self._lock:
            self.target_path = path
            self.target_arr = arr
            self.target_size = (orig[0], orig[1])
            import io
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            self.target_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"path": path, "original_size": list(orig),
                "work_size": list(work_size)}

    def get_target(self) -> tuple:
        """(path, b64, work_size)；未设置抛 KeyError 语义由调用方处理。"""
        with self._lock:
            if self.target_arr is None:
                return None
            h, w = self.target_arr.shape[:2]
            return self.target_path, self.target_b64, (w, h)

    def reset(self) -> None:
        """abort 清理。"""
        with self._lock:
            self.target_path = None
            self.target_arr = None
            self.target_b64 = None
            self.palette = None
            self.covered_series.clear()
            self.baseline_hash = None
            self.aborted = True


# 全局单例
store = SessionStore()