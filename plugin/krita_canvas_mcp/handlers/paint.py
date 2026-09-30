# LibKis 绘画执行类工具实现。所有函数在 Krita 主线程由 dispatcher 调用。
# 对应 Node.paintLine / paintPath / paintPolygon / paintEllipse / paintRectangle / setPixelData。
from PyQt6.QtCore import QByteArray, QPointF, QRectF
from PyQt6.QtGui import QImage, QPainterPath
from krita import Krita

import base64


def _active_doc(params: dict):
    """取目标文档：优先 document_id（当前仅支持活动文档），无活动文档则报错。"""
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _resolve_node(doc, node_id):
    """按 node_id（uuid 或名称）定位节点；为空则取活动节点。"""
    if not node_id:
        return doc.activeNode()
    try:
        n = doc.nodeByUniqueID(node_id)
        if n is not None:
            return n
    except Exception:
        pass
    return doc.nodeByName(node_id)


def _style(s):
    """stroke/fill 风格字符串安全默认。LibKis 约定 ForegroundColor/BackgroundColor/None/Pattern。"""
    return s or "ForegroundColor"


def paint_line(params: dict) -> dict:
    """两点直线，可带首尾压感。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    node.paintLine(
        QPointF(params["x1"], params["y1"]),
        QPointF(params["x2"], params["y2"]),
        params.get("pressure1", 1.0),
        params.get("pressure2", 1.0),
        _style(params.get("stroke_style")),
    )
    return {"drawn": True}


def _build_path(points, smooth: bool, closed: bool) -> QPainterPath:
    """折线/贝塞尔路径构造：
    - smooth=True 时用二次贝塞尔（各段以相邻控制点平滑连接）
    - closed=True 闭合路径（配合 fill 填充）
    """
    path = QPainterPath()
    pts = [QPointF(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 2:
        raise RuntimeError("INVALID_PARAM: points 至少 2 个")
    path.moveTo(pts[0])
    if smooth:
        # 中点二次贝塞尔：控制点取当前点，终点取其与下一顶点中点
        for i in range(1, len(pts) - 1):
            mid = QPointF((pts[i].x() + pts[i + 1].x()) / 2,
                          (pts[i].y() + pts[i + 1].y()) / 2)
            path.quadTo(pts[i], mid)
        path.lineTo(pts[-1])
    else:
        for p in pts[1:]:
            path.lineTo(p)
    if closed:
        path.closeSubpath()
    return path


def paint_path(params: dict) -> dict:
    """自由笔画：LLM 预测折线顶点，用当前笔刷偶发绘制。smooth 贝塞尔插值。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    path = _build_path(params["points"],
                       params.get("smooth", True),
                       params.get("closed", False))
    node.paintPath(
        path,
        _style(params.get("stroke_style")),
        _style(params.get("fill_style", "None")),
    )
    return {"drawn": True}


def paint_shape(params: dict) -> dict:
    """椭圆/矩形/多边形色块：stroke_style 描边 + fill_style 填充。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    shape = params["shape"]
    stroke = _style(params.get("stroke_style", "None"))
    fill = _style(params.get("fill_style", "ForegroundColor"))

    if shape in ("ellipse", "rectangle"):
        r = params["rect"]
        rect = QRectF(r["x"], r["y"], r["width"], r["height"])
        if shape == "ellipse":
            node.paintEllipse(rect, stroke, fill)
        else:
            node.paintRectangle(rect, stroke, fill)
    elif shape == "polygon":
        pts = [QPointF(p[0], p[1]) for p in params["points"]]
        node.paintPolygon(pts, stroke, fill)
    else:
        raise RuntimeError(f"INVALID_PARAM: 未知 shape {shape}")
    return {"drawn": True}


def _image_from_b64(b64: str) -> QImage:
    """base64 PNG → QImage（转 ARGB32，其内存字节序在小端机为 B,G,R,A，与 setPixelData 期望一致）。"""
    raw = base64.b64decode(b64)
    img = QImage.fromData(QByteArray(raw), "PNG")
    if img.isNull():
        raise RuntimeError("INVALID_PARAM: 图片解码失败")
    return img.convertToFormat(QImage.Format_ARGB32)


def write_pixels(params: dict) -> dict:
    """把 LLM 生成的像素补丁直接写入图层指定区域。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    img = _image_from_b64(params["image_b64"])
    if img.width() != params["width"] or img.height() != params["height"]:
        raise RuntimeError("INVALID_PARAM: 图片尺寸与 width/height 不一致")

    # 逐行取原始字节（bytesPerLine 可能含对齐填充，按需裁剪到行宽）
    # PyQt6 下用 bits().asstring() 一次性取整块内存，兼容性好于逐行 scanLine
    line = img.bytesPerLine()
    need = img.width() * 4
    raw = img.bits().asstring(img.byteCount())
    data = b"".join(
        raw[i * line:i * line + need] for i in range(img.height())
    )

    blend = params.get("blend_mode", "overwrite")
    if blend != "overwrite":
        # alpha_composite：读原图层像素，按 alpha 合成（numpy 可用则向量化）
        try:
            import numpy as np
        except ImportError:
            raise RuntimeError("NOT_IMPLEMENTED: alpha_composite 需要 numpy")
        cur_raw = bytes(node.pixelData(params["x"], params["y"],
                                       params["width"], params["height"]))
        cur = np.frombuffer(cur_raw, dtype=np.uint8).reshape(
            params["height"], params["width"], 4).astype(np.float32)
        new = np.frombuffer(data, dtype=np.uint8).reshape(
            params["height"], params["width"], 4).astype(np.float32)
        # BGRA 序：alpha 在第 4 通道
        a_new = new[..., 3:4] / 255.0
        out = new * a_new + cur * (1 - a_new)
        data = out.astype(np.uint8).tobytes()

    node.setPixelData(QByteArray(data), params["x"], params["y"],
                      params["width"], params["height"])
    return {"written": True, "width": img.width(), "height": img.height()}


def check_paintability(params: dict) -> dict:
    """当前笔刷下该图层可否绘制（PAINT/VECTOR/CLONE/UNPAINTABLE...）。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    return {"paint_ability": node.paintAbility()}


def wait_for_done(params: dict) -> dict:
    """阻塞至后台笔刷任务完成（可选刷新投影合成）。"""
    doc = _active_doc(params)
    doc.waitForDone()
    if params.get("refresh_projection", True):
        doc.refreshProjection()
    return {"synced": True}