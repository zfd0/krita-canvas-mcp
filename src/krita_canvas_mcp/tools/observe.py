"""观测/快照类工具注册 (适配 mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="get_document_info",
        description="获取当前文档元信息：尺寸、色彩模型、色深、配置文件、分辨率、是否已修改",
    )
    def get_document_info(document_id: str | None = None) -> str:
        return _call("get_document_info", {"document_id": document_id})

    @mcp.tool(
        name="get_canvas_snapshot",
        description=(
            "拍摄当前画布合成结果快照，返回 PNG(base64)与快照哈希。"
            "这是闭环绘画的主观测通道。"
        ),
    )
    def get_canvas_snapshot(
        document_id: str | None = None,
        region: dict | None = None,
        max_side: int = 1024,
        include_alpha: bool = False,
    ) -> str:
        return _call(
            "get_canvas_snapshot",
            {"document_id": document_id, "region": region,
             "max_side": max_side, "include_alpha": include_alpha},
            quiet=True,
        )

    @mcp.tool(
        name="get_node_pixels",
        description="读取单个图层内容(非合成)，用于分析某一笔或某一层，返回PNG(base64)",
    )
    def get_node_pixels(
        node_id: str, region: dict | None = None, max_side: int = 1024,
    ) -> str:
        return _call("get_node_pixels",
                     {"node_id": node_id, "region": region, "max_side": max_side}, quiet=True)

    @mcp.tool(
        name="list_channels",
        description="列出图层全部颜色/alpha通道(名称/可见性/位宽/边界)。",
    )
    def list_channels(node_id: str) -> str:
        return _call("list_channels", {"node_id": node_id}, quiet=True)

    @mcp.tool(
        name="get_channel_pixels",
        description="读取单通道灰度图，返回 PNG(base64)。",
    )
    def get_channel_pixels(
        node_id: str, channel: str, region: dict | None = None,
    ) -> str:
        return _call("get_channel_pixels",
                     {"node_id": node_id, "channel": channel,
                      "region": region}, quiet=True)

    @mcp.tool(
        name="get_selection_pixels",
        description="将当前选区输出为灰度蒙版PNG(0~255 selectedness)",
    )
    def get_selection_pixels(document_id: str | None = None, region: dict | None = None) -> str:
        return _call("get_selection_pixels",
                     {"document_id": document_id, "region": region}, quiet=True)

    @mcp.tool(
        name="get_view_state",
        description="读回视图状态(缩放/旋转/镜像)与当前绘画参数(笔刷/颜色)。",
    )
    def get_view_state() -> str:
        return _call("get_view_state", {}, quiet=True)