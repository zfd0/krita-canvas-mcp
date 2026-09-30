# 文档 IO / 应用级操作类工具实现。
# create_document / open_document / close_document / save_document /
# get_krita_info / execute_action / get_setting / set_setting
from krita import Krita, InfoObject

import json
import os
import re
from PyQt6.QtCore import QUuid

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _app():
    return Krita.instance()


# ---------------------------------------------------------------- 文档生命周期

def create_document(params: dict) -> dict:
    app = _app()
    doc = app.createDocument(
        int(params.get("width", 512)),
        int(params.get("height", 512)),
        params.get("name", "untitled"),
        params.get("color_model", "RGBA"),
        params.get("color_depth", "U8"),
        params.get("profile", ""),
        float(params.get("resolution", 300)),
    )
    doc.setBatchmode(True)
    win = app.activeWindow()
    if win is not None:
        win.addView(doc)
    app.setActiveDocument(doc)

    # ★ 关键：显式激活首个可绘制节点，避免后续 activeNode() 返回 None
    root = doc.rootNode()
    children = root.childNodes() if root else []
    if children:
        try:
            doc.setActiveNode(children[0])
        except Exception:
            pass
    doc.refreshProjection()
    return {"document_id": doc.fileName() or f"untitled_{id(doc)}",
            "name": params.get("name", "untitled"),
            "width": doc.width(), "height": doc.height()}


def open_document(params: dict) -> dict:
    """打开图像文件并展示。"""
    app = _app()
    path = params["file_path"]
    doc = app.openDocument(path)
    if doc is None:
        raise RuntimeError(f"IO_ERROR: 打开失败 {path}")
    doc.setBatchmode(True)
    win = app.activeWindow()
    if win is not None:
        win.addView(doc)
    app.setActiveDocument(doc)
    return {"document_id": doc.fileName(),
            "width": doc.width(), "height": doc.height()}


def close_document(params: dict) -> dict:
    """按 document_id 关闭文档（无保存提示）。
    匹配优先级：文件名 > untitled_<id> > 文档标题(name)；空值=活动文档。"""
    app = _app()
    did = params.get("document_id", "")
    if not did:
        doc = app.activeDocument()
        targets = [doc] if doc is not None else []
    else:
        targets = []
        for doc in app.documents():
            key = doc.fileName() or f"untitled_{id(doc)}"
            try:
                title = doc.name()
            except Exception:
                title = ""
            if did in (key, title):
                targets.append(doc)
    if not targets and did:
        raise RuntimeError(f"SESSION_NOT_FOUND: 文档不存在 {did}")
    for doc in targets:
        doc.setBatchmode(True)
        doc.close()
    return {"closed": len(targets), "document_id": did}


# ---------------------------------------------------------------- 保存/导出

def _export_config(params: dict) -> InfoObject:
    """按 format 生成 InfoObject 导出配置。"""
    info = InfoObject()
    fmt = params.get("format", "png")
    if fmt == "png":
        cfg = params.get("png_config", {})
        for key, val in cfg.items():
            info.setProperty(_snake_to_camel(key), val)
        info.setProperty("alpha", cfg.get("alpha", True))
        info.setProperty("compression", cfg.get("compression", 6))
    elif fmt == "jpeg":
        cfg = params.get("jpeg_config", {})
        info.setProperty("quality", cfg.get("quality", 90))
    return info


def _snake_to_camel(name: str) -> str:
    """snake_case → camelCase（force_srgb → forceSRGB）。"""
    head, *tail = name.split("_")
    return head + "".join(t.capitalize() for t in tail)


def save_document(params: dict) -> dict:
    """save / save_as / export（可带节点级导出）。"""
    app = _app()
    doc = app.activeDocument()
    if doc is None:
        raise RuntimeError("NO_ACTIVE_DOCUMENT")
    mode = params.get("mode", "export")
    out_path = None

    if mode == "save":
        ok = doc.save()
        out_path = doc.fileName()
    elif mode == "save_as":
        path = params["file_path"]
        ok = doc.saveAs(path)
        out_path = path
    else:  # export
        path = params["file_path"]
        node_id = params.get("node_id")
        if node_id:
            raw = str(node_id)
            node = doc.nodeByName(raw)
            if node is None:
                # 尝试通过 QUuid 对象查找（Krita 6.0+ 要求 QUuid 而非字符串）
                uuid_obj = None
                if _UUID_RE.match(raw):
                    uuid_obj = QUuid(raw)
                elif raw.startswith("{") and raw.endswith("}"):
                    uuid_obj = QUuid(raw[1:-1])
                if uuid_obj and not uuid_obj.isNull():
                    try:
                        node = doc.nodeByUniqueID(uuid_obj)
                    except Exception:
                        node = None
            if node is None:
                raise RuntimeError(f"INVALID_NODE: {node_id}")
            out_path = path
            ok = node.exportImage(path, _export_config(params)) \
                if hasattr(node, "exportImage") else False
            if not ok:
                # Node 导出走 save 接口
                ok = node.save(path, doc.xRes(), doc.yRes(), _export_config(params))
            out_path = path if ok else None
        else:
            ok = doc.exportImage(path, _export_config(params))
            out_path = path if ok else None
    if not ok:
        raise RuntimeError(f"IO_ERROR: 保存失败 {params.get('file_path', '')}")
    exists = bool(out_path and os.path.exists(out_path))
    return {"ok": ok, "path": out_path, "file_exists": exists}


# ---------------------------------------------------------------- 应用信息/动作

def get_krita_info(params: dict) -> dict:
    """Krita 版本等信息。"""
    app = _app()
    doc = app.activeDocument()
    return {
        "version": app.version(),
        "batchmode": app.batchmode(),
        "has_active_document": doc is not None,
    }


def execute_action(params: dict) -> dict:
    """list_only 列出全部动作名；否则按名触发内置动作。"""
    app = _app()
    if params.get("list_only"):
        return {"actions": [a.objectName() for a in app.actions() if a]}
    name = params["action_name"]
    act = app.action(name)
    if act is None:
        raise RuntimeError(f"ACTION_NOT_FOUND: {name}")
    act.trigger()
    doc = app.activeDocument()
    if doc is not None:
        doc.refreshProjection()
    return {"triggered": name}


# ---------------------------------------------------------------- 设置持久化

def get_setting(params: dict) -> dict:
    """读 kritarc 持久化设置。"""
    app = _app()
    group = params.get("group", "krita_canvas_mcp")
    value = app.readSetting(group, params["name"],
                            params.get("default_value", ""))
    return {"group": group, "name": params["name"], "value": value}


def set_setting(params: dict) -> dict:
    """写 kritarc 持久化设置。"""
    app = _app()
    group = params.get("group", "krita_canvas_mcp")
    app.writeSetting(group, params["name"], str(params["value"]))
    return {"group": group, "name": params["name"],
            "value": str(params["value"])}