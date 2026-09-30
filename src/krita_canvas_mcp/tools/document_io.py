"""文档 IO / 应用控制类工具注册 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="create_document",
        description="新建画布文档并展示（色彩模型/色深/分辨率可指定）。",
    )
    def create_document(
        width: int, height: int, name: str = "untitled",
        color_model: str = "RGBA", color_depth: str = "U8",
        profile: str = "", resolution: float = 300,
    ) -> str:
        return _call("create_document", {
            "width": width, "height": height, "name": name,
            "color_model": color_model, "color_depth": color_depth,
            "profile": profile, "resolution": resolution,
        })

    @mcp.tool(
        name="open_document",
        description="打开图像文件并展示（典型用途：加载目标图像）。",
    )
    def open_document(file_path: str) -> str:
        return _call("open_document", {"file_path": file_path})

    @mcp.tool(
        name="close_document",
        description="按 document_id 关闭文档（无保存提示）。",
    )
    def close_document(document_id: str | None = None) -> str:
        return _call("close_document", {"document_id": document_id})

    @mcp.tool(
        name="save_document",
        description="保存/另存/导出（PNG/JPEG/KRA，支持节点级导出）。",
    )
    def save_document(
        mode: str = "export", file_path: str | None = None,
        format: str = "png", png_config: dict | None = None,
        jpeg_config: dict | None = None, node_id: str | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "mode": mode, "file_path": file_path, "format": format,
            "png_config": png_config, "jpeg_config": jpeg_config,
            "node_id": node_id,
        }.items() if v is not None}
        return _call("save_document", params)

    @mcp.tool(
        name="get_krita_info",
        description="Krita 版本/批处理模式/活动文档状态。",
    )
    def get_krita_info() -> str:
        return _call("get_krita_info", {}, quiet=True)

    @mcp.tool(
        name="execute_action",
        description="触发任意 Krita 内建动作。list_only=true 列出全部动作名；"
                    "常用 edit_undo/edit_redo/clear。",
    )
    def execute_action(
        action_name: str | None = None, list_only: bool = False,
    ) -> str:
        return _call("execute_action",
                     {"action_name": action_name, "list_only": list_only})

    @mcp.tool(
        name="get_setting",
        description="读取插件持久化设置(kritarc)。",
    )
    def get_setting(
        name: str, group: str = "krita_canvas_mcp",
        default_value: str = "",
    ) -> str:
        return _call("get_setting", {"name": name, "group": group,
                                     "default_value": default_value},
                     quiet=True)

    @mcp.tool(
        name="set_setting",
        description="写入插件持久化设置(kritarc)。",
    )
    def set_setting(name: str, value: str,
                    group: str = "krita_canvas_mcp") -> str:
        return _call("set_setting",
                     {"name": name, "value": str(value), "group": group})

    @mcp.tool(
        name="set_view_state",
        description="设置视图：zoom/rotation/mirror/center_to/reset_view。",
    )
    def set_view_state(
        zoom: float | None = None, rotation: float | None = None,
        mirror: bool | None = None, center_to: list | None = None,
        reset_view: bool = False,
    ) -> str:
        params = {k: v for k, v in {
            "zoom": zoom, "rotation": rotation, "mirror": mirror,
            "center_to": center_to, "reset_view": reset_view,
        }.items() if v is not None or k == "reset_view"}
        return _call("set_view_state", params)