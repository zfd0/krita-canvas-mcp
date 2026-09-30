"""闭环绘画 Agent 包。

Agent 架构：每轮
  观测(快照+diff) → 装配上下文(目标图+快照+热力图+状态文本) → GLM-4.6V 决策
  → 阶段硬约束校验 → 经 HTTP bridge 调用 Krita 插件执行 → 记录/停滞检测
  → 终止判定(done/max_iter/converged/stalled)。
"""
from .glm_client import GLMVisionClient
from .loop import AgentLoop

__all__ = ["AgentLoop", "GLMVisionClient"]