"""笔刷/颜色/采样类工具注册 (mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="set_colors",
        description="设置前景/背景色。颜色支持 '#RRGGBB' 或 [r,g,b(,a)](0-255 或 0-1)。",
    )
    def set_colors(foreground=None, background=None) -> str:
        params = {}
        if foreground is not None:
            params["foreground"] = foreground
        if background is not None:
            params["background"] = background
        return _call("set_colors", params)

    @mcp.tool(
        name="set_brush_params",
        description="批量设置画笔参数：size(px)/opacity/flow/rotation(度)/pattern_size，缺省项不动。",
    )
    def set_brush_params(
        size: float | None = None, opacity: float | None = None,
        flow: float | None = None, rotation: float | None = None,
        pattern_size: float | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "size": size, "opacity": opacity, "flow": flow,
            "rotation": rotation, "pattern_size": pattern_size,
        }.items() if v is not None}
        return _call("set_brush_params", params)

    @mcp.tool(
        name="list_resources",
        description="枚举可用资源名：preset(笔刷预设)/brush/pattern/gradient/palette/workspace。",
    )
    def list_resources(resource_type: str = "preset", filter: str | None = None) -> str:
        return _call("list_resources",
                     {"resource_type": resource_type, "filter": filter})

    @mcp.tool(
        name="set_brush_preset",
        description="按名称激活笔刷预设，后续 paint_* 均使用该预设。",
    )
    def set_brush_preset(preset_name: str, resource_type: str = "preset") -> str:
        return _call("set_brush_preset", {"preset_name": preset_name,
                                           "resource_type": resource_type})

    @mcp.tool(
        name="set_blending_mode",
        description="设置画笔混合模式(view级)或图层混合模式(node级)",
    )
    def set_blending_mode(
        mode: str, scope: str = "brush", node_id: str | None = None,
    ) -> str:
        params = {"mode": mode, "scope": scope}
        if scope == "node" and node_id is not None:
            params["node_id"] = node_id
        return _call("set_blending_mode", params)

    @mcp.tool(
        name="set_brush_flags",
        description="橡皮模式/锁定透明像素/禁用压感开关。",
    )
    def set_brush_flags(
        eraser_mode: bool | None = None,
        global_alpha_lock: bool | None = None,
        disable_pressure: bool | None = None,
    ) -> str:
        params = {k: v for k, v in {
            "eraser_mode": eraser_mode, "global_alpha_lock": global_alpha_lock,
            "disable_pressure": disable_pressure,
        }.items() if v is not None}
        return _call("set_brush_flags", params)

    @mcp.tool(
        name="sample_color",
        description="从目标图/画布/图层采样颜色，返回多种色彩空间表示。AI调色的事实来源，防漂移",
    )
    def sample_color(
        x: int, y: int, source: str = "target",
        node_id: str | None = None, radius: int = 0,
        reduce: str = "median",
        color_space: str = "srgb_hex",
        exclude_alpha_below: float = 0.1,
        multiple: list | None = None,
    ) -> str:
        """采样颜色。multiple 支持多点批量采样。"""
        params: dict = {
            "x": x, "y": y, "source": source,
            "node_id": node_id, "radius": radius,
            "reduce": reduce, "color_space": color_space,
            "exclude_alpha_below": exclude_alpha_below,
        }
        if multiple is not None:
            params["multiple"] = multiple
        return _call("sample_color", params)

    @mcp.tool(
        name="undo",
        description="撤销上一步绘画操作(支持多步)。闭环迭代的核心纠错通道。",
    )
    def undo(steps: int = 1, flush_before: bool = True) -> str:
        return _call("undo", {"steps": steps, "flush_before": flush_before})

    @mcp.tool(
        name="redo",
        description="重做被撤销的操作。",
    )
    def redo(steps: int = 1) -> str:
        return _call("redo", {"steps": steps})