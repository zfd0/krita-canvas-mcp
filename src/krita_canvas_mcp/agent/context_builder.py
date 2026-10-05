"""上下文装配 + LLM 输出解析（docs/草稿.txt 第二/三/四节契约）。

- parse_action: 解析 LLM 输出为动作 dict（plan/next_stage/done 或工具调用）。
- build_user_text: 装配每轮文本上下文，拆为 (stable_prefix, volatile_suffix) 两段，
  稳定段置于图像之前以命中 LLM 前缀缓存，变化段置于图像之后。

系统提示词已迁至 prompts.py（单一来源），此处不再持有。
"""
from __future__ import annotations

import json
import re

from .session_state import norm_color_token


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


def _norm_bbox(v) -> list:
    """规范化 bbox 为 [x, y, w, h] 四整数。

    容错模型偶发的双层嵌套（如 [[x,y,w,h]]），并统一转为整数。
    格式非法时抛 ParseError，交给 loop 按“输出解析失败”降级重试。
    """
    if isinstance(v, (list, tuple)):
        # 兼容双层嵌套：[[425,30,575,230]] → [425,30,575,230]
        if len(v) == 1 and isinstance(v[0], (list, tuple)) and len(v[0]) == 4:
            v = v[0]
        if len(v) == 4:
            try:
                return [int(round(float(x))) for x in v]
            except (TypeError, ValueError):
                raise ParseError(f"region bbox 数值非法: {v!r}")
    raise ParseError(f"region bbox 应为 [x,y,w,h]，实际为 {v!r}")


def _norm_points(pts) -> list:
    """规范化 points 为 [[x,y],...] 列表。

    容错模型可能返回 {"x": ..., "y": ...} 字典格式。
    """
    if not isinstance(pts, list):
        raise ParseError(f"points 应为数组，实际为 {pts!r}")
    result = []
    for p in pts:
        if isinstance(p, (list, tuple)):
            if len(p) >= 2:
                result.append([float(p[0]), float(p[1])])
            else:
                raise ParseError(f"point 至少需要 2 个坐标，实际为 {p!r}")
        elif isinstance(p, dict):
            # 兼容 {"x": 100, "y": 200} 格式
            if "x" in p and "y" in p:
                result.append([float(p["x"]), float(p["y"])])
            else:
                raise ParseError(f"point 字典缺少 x/y 字段: {p!r}")
        else:
            raise ParseError(f"point 格式非法: {p!r}")
    return result


def _norm_str_param(params: dict, key: str, default: str = "") -> str:
    """规范化字符串参数，如果是 dict 则取第一个值或返回 default。"""
    val = params.get(key, default)
    if isinstance(val, dict):
        # 模型可能错误地传了字典，尝试取 size 或其他值
        first_key = next(iter(val.keys()), None)
        if first_key:
            return str(val[first_key])
        return default
    return str(val) if val is not None else default


def parse_action(text: str) -> dict:
    """LLM 输出文本 → 动作 dict。
    - 特殊动作: {action: plan|next_stage|done}
    - 工具调用: {thought, stage, tool, params, color?}
    """
    obj = _extract_json(_strip_fence(text))
    if not isinstance(obj, dict):
        raise ParseError("输出必须是 JSON 对象")
    obj.setdefault("thought", "")
    # 工具调用分支优先：模型常同时给出 tool/params 与 "action":"draw"，
    # 若先命中 "action" 分支提前返回，会跳过 points/color 等参数归一化，
    # 导致下游拿到 list 型 color 等而崩溃。
    if "tool" in obj and "params" in obj:
        if not isinstance(obj.get("tool"), str) or not obj["tool"]:
            raise ParseError("tool 必须是非空字符串")
        if not isinstance(obj["params"], dict):
            raise ParseError("params 必须是对象")
        # 归一化 color：兼容 [r,g,b] / {"r":..,"g":..,"b":..} / '#hex' / 'cN'，
        # 避免 list/dict 进入账本 resolve 的字符串处理而崩溃
        if "color" in obj:
            obj["color"] = norm_color_token(obj["color"])
        # stage 必须是字符串，非字符串则丢弃，由下游按服务端当前阶段处理
        if "stage" in obj and not isinstance(obj["stage"], str):
            obj.pop("stage")
        # 规范化 points 格式（兼容 {"x", "y"} 字典）
        if "points" in obj["params"]:
            try:
                obj["params"]["points"] = _norm_points(obj["params"]["points"])
            except ParseError as e:
                raise ParseError(f"points 解析失败: {e}")
        # 规范化 stroke_style 和 fill_style（必须是字符串）
        for key in ("stroke_style", "fill_style"):
            if key in obj["params"]:
                obj["params"][key] = _norm_str_param(obj["params"], key, "ForegroundColor" if key == "stroke_style" else "None")
        # 移除无效的 node_id（null 或不存在的节点）
        if obj["params"].get("node_id") is None or obj["params"].get("node_id") == "":
            obj["params"].pop("node_id", None)
        return obj
    if "action" in obj:
        # plan 动作：校验/规范化 regions，避免下游按 [x,y,w,h] 解包或取字段崩溃
        if obj.get("action") == "plan":
            regions = obj.get("regions")
            if not isinstance(regions, list):
                raise ParseError("plan 必须包含 regions 数组")
            for i, r in enumerate(regions):
                if not isinstance(r, dict):
                    raise ParseError("plan regions 的每一项必须是对象 {id,name,bbox}")
                # 强制 id/name 为非空字符串：模型可能给出 null/数字，若原样保留，
                # 下游 build_user_text 的 r["id"] + r["name"] 与 r['name'][:6] 会抛
                # TypeError，直接中断闭环
                rid = r.get("id")
                r["id"] = str(rid) if rid not in (None, "") else f"r{i + 1}"
                name = r.get("name")
                r["name"] = str(name) if name not in (None, "") else ""
                if "bbox" in r:
                    r["bbox"] = _norm_bbox(r["bbox"])
        return obj
    raise ParseError(f"缺少 action 或 tool 字段: {json.dumps(obj, ensure_ascii=False)[:200]}")


