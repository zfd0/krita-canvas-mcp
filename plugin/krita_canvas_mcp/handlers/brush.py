# 笔刷/颜色/采样类工具实现。
# set_colors / set_brush_params / set_brush_preset / list_resources /
# sample_color（canvas、node 源）/ undo / redo / set_blending_mode / set_brush_flags
from krita import Krita, ManagedColor
from PyQt6.QtGui import QColor, QImage

import colorsys


def _active_view():
    """取活动视图；无窗口时报错。"""
    app = Krita.instance()
    win = app.activeWindow()
    if win is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT: 无窗口")
    view = win.activeView()
    if view is None:
        # 兜底：取第一个视图
        views = win.views()
        view = views[0] if views else None
    if view is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT: 无视图，请先 create_document")
    return view


def _parse_color(value) -> tuple:
    """颜色统一解析：支持 [r,g,b(,a)](0-255 或 0-1 浮点) 或 '#RRGGBB(AA)'。返回 (r,g,b,a) 0-255。"""
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        s = s + "FF" * (len(s) in (6, 8) and 0 or 1)
        if len(s) == 6:
            s += "FF"
        if len(s) != 8:
            raise RuntimeError("INVALID_PARAM: 颜色 hex 格式错误")
        try:
            r, g, b, a = (int(s[i:i + 2], 16) for i in (0, 2, 4, 6))
        except ValueError:
            raise RuntimeError("INVALID_PARAM: 颜色 hex 解析失败")
        return r, g, b, a
    if isinstance(value, (list, tuple)) and 3 <= len(value) <= 4:
        vals = [float(v) for v in value]
        if max(vals) <= 1.0:  # 0-1 浮点
            vals = [v * 255.0 for v in vals]
        if len(vals) == 3:
            vals.append(255.0)
        return tuple(int(round(v)) for v in vals)
    raise RuntimeError("INVALID_PARAM: 颜色格式应为 hex 或 RGBA 列表")


def _make_managed_color(value):
    """http 手写 QColor → ManagedColor（以活动视图的显示色彩管理为准）。"""
    r, g, b, a = _parse_color(value)
    col = ManagedColor.fromQColor(QColor(r, g, b, a))
    return col


def set_colors(params: dict) -> dict:
    """设置前景/背景色。"""
    view = _active_view()
    out = {}
    if "foreground" in params:
        view.setForeGroundColor(_make_managed_color(params["foreground"]))
        out["foreground"] = params["foreground"]
    if "background" in params:
        view.setBackGroundColor(_make_managed_color(params["background"]))
        out["background"] = params["background"]
    return out


def set_brush_params(params: dict) -> dict:
    """批量设置画笔参数（缺省项不动）。"""
    view = _active_view()
    applied = []
    if "size" in params:
        view.setBrushSize(float(params["size"])); applied.append("size")
    if "opacity" in params:
        view.setPaintingOpacity(float(params["opacity"])); applied.append("opacity")
    if "flow" in params:
        view.setPaintingFlow(float(params["flow"])); applied.append("flow")
    if "rotation" in params:
        view.setBrushRotation(float(params["rotation"])); applied.append("rotation")
    if "pattern_size" in params:
        view.setPatternSize(float(params["pattern_size"])); applied.append("pattern_size")
    return {"applied": applied}


def list_resources(params: dict) -> dict:
    """枚举指定类型的资源名列表（preset/brush/pattern/gradient/palette/workspace）。"""
    rtype = params.get("resource_type", "preset")
    app = Krita.instance()
    res = app.resources(rtype)
    names = sorted(res.keys()) if res else []
    flt = params.get("filter")
    if flt:
        names = [n for n in names if flt.lower() in n.lower()]
    return {"resource_type": rtype, "count": len(names), "names": names}


def set_brush_preset(params: dict) -> dict:
    """按名称激活笔刷预设。"""
    view = _active_view()
    app = Krita.instance()
    rtype = params.get("resource_type", "preset")
    resources = app.resources(rtype)
    name = params["preset_name"]
    key = None
    for k in resources.keys():
        if k == name:
            key = k
            break
    if key is None:
        raise RuntimeError(f"ACTION_NOT_FOUND: 预设不存在 {name}")
    view.setCurrentBrushPreset(resources[key])
    return {"preset": key}


def set_blending_mode(params: dict) -> dict:
    """设置画笔(view级)或图层(node级)混合模式。scope 默认 brush。"""
    scope = params.get("scope", "brush")
    if scope == "node":
        node_id = params.get("node_id")
        if not node_id:
            raise RuntimeError("INVALID_PARAM: scope=node 时需要 node_id")
        doc = Krita.instance().activeDocument()
        if doc is None:
            raise RuntimeError("NO_ACTIVE_DOCUMENT")
        node = _node(doc, node_id)
        if node is None:
            raise RuntimeError(f"INVALID_NODE: 找不到节点 {node_id}")
        node.setBlendingMode(str(params["mode"]))
        doc.refreshProjection()
        return {"scope": "node", "node_id": node_id, "mode": params["mode"]}
    # brush scope (default)
    view = _active_view()
    view.setCurrentBlendingMode(str(params["mode"]))
    return {"scope": "brush", "mode": params["mode"]}


