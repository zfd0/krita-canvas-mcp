from krita import Krita
from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
import base64

def get_document_info(params: dict) -> dict:
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return {
        "document_id": doc.fileName() or f"untitled_{id(doc)}",
        "name": doc.name() if hasattr(doc, "name") else doc.fileName(),
        "width": doc.width(),
        "height": doc.height(),
        "color_model": doc.colorModel(),
        "color_depth": doc.colorDepth(),
        "resolution": doc.resolution(),
        "is_modified": doc.isModified(),
    }

def get_canvas_snapshot(params: dict) -> dict:
    app = Krita.instance()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")

    # 取合成图
    x = y = 0
    w, h = doc.width(), doc.height()
    region = params.get("region")
    if region:
        x, y = region["x"], region["y"]
        w, h = region["width"], region["height"]

    img = doc.projection(x, y, w, h)  # QImage

    # 缩放到 max_side
    max_side = params.get("max_side", 1024)
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.scaled(int(w * scale), int(h * scale))

    # QImage → PNG → base64
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    b64 = base64.b64encode(bytes(ba)).decode("ascii")

    return {
        "image_b64": b64,
        "mime_type": "image/png",
        "region": {"x": x, "y": y, "width": w, "height": h},
        "original_size": {"width": doc.width(), "height": doc.height()},
        "snapshot_hash": _hash(img),
    }

def _hash(img) -> str:
    # 简单 perceptual hash，占位
    import hashlib
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return hashlib.md5(bytes(ba)).hexdigest()[:16]