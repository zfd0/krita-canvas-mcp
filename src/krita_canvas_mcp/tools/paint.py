"""绘画执行类工具注册 (mcp 2.x)。
画布上的一切笔迹写入经这些工具转发到 Krita 插件执行。
"""
from mcp.server.mcpserver import MCPServer

from ._common import call as _call


def register(mcp: MCPServer) -> None:

    @mcp.tool(
        name="paint_line",
        description="用当前笔刷在两点间绘制一条直线，可带首尾压感。适合轮廓/排线。"
                    "绘制后请调用 wait_for_done 确保投影同步。",
    )
    def paint_line(
        x1: int, y1: int, x2: int, y2: int,
        node_id: str | None = None,
        pressure1: float = 1.0, pressure2: float = 1.0,
        stroke_style: str = "ForegroundColor",
    ) -> str:
        return _call(
            "paint_line",
            {"node_id": node_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
             "pressure1": pressure1, "pressure2": pressure2,
             "stroke_style": stroke_style},
        )

    @mcp.tool(
        name="paint_path",
        description="用当前笔刷沿折线/贝塞尔路径绘制自由笔画。points 为 [[x,y(,压感)],...]，"
                    "smooth=True 贝塞尔平滑，closed=True 闭合填充。闭环绘画主执行通道。",
    )
    def paint_path(
        points: list,
        node_id: str | None = None,
        smooth: bool = True, closed: bool = False,
        stroke_style: str = "ForegroundColor", fill_style: str = "None",
    ) -> str:
        return _call(
            "paint_path",
            {"node_id": node_id, "points": points, "smooth": smooth,
             "closed": closed, "stroke_style": stroke_style,
             "fill_style": fill_style},
        )

    @mcp.tool(
        name="paint_shape",
        description="绘制椭圆/矩形/多边形色块(可选描边+填充)，用于大色块铺底。",
    )
    def paint_shape(
        shape: str,
        node_id: str | None = None,
        rect: dict | None = None, points: list | None = None,
        stroke_style: str = "None", fill_style: str = "ForegroundColor",
    ) -> str:
        return _call(
            "paint_shape",
            {"node_id": node_id, "shape": shape, "rect": rect,
             "points": points, "stroke_style": stroke_style,
             "fill_style": fill_style},
        )

    @mcp.tool(
        name="write_pixels",
        description="将像素补丁(RGBA PNG/base64)直接写入图层指定区域，用于精细重建。",
    )
    def write_pixels(
        x: int, y: int, width: int, height: int, image_b64: str,
        node_id: str | None = None, blend_mode: str = "overwrite",
    ) -> str:
        return _call(
            "write_pixels",
            {"node_id": node_id, "x": x, "y": y, "width": width,
             "height": height, "image_b64": image_b64,
             "blend_mode": blend_mode},
        )

    @mcp.tool(
        name="check_paintability",
        description="检查图层能否被当前笔刷绘制(PAINT/VECTOR/CLONE/UNPAINTABLE等)。",
    )
    def check_paintability(node_id: str) -> str:
        return _call("check_paintability", {"node_id": node_id})

    @mcp.tool(
        name="wait_for_done",
        description="阻塞至所有后台笔刷任务完成并刷新投影合成，确保下次快照为最新画面。",
    )
    def wait_for_done(refresh_projection: bool = True) -> str:
        return _call("wait_for_done", {"refresh_projection": refresh_projection})