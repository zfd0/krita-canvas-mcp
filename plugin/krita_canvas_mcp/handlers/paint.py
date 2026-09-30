# LibKis 绘画执行类工具实现。所有函数在 Krita 主线程由 dispatcher 调用。
# 对应 Node.paintLine / paintPath / paintPolygon / paintEllipse / paintRectangle / setPixelData。
from PyQt6.QtCore import QByteArray, QPoint, QPointF, QRectF, QUuid
from PyQt6.QtGui import QImage, QPainterPath
from krita import Krita

import base64
import re

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _active_doc(params: dict):
    """取目标文档：优先 document_id（当前仅支持活动文档），无活动文档则报错。"""
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _resolve_node(doc, node_id, required=True):
    """按 node_id（uuid 或名称）定位节点；为空则取活动节点。
    兼容纯 UUID、带大括号两种输入格式。"""
    n = None
    if not node_id:
        n = doc.activeNode()
        if n is None:
            children = doc.rootNode().childNodes()
            if children:
                n = children[0]
    else:
        raw = str(node_id)
        # 尝试通过 QUuid 对象查找（Krita 6.0+ 要求 QUuid 而非字符串）
        uuid_obj = None
        if _UUID_RE.match(raw):
            uuid_obj = QUuid(raw)
        elif raw.startswith("{") and raw.endswith("}"):
            uuid_obj = QUuid(raw[1:-1])
        if uuid_obj and not uuid_obj.isNull():
            try:
                n = doc.nodeByUniqueID(uuid_obj)
            except Exception:
                n = None
        if n is None:
            n = doc.nodeByName(raw)
    if n is None and required:
        raise RuntimeError(f"INVALID_NODE: 找不到节点 {node_id!r}")
    return n


def _style(s):
    """stroke/fill 风格字符串安全默认。LibKis 约定 ForegroundColor/BackgroundColor/None/Pattern。"""
    return s or "ForegroundColor"


def paint_line(params: dict) -> dict:
    """两点直线，可带首尾压感。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    # LibKis paintLine 只接受整数 QPoint（浮点强转 int 兼容 LLM 输出）
    node.paintLine(
        QPoint(int(params["x1"]), int(params["y1"])),
        QPoint(int(params["x2"]), int(params["y2"])),
        params.get("pressure1", 1.0),
        params.get("pressure2", 1.0),
        _style(params.get("stroke_style")),
    )
    doc.refreshProjection()
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
    doc.refreshProjection()
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
    doc.refreshProjection()
    return {"drawn": True}


def _image_from_b64(b64: str) -> QImage:
    """base64 PNG → QImage（转 ARGB32，其内存字节序在小端机为 B,G,R,A，与 setPixelData 期望一致）。"""
    raw = base64.b64decode(b64)
    img = QImage.fromData(QByteArray(raw), "PNG")
    if img.isNull():
        raise RuntimeError("INVALID_PARAM: 图片解码失败")
    return img.convertToFormat(QImage.Format.Format_ARGB32)


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
    raw = img.bits().asstring(img.sizeInBytes())
    data = b"".join(
        raw[i * line:i * line + need] for i in range(img.height())
    )

    blend = params.get("blend_mode", "overwrite")
    if blend != "overwrite":
        # alpha_composite：读取原图层像素，逐像素按 alpha 混合。
        # 不用 numpy（Krita 内置 Python 通常未安装第三方库），补丁规模小，纯 Python 足够。
        cur_raw = bytes(node.pixelData(params["x"], params["y"],
                                       params["width"], params["height"]))
        new_ba = bytearray(data)
        h, w = params["height"], params["width"]
        for i in range(h):
            base = i * w * 4
            for j in range(w):
                o = base + j * 4
                a = new_ba[o + 3]
                ia = 255 - a
                for c in range(3):
                    new_ba[o + c] = (new_ba[o + c] * a
                                     + cur_raw[o + c] * ia) // 255
                new_ba[o + 3] = a + (cur_raw[o + 3] * ia) // 255
        data = bytes(new_ba)

    node.setPixelData(QByteArray(data), params["x"], params["y"],
                      params["width"], params["height"])
    doc.refreshProjection()
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