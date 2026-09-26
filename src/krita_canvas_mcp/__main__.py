"""入口：python -m krita_canvas_mcp 或 krita-canvas-mcp (适配 mcp 2.x)

默认 stdio transport（供 Claude Desktop / Cursor 等 MCP 客户端使用）。
用 --transport streamable-http 起 HTTP 服务，便于 curl 调试。
"""
import argparse
import sys

from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="krita-canvas-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP 传输方式（默认 stdio）。注意 mcp 2.x 已移除 sse 传输。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8765, help="HTTP 监听端口")
    args = parser.parse_args()

    mcp = build_server()

    if args.transport == "stdio":
        mcp.run()  # 默认 stdio
        return

    # 2.x 中 host/port 作为 run() 的参数传入
    try:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    except Exception as e:
        print(f"启动 {args.transport} 传输失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()