"""Agent CLI 入口。

用法:
    python -m krita_canvas_mcp.agent --target <目标图> [选项]

前置条件:
    1. Krita 已打开并启用 krita_canvas_mcp 插件(127.0.0.1:5678/rpc)
    2. （可选）画布(活动文档)已按目标图尺寸新建；若无活动文档则自动创建
"""
import argparse

from .loop import AgentLoop


def main() -> None:
    parser = argparse.ArgumentParser(prog="krita-canvas-agent",
                                     description="Krita 闭环绘画代理")
    parser.add_argument("--target", required=True, help="目标图像路径")
    parser.add_argument("--max-iterations", type=int, default=200,
                        help="迭代上限")
    parser.add_argument("--api-key", default=None, help="GLM API Key(缺省用环境变量/内置)")
    parser.add_argument("--model", default=None, help="模型名(默认 glm-4.6v-flash)")
    parser.add_argument("--out-dir", default="outputs", help="结果输出目录")
    parser.add_argument("--endpoint", default=None,
                        help="Krita 插件 RPC 地址(默认 http://127.0.0.1:5678/rpc)")
    args = parser.parse_args()

    loop = AgentLoop(
        target_path=args.target,
        api_key=args.api_key,
        model=args.model,
        max_iterations=args.max_iterations,
        out_dir=args.out_dir,
        endpoint=args.endpoint or "http://127.0.0.1:5678/rpc",
    )
    try:
        summary = loop.run()
        print("=== 会话结果 ===")
        import json
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("\n[agent] 用户中断。最终快照与摘要已尽量落盘(见 outputs/)。")


if __name__ == "__main__":
    main()