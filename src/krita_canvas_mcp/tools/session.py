"""会话/文档枚举 + 有状态闭环观测工具 (mcp 2.x)。

会话状态（目标图/历史/调色板/进度）由 MCP 进程内 SessionStore 维护，
diff/target 等工具依赖它（闭环决策与终止判断的核心输入）。
"""
import base64
import io
import json
import time

import numpy as np
from PIL import Image, ImageFilter
from mcp.server.mcpserver import MCPServer

from ..agent.session_state import CanvasMetrics
from ..envelope import err, ok
from ..errors import ErrCode, KritaError
from ._common import call as _call
from .session_store import store


def _snapshot_arr(max_side: int = 1024) -> np.ndarray:
    """当前画布快照 → numpy RGB。"""
    data = _decode(_call("get_canvas_snapshot", {"max_side": max_side}))
    img = Image.open(io.BytesIO(base64.b64decode(data["image_b64"]))).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _decode(text: str) -> dict:
    """MCP envelope JSON 字符串 → data dict；失败抛 KritaError。"""
    obj = json.loads(text)
    if not obj.get("ok"):
        raise KritaError(ErrCode(obj.get("err_code", "IO_ERROR")),
                         obj.get("message", "unknown"))
    return obj.get("data", {})


def _canvas_size() -> tuple:
    info = _decode(_call("get_document_info", {}, quiet=True))
    return int(info["width"]), int(info["height"])


