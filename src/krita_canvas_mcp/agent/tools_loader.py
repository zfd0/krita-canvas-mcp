"""从 mcp-tools-schema.json 加载并转换工具定义为 OpenAI function calling 格式。

schema 是单一事实来源（包内 resources/mcp-tools-schema.json），此处只负责格式转换：
  MCP 格式 → OpenAI tools 格式
  inputSchema / name / description → type:function / function.name / function.parameters
"""
from __future__ import annotations

import json
from pathlib import Path


def _convert_tool(tool: dict) -> dict:
    """单个 MCP 工具 → OpenAI function tool。"""
    input_schema = tool.get("inputSchema", {})
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": input_schema,
        },
    }


def build_agent_tools(schema_path: str | None = None) -> list[dict]:
    """从 schema 文件构建 agents 可用的 tools 列表。

    优先使用显式路径，否则自动从 __file__ 推导项目根目录。
    返回空列表表示加载失败（调用方可降级为无 tools）。
    """
    if schema_path is None:
        # __file__ = .../krita_canvas_mcp/agent/tools_loader.py
        # parent.parent = 包根 krita_canvas_mcp；schema 位于包的 resources/ 下
        pkg_root = Path(__file__).resolve().parent.parent
        schema_path = str(pkg_root / "resources" / "mcp-tools-schema.json")
    try:
        with open(schema_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[agent] 无法加载工具 schema: {e}，将不注入结构化 tools")
        return []
    tools = [_convert_tool(t) for t in data.get("tools", [])]
    print(f"[agent] 已加载 {len(tools)} 个工具 schema → OpenAI tools 格式")
    return tools
