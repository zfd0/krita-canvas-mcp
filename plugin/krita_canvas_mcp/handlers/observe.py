from krita import Krita
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
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
        "is_modified": doc.modified(),
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

    # LibKis 写像素/笔刷操作后投影为惰性合成：读取前强制刷新，保证快照为最新画面
    doc.refreshProjection()
    img = doc.projection(x, y, w, h)  # QImage

    # 缩放到 max_side
    max_side = params.get("max_side", 1024)
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.scaled(int(w * scale), int(h * scale))

    # QImage → PNG → base64
    ba = QByteArray()
    buf = QBuffer(ba)
    # PyQt6: OpenModeFlag 是 scoped 枚举，扁平写法 QIODevice.WriteOnly 已移除
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
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
    buf.open(QIODevice.OpenModeFlag.WriteOnly)  # PyQt6 scoped 枚举
    img.save(buf, "PNG")
    return hashlib.md5(bytes(ba)).hexdigest()[:16]


def list_documents(params: dict) -> dict:
    """枚举全部打开的文档，供多文档寻址。"""
    app = Krita.instance()
    docs = app.documents()
    out = []
    active = app.activeDocument()
    active_id = None
    for d in docs:
        did = d.fileName() or f"untitled_{id(d)}"
        if d is active:
            active_id = did
        out.append({
            "document_id": did,
            "name": d.name() if hasattr(d, "name") else did,
            "path": d.fileName(),
            "width": d.width(),
            "height": d.height(),
            "color_model": d.colorModel(),
            "color_depth": d.colorDepth(),
            "is_active": d is active,
            "is_modified": d.modified(),
        })
    return {"active_document_id": active_id, "documents": out}