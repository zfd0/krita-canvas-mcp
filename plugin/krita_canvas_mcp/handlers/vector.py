# 矢量图层/形状类工具实现。
# vector_add_svg / vector_get_shapes / vector_shape_op / vector_export_svg
from krita import Krita
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QTransform


def _doc(params: dict):
    doc = Krita.instance().activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _vec_layer(doc, node_id):
    """定位矢量图层（缺省活动节点）。"""
    node = None
    if node_id:
        try:
            node = doc.nodeByUniqueID(node_id)
        except Exception:
            node = None
        if node is None:
            node = doc.nodeByName(node_id)
    else:
        node = doc.activeNode()
    if node is None or str(node.type()) != "vectorlayer":
        raise RuntimeError("INVALID_NODE: 需要矢量图层")
    return node


def _shape_desc(shape, index: int) -> dict:
    """Shape → 可序列化描述。"""
    bb = shape.boundingBox()
    pos = shape.position()
    t = shape.transformation()
    parent = shape.parentShape()
    return {
        "index": index,
        "name": shape.name(),
        "type": shape.type(),
        "z_index": shape.zIndex(),
        "visible": shape.visible(),
        "selected": shape.isSelected(),
        "bbox": [round(bb.x(), 2), round(bb.y(), 2),
                 round(bb.width(), 2), round(bb.height(), 2)],
        "position": [round(pos.x(), 2), round(pos.y(), 2)],
        "transform": [round(t.m11(), 3), round(t.m12(), 3),
                      round(t.m21(), 3), round(t.m22(), 3),
                      round(t.dx(), 2), round(t.dy(), 2)],
        "parent": parent.name() if parent is not None else None,
    }


def vector_add_svg(params: dict) -> dict:
    """SVG 字符串加入矢量图层。"""
    doc = _doc(params)
    layer = _vec_layer(doc, params.get("node_id"))
    shapes = layer.addShapesFromSvg(str(params["svg"]))
    names = [s.name() for s in shapes]
    if params.get("group_name"):
        g = layer.createGroupShape(params["group_name"], shapes)
        names = [g.name() if g is not None else "group(failed)"]
    doc.refreshProjection()
    return {"added": len(shapes), "shape_names": names}


def vector_get_shapes(params: dict) -> dict:
    """枚举矢量图层 top-level 形状。"""
    doc = _doc(params)
    layer = _vec_layer(doc, params.get("node_id"))
    shapes = layer.shapes()
    return {"count": len(shapes),
            "shapes": [_shape_desc(s, i) for i, s in enumerate(shapes)]}


def _lookup(layer, index: int):
    """按 index 取形状。"""
    shapes = layer.shapes()
    if not (0 <= index < len(shapes)):
        raise RuntimeError(f"INVALID_PARAM: shape_index {index} 越界")
    return shapes[index]


def vector_shape_op(params: dict) -> dict:
    """矢量形状操作：remove/select/deselect/set_visible/set_position/
    set_transform/set_zindex/group。"""
    doc = _doc(params)
    layer = _vec_layer(doc, params.get("node_id"))
    op = params["op"]

    if op == "group":
        indices = params.get("group_with", [])
        shapes = layer.shapes()
        picked = [shapes[i] for i in indices if 0 <= i < len(shapes)]
        g = layer.createGroupShape(params.get("group_name", "group"), picked)
        doc.refreshProjection()
        return {"op": op, "grouped": len(picked),
                "group_name": g.name() if g is not None else None}

    idx = int(params.get("shape_index", 0))
    shape = _lookup(layer, idx)

    if op == "remove":
        shape.remove()
    elif op == "select":
        shape.select()
    elif op == "deselect":
        shape.deselect()
    elif op == "set_visible":
        shape.setVisible(bool(params.get("visible", True)))
    elif op == "set_position":
        x, y = params["position"]
        shape.setPosition(QPointF(float(x), float(y)))
    elif op == "set_transform":
        m = params["matrix"]
        shape.setTransformation(QTransform(m[0], m[1], m[2], m[3], m[4], m[5]))
    elif op == "set_zindex":
        shape.setZIndex(int(params.get("z_index", 0)))
    else:
        raise RuntimeError(f"INVALID_PARAM: 未知形状 op {op}")
    doc.refreshProjection()
    return {"op": op, "index": idx, "name": shape.name()}


def vector_export_svg(params: dict) -> dict:
    """整层或单形状导出 SVG 字符串。"""
    doc = _doc(params)
    layer = _vec_layer(doc, params.get("node_id"))
    if "shape_index" in params and params["shape_index"] is not None:
        shape = _lookup(layer, int(params["shape_index"]))
        svg = shape.toSvg(True, False)
        scope = "shape"
    else:
        svg = layer.toSvg()
        scope = "layer"
    return {"scope": scope, "svg": svg,
            "contains_path": "path" in svg.lower()}  # 简化判据