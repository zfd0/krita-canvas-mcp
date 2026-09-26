# 未实现工具的统一占位。
from mcp.server.mcpserver import MCPServer
from ..envelope import err
from ..errors import KritaError, ErrCode

# (name, description) 列表，从 schema 里批量导入
STUB_TOOLS = [
    ("paint_line", "用当前笔刷绘制直线"),
    ("paint_path", "沿折线/贝塞尔路径绘制自由笔画"),
    ("paint_shape", "绘制椭圆/矩形/多边形色块"),
    # ... 其余全部工具
]

def register(mcp: MCPServer):
    def make(name: str, desc: str):
        def tool(**kwargs) -> str:
            return err(KritaError(ErrCode.NOT_IMPLEMENTED,
                                  f"{name} 尚未实现"))
        tool.__name__ = name
        return tool
    for name, desc in STUB_TOOLS:
        mcp.tool(name=name, description=desc)(make(name, desc))