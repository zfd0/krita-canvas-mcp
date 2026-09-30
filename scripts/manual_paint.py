"""人工绘制模式（独立程序，不修改 src/ 下任何文件）。

每轮显示画布状态，由人工输入动作，直接通过 Krita 插件 RPC 执行。
与 AgentLoop 的自动 LLM 闭环完全独立。

用法:
    python scripts/manual_paint.py --target <目标图路径> [--host 127.0.0.1:5678]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

# ------------------------------------------------------------------ 配置
DEFAULT_ENDPOINT = "http://127.0.0.1:5678/rpc"
SNAPSHOT_MAX_SIDE = 768
HEATMAP_MAX_SIDE = 384

# 可用工具列表（供提示展示）
AVAILABLE_TOOLS = [
    "paint_line", "paint_path", "paint_shape", "write_pixels",
    "check_paintability", "wait_for_done",
    "set_colors", "set_brush_params", "set_brush_preset", "list_resources",
    "set_blending_mode", "set_brush_flags", "sample_color",
    "undo", "redo",
    "get_document_info", "get_canvas_snapshot", "get_node_tree",
    "create_document", "open_document", "close_document", "save_document",
    "transform_document", "apply_filter",
]

# ------------------------------------------------------------------ HTTP 桥接


def rpc_call(endpoint: str, method: str, params: dict | None = None,
             timeout: float = 60.0) -> dict:
    """向 Krita 插件发送 RPC 调用并返回解析后的响应。"""
    body = {"method": method, "params": params or {}}
    try:
        resp = httpx.post(endpoint, json=body, timeout=timeout)
    except httpx.ConnectError as e:
        sys.exit(f"无法连接 Krita 插件 ({endpoint})；请确认 Krita 已打开且插件已启用。\n{e}")
    if resp.status_code != 200:
        sys.exit(f"Krita 插件返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("ok", False):
        err_code = data.get("err_code", "UNKNOWN")
        err_msg = data.get("message", "unknown error")
        sys.exit(f"[{err_code}] {err_msg}")
    return data.get("data", {})


# ------------------------------------------------------------------ 图像工具


def pil_to_b64(img: Image.Image, fmt: str = "PNG") -> str:
    """PIL Image → base64 字符串。"""
    buf = io.BytesIO()
    img.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def b64_to_pil(b64: str) -> Image.Image:
    """base64 PNG → PIL Image (RGB)。"""
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def resize_if_needed(img: Image.Image, max_side: int) -> Image.Image:
    """超过 max_side 时等比缩小。"""
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / max(w, h)
    return img.resize((int(w * scale), int(h * scale)))


# ------------------------------------------------------------------ 差异度量（本地计算，无需 numpy）


def compute_diff_metrics(target_arr: list[list[list[int]]],
                          canvas_arr: list[list[list[int]]]) -> dict:
    """逐像素计算 MAE、覆盖率（ΔE < 6 的像素占比）。

    target_arr / canvas_arr: [[R,G,B], ...] 一维列表，长度相同。
    """
    n = len(target_arr)
    if n == 0:
        return {"mae": 0.0, "covered_pct": 0.0}

    total_err = 0.0
    covered = 0
    for t_px, c_px in zip(target_arr, canvas_arr):
        # sRGB → LAB 简化版：直接用 RGB 欧氏距离近似（足够用于覆盖率判断）
        diff = sum((t - c) ** 2 for t, c in zip(t_px, c_px))
        total_err += diff ** 0.5
        # 简化阈值：各通道差值之和 < 18（≈ ΔE < 6 的近似）
        channel_sum = sum(abs(t - c) for t, c in zip(t_px, c_px))
        if channel_sum < 18:
            covered += 1

    return {
        "mae": round(total_err / n, 2),
        "covered_pct": round(covered / n, 4),
    }


def array_to_pixels(img: Image.Image) -> list[list[list[int]]]:
    """PIL RGB Image → [[R,G,B], ...] 平铺列表（行优先）。"""
    pixels = list(img.getdata())
    return [list(px) for px in pixels]


# ------------------------------------------------------------------ 快照获取


def get_snapshot(endpoint: str, work_w: int, work_h: int) -> tuple[Image.Image, dict]:
    """获取画布快照，缩放到 work_w×work_h。"""
    data = rpc_call(endpoint, "get_canvas_snapshot", {"max_side": SNAPSHOT_MAX_SIDE})
    img = b64_to_pil(data["image_b64"])
    if img.size != (work_w, work_h):
        img = img.resize((work_w, work_h))
    return img, data


# ------------------------------------------------------------------ 人工输入解析


def parse_action_input(raw: str) -> dict:
    """解析用户输入的文本为动作 dict。

    支持格式：
      - JSON 对象（完整 tool + params）
      - 快捷命令：done / quit / next_stage A B / plan
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("空输入")

    low = raw.lower()

    # 快捷命令
    if low in ("done", "quit", "q"):
        if low == "done":
            thought = input("  终止理由（回车跳过）: ").strip()
            return {"action": "done", "thought": thought}
        return {"action": "done", "thought": "人工退出"}

    if low == "plan":
        print("  请输入 plan JSON（单行或多行，空行结束）:")
        lines: list[str] = []
        while True:
            line = input("  > ").strip()
            if line == "":
                break
            lines.append(line)
        combined = " ".join(lines)
        try:
            parsed = json.loads(combined)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败: {e}")
        if "action" not in parsed:
            parsed["action"] = "plan"
        return parsed

    if low.startswith("next_stage"):
        parts = raw.split()
        from_s = parts[1] if len(parts) > 1 else ""
        to_s = parts[2] if len(parts) > 2 else ""
        return {"action": "next_stage", "from": from_s, "to": to_s,
                "thought": "人工切换阶段"}

    # 尝试 JSON 解析
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    raise ValueError(f"无法解析输入: {raw[:100]}")


