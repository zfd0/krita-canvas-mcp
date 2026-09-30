# 图层/节点管理类工具实现。
# create_node / set_node_props / manage_node / get_node_tree
from krita import Krita
from PyQt6.QtCore import QUuid

import re

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _active_doc(params: dict):
    """取活动文档。"""
    doc = Krita.instance().activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    return doc


def _resolve_node(doc, node_id, required=True):
    """按 node_id（uuid 或名称）定位节点；为空取活动节点；"root" 取根节点。
    兼容纯 UUID、带大括号两种输入格式。
    required=True 时找不到抛 INVALID_NODE。"""
    n = None
    if node_id in (None, ""):
        n = doc.activeNode()
        if n is None:
            children = doc.rootNode().childNodes()
            if children:
                n = children[0]
    elif node_id == "root":
        n = doc.rootNode()
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


def _find_by_name(doc, name):
    """递归按名称查找节点（不依赖 nodeByName 的查找范围语义）。"""
    def rec(n):
        if n.name() == name:
            return n
        for c in n.childNodes():
            r = rec(c)
            if r is not None:
                return r
        return None
    return rec(doc.rootNode())


def _node_tag(node) -> str:
    """节点的稳定标识（纯 UUID 字符串，无 PyQt 包装、无大括号）。"""
    try:
        u = node.uniqueId()
        if u is None:
            return node.name()
        # PyQt6 QUuid: 优先 toString()，否则 str()，最后正则兜底
        try:
            s = u.toString()
        except AttributeError:
            s = str(u)
        m = _UUID_RE.search(str(s))
        if m:
            return m.group(0)
        return node.name()
    except Exception:
        return node.name()


def _walk(node, out: dict, depth: int = 0, recursive: bool = True):
    """导出节点树信息。recursive=False 时只输出直接子节点基本信息。"""
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
    if recursive:
        for c in children:
            cc = {}
            _walk(c, cc, depth + 1, recursive=True)
            out["children"].append(cc)
    else:
        for c in children:
            cc = {"name": c.name(), "type": c.type(), "uuid": _node_tag(c),
                  "visible": c.visible(), "opacity": c.opacity()}
            out["children"].append(cc)


def get_node_tree(params: dict) -> dict:
    """图层树（支持 recursive 控制是否递归输出子节点）。"""
    doc = _active_doc(params)
    recursive = params.get("recursive", True)
    root = doc.rootNode()
    tree = {}
    _walk(root, tree, recursive=recursive)
    return {"document_id": doc.fileName() or "untitled", "tree": tree}


def create_node(params: dict) -> dict:
    """创建图层/蒙版并挂到父节点（可指定插入位置与激活）。
    filelayer 类型需要 file_path。fill_color 可为 paintlayer/colorizemask 设置初始填充色。"""
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

    # 可选初始填充色
    fill_color = params.get("fill_color")
    if fill_color and ntype in ("paintlayer", "colorizemask"):
        from PyQt6.QtGui import QColor
        from krita import ManagedColor
        vals = [float(v) for v in fill_color[:4]]
        if max(vals) <= 1.0:
            vals = [v * 255.0 for v in vals]
        r, g, b, a = (int(round(v)) for v in vals)
        mc = ManagedColor.fromQColor(QColor(r, g, b, a))
        node.fillNode(mc)

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
    from PyQt6.QtGui import QColor
    from krita import InfoObject, Selection
    doc = _active_doc(params)
    name = params.get("name", "fill")
    color = params.get("color")
    if color:
        # 支持 0~1 浮点 或 0~255 整数
        vals = [float(v) for v in color[:3]]
        if max(vals) <= 1.0:
            vals = [v * 255.0 for v in vals]
        r, g, b = (int(round(v)) for v in vals)
        sel = Selection()
        info = InfoObject()
        info.setProperty("color", QColor(r, g, b))
        layer = doc.createFillLayer(name, "color", info, sel)
    else:
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
        # 记录下层兄弟名：Krita 6 合并销毁上下两层后重建的结果节点沿用下层名
        parent = node.parentNode() or doc.rootNode()
        siblings = list(parent.childNodes())
        idx = siblings.index(node) if node in siblings else -1
        below_name = siblings[idx - 1].name() if idx > 0 else None
        merged = node.mergeDown()
        if merged is None:
            # mergeDown 返回 None：按保留下来的下层名查找新结果节点
            if below_name:
                merged = _find_by_name(doc, below_name)
            if merged is None:
                merged = _find_by_name(doc, node.name())
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