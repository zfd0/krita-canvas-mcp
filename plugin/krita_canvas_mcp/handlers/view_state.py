# 视图状态读写类工具实现。
# get_view_state / set_view_state
from krita import Krita


def _view():
    """取活动视图，无则报错。"""
    app = Krita.instance()
    win = app.activeWindow()
    if win is None or win.activeView() is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return win.activeView()


def _fg_hex(view) -> str:
    """活动前景色 → #RRGGBB。"""
    try:
        col = view.foregroundColor()
        if col is not None:
            qc = col.colorForCanvas(view.canvas())
            return "#%02X%02X%02X" % (qc.red(), qc.green(), qc.blue())
    except Exception:
        pass
    return None


def _canvas(view):
    """取视图画布；无画布（如视图未就绪/浮动窗口异常）时报错。"""
    canvas = view.canvas()
    if canvas is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT: 视图无可用画布")
    return canvas


def get_view_state(params: dict) -> dict:
    """读回视图（缩放/旋转/镜像）与当前绘画参数。"""
    view = _view()
    canvas = _canvas(view)
    preset = view.currentBrushPreset()
    return {
        "zoom": round(canvas.zoomLevel(), 4),
        "rotation": round(canvas.rotation(), 2),
        "mirror": canvas.mirror(),
        "brush_size": round(view.brushSize(), 2),
        "opacity": round(view.paintingOpacity(), 3),
        "flow": round(view.paintingFlow(), 3),
        "brush_rotation": round(view.brushRotation(), 2),
        "pattern_size": round(view.patternSize(), 2),
        "blending_mode": view.currentBlendingMode(),
        "eraser_mode": view.eraserMode(),
        "global_alpha_lock": view.globalAlphaLock(),
        "disable_pressure": view.disablePressure(),
        "foreground": _fg_hex(view),
        "brush_preset": preset.name() if preset is not None else None,
    }


def set_view_state(params: dict) -> dict:
    """设置视图：缩放/平移/旋转/镜像/复位。"""
    view = _view()
    canvas = _canvas(view)
    applied = []
    if params.get("reset_view"):
        canvas.resetZoom()
        canvas.resetRotation()
        canvas.setMirror(False)
        applied.extend(["reset_zoom", "reset_rotation", "mirror_off"])
    else:
        if "zoom" in params:
            canvas.setZoomLevel(float(params["zoom"]))
            applied.append("zoom")
        if "rotation" in params:
            canvas.setRotation(float(params["rotation"]))
            applied.append("rotation")
        if "mirror" in params:
            canvas.setMirror(bool(params["mirror"]))
            applied.append("mirror")
        if "center_to" in params:
            from PyQt6.QtCore import QPointF
            x, y = params["center_to"]
            canvas.setPreferredCenter(QPointF(float(x), float(y)))
            applied.append("center_to")
    return {"applied": applied,
            "zoom": round(canvas.zoomLevel(), 4),
            "rotation": round(canvas.rotation(), 2),
            "mirror": canvas.mirror()}