"""图层/节点管理类工具注册 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="get_node_tree",
        description="获取完整图层树(名称/类型/uuid/可见性/不透明度/混合模式/子节点)。",
    )
    def get_node_tree(document_id: str | None = None) -> str:
        return _call("get_node_tree", {"document_id": document_id})

    @mcp.tool(
        name="create_node",
        description="创建图层/蒙版并挂到父节点。node_type 可取 paintlayer/grouplayer/"
                    "vectorlayer/filllayer/clonelayer/filtermask/selectionmask/filelayer 等。",
    )
    def create_node(
        name: str, node_type: str,
        parent_id: str | None = None, above_id: str | None = None,
        set_active: bool = True, file_path: str | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "name": name, "node_type": node_type, "parent_id": parent_id,
            "above_id": above_id, "set_active": set_active,
            "file_path": file_path,
        }.items() if v is not None}
        return _call("create_node", params)

    @mcp.tool(
        name="create_fill_layer",
        description="创建整层纯色/图案填充层（快速铺底）。",
    )
    def create_fill_layer(
        name: str = "fill", color: list | None = None,
        pattern_name: str | None = None, parent_id: str | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "name": name, "color": color, "pattern_name": pattern_name,
            "parent_id": parent_id,
        }.items() if v is not None}
        return _call("create_fill_layer", params)

    @mcp.tool(
        name="set_node_props",
        description="批量设置图层属性：name/visible/locked/opacity(0-255)/blending_mode/"
                    "alpha_locked/inherit_alpha。",
    )
    def set_node_props(
        node_id: str | None = None, name: str | None = None,
        visible: bool | None = None, locked: bool | None = None,
        opacity: int | None = None, blending_mode: str | None = None,
        alpha_locked: bool | None = None, inherit_alpha: bool | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "node_id": node_id, "name": name, "visible": visible,
            "locked": locked, "opacity": opacity, "blending_mode": blending_mode,
            "alpha_locked": alpha_locked, "inherit_alpha": inherit_alpha,
        }.items() if v is not None}
        return _call("set_node_props", params)

    @mcp.tool(
        name="manage_node",
        description="节点操作：remove 删除/duplicate 复制/merge_down 向下合并/"
                    "move 平移/set_active 设为活动/reorder 重排。",
    )
    def manage_node(
        node_id: str, op: str,
        x: int | None = None, y: int | None = None,
        new_name: str | None = None, above_id: str | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "node_id": node_id, "op": op, "x": x, "y": y,
            "new_name": new_name, "above_id": above_id,
        }.items() if v is not None}
        return _call("manage_node", params)
