from mcp.server.mcpserver import MCPServer

from . import (brush, document_io, fx, imaging, node, observe, paint,
               session, vector)


def register_all(mcp: MCPServer) -> None:
    observe.register(mcp)
    paint.register(mcp)
    brush.register(mcp)
    node.register(mcp)
    imaging.register(mcp)
    vector.register(mcp)
    fx.register(mcp)
    document_io.register(mcp)
    session.register(mcp)