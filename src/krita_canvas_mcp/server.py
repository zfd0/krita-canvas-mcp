"""MCP Server 装配 (适配 mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from .tools import register_all

SERVER_NAME = "krita-canvas-mcp"


def build_server() -> MCPServer:
    mcp = MCPServer(SERVER_NAME)
    register_all(mcp)
    return mcp