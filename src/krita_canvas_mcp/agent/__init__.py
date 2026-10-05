"""闭环绘画 Agent 包。

Agent 架构：每轮
  观测(快照+diff) → 装配上下文(目标图+快照+热力图+状态文本) → VLM 决策
  → 阶段硬约束校验 → 经 HTTP bridge 调用 Krita 插件执行 → 记录
  → 终止判定(done/max_iter)。
阶段序列：O 计划 → A 草图 → B 线稿 → C 填色 → D 光影。
"""
from .loop import AgentLoop
from .vlm_client import VLMClient

__all__ = ["AgentLoop", "VLMClient"]