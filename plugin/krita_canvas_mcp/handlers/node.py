# 图层/节点管理类工具实现。
# create_node / set_node_props / manage_node / get_node_tree
from krita import Krita


def _active_doc(params: dict):
    """取活动文档。"""
    doc = Krita.instance().activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _resolve_node(doc, node_id):
    """按 node_id（uuid 或名称）定位节点；为空取活动节点；"root" 取根节点。"""
    if node_id in (None, ""):
        return doc.activeNode()
    if node_id == "root":
        return doc.rootNode()
    try:
        n = doc.nodeByUniqueID(node_id)
        if n is not None:
            return n
    except Exception:
        pass
    return doc.nodeByName(node_id)


def _node_tag(node) -> str:
    """节点的稳定标识（uuid 字符串，优先）。"""
    try:
        u = node.uniqueId()
        if u is not None and str(u):
            return str(u)
    except Exception:
        pass
    return node.name()


def _walk(node, out: dict, depth: int = 0):
    """递归导出节点树信息。"""
    out["name"] = node.name()
    out["type"] = node.type()
    out["uuid"] = _node_tag(node)
    out["visible"] = node.visible()
    out["opacity"] = node.opacity()
    try:
        out["blending_mode"] = node.blendingMode()
    except Exception:
        out["blending_mode"] = None
    children = node.childNodes()
    out["child_count"] = len(children)
    out["children"] = []
    for c in children:
        cc = {}
        _walk(c, cc, depth + 1)
        out["children"].append(cc)


def get_node_tree(params: dict) -> dict:
    """完整图层树（递归）。"""
    doc = _active_doc(params)
    root = doc.rootNode()
    tree = {}
    _walk(root, tree)
    return {"document_id": doc.fileName() or "untitled", "tree": tree}


def create_node(params: dict) -> dict:
    """创建图层/蒙版并挂到父节点（可指定插入位置与激活）。
    filelayer 类型需要 file_path。"""
    doc = _active_doc(params)
    name = params["name"]
    ntype = params["node_type"]

    if ntype == "filelayer":
        if not params.get("file_path"):
            raise RuntimeError("INVALID_PARAM: filelayer 需要 file_path")
        node = doc.createFileLayer(name, params["file_path"], "None", "Bicubic")
    else:
        node = doc.createNode(name, ntype)
    if node is None:
        raise RuntimeError(f"INVALID_PARAM: 创建节点失败 {ntype}")

    parent = _resolve_node(doc, params.get("parent_id", "root"))
    above_id = params.get("above_id")
    above = _resolve_node(doc, above_id) if above_id else None
    parent.addChildNode(node, above)

    if params.get("set_active", True):
        doc.setActiveNode(node)
    doc.refreshProjection()

    return {"uuid": _node_tag(node), "name": name, "type": ntype,
            "active": params.get("set_active", True)}


def create_fill_layer(params: dict) -> dict:
    """整层纯色填充（color generator）。"""
    from PyQt6.QtGui import QColor
    from krita import InfoObject, Selection
    doc = _active_doc(params)
    name = params.get("name", "fill")

    color = params.get("color")
    if color:
        r, g, b = (int(round(float(v))) for v in color[:3])
        sel = Selection()
        info = InfoObject()
        info.setProperty("color", QColor(r, g, b))
        layer = doc.createFillLayer(name, "color", info, sel)
    else:
        # pattern 模式：InfoObject 指定图案名
        from krita import InfoObject, Selection
        sel = Selection()
        info = InfoObject()
        info.setProperty("pattern", params.get("pattern_name", ""))
        layer = doc.createFillLayer(name, "pattern", info, sel)
    if layer is None:
        raise RuntimeError("INVALID_PARAM: 创建填充层失败")
    parent = _resolve_node(doc, params.get("parent_id", "root"))
    parent.addChildNode(layer, None)
    doc.refreshProjection()
    return {"uuid": _node_tag(layer), "name": name, "type": "filllayer"}


def set_node_props(params: dict) -> dict:
    """批量设置图层属性（缺省项不动）。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    applied = []
    mapping = {
        "name": ("setName", True), "visible": ("setVisible", False),
        "locked": ("setLocked", False), "opacity": ("setOpacity", True),
        "blending_mode": ("setBlendingMode", True),
        "alpha_locked": ("setAlphaLocked", False),
        "inherit_alpha": ("setInheritAlpha", False),
    }
    for key, (setter, _cast) in mapping.items():
        if key in params:
            val = params[key]
            if key in ("visible", "locked", "alpha_locked", "inherit_alpha"):
                val = bool(val)
            getattr(node, setter)(val)
            applied.append(key)
    if params.get("set_active"):
        doc.setActiveNode(node)
        applied.append("set_active")
    return {"node": node.name(), "applied": applied}


def manage_node(params: dict) -> dict:
    """节点操作：删除/复制/向下合并/平移/设为活动。"""
    doc = _active_doc(params)
    node = _resolve_node(doc, params.get("node_id"))
    op = params["op"]
    if op == "remove":
        node.remove()
        return {"op": op, "uuid": _node_tag(node)}
    if op == "duplicate":
        dup = node.duplicate()
        name = params.get("new_name")
        if name:
            dup.setName(name)
        if dup is not None:
            node.parentNode().addChildNode(dup, node)
        doc.refreshProjection()
        return {"op": op, "uuid": _node_tag(dup) if dup else None}
    if op == "merge_down":
        merged = node.mergeDown()
        doc.refreshProjection()
        return {"op": op, "uuid": _node_tag(merged) if merged else None}
    if op == "move":
        node.move(int(params.get("x", 0)), int(params.get("y", 0)))
        doc.refreshProjection()
        return {"op": op, "uuid": _node_tag(node)}
    if op == "set_active":
        doc.setActiveNode(node)
        return {"op": op, "uuid": _node_tag(node)}
    if op == "reorder":
        parent = node.parentNode()
        if parent is None:
            raise RuntimeError("INVALID_PARAM: 根节点不可重排")
        children = list(parent.childNodes())
        if node not in children:
            raise RuntimeError("INVALID_PARAM: 节点不在父层子列表")
        children.remove(node)
        above = _resolve_node(doc, params.get("above_id")) \
            if params.get("above_id") else None
        if above is not None and above in children:
            children.insert(children.index(above), node)
        else:
            children.append(node)
        parent.setChildNodes(children)
        doc.refreshProjection()
        return {"op": op, "uuid": _node_tag(node)}
    raise RuntimeError(f"INVALID_PARAM: 未知 op {op}")