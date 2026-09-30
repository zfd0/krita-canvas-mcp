"""滤镜/文档变换类工具注册 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="apply_filter",
        description="对图层应用滤镜。list_only=true 仅列名；"
                    "as_filter_layer=true 创建非破坏滤镜层。",
    )
    def apply_filter(
        filter_name: str | None = None, node_id: str | None = None,
        config: dict | None = None, as_filter_layer: bool = False,
        list_only: bool = False, region: dict | None = None,
    ) -> str:
        return _call("apply_filter", {
            "filter_name": filter_name, "node_id": node_id, "config": config,
            "as_filter_layer": as_filter_layer, "list_only": list_only,
            "region": region,
        })

    @mcp.tool(
        name="get_filter_config",
        description="读取滤镜默认配置模板（属性名与默认值）。",
    )
    def get_filter_config(filter_name: str) -> str:
        return _call("get_filter_config", {"filter_name": filter_name},
                     quiet=True)

    @mcp.tool(
        name="transform_document",
        description="整幅文档变换：scale/rotate/shear/crop/resize。",
    )
    def transform_document(
        op: str, width: int | None = None, height: int | None = None,
        angle: float | None = None, angle_y: float | None = None,
        x_res: float | None = None, y_res: float | None = None,
        region: dict | None = None, strategy: str = "Bicubic",
    ) -> str:
        params = {k: v for k, v in {
            "op": op, "width": width, "height": height, "angle": angle,
            "angle_y": angle_y, "x_res": x_res, "y_res": y_res,
            "region": region, "strategy": strategy,
        }.items() if v is not None}
        return _call("transform_document", **params)

    @mcp.tool(
        name="transform_node",
        description="图层几何变换：scale/rotate/shear/crop。",
    )
    def transform_node(
        node_id: str, op: str, width: int | None = None,
        height: int | None = None, angle: float | None = None,
        angle_y: float | None = None, crop_region: dict | None = None,
        strategy: str = "Bicubic",
    ) -> str:
        params = {k: v for k, v in {
            "node_id": node_id, "op": op, "width": width, "height": height,
            "angle": angle, "angle_y": angle_y,
            "crop_region": crop_region, "strategy": strategy,
        }.items() if v is not None}
        return _call("transform_node", **params)