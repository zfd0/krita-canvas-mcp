from mcp.server.mcpserver import MCPServer

from . import brush, node, observe, paint, session


def register_all(mcp: MCPServer) -> None:
    observe.register(mcp)
    paint.register(mcp)
    brush.register(mcp)
    node.register(mcp)
    session.register(mcp)