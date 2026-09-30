"""矢量类工具注册 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="vector_add_svg",
        description="把 SVG 字符串加入矢量图层(坐标单位 pt)。",
    )
    def vector_add_svg(
        node_id: str, svg: str, group_name: str | None = None,
    ) -> str:
        return _call("vector_add_svg",
                     {"node_id": node_id, "svg": svg,
                      "group_name": group_name})

    @mcp.tool(
        name="vector_get_shapes",
        description="枚举矢量图层 top-level 形状(名称/bbox/变换/选中态)。",
    )
    def vector_get_shapes(node_id: str) -> str:
        return _call("vector_get_shapes", {"node_id": node_id}, quiet=True)

    @mcp.tool(
        name="vector_shape_op",
        description="矢量形状操作：remove/select/deselect/set_visible/"
                    "set_position/set_transform/set_zindex/group。",
    )
    def vector_shape_op(
        node_id: str, op: str, shape_index: int = 0,
        position: list | None = None, matrix: list | None = None,
        z_index: int | None = None, visible: bool | None = None,
        group_name: str | None = None, group_with: list | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "node_id": node_id, "op": op, "shape_index": shape_index,
            "position": position, "matrix": matrix, "z_index": z_index,
            "visible": visible, "group_name": group_name,
            "group_with": group_with,
        }.items() if v is not None or k in ("op", "node_id", "shape_index")}
        return _call("vector_shape_op", params)

    @mcp.tool(
        name="vector_export_svg",
        description="整层或单形状导出为 SVG 字符串。",
    )
    def vector_export_svg(
        node_id: str, shape_index: int | None = None,
    ) -> str:
        return _call("vector_export_svg",
                     {"node_id": node_id, "shape_index": shape_index},
                     quiet=True)