def build_context_text(stage: str, iteration: int, plan: dict | None,
                       metrics: dict, region_rows: list[dict],
                       color_ledger: list[str], recent_actions: list[str],
                       feedback: list[str], stall_note: str | None) -> str:
    """装配每轮上下文文本。"""
    lines = []
    lines.append(f"阶段: {stage}    迭代: {iteration}")

    if plan:
        lines.append(f"构图: {plan.get('composition', '')}")
        for r in plan.get("regions", []):
            lines.append(f"  {r['id']}: {r.get('name', '')} bbox={r.get('bbox', '')}")

    lines.append("")
    lines.append(f"进度: covered={metrics.get('covered_pct', '?'):.1%}  "
                 f"MAE={metrics.get('mae', '?')}")

    if region_rows:
        lines.append("区域状态:")
        for r in region_rows:
            lines.append(f"  {r['id']}: {r.get('name','')[:6]:<6} "
                         f"覆盖={r.get('covered_pct',0):.0%}  [{r.get('status','-')}]")

    if color_ledger:
        lines.append("")
        lines.append("颜色账本:")
        lines.extend(f"  {c}" for c in color_ledger)

    if recent_actions:
        lines.append("")
        lines.append("最近动作:")
        lines.extend(f"  {a}" for a in recent_actions[-5:])

    if stall_note:
        lines.append("")
        lines.append(stall_note)

    if feedback:
        lines.append("")
        lines.append("⚠ 上轮反馈:")
        lines.extend(f"  - {f}" for f in feedback)

    lines.append("")
    lines.append("可用工具: " + ", ".join(AVAILABLE_TOOLS[:8]) + " ...")
    lines.append("输入 JSON 动作 / done / plan / next_stage A B / q")
    return "\n".join(lines)