def _kmeans(pixels: np.ndarray, k: int, iters: int = 10) -> np.ndarray:
    """手写 k-means（无 sklearn 依赖）。pixels: (N,3) float。"""
    rng = np.random.default_rng(0)
    n = len(pixels)
    if n <= k:
        return pixels
    centers = pixels[rng.choice(n, k, replace=False)].astype(np.float64)
    for _ in range(iters):
        d = ((pixels[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
        labels = d.argmin(1)
        new = np.array([
            pixels[labels == i].mean(0) if (labels == i).any() else centers[i]
            for i in range(k)])
        if np.allclose(new, centers):
            break
        centers = new
    return centers


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="list_documents",
        description="枚举当前 Krita 打开的全部文档(多文档寻址用)。",
    )
    def list_documents() -> str:
        return _call("list_documents", {}, quiet=True)

    # ------------------------------------------------------------ 目标图

    @mcp.tool(
        name="set_target_image",
        description="登记闭环临摹的目标图像（加载缓存，供 diff/validate 使用）。",
    )
    def set_target_image(file_path: str) -> str:
        cw, ch = _canvas_size()
        return ok(store.set_target(file_path, (cw, ch)))

    @mcp.tool(
        name="get_target_image",
        description="获取目标图像 PNG(base64)。variant 可选 original/gray/edge/"
                    "palette_quantized。",
    )
    def get_target_image(
        region: dict | None = None, max_side: int = 1024,
        variant: str = "original", quantize_colors: int = 8,
        include_alpha: bool = False,
    ) -> str:
        tgt = store.get_target()
        if tgt is None:
            return err(KritaError(ErrCode.NO_TARGET_IMAGE,
                                  "尚未设置目标图像，请先调用 set_target_image"))
        path, b64, (w, h) = tgt
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        if variant == "gray":
            img = img.convert("L").convert("RGB")
        elif variant == "edge":
            img = img.convert("L").filter(ImageFilter.FIND_EDGES).convert("RGB")
        elif variant == "palette_quantized":
            img = img.quantize(colors=int(quantize_colors)).convert("RGB")
        if region:
            r = region
            img = img.crop((int(r["x"]), int(r["y"]),
                            int(r["x"]) + int(r["width"]),
                            int(r["y"]) + int(r["height"])))
        if max(img.size) > max_side:
            s = max_side / max(img.size)
            img = img.resize((int(img.width * s), int(img.height * s)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return ok({"image_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
                   "mime_type": "image/png", "variant": variant,
                   "region": region,
                   "size": [img.width, img.height],
                   "source_path": path})

    @mcp.tool(
        name="validate_canvas_target",
        description="校验画布与目标图尺寸/色彩模型是否匹配。",
    )
    def validate_canvas_target(
        document_id: str | None = None, strict: bool = True,
    ) -> str:
        tgt = store.get_target()
        cw, ch = _canvas_size()
        info = _decode(_call("get_document_info",
                             {"document_id": document_id}, quiet=True))
        if tgt is None:
            return ok({"valid": False, "mismatch_reason": "no_target",
                       "canvas_size": [cw, ch]})
        path, b64, (tw, th) = tgt
        valid = (cw, ch) == (tw, th)
        reason = None if valid else "size"
        if not valid and strict:
            return err(KritaError(
                ErrCode.SIZE_MISMATCH,
                f"画布 {cw}x{ch} 与目标图 {tw}x{th} 尺寸不一致"))
        return ok({"valid": valid, "canvas_size": [cw, ch],
                   "target_size": [tw, th], "mismatch_reason": reason,
                   "color_model": info.get("color_model")})

    # ------------------------------------------------------------ 差异/进度

    @mcp.tool(
        name="diff_with_target",
        description="画布与目标图的差异：标量指标(mae/rmse/psnr/ssim/ΔE)+热点+可选热力图。",
    )
    def diff_with_target(
        region: dict | None = None, max_side: int = 1024,
        mode: str = "perceptual", include_heatmap: bool = True,
        top_k_hotspots: int = 8,
    ) -> str:
        tgt = store.get_target()
        if tgt is None:
            return err(KritaError(ErrCode.NO_TARGET_IMAGE,
                                  "尚未设置目标图像"))
        path, b64, (w, h) = tgt
        target = np.asarray(
            Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"),
            dtype=np.uint8)
        canvas = _snapshot_arr(max(w, h))
        if canvas.shape[:2] != target.shape[:2]:
            img = Image.fromarray(canvas).resize((w, h))
            canvas = np.asarray(img, dtype=np.uint8)
        m = CanvasMetrics(target, canvas)
        scalars = m.scalars()
        result = {"metrics": scalars,
                  "covered_pct": round(m.covered_pct(), 4),
                  "mode": mode,
                  "hotspots": m.hotspots(top_k_hotspots)}
        if include_heatmap:
            b64h, hw, hh = m.heatmap_b64(512)
            result["heatmap_png_b64"] = b64h
            result["heatmap_region"] = {"x": 0, "y": 0,
                                        "width": hw, "height": hh}
        store.covered_series.append(m.covered_pct())
        return ok(result)

    @mcp.tool(
        name="get_paint_progress",
        description="画布相对基线的推进统计：覆盖度/变化/未触及区域。",
    )
    def get_paint_progress(
        against: str = "target", region: dict | None = None,
        include_mask: bool = False, recent_rounds: int = 3,
    ) -> str:
        tgt = store.get_target()
        if tgt is None:
            return err(KritaError(ErrCode.NO_TARGET_IMAGE,
                                  "尚未设置目标图像"))
        path, b64, (w, h) = tgt
        target = np.asarray(
            Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"),
            dtype=np.uint8)
        canvas = _snapshot_arr(max(w, h))
        if canvas.shape[:2] != target.shape[:2]:
            canvas = np.asarray(
                Image.fromarray(canvas).resize((w, h)), dtype=np.uint8)
        m = CanvasMetrics(target, canvas)
        cov = m.covered_pct()
        series = store.covered_series
        delta_last = round(cov - series[-2], 4) if len(series) >= 2 else 0.0
        delta_stage = round(cov - series[0], 4) if len(series) >= 2 else 0.0
        result = {"against": against,
                  "overall": {"covered_pct": round(cov, 4),
                              "covered_pct_delta_last_action": delta_last,
                              "covered_pct_delta_last_stage": delta_stage},
                  "recent_rounds": [round(v, 4) for v in series[-recent_rounds:]]}
        if include_mask:
            b64h, hw, hh = m.heatmap_b64(512)
            result["mask_png_b64"] = b64h
            result["mask_region"] = {"x": 0, "y": 0,
                                     "width": hw, "height": hh}
        return ok(result)

    # ------------------------------------------------------------ 历史/状态

    @mcp.tool(
        name="get_action_history",
        description="拉取最近的工具调用记录（full/summary/stats_only）。",
    )
    def get_action_history(
        last_n: int = 10, since_iteration: int = 0,
        format: str = "summary",
        filter_action: list | None = None,
        include_failed: bool = False,
    ) -> str:
        hist = [h for h in store.history if h["iter"] > since_iteration]
        if not include_failed:
            hist = [h for h in hist if h["ok"]]
        if filter_action:
            hist = [h for h in hist if h["action"] in filter_action]
        hist = hist[-last_n:]
        by_stage, by_action, failed = {}, {}, 0
        for h in store.history:
            by_stage[h["stage"]] = by_stage.get(h["stage"], 0) + 1
            by_action[h["action"]] = by_action.get(h["action"], 0) + 1
            failed += 0 if h["ok"] else 1
        if format == "stats_only":
            data = {"total_iterations": store.iteration, "by_stage": by_stage,
                    "by_action": by_action, "failed_count": failed}
        elif format == "summary":
            data = {"total_iterations": store.iteration,
                    "by_stage": by_stage, "by_action": by_action,
                    "failed_count": failed,
                    "recent": [h["action"] for h in hist[-5:]]}
        else:
            data = {"total_iterations": store.iteration,
                    "returned_count": len(hist), "actions": hist}
        return ok(data)

    @mcp.tool(
        name="get_session_state",
        description="当前会话状态：iteration/stage/target/历史统计。",
    )
    def get_session_state(with_history: bool = False) -> str:
        snap = store.snapshot()
        if with_history:
            snap["history_summary"] = {
                "actions": [h["action"] for h in store.history[-20:]],
                "stage": store.stage,
            }
        return ok(snap)

    @mcp.tool(
        name="abort_session",
        description="终止当前会话：可选保存部分结果/清空画布，清理目标缓存。",
    )
    def abort_session(
        session_id: str | None = None, save_partial: bool = True,
        keep_canvas: bool = True,
    ) -> str:
        path = None
        if save_partial:
            try:
                data = _decode(_call("get_canvas_snapshot", {}, quiet=True))
                import os
                os.makedirs("outputs", exist_ok=True)
                path = f"outputs/partial_{int(time.time())}.png"
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data["image_b64"]))
            except Exception:
                path = None
        if not keep_canvas:
            try:
                _decode(_call("execute_action", {"action_name": "clear"}))
            except KritaError:
                pass
        iters = store.iteration
        store.reset()
        return ok({"aborted_at_iteration": iters,
                   "partial_saved_to": path,
                   "history_retained": False})

    # ------------------------------------------------------------ 调色板/哈希

    @mcp.tool(
        name="extract_palette",
        description="从画布合成画面提取主色调色板(kmeans 或 median_cut)。",
    )
    def extract_palette(
        max_colors: int = 8, quantize_algorithm: str = "kmeans",
        save_as_palette: str | None = None,
    ) -> str:
        canvas = _snapshot_arr(512)
        flat = canvas.reshape(-1, 3).astype(np.float64)
        if quantize_algorithm == "median_cut":
            img = Image.fromarray(canvas).quantize(colors=int(max_colors))
            pal = img.getpalette()[: max_colors * 3]
            colors = ["#%02X%02X%02X" % tuple(pal[i:i + 3])
                      for i in range(0, len(pal), 3)]
        else:
            centers = _kmeans(flat, int(max_colors))
            colors = ["#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))
                      for c in centers]
        if save_as_palette:
            store.palette = colors
        return ok({"algorithm": quantize_algorithm, "colors": colors,
                   "saved_as": save_as_palette})

    @mcp.tool(
        name="get_palette",
        description="读回上次保存的调色板（extract_palette save_as_palette）。",
    )
    def get_palette() -> str:
        if store.palette is None:
            return err(KritaError(ErrCode.NO_TARGET_IMAGE,
                                  "尚无保存的调色板"))
        return ok({"colors": store.palette})

    @mcp.tool(
        name="get_snapshot_hash",
        description="当前画布快照哈希（连调两次可判断画布是否变化）。",
    )
    def get_snapshot_hash(
        document_id: str | None = None, algorithm: str = "perceptual",
        region: dict | None = None,
    ) -> str:
        data = _decode(_call("get_canvas_snapshot",
                             {"document_id": document_id, "region": region},
                             quiet=True))
        h = data.get("snapshot_hash", "")
        changed = store.last_snapshot_hash is not None \
            and store.last_snapshot_hash != h
        store.last_snapshot_hash = h
        return ok({"hash": h, "algorithm": algorithm,
                   "changed_since_last": changed})