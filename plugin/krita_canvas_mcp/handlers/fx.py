# 滤镜 / 变换类工具实现。
# apply_filter / get_filter_config / transform_document / transform_node
from krita import Krita, Selection
from PyQt6.QtCore import QPointF, QUuid

import math
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


def _node(doc, node_id):
    if not node_id:
        return doc.activeNode()
    raw = str(node_id)
    # 尝试通过 QUuid 对象查找（Krita 6.0+ 要求 QUuid 而非字符串）
    uuid_obj = None
    if _UUID_RE.match(raw):
        uuid_obj = QUuid(raw)
    elif raw.startswith("{") and raw.endswith("}"):
        uuid_obj = QUuid(raw[1:-1])
    n = None
    if uuid_obj and not uuid_obj.isNull():
        try:
            n = doc.nodeByUniqueID(uuid_obj)
        except Exception:
            n = None
    if n is None:
        n = doc.nodeByName(raw)
    return n


def _strategies():
    return ["Hermite", "Bicubic", "Box", "Bilinear", "Bell",
            "BSpline", "Lanczos3", "Mitchell"]


def _pick_strategy(params: dict) -> str:
    s = params.get("strategy", "Bicubic")
    return s if s in _strategies() else "Bicubic"


# ---------------------------------------------------------------- 滤镜

def apply_filter(params: dict) -> dict:
    """对图层应用滤镜（list_only 仅列名；as_filter_layer 创建滤镜层）。"""
    app = Krita.instance()
    if params.get("list_only"):
        return {"filters": list(app.filters())}

    doc = _doc(params)
    fname = params["filter_name"]
    f = app.filter(fname)
    if f is None:
        raise RuntimeError(f"FILTER_NOT_FOUND: {fname}")

    config = params.get("config") or {}
    if config:
        info = f.configuration()
        for k, v in config.items():
            info.setProperty(k, v)
        f.setConfiguration(info)

    if params.get("as_filter_layer"):
        node = _node(doc, params.get("node_id"))
        sel = Selection()
        sel.selectAll(node, 255)
        layer = doc.createFilterLayer(fname + "_fx", f, sel)
        parent = node.parentNode() or doc.rootNode()
        parent.addChildNode(layer, None)
        doc.refreshProjection()
        return {"applied": True, "as_filter_layer": True,
                "filter": fname}

    node = _node(doc, params.get("node_id"))
    x, y, w, h = 0, 0, doc.width(), doc.height()
    region = params.get("region")
    if region:
        x, y, w, h = (int(region["x"]), int(region["y"]),
                      int(region["width"]), int(region["height"]))
    ok = f.apply(node, x, y, w, h)
    doc.refreshProjection()
    return {"applied": ok, "as_filter_layer": False, "filter": fname}


def get_filter_config(params: dict) -> dict:
    """读滤镜默认配置模板（属性名 + 值）。"""
    app = Krita.instance()
    f = app.filter(params["filter_name"])
    if f is None:
        raise RuntimeError(f"FILTER_NOT_FOUND: {params['filter_name']}")
    info = f.configuration()
    props = {}
    for k in info.properties().keys():
        v = info.property(k)
        props[k] = str(v)
    return {"filter": params["filter_name"], "config": props}


# ---------------------------------------------------------------- 文档变换

def transform_document(params: dict) -> dict:
    """整幅文档变换：scale/rotate/shear/crop/resize。"""
    doc = _doc(params)
    op = params["op"]
    if op == "scale":
        doc.scaleImage(int(params["width"]), int(params["height"]),
                       int(params.get("x_res", doc.xRes())),
                       int(params.get("y_res", doc.yRes())),
                       _pick_strategy(params))
    elif op == "rotate":
        doc.rotateImage(math.radians(float(params.get("angle", 0))))
    elif op == "shear":
        doc.shearImage(float(params.get("angle", 0)),
                       float(params.get("angle_y", 0)))
    elif op == "crop":
        r = params["region"]
        doc.crop(int(r["x"]), int(r["y"]), int(r["width"]), int(r["height"]))
    elif op == "resize":
        r = params.get("region")
        if r:
            doc.resizeImage(int(r["x"]), int(r["y"]),
                            int(r["width"]), int(r["height"]))
        if "x_res" in params:
            doc.setXRes(float(params["x_res"]))
        if "y_res" in params:
            doc.setYRes(float(params["y_res"]))
    else:
        raise RuntimeError(f"INVALID_PARAM: 未知变换 op {op}")
    doc.refreshProjection()
    return {"op": op, "width": doc.width(), "height": doc.height(),
            "x_res": doc.xRes(), "y_res": doc.yRes()}


def transform_node(params: dict) -> dict:
    """图层变换：scale/rotate/shear/crop。"""
    doc = _doc(params)
    node = _node(doc, params.get("node_id"))
    op = params["op"]
    if op == "scale":
        node.scaleNode(QPointF(0, 0),
                       int(params["width"]), int(params["height"]),
                       _pick_strategy(params))
    elif op == "rotate":
        node.rotateNode(math.radians(float(params.get("angle", 0))))
    elif op == "shear":
        node.shearNode(float(params.get("angle", 0)),
                       float(params.get("angle_y", 0)))
    elif op == "crop":
        r = params["crop_region"]
        node.cropNode(int(r["x"]), int(r["y"]),
                      int(r["width"]), int(r["height"]))
    else:
        raise RuntimeError(f"INVALID_PARAM: 未知图层变换 op {op}")
    doc.refreshProjection()
    b = node.bounds()
    return {"op": op, "node": node.name(),
            "bounds": [b.x(), b.y(), b.width(), b.height()]}