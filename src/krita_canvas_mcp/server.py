"""MCP Server 装配 (适配 mcp 2.x)。"""
from mcp.server.mcpserver import MCPServer

from .prompts import load_system_prompt, register as register_prompts
from .tools import register_all

SERVER_NAME = "krita-canvas-mcp"


def build_server() -> MCPServer:
    """装配 MCP Server。

    instructions 随 initialize 返回给客户端（Claude/Cursor 等会自动注入系统提示），
    与内置闭环 Agent 共用同一份临摹引导提示词（单一来源 prompts.py）。
    另注册 tracing_workflow 提示词供外部 Agent 经 prompts/get 显式拉取。
    """
    mcp = MCPServer(SERVER_NAME, instructions=load_system_prompt())
    register_all(mcp)
    register_prompts(mcp)
    return mcp