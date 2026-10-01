"""阶段硬约束（docs/草稿.txt 第五节）。

服务端按 stage 校验本轮动作，不符则拒绝并返回原因让模型重试。
硬约束只锁最常见的翻车：A 上色、B 用粗笔、C 透明叠色、D 造新色。
"""
from __future__ import annotations

STAGE_SEQ = ["A", "B", "C", "D"]
STAGE_NAMES = {"A": "草图", "B": "线稿", "C": "填色", "D": "光影"}

# 每阶段动作数下限（低于则拒绝 next_stage）
MIN_ACTIONS = {"A": 8, "B": 8, "C": 6, "D": 6}

# 硬约束表：允许工具 + 校验函数列表
# 校验函数签名: (tool: str, params: dict) -> (ok: bool, err: str|None)
_LIMIT_COLOR = {"#808080", "#7F8C8D", "#A0A0A0", "#90A4AE", "#B0B0B0",
                "#00BFFF", "#5B9BD5", "#87CEEB"}


def _grayish(hexv: str) -> bool:
    """粗略判断颜色是否灰/蓝系（A 阶段只允许灰/蓝）。"""
    h = hexv.lstrip("#")
    if len(h) < 6:
        return False
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    if abs(r - g) < 40 and abs(g - b) < 40:   # 灰
        return True
    if b >= r and b >= g and b > 80:          # 蓝
        return True
    return False


def _check_a(tool: str, params: dict, color: str | None):
    if tool in ("paint_path", "paint_shape") and \
            params.get("fill_style") not in (None, "None"):
        return False, ("STAGE_A_NO_FILL: 草图阶段禁止填充色块"
                       "（paint_path 的 fill_style 必须为 None）")
    if color and not _grayish(color):
        return False, "STAGE_A_GRAY_ONLY: 草图阶段仅允许灰/蓝色"
    if tool in ("set_brush_params", "paint_path", "paint_line") \
            and params.get("size") is not None \
            and not (4 <= float(params["size"]) <= 12):
        return False, "STAGE_A_BRUSH_SIZE: 草图阶段笔刷 size 应为 4~12"
    return True, None


def _check_b(tool: str, params: dict, color: str | None):
    if tool in ("set_brush_params", "paint_path", "paint_line") \
            and params.get("size") is not None \
            and float(params["size"]) > 3:
        return False, "STAGE_B_TOO_THICK: 线稿阶段笔刷 size 应 ≤3"
    if tool in ("paint_path", "paint_shape") and \
            params.get("fill_style") not in (None, "None"):
        return False, "STAGE_B_NO_FILL: 线稿阶段禁止填充"
    if color and not _grayish(color):
        return False, "STAGE_B_GRAY_ONLY: 线稿阶段尚未引入实色"
    return True, None


def _check_c(tool: str, params: dict, color: str | None):
    if tool == "set_brush_params" and params.get("opacity") is not None \
            and float(params["opacity"]) < 0.9:
        return False, "STAGE_C_TRANSPARENT: 填色阶段禁止半透明叠色(opacity<0.9)"
    return True, None


def _check_d(tool: str, params: dict, color: str | None):
    if tool == "set_brush_params" and params.get("opacity") is not None \
            and float(params["opacity"]) < 0.9:
        return False, "STAGE_D_SOLID: 光影阶段建议实色"
    return True, None


# stage -> (允许工具集合, 校验函数)
RULES = {
    "A": ({"paint_path", "paint_line", "set_brush_params", "set_brush_preset",
           "undo", "redo", "sample_color", "set_colors"}, _check_a),
    "B": ({"paint_path", "paint_line", "set_brush_params", "set_brush_preset",
           "undo", "redo", "sample_color", "set_colors"}, _check_b),
    "C": ({"paint_path", "paint_shape", "set_colors", "sample_color",
           "set_brush_params", "set_brush_preset", "set_brush_flags",
           "undo", "redo", "get_canvas_snapshot"}, _check_c),
    "D": ({"paint_path", "paint_shape", "set_colors", "set_blending_mode",
           "set_brush_params", "set_brush_preset", "set_brush_flags",
           "sample_color", "undo", "redo", "get_canvas_snapshot"}, _check_d),
}


def validate(stage: str, tool: str, params: dict,
             color: str | None = None,
             color_token: str | None = None) -> tuple:
    """阶段硬约束校验。
    :param color:       已解析的 #RRGGBB（供灰度/颜色类校验使用）
    :param color_token: 动作里的原始颜色引用（c1/c2/#hex），用于识别遗留色槽
    :return: (ok: bool, err: str|None)
    """
    if stage not in RULES:
        return False, f"STAGE_UNKNOWN: 非法阶段 {stage}（应为 A/B/C/D）"
    allowed, check = RULES[stage]
    if tool not in allowed:
        return False, (f"STAGE_{stage}_TOOL_FORBIDDEN: "
                       f"阶段 {stage}({STAGE_NAMES[stage]}) 不允许使用 {tool}，"
                       f"允许: {sorted(allowed)}")
    # C/D 颜色阶段禁止引用 c1/c2：它们在 A/B 阶段几乎总是草稿灰残留，
    # 模型采样到正确色后仍写 c1 会导致“采样蓝、下笔灰”的串色问题
    if (stage in ("C", "D") and tool and tool.startswith("paint")
            and (color_token or "").lower() in ("c1", "c2")):
        return False, (f"STAGE_{stage}_LEGACY_COLOR: {stage} 阶段禁止用 "
                       f"{color_token} 填色（多为 A/B 草稿灰）。"
                       f"请省略 color 字段（沿用最近采样色），"
                       f"或使用采样返回的 #RRGGBB")
    return check(tool, params, color)


def validate_next_stage(state, from_stage: str, to_stage: str) -> tuple:
    """阶段切换校验。
    :return: (ok: bool, err: str|None)
    """
    if from_stage != state.stage:
        return False, (f"STAGE_MISMATCH: 声明 from={from_stage}，"
                       f"但当前阶段是 {state.stage}")
    try:
        fi, ti = STAGE_SEQ.index(from_stage), STAGE_SEQ.index(to_stage)
    except ValueError:
        return False, f"STAGE_UNKNOWN: 阶段应为 A/B/C/D，得到 {from_stage}/{to_stage}"
    if ti != fi + 1:
        return False, f"STAGE_SKIP: 不允许从 {from_stage} 跳到 {to_stage}，必须顺序推进"
    need = MIN_ACTIONS.get(from_stage, 0)
    done = state.stage_actions.get(from_stage, 0)
    if done < need:
        return False, (f"STAGE_NOT_READY: 阶段 {from_stage} 仅完成 {done} 个动作，"
                       f"不足 {need} 个，请继续当前阶段工作")
    return True, None