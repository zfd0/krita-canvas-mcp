"""上下文装配 + LLM 输出解析（docs/草稿.txt 第二/三/四节契约）。

- SYSTEM_PROMPT: 系统提示词(与 docs/系统提示词草稿.txt 配套)。
- parse_action: 解析 LLM 输出为动作 dict（plan/next_stage/done 或工具调用）。
- build_user_text: 按第四节模板装配每轮文本上下文。
"""
from __future__ import annotations

import json
import os
import re

# 系统提示词：默认内嵌，可用环境变量 KRITA_AGENT_PROMPT_FILE 指定外部文件覆盖
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


def load_system_prompt() -> str:
    """系统提示词：优先外部文件(环境变量)，否则内嵌默认。"""
    path = os.environ.get("KRITA_AGENT_PROMPT_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return SYSTEM_PROMPT