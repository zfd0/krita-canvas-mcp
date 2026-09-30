"""闭环绘画 Agent 主循环。

用法（独立 CLI）：
    python -m krita_canvas_mcp.agent --target <目标图路径> [--max-iterations 200]

流程契约见 docs/草稿.txt：
    L0 首轮 plan → L1 next_stage 显式切换(校验) → L2 每轮单工具调用(硬约束校验)
    → 三重终止(done/max_iter/converged) + 停滞检测。
"""
from __future__ import annotations

import json
import os
import time
import traceback

import numpy as np
from PIL import Image

from ..bridge import DEFAULT_ENDPOINT, KritaBridge
from ..errors import ErrCode, KritaError
from .context_builder import (ParseError, build_plan_prompt, build_user_text,
                              load_system_prompt, parse_action)
from .glm_client import DEFAULT_MODEL, GLMVisionClient, pil_to_b64
from .session_state import ActionRecord, CanvasMetrics, SessionState
from .stage_rules import STAGE_NAMES, validate, validate_next_stage

# 快照缩放上限（控制多模态体积）
SNAPSHOT_MAX_SIDE = 768
HEATMAP_MAX_SIDE = 384
# 热力图注入门槛：C/D 阶段给（A/B 结构阶段看轮廓不看颜色）
HEATMAP_STAGES = {"C", "D"}


