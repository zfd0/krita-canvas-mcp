# 节点像素 / 通道 / 选区类工具实现。
# get_node_pixels / list_channels / get_channel_pixels / set_channel_pixels /
# get_selection_pixels / set_selection_pixels / selection_op
from krita import Krita, Selection
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QRect, QUuid, Qt
from PyQt6.QtGui import QImage

import base64
import re

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _doc(params: dict):
    doc = Krita.instance().activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _node(doc, node_id, required=True):
    """按 node_id（uuid 或名称）定位节点；为空取活动节点。
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


def _img_to_png_b64(img: QImage) -> str:
    """QImage → PNG base64。"""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(ba)).decode("ascii")


def _b64_to_img(b64: str) -> QImage:
    raw = base64.b64decode(b64)
    img = QImage.fromData(QByteArray(raw), "PNG")
    if img.isNull():
        raise RuntimeError("INVALID_PARAM: 图片解码失败")
    return img


def _region(params: dict, w: int, h: int) -> tuple:
    """解析 region 参数（纯像素坐标），缺省全图。"""
    r = params.get("region")
    if r:
        return (max(0, int(r["x"])), max(0, int(r["y"])),
                max(1, int(r["width"])), max(1, int(r["height"])))
    return 0, 0, w, h


# ---------------------------------------------------------------- 节点像素

def get_node_pixels(params: dict) -> dict:
    """读取单图层矩形像素 → PNG(base64)。支持 max_side 降采样。"""
    doc = _doc(params)
    node = _node(doc, params.get("node_id"))
    x, y, w, h = _region(params, doc.width(), doc.height())
    raw = bytes(node.pixelData(x, y, w, h))
    if not raw:
        raise RuntimeError("INVALID_PARAM: 节点无可读像素")
    # 整数 RGBA 内存序 B,G,R,A ↔ ARGB32 一致，直接构造
    img = QImage(raw, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
    # max_side 降采样
    max_side = params.get("max_side", 1024)
    if max(img.width(), img.height()) > max_side:
        scale = max_side / float(max(img.width(), img.height()))
        img = img.scaled(int(img.width() * scale), int(img.height() * scale),
                         Qt.KeepAspectRatio)
    return {"image_b64": _img_to_png_b64(img), "mime_type": "image/png",
            "region": {"x": x, "y": y, "width": img.width(), "height": img.height()},
            "content_has_alpha": img.format() == QImage.Format.Format_ARGB32}


# ---------------------------------------------------------------- 通道

def list_channels(params: dict) -> dict:
    """列出图层全部通道。"""
    doc = _doc(params)
    node = _node(doc, params.get("node_id"))
    out = []
    for ch in node.channels():
        b = ch.bounds()
        out.append({"name": ch.name(),
                    "position": ch.position(),
                    "channel_size": ch.channelSize(),
                    "visible": ch.visible(),
                    "bounds": [b.x(), b.y(), b.width(), b.height()]})
    return {"channels": out, "count": len(out)}


def get_channel_pixels(params: dict) -> dict:
    """读取单通道灰度图 → PNG(base64)。"""
    doc = _doc(params)
    node = _node(doc, params.get("node_id"))
    x, y, w, h = _region(params, doc.width(), doc.height())
    target = None
    for ch in node.channels():
        if ch.name() == params["channel"]:
            target = ch
            break
    if target is None:
        raise RuntimeError(f"INVALID_PARAM: 通道不存在 {params['channel']}")
    raw = bytes(target.pixelData(QRect(x, y, w, h)))
    img = QImage(raw, w, h, w, QImage.Format.Format_Grayscale8).copy()
    return {"image_b64": _img_to_png_b64(img), "mime_type": "image/png",
            "channel": params["channel"],
            "region": {"x": x, "y": y, "width": w, "height": h}}


def set_channel_pixels(params: dict) -> dict:
    """灰度 PNG 补丁写入单通道。"""
    doc = _doc(params)
    node = _node(doc, params.get("node_id"))
    target = None
    for ch in node.channels():
        if ch.name() == params["channel"]:
            target = ch
            break
    if target is None:
        raise RuntimeError(f"INVALID_PARAM: 通道不存在 {params['channel']}")
    img = _b64_to_img(params["image_b64"]).convertToFormat(
        QImage.Format.Format_Grayscale8)
    raw = img.bits().asstring(img.sizeInBytes())
    line = img.bytesPerLine()
    need = img.width()
    packed = b"".join(raw[i * line:i * line + need]
                      for i in range(img.height()))
    target.setPixelData(QByteArray(packed),
                        QRect(params["x"], params["y"],
                              img.width(), img.height()))
    doc.refreshProjection()
    return {"written": True,
            "channel": params["channel"],
            "width": img.width(), "height": img.height()}


# ---------------------------------------------------------------- 选区

def _selection_bounds(sel):
    """选区边界。LibKis Selection 无 bounds()，用 x/y/width/height 拼装。"""
    return [sel.x(), sel.y(), sel.width(), sel.height()]


def get_selection_pixels(params: dict) -> dict:
    """当前选区 → 灰度蒙版 PNG。无选区返回全黑。支持 document_id 定位文档。"""
    app = Krita.instance()
    if params.get("document_id"):
        doc = next((d for d in app.documents()
                    if (d.fileName() or f"untitled_{id(d)}") == params["document_id"]),
                   None)
        if doc is None:
            raise RuntimeError(f"INVALID_PARAM: 找不到文档 {params['document_id']}")
    else:
        doc = _doc(params)
    sel = doc.selection()
    x, y, w, h = _region(params, doc.width(), doc.height())
    if sel is None:
        # 无选区时返回全黑灰度蒙版
        raw = b"\x00" * (w * h)
        png = _gray_png(w, h, raw)
        return {"image_b64": base64.b64encode(png).decode("ascii"),
                "has_selection": False, "selection_bounds": None,
                "region": {"x": x, "y": y, "width": w, "height": h}}
    raw = bytes(sel.pixelData(x, y, w, h))
    img = QImage(raw, w, h, w, QImage.Format.Format_Grayscale8).copy()
    return {"image_b64": _img_to_png_b64(img), "has_selection": True,
            "selection_bounds": _selection_bounds(sel),
            "region": {"x": x, "y": y, "width": w, "height": h}}


def _gray_png(w: int, h: int, raw: bytes) -> bytes:
    """标准库灰度 PNG 编码（8bit）。"""
    import struct, zlib
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    body = b"".join(b"\x00" + raw[i:i + w]
                    for i in range(0, len(raw), w))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(body)) + chunk(b"IEND", b""))


def set_selection_pixels(params: dict) -> dict:
    """灰度 PNG 蒙版写入并激活为文档选区。"""
    doc = _doc(params)
    img = _b64_to_img(params["image_b64"]).convertToFormat(
        QImage.Format.Format_Grayscale8)
    raw = img.bits().asstring(img.sizeInBytes())
    line = img.bytesPerLine()
    need = img.width()
    packed = b"".join(raw[i * line:i * line + need]
                      for i in range(img.height()))
    sel = Selection()
    sel.setPixelData(QByteArray(packed), params["x"], params["y"],
                     img.width(), img.height())
    if params.get("apply_to_document", True):
        doc.setSelection(sel)
    return {"written": True, "width": img.width(), "height": img.height()}


def selection_op(params: dict) -> dict:
    """选区操作：select_rect/select_all/clear/invert/feather/grow/shrink/
    smooth/border/erode/dilate/move/resize。操作后写回文档选区。"""
    doc = _doc(params)
    op = params["op"]
    sel = None
    if op.startswith("select_"):
        sel = Selection()  # 新建选区
    else:
        sel = doc.selection()
        if sel is None:
            if op in ("clear", "move", "resize"):
                sel = Selection()
            else:
                raise RuntimeError("INVALID_PARAM: 当前无选区，无法执行 " + op)

    if op == "select_rect":
        sel.select(int(params.get("x", 0)), int(params.get("y", 0)),
                   int(params.get("width", 0)), int(params.get("height", 0)),
                   int(params.get("value", 255)))
    elif op == "select_all":
        node = _node(doc, params.get("node_ref"))
        sel.selectAll(node, int(params.get("value", 255)))
    elif op == "clear":
        sel.clear()
    elif op == "invert":
        sel.invert()
    elif op == "feather":
        sel.feather(int(params.get("radius", 1)))
    elif op == "grow":
        r = int(params.get("radius", 1))
        sel.grow(r, r)
    elif op == "shrink":
        r = int(params.get("radius", 1))
        sel.shrink(r, r, bool(params.get("edge_lock", False)))
    elif op == "smooth":
        sel.smooth()
    elif op == "border":
        r = int(params.get("radius", 1))
        sel.border(r, r)
    elif op == "erode":
        sel.erode()
    elif op == "dilate":
        sel.dilate()
    elif op == "move":
        sel.move(int(params.get("x", 0)), int(params.get("y", 0)))
    elif op == "resize":
        sel.resize(int(params.get("width", 0)), int(params.get("height", 0)))
    else:
        raise RuntimeError(f"INVALID_PARAM: 未知选区 op {op}")

    if op == "clear":
        # clear 表示移除选区：不应把已清空的选区写回文档（否则 doc.selection() 恒非 None）
        doc.setSelection(None)
    else:
        doc.setSelection(sel)
    doc.refreshProjection()
    return {"op": op, "bounds": _selection_bounds(sel)}