def build_plan_prompt(canvas_w: int, canvas_h: int) -> str:
    """O 计划阶段的 plan 请求文本（画布真实像素尺寸，不缩放）。"""
    return (
        f"当前为 O 计划阶段。画布尺寸 {canvas_w}×{canvas_h}（像素坐标与此一致）。"
        f"请观察目标图像，输出 plan JSON：\n"
        "{\"action\":\"plan\",\"composition\":\"一句话构图描述\","
        "\"regions\":[{\"id\":\"r1\",\"name\":\"区域名\",\"bbox\":[x,y,w,h]}...3~8个],"
        "\"palette_hint\":[\"色相倾向描述...\"],"
        "\"stage_plan\":\"O→A→B→C→D 简述\",\"risks\":\"难点提醒\"}\n"
        "plan 就绪后，下一轮输出 next_stage 进入 A 阶段。只输出该 JSON，不要其他文字。"
    )


def build_user_text(state, metrics: dict, cov: float, regions_rows: list,
                    feedback: list | None = None,
                    sample_result: dict | None = None,
                    painted: float = 0.0, primary: str = "matched",
                    stage_note: str | None = None) -> tuple[str, str]:
    """每轮文本上下文装配，返回 (stable_prefix, volatile_suffix)。

    为提升 LLM 前缀缓存命中率，按「字节稳定性」拆段：
    - stable_prefix: 同一阶段内逐轮字节一致的内容（stage + plan），置于图像
      之前，可被前缀缓存命中（stage/plan 仅在 next_stage 时变）。
    - volatile_suffix: 每轮变化的内容（iteration/账本/近 5 步/进度/采样/反馈），
      置于图像之后。

    :param state:     SessionState
    :param metrics:   CanvasMetrics.scalars() + covered
    :param cov:       全图匹配度（ΔE<6 占比）
    :param painted:   全图已绘占比（白底画布上已落笔的像素比例）
    :param primary:   阶段主指标 "painted"(A/B) | "matched"(C/D)
    :param regions_rows: by_regions 行
    :param feedback:  上一轮被拒原因/提示，注入本轮
    :param stage_note: 阶段提示（O 阶段切换 / 动作数达标）
    """
    p = state.plan or {}

    # ---- 稳定前缀：stage + plan（置于图像之前，供前缀缓存命中）----
    plines = ["--- 阶段与计划（固定）---", f"stage: {state.stage}"]
    if p:
        plines.append(f"plan 构图: {p.get('composition', '')}")
        rids = " ".join(r["id"] + r.get("name", "") for r in p.get("regions", []))
        plines.append(f"plan regions: {rids}")
        if p.get("risks"):
            plines.append(f"plan risks: {p['risks']}")
    if p.get("stage_goals_cur"):
        plines.append(f"stage_goals: {p['stage_goals_cur']}")
    prefix = "\n".join(plines)

    # ---- 变化后缀：每轮都在变的内容（置于图像之后）----
    lines = []
    lines.append("--- 会话状态 ---")
    lines.append(f"iteration: {state.iteration}")

    lines.append("")
    lines.append("--- 颜色账本（按使用频率）---")
    ledger_lines = state.ledger.lines(20)
    lines += ledger_lines if ledger_lines else ["（尚无登记颜色）"]

    lines.append("")
    lines.append("--- 最近动作（近 5 步）---")
    lines += state.recent_summary(5) or ["（无）"]

    lines.append("")
    lines.append("--- 进度摘要 ---")
    pri_label = ("已绘(主指标·A/B看结构推进)" if primary == "painted"
                 else "匹配(主指标·C/D看颜色贴合)")
    lines.append(f"painted 已绘: {painted:.3f}   covered 匹配(前景ΔE<6): {cov:.3f}   "
                 f"主指标: {pri_label}")
    lines.append(f"delta_e_mean: {metrics.get('delta_e_mean', '?')}   "
                 f"ssim: {metrics.get('ssim', '?')}")
    for r in regions_rows:
        lines.append(f"  {r['id']:>3} {r['name'][:6]:<6} "
                     f"已绘{r['painted_pct']:.2f} 匹配{r['covered_pct']:.2f} "
                     f"{r['status']}")
    if sample_result:
        lines.append(f"上一轮采样结果: {sample_result[:200]}")

    if stage_note:
        lines.append("")
        lines.append(stage_note)
    if feedback:
        lines.append("")
        lines.append("⚠ 上轮输出的修正要求：")
        lines += [f"  - {f}" for f in feedback]

    lines.append("")
    lines.append("请输出本轮动作 JSON（一个对象，声明 stage）。")
    return prefix, "\n".join(lines)