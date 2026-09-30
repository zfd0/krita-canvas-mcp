# Krita Canvas MCP

将 Krita 封装为 MCP 工具 + 闭环绘画 Agent：LLM 基于画布快照逐笔预测动作，调用 Krita LibKis API 执行，迭代完成对目标图像的过程重建。

## 架构

```
┌──────────────┐  MCP tools    ┌──────────────────┐  JSON-RPC HTTP  ┌─────────────┐
│  MCP 客户端   │ ←──────────→ │ external MCP     │ ←─────────────→ │ Krita 插件   │
│ (Claude等)    │  (stdio/http) │ server (src/)    │  127.0.0.1:5678 │ (plugin/)    │
└──────────────┘               └────────┬─────────┘                 └─────────────┘
                                        │ 直接 bridge.call()                ↑
                                        ▼                                    │
                               ┌──────────────────┐  ┌────────────────────┐  │
                               │ 闭环 Agent (loop) │→→│ GLM-4.6V 多模态 LLM │  │
                               │ target+快照+热力图 │  └────────────────────┘  │
                               └──────────────────┘                           │
```
- `plugin/`：Krita 内插件（LibKis 必须在主线程执行，HTTP 请求经队列由 QTimer 消费）
- `src/krita_canvas_mcp/`：MCP 服务器（无状态工具转发）+ `agent/` 闭环绘画代理（有状态：stage/plan/颜色账本/动作历史/停滞检测）
- `docs/`：闭环协作契约（草稿.txt）与系统提示词（系统提示词草稿.txt）

## 快速开始

### 1. 部署 Krita 插件

```
python scripts/install_plugin.py        # 复制到 %APPDATA%\krita\pykrita
```

重启 Krita → 设置 → 配置 Krita → Python 插件管理器 → 勾选 `Krita Canvas MCP` → 再重启。
控制台出现 `[krita-canvas-mcp] HTTP RPC listening on 127.0.0.1:5678` 即成功。

### 2. 新建画布

在 Krita 中按目标图尺寸新建文档（推荐 RGBA/U8，白底填充可选）。

### 3. 跑闭环 Agent

```
python -m krita_canvas_mcp.agent --target <目标图路径> --max-iterations 200 --out-dir outputs
```

选项：`--api-key`(默认内置临时密钥，可用环境变量 `GLM_API_KEY` 覆盖)、`--model`(默认 `glm-4.6v-flash`)、`--endpoint`。

结果落盘 `outputs/`：`final_*.png`（最终画布）、`summary_*.json`（指标/阶段分布/终止原因）、`action_history.jsonl`（逐轮记录）。

### 4. 仅用 MCP 工具（不跑闭环）

```
python -m krita_canvas_mcp                          # stdio（Claude Desktop 等）
python -m krita_canvas_mcp --transport streamable-http --port 8765
```

## 闭环契约（要点）

- **四阶段** A 草图 → B 线稿 → C 填色 → D 光影，显式 `next_stage` 切换（服务端校验：顺序推进 + 本阶段最少成功动作数，不满足拒绝并回显原因让模型重试）
- **每轮** 一个动作：`{"thought":"≤60字三段式","stage":"C","tool":"paint_path","params":{...},"color":"c3"}`
- **硬约束** 按阶段锁翻车：A/B 禁止填充与实色、B 笔刷 ≤3px、C/D 禁止半透明叠色
- **上下文装配** 每轮全量给：目标图 + 画布快照 + 热力图(C/D 阶段) + 会话状态 + 颜色账本(c1..cN) + 近 5 步动作 + 区域进度
- **三重终止** LLM 自报 `done` / 达到迭代上限 / 像素收敛(ΔE<4 且 SSIM>0.92)，附加停滞检测：连续 3 轮无改善注入提示、5 轮强制终止
- LLM 的 `color` 字段支持 `cN`(账本编号) 或 `#RRGGBB`，执行前自动转为 `set_colors(foreground)`

## 工具清单

MCP 工具定义见 `mcp-tools-schema.json`（56 个）。当前已实现链路：

| 类别 | 工具 |
|---|---|
| 观测 | get_document_info / get_canvas_snapshot / list_documents / get_node_tree / sample_color |
| 绘画 | paint_line / paint_path / paint_shape / write_pixels / check_paintability / wait_for_done |
| 笔刷 | set_colors / set_brush_params / set_brush_preset / list_resources / set_blending_mode / set_brush_flags / undo / redo |
| 节点 | create_node / set_node_props / manage_node |

## 开发自测

纯逻辑（不依赖 Krita/GLM）的 mock 端到端已通过：
- 阶段硬约束全部用例（A 禁填充/禁实色、B 粗笔、C 透明叠色）
- plan→paint_path→done 主循环；违规→拒绝→修正恢复路径
- 差异度量(mae/rmse/psnr/ssim/ΔE/covered_pct/热力图)、颜色账本 cN 解析、LLM 输出 JSON 容错解析