class AgentLoop:
    """闭环绘画循环。依赖：Krita 插件 HTTP RPC 运行中 + GLM API 可用。"""

    def __init__(self, target_path: str, api_key: str | None = None,
                 model: str | None = None, max_iterations: int = 200,
                 out_dir: str = "outputs",
                 endpoint: str = DEFAULT_ENDPOINT,
                 plan_retries: int = 2):
        self.target_path = target_path
        self.max_iterations = max_iterations
        self.out_dir = out_dir
        self.bridge = KritaBridge(endpoint=endpoint)
        self.glm = GLMVisionClient(api_key=api_key,
                                   model=model or DEFAULT_MODEL)
        self.plan_retries = plan_retries
        self.system = load_system_prompt()
        self.state = SessionState(out_dir=out_dir)
        os.makedirs(out_dir, exist_ok=True)
        self.session_id = time.strftime("%Y%m%d_%H%M%S")

    # ------------------------------------------------------------ 初始化

    @staticmethod
    def _work_size(canvas_w: int, canvas_h: int) -> tuple:
        """视觉工作尺寸：画布超过快照上限时等比缩小，target/snapshot/diff 三者严格同尺寸。"""
        if max(canvas_w, canvas_h) <= SNAPSHOT_MAX_SIDE:
            return canvas_w, canvas_h
        scale = SNAPSHOT_MAX_SIDE / max(canvas_w, canvas_h)
        return int(canvas_w * scale), int(canvas_h * scale)

    def _load_target(self, work_w: int, work_h: int) -> np.ndarray:
        """加载目标图并缩放到视觉工作尺寸（坐标对齐前提）。"""
        img = Image.open(self.target_path).convert("RGB")
        if (img.width, img.height) != (work_w, work_h):
            print(f"[agent] 目标图 {img.size} 已对齐到工作尺寸 {work_w}x{work_h}"
                  f"（过程重建以画布尺寸为准）")
            img = img.resize((work_w, work_h))
        return np.asarray(img, dtype=np.uint8)

    def _snapshot_rgb(self, work_w: int, work_h: int) -> np.ndarray:
        """拉取画布快照并解码为 numpy RGB（强制对齐到工作尺寸）。"""
        data = self.bridge.call("get_canvas_snapshot", {"max_side": SNAPSHOT_MAX_SIDE})
        b64 = data["image_b64"]
        import base64
        img = Image.open(__import__("io").BytesIO(base64.b64decode(b64))).convert("RGB")
        if img.size != (work_w, work_h):
            img = img.resize((work_w, work_h))
        return np.asarray(img, dtype=np.uint8), data

    # ------------------------------------------------------------ LLM 交互

    def _ask(self, text: str, images: list[dict]) -> str:
        """调 GLM 取回原始文本。"""
        return self.glm.chat(self.system, text, images)

    # ------------------------------------------------------------ 执行

    def _digest(self, action: dict) -> dict:
        """动作参数 → 轻量摘要（历史展示用，不带完整点列）。"""
        params = action.get("params", {})
        d = {}
        if "points" in params:
            pts = params["points"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            d["points_count"] = len(pts)
            d["bbox"] = [int(min(xs)), int(min(ys)),
                         int(max(xs) - min(xs)), int(max(ys) - min(ys))]
        if "size" in params:
            d["size"] = params["size"]
        if "preset_name" in params:
            d["preset"] = params["preset_name"]
        return d

    def _execute(self, action: dict) -> dict:
        """执行一个工具动作。
        - color(cN/#hex) 先解析为前景色自动 set_colors
        - 工具走 bridge；绘画类执行后 wait_for_done 保投影一致
        :return: (执行结果 dict, 是否失败)
        """
        tool = action["tool"]
        params = dict(action.get("params", {}))
        executed = []

        # 1) 颜色前置处理：LLM 的 color 字段 → set_colors
        color_token = action.get("color")
        if color_token:
            hexv = self.state.ledger.resolve(color_token)
            if hexv is None:
                raise KritaError(ErrCode.INVALID_PARAM,
                                 f"颜色引用无法解析: {color_token}")
            self.bridge.call("set_colors", {"foreground": hexv})
            self.state.ledger.record(hexv)
            executed.append(("set_colors", hexv))

        # 2) 主工具
        t0 = time.time()
        result = self.bridge.call(tool, params)
        ms = int((time.time() - t0) * 1000)
        executed.append((tool, params))

        # 3) 绘画类后等待投影同步
        if tool.startswith("paint") or tool in ("write_pixels", "undo", "redo"):
            self.bridge.call("wait_for_done", {})

        ref = f"{color_token}/" if color_token else ""
        return {
            "tool": tool, "executed": executed, "result": json.dumps(result)[:400],
            "elapsed_ms": ms, "digest": self._digest(action),
            "params_ref": ref + json.dumps(params, ensure_ascii=False)[:200],
        }

    # ------------------------------------------------------------ 主循环

    def run(self) -> dict:
        """跑完整闭环，返回最终结果摘要。"""
        # 0) 连接与画布校验
        doc = self.bridge.call("get_document_info", {})
        canvas_w, canvas_h = int(doc["width"]), int(doc["height"])
        work_w, work_h = self._work_size(canvas_w, canvas_h)
        print(f"[agent] 画布 {canvas_w}x{canvas_h} (工作尺寸 {work_w}x{work_h}) | "
              f"目标 {self.target_path} | 模型 {self.glm.model}")

        # 1) 加载目标图并对齐
        target_arr = self._load_target(work_w, work_h)
        target_b64 = pil_to_b64(Image.fromarray(target_arr, "RGB"), "JPEG")

        # 2) L0：首轮 plan
        plan = None
        plan_err = ""
        for attempt in range(self.plan_retries + 1):
            try:
                raw = self._ask(build_plan_prompt(canvas_w, canvas_h),
                                [{"image_b64": target_b64, "mime": "image/jpeg"}])
                act = parse_action(raw)
                if act.get("action") != "plan":
                    raise ParseError(f"首轮应为 plan，得到 {act.get('action')}")
                regions = act.get("regions", [])
                if not (3 <= len(regions) <= 8):
                    raise ParseError(f"regions 数量应为 3~8，得到 {len(regions)}")
                plan = act
                print(f"[agent] plan 就绪: {plan.get('composition', '')[:40]}")
                break
            except ParseError as e:
                plan_err = f"plan 解析失败: {e}"
                print(f"[agent] {plan_err}（重试 {attempt + 1}/{self.plan_retries}）")
        if plan is None:
            raise RuntimeError(plan_err or "plan 生成失败")
        self.state.plan = plan

        # 3) 迭代循环
        feedback: list[str] = []
        sample_result = None
        stop_reason = "loop_exit"

        while True:
            self.state.iteration += 1
            it = self.state.iteration
            if it > self.max_iterations:
                stop_reason = "max_iter"
                break

            # ---- 观测：快照 + 差异 + 停滞 ----
            try:
                canvas_arr, snap_data = self._snapshot_rgb(work_w, work_h)
            except KritaError as e:
                print(f"[agent] 快照失败: {e}，5s 后重试")
                time.sleep(5)
                continue
            metrics = CanvasMetrics(target_arr, canvas_arr)
            scalars = metrics.scalars()
            cov = metrics.covered_pct()
            region_rows = metrics.by_regions((self.state.plan or {}).get("regions", []))
            stall = self.state.update_stall(cov)

            stall_note = None
            if stall >= 3:
                hot = next((r for r in region_rows if r["status"] == "hot"), None)
                zone = f"r{hot['id']} 区域" if hot else "当前区域"
                stall_note = (f"⚠ 连续 {stall} 轮在 {zone} 改动无明显改善。"
                              f"建议：切换区域 / 检查颜色 / 或输出 done。")

            images = [{"image_b64": target_b64, "mime": "image/jpeg"},
                      {"image_b64": snap_data["image_b64"], "mime": "image/png"}]
            if self.state.stage in HEATMAP_STAGES:
                hm_b64, _, _ = metrics.heatmap_b64(HEATMAP_MAX_SIDE)
                images.append({"image_b64": hm_b64, "mime": "image/png"})

            # ---- 装配上下文 → LLM ----
            text = build_user_text(
                self.state, scalars, cov, region_rows,
                feedback=feedback, stall_note=stall_note,
                sample_result=sample_result,
            )
            feedback, sample_result = [], None
            try:
                raw = self._ask(text, images)
                action = parse_action(raw)
            except ParseError as e:
                feedback = [str(e), "请严格输出单一 JSON 对象"]
                print(f"[agent] iter {it}: 输出解析失败 - {e}")
                continue

            # ---- 动作分发 ----
            kind = action.get("action")

            if kind == "done":
                stop_reason = "llm_done"
                self._log(it, self.state.stage, "done", {}, action.get("thought", ""), True, 0)
                break

            if kind == "plan":
                # 允许纠正 plan（重解析 regions）
                regions = action.get("regions", [])
                if 3 <= len(regions) <= 8:
                    self.state.plan = action
                    print(f"[agent] iter {it}: plan 已更新")
                else:
                    feedback = ["plan regions 数量必须为 3~8"]
                continue

            if kind == "next_stage":
                ok_, err_ = validate_next_stage(self.state,
                                                action.get("from", ""),
                                                action.get("to", ""))
                if not ok_:
                    feedback = [err_]
                    self._log(it, self.state.stage, "next_stage", {}, "", False, 0)
                    print(f"[agent] iter {it}: 阶段切换被拒 - {err_}")
                    continue
                self.state.stage = action["to"]
                self.state.stage_cov_anchor[action["from"]] = cov
                self.state.plan["stage_goals_cur"] = action.get("stage_goals", [])
                self.state.plan["open_issues"] = action.get("open_issues", [])
                print(f"[agent] iter {it}: {action['from']}→{action['to']} "
                      f"({STAGE_NAMES.get(action['to'], '')})")
                self._log(it, action["to"], "next_stage", action, action.get("thought", ""), True, 0)
                continue

            # 普通工具调用
            tool = action.get("tool")
            stage_decl = action.get("stage", self.state.stage)
            color_token = action.get("color")
            ok_, err_ = validate(stage_decl, tool, action.get("params", {}),
                                 self.state.ledger.resolve(color_token) if color_token else None)
            if not ok_:
                feedback = [err_]
                self._log(it, self.state.stage, tool, {}, action.get("thought", ""), False, 0)
                print(f"[agent] iter {it}: 硬约束拒绝 - {err_}")
                continue
            if stage_decl != self.state.stage:
                feedback = [f"声明的 stage={stage_decl} 与服务端当前 {self.state.stage} 不符，用后者"]

            try:
                res = self._execute(action)
            except KritaError as e:
                feedback = [f"工具执行失败: {e.message}"]
                self._log(it, self.state.stage, tool, self._digest(action),
                          action.get("thought", ""), False, 0)
                print(f"[agent] iter {it}: 执行失败 - {e.message}")
                continue

            self._log(it, self.state.stage, tool, res["digest"],
                      action.get("thought", ""), True, res["elapsed_ms"],
                      color=color_token or "")
            if tool == "sample_color":
                try:
                    sample_result = res["result"]
                except Exception:
                    pass
            print(f"[agent] iter {it}: {tool} ok ({res['elapsed_ms']}ms) "
                  f"cov={cov:.3f} de={scalars['delta_e_mean']}")

            # ---- 终止判定（非 done 路径）----
            stop_now, reason = self.state.conclude(False, scalars, self.max_iterations)
            if stop_now:
                stop_reason = reason
                break

        # 4) 收尾：保存结果
        final = self._finalize(stop_reason, target_b64, scalars)
        return final

    # ------------------------------------------------------------ 收尾

    def _log(self, it, stage, tool, digest, thought, ok_, ms, color=""):
        """记录动作到会话历史（同步写 jsonl）。"""
        self.state.record_action(ActionRecord(
            iter=it, stage=stage, tool=tool, params_digest=digest,
            thought=thought, ok=ok_, elapsed_ms=ms, color=color,
        ))

    def _finalize(self, reason: str, target_b64: str, scalars: dict) -> dict:
        """保存最终快照/状态，返回摘要。"""
        try:
            doc = self.bridge.call("get_document_info", {})
            work_w, work_h = self._work_size(int(doc["width"]), int(doc["height"]))
            canvas_arr, snap_data = self._snapshot_rgb(work_w, work_h)
            final_img = Image.fromarray(canvas_arr, "RGB")
            final_path = os.path.join(self.out_dir,
                                      f"final_{self.session_id}.png")
            final_img.save(final_path)
        except Exception:
            traceback.print_exc()
            snap_data, final_path = {}, None

        summary = {
            "session_id": self.session_id,
            "stop_reason": reason,
            "iterations": self.state.iteration,
            "metrics": scalars,
            "stage_breakdown": dict(self.state.stage_actions),
            "history_count": len(self.state.history),
            "final_image_path": final_path,
            "target_path": self.target_path,
        }
        meta_path = os.path.join(self.out_dir,
                                 f"summary_{self.session_id}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[agent] 完成: stop_reason={reason} "
              f"iterations={self.state.iteration} 摘要→{meta_path}")
        return summary