# ------------------------------------------------------------------ 主循环


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="manual-paint",
        description="Krita 人工绘制模式（独立程序）",
    )
    parser.add_argument("--target", required=True, help="目标图像路径")
    parser.add_argument("--host", default=DEFAULT_ENDPOINT,
                        help=f"Krita RPC 地址（默认 {DEFAULT_ENDPOINT}）")
    parser.add_argument("--out-dir", default="outputs", help="结果输出目录")
    args = parser.parse_args()

    endpoint = args.host
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    session_id = time.strftime("%Y%m%d_%H%M%S")

    # ---- 0) 检查/创建画布 ----
    print("[manual] 检查画布状态...")
    try:
        doc_info = rpc_call(endpoint, "get_document_info", {})
    except SystemExit as e:
        msg = str(e)
        if "NO_ACTIVE_DOCUMENT" in msg or "无法连接" in msg:
            print("[manual] 无活动文档，加载目标图尺寸并创建画布...")
            target_img = Image.open(args.target).convert("RGB")
            rpc_call(endpoint, "create_document", {
                "width": target_img.width,
                "height": target_img.height,
                "name": f"manual_{session_id}",
                "color_model": "RGBA",
                "color_depth": "U8",
                "resolution": 300,
            })
            doc_info = rpc_call(endpoint, "get_document_info", {})
        else:
            raise

    canvas_w = int(doc_info["width"])
    canvas_h = int(doc_info["height"])
    work_w, work_h = min(canvas_w, SNAPSHOT_MAX_SIDE), min(canvas_h, SNAPSHOT_MAX_SIDE)
    if max(canvas_w, canvas_h) > SNAPSHOT_MAX_SIDE:
        scale = SNAPSHOT_MAX_SIDE / max(canvas_w, canvas_h)
        work_w, work_h = int(canvas_w * scale), int(canvas_h * scale)

    print(f"[manual] 画布 {canvas_w}x{canvas_h} (工作尺寸 {work_w}x{work_h})")
    print(f"[manual] 目标图: {args.target}")
    print()

    # ---- 1) 加载目标图 ----
    target_img = resize_if_needed(Image.open(args.target).convert("RGB"), max(work_w, work_h))
    target_arr = array_to_pixels(target_img)
    target_b64 = pil_to_b64(target_img, "JPEG")
    print(f"[manual] 目标图尺寸: {target_img.size}")
    print()

    # ---- 2) 状态变量 ----
    stage = "A"
    iteration = 0
    plan: dict | None = None
    color_ledger: list[str] = []   # ["c1 #F5D5C0 ×3", ...]
    recent_actions: list[str] = []
    feedback: list[str] = []
    stall_count = 0
    last_covered = 0.0

    # ---- 3) L0: 获取 plan ----
    print("━━━ 请描述构图 plan ━━━")
    print("输入 plan JSON（或按回车自动生成框架）:")
    plan_input = input("> ").strip()
    if plan_input:
        try:
            plan = parse_action_input(plan_input)
            if plan.get("action") != "plan":
                plan = {"action": "plan", **plan}
        except ValueError:
            print("  ⚠ 解析失败，使用空 plan")
            plan = {"action": "plan", "composition": "", "regions": [], "stage_plan": "A→B→C→D"}
    else:
        plan = {"action": "plan", "composition": "人工规划",
                "regions": [], "stage_plan": "A→B→C→D"}
    print(f"[manual] plan: {plan.get('composition', '')}")
    print()

    # ---- 4) 主循环 ----
    MAX_ITER = 200
    history_path = os.path.join(out_dir, f"history_{session_id}.jsonl")

    while iteration < MAX_ITER:
        iteration += 1

        # 获取快照
        try:
            canvas_img, snap_data = get_snapshot(endpoint, work_w, work_h)
        except SystemExit as e:
            print(f"[manual] 快照失败: {e}，5s 后重试")
            time.sleep(5)
            iteration -= 1
            continue

        canvas_arr = array_to_pixels(canvas_img)
        metrics = compute_diff_metrics(target_arr, canvas_arr)
        cov = metrics["covered_pct"]

        # 停滞检测
        if cov - last_covered < 0.002:
            stall_count += 1
        else:
            stall_count = 0
        last_covered = cov

        stall_note = None
        if stall_count >= 3:
            stall_note = (f"⚠ 连续 {stall_count} 轮无明显改善，建议切换区域或检查颜色")

        # 渲染快照到终端（ASCII 缩略图，可选）
        print(f"\n{'━' * 60}")
        print(f" iter {iteration:3d} | stage={stage} | covered={cov:.1%} | MAE={metrics['mae']:.1f} "
              f"| stall={stall_count}")
        print(f"{'━' * 60}")

        # 显示上下文
        context = build_context_text(stage, iteration, plan, metrics,
                                     [], color_ledger, recent_actions,
                                     feedback, stall_note)
        print(context)
        print()

        # 显示当前画布缩略图（ANSI 彩色点阵，仅前 40 行）
        try:
            _print_canvas_thumb(canvas_img, cols=60, rows=30)
        except Exception:
            pass
        print()

        # 等待输入
        feedback = []
        try:
            user_input = input("[manual] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[manual] 用户中断")
            break

        try:
            action = parse_action_input(user_input)
        except ValueError as e:
            feedback = [str(e)]
            print(f"  ⚠ {e}")
            continue

        # ---- 处理动作 ----
        kind = action.get("action")

        if kind == "done":
            print(f"\n[manual] 人工终止 (iter={iteration})")
            break

        if kind == "plan":
            regions = action.get("regions", [])
            if 3 <= len(regions) <= 8:
                plan = action
                print(f"  ✓ plan 已更新 ({len(regions)} 个区域)")
            else:
                feedback = ["plan regions 数量必须为 3~8"]
                print(f"  ⚠ {feedback[-1]}")
            continue

        if kind == "next_stage":
            from_s = action.get("from", "")
            to_s = action.get("to", "")
            if to_s in ("A", "B", "C", "D") and to_s != stage:
                stage = to_s
                print(f"  ✓ 阶段切换: {from_s} → {to_s}")
            else:
                feedback = [f"无效阶段切换: {from_s} → {to_s}（应为 A/B/C/D 之一）"]
                print(f"  ⚠ {feedback[-1]}")
            continue

        # 工具调用
        tool = action.get("tool")
        params = action.get("params", {})
        if not tool:
            feedback = ["缺少 tool 字段"]
            print(f"  ⚠ {feedback[-1]}")
            continue

        if tool not in AVAILABLE_TOOLS:
            feedback = [f"未知工具: {tool}"]
            print(f"  ⚠ {feedback[-1]}")
            continue

        # 颜色前置处理
        color_token = action.get("color", "")
        if color_token:
            # 简单解析：cN → 从 ledger 查找，#RRGGBB 直接使用
            hex_color = None
            if color_token.startswith("#"):
                hex_color = color_token.upper()
            elif color_token.lower().startswith("c") and color_token[1:].isdigit():
                idx = int(color_token[1:])
                if 1 <= idx <= len(color_ledger):
                    parts = color_ledger[idx - 1].split()
                    if len(parts) >= 2:
                        hex_color = parts[1].upper()
            if hex_color:
                rpc_call(endpoint, "set_colors", {"foreground": hex_color})
                print(f"  颜色 → {hex_color}")
            else:
                feedback = [f"无法解析颜色引用: {color_token}"]
                print(f"  ⚠ {feedback[-1]}")
                continue

        # 执行工具
        t0 = time.time()
        try:
            result = rpc_call(endpoint, tool, params)
            elapsed = int((time.time() - t0) * 1000)
            print(f"  ✓ {tool} ({elapsed}ms)")

            # 记录颜色
            if hex_color:
                color_ledger.append(f"c{len(color_ledger)+1} {hex_color} ×1")
                # 简化：实际应合并计数

            # 记录动作历史
            digest = f"{tool} params={json.dumps(params, ensure_ascii=False)[:80]}"
            recent_actions.append(f"#{iteration} {digest}")
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"iter": iteration, "stage": stage,
                                    "tool": tool, "params": params,
                                    "elapsed_ms": elapsed,
                                    "covered_pct": cov},
                                   ensure_ascii=False) + "\n")

        except SystemExit as e:
            elapsed = int((time.time() - t0) * 1000)
            print(f"  ✗ {tool} 失败 ({elapsed}ms): {e}")
            feedback = [str(e)]
            continue

        # 绘画类操作后等待同步
        if tool.startswith("paint") or tool in ("write_pixels", "undo", "redo"):
            try:
                rpc_call(endpoint, "wait_for_done", {})
            except SystemExit:
                pass

    # ---- 5) 保存最终结果 ----
    try:
        final_img, _ = get_snapshot(endpoint, work_w, work_h)
        final_path = os.path.join(out_dir, f"final_{session_id}.png")
        final_img.save(final_path)
        print(f"\n[manual] 最终快照 → {final_path}")
    except Exception as e:
        print(f"\n[manual] 保存失败: {e}")

    summary = {
        "session_id": session_id,
        "stop_reason": "manual_done",
        "iterations": iteration,
        "final_covered_pct": cov if 'cov' in dir() else 0.0,
        "final_image_path": os.path.join(out_dir, f"final_{session_id}.png"),
        "target_path": args.target,
    }
    meta_path = os.path.join(out_dir, f"summary_{session_id}.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[manual] 摘要 → {meta_path}")


def _print_canvas_thumb(img: Image.Image, cols: int = 60, rows: int = 30) -> None:
    """将画布缩略图以 ANSI 彩色字符打印到终端。"""
    small = img.resize((cols, rows))
    pixels = list(small.getdata())
    for i in range(rows):
        line = ""
        for j in range(cols):
            r, g, b = pixels[i * cols + j]
            # 简单 ASCII + ANSI 颜色
            brightness = (r + g + b) / 3 / 255
            if brightness < 0.15:
                ch = " "
            elif brightness < 0.4:
                ch = "·"
            elif brightness < 0.7:
                ch = ":"
            else:
                ch = "-"
            # ANSI 颜色（仅支持基本 16 色，简化处理）
            line += f"\033[38;2;{r};{g};{b}m{ch}\033[0m"
        print(line)


if __name__ == "__main__":
    main()
