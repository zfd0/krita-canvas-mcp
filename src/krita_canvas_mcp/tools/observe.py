"""观测/快照类工具 (适配 mcp 2.x)。

当前实现：
    - get_document_info
    - get_canvas_snapshot
其余观测类工具待 Krita 侧 handler 补齐后逐个迁移进来。
"""
from mcp.server.mcpserver import MCPServer

from ..bridge import bridge
from ..envelope import err, ok
from ..errors import KritaError


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="get_document_info",
        description="获取当前文档元信息：尺寸、色彩模型、色深、配置文件、分辨率、是否已修改",
    )
    def get_document_info(document_id: str | None = None) -> str:
        try:
            data = bridge.call("get_document_info", {"document_id": document_id})
            return ok(data)
        except KritaError as e:
            return err(e)

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
        try:
            data = bridge.call(
                "get_canvas_snapshot",
                {
                    "document_id": document_id,
                    "region": region,
                    "max_side": max_side,
                    "include_alpha": include_alpha,
                },
            )
            return ok(data)
        except KritaError as e:
            return err(e)