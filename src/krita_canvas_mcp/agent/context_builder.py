"""上下文装配 + LLM 输出解析（docs/草稿.txt 第二/三/四节契约）。

- parse_action: 解析 LLM 输出为动作 dict（plan/next_stage/done 或工具调用）。
- build_user_text: 按第四节模板装配每轮文本上下文。

系统提示词已迁至 prompts.py（单一来源），此处不再持有。
"""
from __future__ import annotations

import json
import re


class ParseError(Exception):
    """LLM 输出无法解析为合法动作。"""


def _strip_fence(text: str) -> str:
    """剥离 ```json ... ``` 围栏与首尾空白。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        return m.group(1).strip()
    return t


def _extract_json(text: str):
    """从文本中提取首个 JSON 对象（容错：找最外层花括号）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ParseError(f"输出中找不到 JSON 对象: {text[:200]}")
    cand = text[start:end + 1]
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        pass
    # 逐层回退：尝试更短的截断逐个解析
    depth = 0
    cut = -1
    for i, ch in enumerate(cand):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                cut = i + 1
                break
    if cut > 0:
        try:
            return json.loads(cand[:cut])
        except json.JSONDecodeError as e:
            raise ParseError(f"JSON 解析失败: {e}; 原始: {text[:200]}")
    raise ParseError(f"JSON 解析失败: {text[:200]}")


def parse_action(text: str) -> dict:
    """LLM 输出文本 → 动作 dict。
    - 特殊动作: {action: plan|next_stage|done}
    - 工具调用: {thought, stage, tool, params, color?}
    """
    obj = _extract_json(_strip_fence(text))
    if not isinstance(obj, dict):
        raise ParseError("输出必须是 JSON 对象")
    obj.setdefault("thought", "")
    if "action" in obj:
        return obj
    if "tool" in obj and "params" in obj:
        obj.setdefault("stage", "A")
        if not isinstance(obj["params"], dict):
            raise ParseError("params 必须是对象")
        return obj
    raise ParseError(f"缺少 action 或 tool 字段: {json.dumps(obj, ensure_ascii=False)[:200]}")


def build_plan_prompt(canvas_w: int, canvas_h: int) -> str:
    """首轮 plan 请求文本。"""
    return (
        f"画布尺寸 {canvas_w}×{canvas_h}。请观察目标图像，输出首轮 plan JSON：\n"
        "{\"action\":\"plan\",\"composition\":\"一句话构图描述\","
        "\"regions\":[{\"id\":\"r1\",\"name\":\"区域名\",\"bbox\":[x,y,w,h]}...3~8个],"
        "\"palette_hint\":[\"色相倾向描述...\"],"
        "\"stage_plan\":\"A→B→C→D 简述\",\"risks\":\"难点提醒\"}\n"
        "只输出该 JSON，不要其他文字。"
    )


def build_user_text(state, metrics: dict, cov: float, regions_rows: list,
                    feedback: list | None = None, stall_note: str | None = None,
                    sample_result: dict | None = None) -> str:
    """每轮文本上下文装配（草稿第四节模板）。
    :param state:     SessionState
    :param metrics:   CanvasMetrics.scalars() + covered
    :param cov:       全图 covered_pct
    :param regions_rows: by_regions 行
    :param feedback: 上一轮被拒原因/提示，注入本轮
    """
    lines = []
    lines.append("--- 会话状态 ---")
    lines.append(f"stage: {state.stage}    iteration: {state.iteration}    "
                 f"stall_rounds: {state.stall_rounds}")
    p = state.plan or {}
    if p:
        lines.append(f"plan 构图: {p.get('composition', '')}")
        rids = " ".join(r["id"] + r.get("name", "") for r in p.get("regions", []))
        lines.append(f"plan regions: {rids}")
        if p.get("risks"):
            lines.append(f"plan risks: {p['risks']}")
    if p.get("stage_goals_cur"):
        lines.append(f"stage_goals: {p['stage_goals_cur']}")

    lines.append("")
    lines.append("--- 颜色账本（按使用频率）---")
    ledger_lines = state.ledger.lines(20)
    lines += ledger_lines if ledger_lines else ["（尚无登记颜色）"]

    lines.append("")
    lines.append("--- 最近动作（近 5 步）---")
    lines += state.recent_summary(5) or ["（无）"]

    lines.append("")
    lines.append("--- 进度摘要 ---")
    lines.append(f"covered_pct: {cov:.3f}   "
                 f"delta_e_mean: {metrics.get('delta_e_mean', '?')}   "
                 f"ssim: {metrics.get('ssim', '?')}")
    for r in regions_rows:
        lines.append(f"  {r['id']:>3} {r['name'][:6]:<6} "
                     f"{r['covered_pct']:.2f} {r['status']}")
    if sample_result:
        lines.append(f"上一轮采样结果: {json.dumps(sample_result, ensure_ascii=False)[:200]}")

    if stall_note:
        lines.append("")
        lines.append(stall_note)
    if feedback:
        lines.append("")
        lines.append("⚠ 上轮输出的修正要求：")
        lines += [f"  - {f}" for f in feedback]

    lines.append("")
    lines.append("请输出本轮动作 JSON（一个对象，声明 stage）。")
    return "\n".join(lines)