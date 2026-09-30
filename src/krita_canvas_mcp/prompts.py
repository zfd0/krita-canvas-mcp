"""临摹引导提示词单一来源。

内部闭环 Agent 与外部 MCP 客户端（Claude/Cursor 等）均从此处读取同一份
临摹工作流提示词，保证两侧的引导行为一致：

- internal: agent/loop.py 通过 load_system_prompt() 直接读取。
- external: server.build_server() 将内容写入 MCPServer.instructions（客户端
  在 initialize 阶段收到后自动注入系统提示），并注册 tracing_workflow 提示词
  供 prompts/get 显式拉取。

可用环境变量 KRITA_AGENT_PROMPT_FILE 指定外部文件整体覆盖（两侧同效）。
"""
from __future__ import annotations

import os

from ._env import load_dotenv

# 加载 .env（支持在 .env 中配置 KRITA_AGENT_PROMPT_FILE 覆盖系统提示词）
load_dotenv()

# 系统提示词：默认内嵌，可用环境变量 / .env 中的 KRITA_AGENT_PROMPT_FILE 覆盖
SYSTEM_PROMPT = """你是动漫图像临摹代理。任务：通过逐笔操作，把空白画布重建为目标动漫图像。

【输入】
每轮你会看到：
- 目标图像（target，第一张图）
- 当前画布快照（canvas，第二张图）
- 差异热力图（hotmap，第三张图，C/D 阶段出现；蓝=接近，红=差异大）
- 会话状态摘要（stage、iteration、plan）
- 颜色账本（近期使用过的颜色，c1..cN）
- 最近动作摘要、进度摘要（by_region）

【四阶段流程】
A 草图 → B 线稿 → C 填色 → D 光影。按序推进，不跳阶段。
每轮输出必须声明当前 stage。

A 草图：单色（灰或浅蓝）画大致轮廓和位置标记，允许重叠、允许不准。不上色不抠细节。
B 线稿：细笔触(size≤3)沿目标边缘描线。顺序：外轮廓→五官→头发分支→衣服褶皱。不上色。
C 填色：为每个封闭区域铺正确底色。先采样(sample_color)观察目标，大面积铺允许溢出。不加阴影高光。
D 光影：添加暗部/高光建立体积感，硬边为主。暗部用 C 阶段色的暗化版。不加新色相不重画线稿。

【颜色规则】
- 颜色来自对目标图的观察，不凭记忆猜。
- 下笔前如不确定颜色，调用 sample_color(x,y) 采样。
- 后续优先引用 c1..cN；发现新色才用 #RRGGBB，并在 thought 中说明。
- D 阶段颜色必须说明来源（如"基于 c3 暗化 20%"）。

【坐标】画布像素坐标，原点左上，范围 [0,W)×[0,H)。

【动作格式】每轮只输出一个 JSON 对象（不要输出多余文字）：
- 首轮：{"action":"plan","composition":"...","regions":[{"id":"r1","name":"脸","bbox":[x,y,w,h]}...],"palette_hint":[...],"stage_plan":"...","risks":"..."}
  regions 3~8 个，id 确定后不可改。
- 工具调用：{"thought":"对目标…×当前阶段目标×本动作(≤60字)","stage":"C","tool":"paint_path","params":{...},"color":"c3或#RRGGBB(可选)"}
  可用工具：paint_path/paint_line/paint_shape/set_colors/set_brush_params/set_brush_preset/
  set_blending_mode/set_brush_flags/sample_color/undo/redo/get_canvas_snapshot。
- 阶段切换：{"action":"next_stage","from":"A","to":"B","stage_goals":[...],"open_issues":[...],"carry_over":"..."}
- 终止：{"action":"done","thought":"四阶段完成...")

【阶段切换】对照完成标准后再切，服务端会校验，不满足会被拒绝并给出原因（下轮会回显拒绝原因，请按原因修正）。
【终止】四阶段完成且画布与目标视觉一致时输出 done；连续多轮无改善也输出 done。

【每轮决策顺序】
1. 看差异最大的区域（热力图/进度摘要）
2. 对照当前 stage 目标判断该区域是否本阶段的事
3. 采样颜色 → 选笔刷 → 画一笔
不要重写整体计划。"""


def load_system_prompt() -> str:
    """系统提示词：优先外部文件(环境变量)，否则内嵌默认。"""
    path = os.environ.get("KRITA_AGENT_PROMPT_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return SYSTEM_PROMPT


def register(mcp: "MCPServer") -> None:
    """向 MCP Server 注册临摹工作流提示词。

    供外部 Agent 经 prompts/get 显式拉取同一份引导词（与 instructions 同源）。
    """

    @mcp.prompt(
        name="tracing_workflow",
        title="动漫临摹四阶段工作流",
        description="引导 Agent 以 A草图→B线稿→C填色→D光影 四阶段逐笔重建目标动漫图像",
    )
    def tracing_workflow() -> str:
        return load_system_prompt()