"""Agent CLI 入口。

用法:
    python -m krita_canvas_mcp.agent --target <目标图> [选项]

前置条件:
    1. Krita 已打开并启用 krita_canvas_mcp 插件(127.0.0.1:5678/rpc)
    2. （可选）画布(活动文档)已按目标图尺寸新建；若无活动文档则自动创建
    3. VLM 配置（base_url / api_key / model）来自项目根 .env，缺失会报错；
       也可用环境变量或 --base-url / --api-key / --model 覆盖
"""
import argparse

from .loop import AgentLoop


def main() -> None:
    parser = argparse.ArgumentParser(prog="krita-canvas-agent",
                                     description="Krita 闭环绘画代理")
    parser.add_argument("--target", required=True, help="目标图像路径")
    parser.add_argument("--max-iterations", type=int, default=200,
                        help="迭代上限")
    parser.add_argument("--api-key", default=None,
                        help="VLM API Key(缺省从 .env / 环境变量读取)")
    parser.add_argument("--model", default=None,
                        help="模型名(缺省从 .env / 环境变量读取)")
    parser.add_argument("--base-url", default=None,
                        help="OpenAI 兼容接口根地址(缺省从 .env / 环境变量读取)")
    parser.add_argument("--retries", type=int, default=None,
                        help="VLM 调用失败重试次数(默认无限；0 表示不重试)")
    parser.add_argument("--raw-output", action="store_true",
                        help="打印 AI 原始输出文本（便于调试）")
    parser.add_argument("--confirm", action="store_true",
                        help="每步执行前暂停等待用户确认（回车继续 / q 退出）")
    parser.add_argument("--enable-thinking", action="store_true",
                        help="启用思考模式（Agnes 模型专用，提升推理能力）")
    parser.add_argument("--out-dir", default="outputs", help="结果输出目录")
    parser.add_argument("--endpoint", default=None,
                        help="Krita 插件 RPC 地址(默认 http://127.0.0.1:5678/rpc)")
    args = parser.parse_args()

    loop = AgentLoop(
        target_path=args.target,
        api_key=args.api_key,
        model=args.model,
        base_url=args.base_url,
        max_iterations=args.max_iterations,
        max_retries=args.retries,
        out_dir=args.out_dir,
        endpoint=args.endpoint or "http://127.0.0.1:5678/rpc",
        raw_output=args.raw_output,
        confirm=args.confirm,
        enable_thinking=args.enable_thinking,
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