def set_brush_flags(params: dict) -> dict:
    """橡皮/锁定透明/禁用压感开关。"""
    view = _active_view()
    out = {}
    if "eraser_mode" in params:
        view.setEraserMode(bool(params["eraser_mode"])); out["eraser_mode"] = params["eraser_mode"]
    if "global_alpha_lock" in params:
        view.setGlobalAlphaLock(bool(params["global_alpha_lock"])); out["global_alpha_lock"] = params["global_alpha_lock"]
    if "disable_pressure" in params:
        view.setDisablePressure(bool(params["disable_pressure"])); out["disable_pressure"] = params["disable_pressure"]
    return out


def undo(params: dict) -> dict:
    """撤销一步或多步（撤销前先等笔刷任务完成，避免错位）。"""
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    if params.get("flush_before", True):
        doc.waitForDone()
    act = app.action("edit_undo")
    if act is None:
        raise RuntimeError("ACTION_NOT_FOUND: edit_undo")
    for _ in range(int(params.get("steps", 1))):
        act.trigger()
    doc.refreshProjection()
    return {"undone": int(params.get("steps", 1))}


def redo(params: dict) -> dict:
    """重做被撤销的操作。"""
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    act = app.action("edit_redo")
    if act is None:
        raise RuntimeError("ACTION_NOT_FOUND: edit_redo")
    for _ in range(int(params.get("steps", 1))):
        act.trigger()
    doc.refreshProjection()
    return {"redone": int(params.get("steps", 1))}


# ---------- 颜色采样 ----------

def _rgb_to_lab(rgb: tuple) -> tuple:
    """sRGB(0-255) → CIELAB(D65)。用于采样输出与差异度量。"""
    r, g, b = (v / 255.0 for v in rgb)
    def _f(t):
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)
    r, g, b = (_f(v) for v in (r, g, b))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    x, y, z = (_f(v) for v in (x, y, z))
    return (116 * y - 16, 500 * (x - y), 200 * (y - z))


def _pack_color(rgb_float: tuple):
    """(r,g,b) 0-255浮点 → 多色彩空间表示dict。"""
    r, g, b = rgb_float
    ri, gi, bi = int(round(r)), int(round(g)), int(round(b))
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    l, a, bb = _rgb_to_lab((ri, gi, bi))
    return {
        "srgb_hex": "#%02X%02X%02X" % (ri, gi, bi),
        "srgb_255": [ri, gi, bi],
        "srgb_float": [round(r / 255.0, 3), round(g / 255.0, 3), round(b / 255.0, 3)],
        "lab": [round(l, 1), round(a, 1), round(bb, 1)],
        "hsv": [round(h * 360), round(s * 100, 1), round(v * 100, 1)],
    }


def _reduce_pixels(pixels, reduce: str) -> tuple:
    """像素集合 → 代表色。pixels: list[(r,g,b,a)]。"""
    try:
        import numpy as np
        arr = np.array(pixels, dtype=np.float32)
        means = arr[:, :3].mean(axis=0)
    except ImportError:
        # 无 numpy 降级：只支持 mean
        n = len(pixels)
        means = (sum(p[0] for p in pixels) / n,
                 sum(p[1] for p in pixels) / n,
                 sum(p[2] for p in pixels) / n)
        return means
    if reduce == "mean":
        return tuple(means)
    if reduce == "median":
        return tuple(arr[:, :3].median(axis=0))
    if reduce in ("mode", "dominant"):
        # 量化到 4bit/通道后统计众数
        q = (arr[:, :3] // 16 * 16 + 8)
        uniq, counts = np.unique(q, axis=0, return_counts=True) \
            if hasattr(np, "unique") else (None, None)
        if uniq is None:
            return tuple(means)
        return tuple(uniq[int(np.argmax(counts))])
    return tuple(means)


def sample_color(params: dict) -> dict:
    """从画布/目标图/图层采样颜色（source in target/canvas/node；target 源由 MCP 会话处理）。
    color_space 控制输出格式，exclude_alpha_below 过滤低 alpha 像素，multiple 支持批量采样。"""
    doc = Krita.instance().activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")

    color_space = params.get("color_space", "srgb_hex")
    exclude_alpha_below = float(params.get("exclude_alpha_below", 0.1))
    multiple = params.get("multiple")

    def _sample_one(px: int, py: int) -> dict:
        """对单个坐标执行采样。"""
        radius = int(params.get("radius", 0))
        reduce = params.get("reduce", "median")

        img = doc.projection(max(0, px - radius), max(0, py - radius),
                             radius * 2 + 1, radius * 2 + 1)
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        w, h = img.width(), img.height()
        pixels = []
        line = img.bytesPerLine()
        raw = img.bits().asstring(img.sizeInBytes())
        for i in range(h):
            for j in range(w):
                off = i * line + j * 4
                b, g, r = raw[off], raw[off + 1], raw[off + 2]
                a = raw[off + 3]
                if a / 255.0 < exclude_alpha_below:
                    continue
                pixels.append((r, g, b, a))

        if not pixels:
            return {"x": px, "y": py, "error": "all pixels excluded by alpha threshold"}

        rep = _reduce_pixels(pixels, reduce)
        packed = _pack_color(rep)
        # 按 color_space 返回指定格式
        if color_space in packed:
            result_color = {color_space: packed[color_space]}
        else:
            result_color = packed  # fallback 返回全部
        return {
            "x": px, "y": py, "radius": radius, "reduce": reduce,
            "samples_count": len(pixels),
            "color": result_color,
            "source": params.get("source", "canvas"),
        }

    if multiple:
        coords = [(p["x"], p["y"]) for p in multiple]
        results = [_sample_one(cx, cy) for cx, cy in coords]
        return {"multiple": results}
    return _sample_one(int(params.get("x", 0)), int(params.get("y", 0)))