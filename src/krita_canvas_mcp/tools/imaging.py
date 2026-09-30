"""成像类工具注册：通道写入 + 选区操作 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="set_channel_pixels",
        description="将灰度补丁(PNG/base64)写入图层的单个通道。",
    )
    def set_channel_pixels(
        node_id: str, channel: str, x: int, y: int, image_b64: str,
    ) -> str:
        return _call("set_channel_pixels",
                     {"node_id": node_id, "channel": channel,
                      "x": x, "y": y, "image_b64": image_b64})

    @mcp.tool(
        name="set_selection_pixels",
        description="灰度PNG蒙版写入并激活为文档选区(0~255 selectedness)。",
    )
    def set_selection_pixels(
        x: int, y: int, image_b64: str, apply_to_document: bool = True,
    ) -> str:
        return _call("set_selection_pixels",
                     {"x": x, "y": y, "image_b64": image_b64,
                      "apply_to_document": apply_to_document})

    @mcp.tool(
        name="selection_op",
        description="选区操作：select_rect/select_all/clear/invert/feather/"
                    "grow/shrink/smooth/border/erode/dilate/move/resize。",
    )
    def selection_op(
        op: str, x: int | None = None, y: int | None = None,
        width: int | None = None, height: int | None = None,
        value: int = 255, radius: int | None = None,
        node_ref: str | None = None, edge_lock: bool = False,
    ) -> str:
        params = {k: v for k, v in {
            "op": op, "x": x, "y": y, "width": width, "height": height,
            "value": value, "radius": radius, "node_ref": node_ref,
            "edge_lock": edge_lock,
        }.items() if v is not None}
        return _call("selection_op", params)