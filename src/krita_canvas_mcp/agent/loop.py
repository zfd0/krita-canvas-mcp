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
from ..prompts import load_system_prompt
from .context_builder import (ParseError, build_plan_prompt, build_user_text,
                              parse_action)
from .session_state import (PAINT_TOOLS, ActionRecord, CanvasMetrics,
                            SessionState)
from .stage_rules import (MIN_ACTIONS, STAGE_NAMES, STAGE_SEQ, validate,
                          validate_next_stage)
from .vlm_client import VLMClient, VLMError, pil_to_b64

# 快照缩放上限（控制多模态体积）
SNAPSHOT_MAX_SIDE = 768
HEATMAP_MAX_SIDE = 384
# 热力图注入门槛：C/D 阶段给（A/B 结构阶段看轮廓不看颜色）
HEATMAP_STAGES = {"C", "D"}


class AgentLoop:
    """闭环绘画循环。依赖：Krita 插件 HTTP RPC 运行中 + 任意 VLM API 可用。"""

    def __init__(self, target_path: str, api_key: str | None = None,
                 model: str | None = None, base_url: str | None = None,
                 max_iterations: int = 200,
                 out_dir: str = "outputs",
                 endpoint: str = DEFAULT_ENDPOINT,
                 plan_retries: int = 2,
                 max_retries: int | None = None,
                 raw_output: bool = False,
                 confirm: bool = False,
                 enable_thinking: bool = False):
        self.target_path = target_path
        self.max_iterations = max_iterations
        self.out_dir = out_dir
        self.bridge = KritaBridge(endpoint=endpoint)
        self.vlm = VLMClient(base_url=base_url, api_key=api_key,
                             model=model)
        self.plan_retries = plan_retries
        self.max_retries = max_retries  # VLM 调用失败重试次数；None=无限
        self.system = load_system_prompt()
        self.state = SessionState(out_dir=out_dir)
        os.makedirs(out_dir, exist_ok=True)
        self.session_id = time.strftime("%Y%m%d_%H%M%S")
        self.state.session_id = self.session_id  # 动作历史带会话号，便于多轮运行区分
        # 可选开关
        self.raw_output = raw_output    # True 时打印 AI 原始响应文本
        self.confirm = confirm          # True 时每步执行前等待用户确认
        self.enable_thinking = enable_thinking  # True 时启用思考模式（Agnes 模型）
        # 上一轮是否为真实绘画动作：停滞只在绘画轮累计（采样/计划轮不计入）
        self._prev_paint = False

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

    def _ask(self, text: str, images: list[dict],
             max_retries: int | None = None,
             enable_thinking: bool = False) -> str:
        """调 VLM 取回原始文本。

        max_retries: None=无限重试(缺省用 self.max_retries)；0=不重试；N=最多重试 N 次。
        API 错误按指数退避重试（3s、6s、…封顶 60s），重试耗尽后抛 VLMError。
        enable_thinking: 是否启用思考模式（Agnes 模型专用）
        """
        if max_retries is None:
            max_retries = self.max_retries
        attempt = 0
        last_err: VLMError | None = None
        while True:
            try:
                raw = self.vlm.chat(self.system, text, images, enable_thinking=enable_thinking)
                if self.raw_output:
                    print(f"[agent][raw] {raw}")
                return raw
            except VLMError as e:
                last_err = e
                if max_retries is not None and attempt >= max_retries:
                    break
                wait = min(3 * (2 ** attempt), 60)
                total = "无限" if max_retries is None else str(max_retries)
                print(f"[agent] VLM 调用失败({e})，{wait}s 后重试 "
                      f"({attempt + 1}/{total})")
                time.sleep(wait)
                attempt += 1
        assert last_err is not None
        raise last_err

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

    def _to_canvas_xy(self, x, y) -> tuple:
        """工作坐标 → 画布真实坐标（画布被缩放时按比例放大）。"""
        sx = self.canvas_w / self.work_w
        sy = self.canvas_h / self.work_h
        return int(round(x * sx)), int(round(y * sy))

    def _scale_paint_params(self, tool: str, params: dict) -> dict:
        """把 LLM 输出的工作坐标还原为画布真实坐标，供 Krita 执行。"""
        p = dict(params)
        if tool == "paint_line":
            if "x1" in p and "y1" in p:
                p["x1"], p["y1"] = self._to_canvas_xy(p["x1"], p["y1"])
            if "x2" in p and "y2" in p:
                p["x2"], p["y2"] = self._to_canvas_xy(p["x2"], p["y2"])
        elif tool == "paint_path":
            if "points" in p:
                p["points"] = [[*self._to_canvas_xy(pt[0], pt[1])] + list(pt[2:])
                               for pt in p["points"]]
        elif tool == "paint_shape":
            if isinstance(p.get("rect"), dict):
                r = dict(p["rect"])
                r["x"], r["y"] = self._to_canvas_xy(r.get("x", 0), r.get("y", 0))
                p["rect"] = r
            if "points" in p:
                p["points"] = [[*self._to_canvas_xy(pt[0], pt[1])] + list(pt[2:])
                               for pt in p["points"]]
        return p

    def _sample_target(self, x: int, y: int, radius: int = 0,
                       reduce: str = "median") -> dict:
        """从目标图(target_arr, 工作坐标)直接采样颜色，无需经过 Krita。"""
        arr = self.target_arr
        h, w = arr.shape[:2]
        x0, x1 = max(0, x - radius), min(w, x + radius + 1)
        y0, y1 = max(0, y - radius), min(h, y + radius + 1)
        if x0 >= x1 or y0 >= y1:
            return {"x": x, "y": y, "source": "target", "error": "采样点越界"}
        sub = arr[y0:y1, x0:x1].reshape(-1, 3)
        rep = np.median(sub, axis=0) if reduce == "median" else sub.mean(axis=0)
        r, g, b = [int(round(float(v))) for v in rep]
        return {"x": x, "y": y, "radius": radius, "source": "target",
                "color": {"srgb_hex": "#%02X%02X%02X" % (r, g, b),
                          "srgb_255": [r, g, b]}}

    def _execute(self, action: dict) -> dict:
        """执行一个工具动作。
        - color(cN/#hex) 先解析为前景色自动 set_colors
        - paint_* 携带 size 时先落地为笔刷尺寸
        - 坐标按 work→canvas 缩放；sample_color(target) 直接在目标图上采样
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

        # 2) 笔刷尺寸落地：paint_* 直接携带 size 时先设置笔刷
        if tool in ("paint_path", "paint_line", "paint_shape") and "size" in params:
            self.bridge.call("set_brush_params", {"size": float(params["size"])})
            executed.append(("set_brush_size", params["size"]))

        # 3) 主工具：target 采样走本地；绘画工具坐标还原到画布真实尺寸
        t0 = time.time()
        if tool == "sample_color" and params.get("source", "target") == "target":
            result = self._sample_target(
                int(params.get("x", 0)), int(params.get("y", 0)),
                int(params.get("radius", 0)), params.get("reduce", "median"))
            # 采样色自动设为前景色：模型可直接绘制（省略 color 字段），
            # 避免“采样到蓝色却用旧色 c1(草稿灰) 下笔”这类串色问题
            hexv = (result.get("color") or {}).get("srgb_hex")
            if hexv:
                self.bridge.call("set_colors", {"foreground": hexv})
                self.state.ledger.record(hexv)
                executed.append(("set_colors(auto)", hexv))
                result["note"] = ("已自动设为当前前景色：可直接省略 color 字段绘制，"
                                  "或将该 #hex 填入 color")
        else:
            if tool in ("paint_line", "paint_path", "paint_shape"):
                params = self._scale_paint_params(tool, params)
            elif tool == "sample_color" and "x" in params and "y" in params:
                params["x"], params["y"] = self._to_canvas_xy(params["x"], params["y"])
            result = self.bridge.call(tool, params)
        ms = int((time.time() - t0) * 1000)
        executed.append((tool, params))

        ref = f"{color_token}/" if color_token else ""
        return {
            "tool": tool, "executed": executed, "result": json.dumps(result)[:400],
            "elapsed_ms": ms, "digest": self._digest(action),
            "params_ref": ref + json.dumps(params, ensure_ascii=False)[:200],
        }

    # ------------------------------------------------------------ 主循环

    def run(self) -> dict:
        """跑完整闭环，返回最终结果摘要。"""
        # 0) 连接与画布校验：无活动文档时自动以目标图尺寸新建
        try:
            doc = self.bridge.call("get_document_info", {})
        except KritaError as e:
            if str(e) == "NO_ACTIVE_DOCUMENT":
                print("[agent] 无活动文档，自动创建与目标图同尺寸画布")
                from PIL import Image as _PILImage
                target_img = _PILImage.open(self.target_path)
                self.bridge.call("create_document", {
                    "width": target_img.width,
                    "height": target_img.height,
                    "name": f"agent_{int(time.time())}",
                    "color_model": "RGBA",
                    "color_depth": "U8",
                    "resolution": 300,
                })
                doc = self.bridge.call("get_document_info", {})
            else:
                raise
        canvas_w, canvas_h = int(doc["width"]), int(doc["height"])
        work_w, work_h = self._work_size(canvas_w, canvas_h)
        # 记录真实/工作尺寸，供后续坐标 work→canvas 换算与 target 采样使用
        self.canvas_w, self.canvas_h = canvas_w, canvas_h
        self.work_w, self.work_h = work_w, work_h
        print(f"[agent] 画布 {canvas_w}x{canvas_h} (工作尺寸 {work_w}x{work_h}) | "
              f"目标 {self.target_path} | 模型 {self.vlm.model}")

        # 1) 加载目标图并对齐
        target_arr = self._load_target(work_w, work_h)
        self.target_arr = target_arr
        target_b64 = pil_to_b64(Image.fromarray(target_arr, "RGB"), "JPEG")

        # 2) L0：首轮 plan
        plan = None
        plan_err = ""
        for attempt in range(self.plan_retries + 1):
            try:
                raw = self._ask(build_plan_prompt(work_w, work_h),
                                [{"image_b64": target_b64, "mime": "image/jpeg"}],
                                enable_thinking=self.enable_thinking)
                act = parse_action(raw)
                if act.get("action") != "plan":
                    raise ParseError(f"首轮应为 plan，得到 {act.get('action')}")
                regions = act.get("regions", [])
                if not (3 <= len(regions) <= 8):
                    raise ParseError(f"regions 数量应为 3~8，得到 {len(regions)}")
                plan = act
                print(f"[agent] plan 就绪: {plan.get('composition', '')[:40]}")
                break
            except (ParseError, VLMError) as e:
                plan_err = f"plan 失败: {e}"
                print(f"[agent] {plan_err}（重试 {attempt + 1}/{self.plan_retries}）")
        if plan is None:
            raise RuntimeError(plan_err or "plan 生成失败")
        self.state.plan = plan

        # 3) 迭代循环
        feedback: list[str] = []
        sample_result = None
        stop_reason = "loop_exit"
        scalars: dict = {}
        vlm_fail_streak = 0

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
            painted = metrics.painted_pct()
            cov = metrics.covered_pct()
            # A/B 结构阶段以“已绘占比”为主指标，C/D 颜色阶段以“匹配度”为主指标；
            # 两者同时展示给模型，主指标用于停滞判决与区域状态
            primary = "painted" if self.state.stage in ("A", "B") else "matched"
            progress = painted if primary == "painted" else cov
            region_rows = metrics.by_regions(
                (self.state.plan or {}).get("regions", []), metric=primary)
            # 停滞只在“上一轮是真实绘画动作”时累计：采样/计划/被拒等不改画布
            # 的轮次不计入，避免 C 阶段连续采样期间被误判停滞
            stall = (self.state.update_stall(progress, self.state.stage)
                     if self._prev_paint else self.state.stall_rounds)
            self._prev_paint = False  # 本轮成功绘画会在下方执行分支重新置位

            stall_note = None
            if stall >= 3:
                hot = next((r for r in region_rows if r["status"] == "hot"), None)
                zone = f"r{hot['id']} 区域" if hot else "当前区域"
                stall_note = (f"⚠ 连续 {stall} 轮在 {zone} 无明显推进。"
                              f"建议：切换区域 / 检查颜色 / 或完成本阶段后 next_stage。")

            # 阶段动作数达标提示：模型容易在原地打磨，达标后主动提示可推进
            stage_note = None
            done_cnt = self.state.stage_actions.get(self.state.stage, 0)
            need = MIN_ACTIONS.get(self.state.stage, 0)
            si = STAGE_SEQ.index(self.state.stage)
            if done_cnt >= need and si + 1 < len(STAGE_SEQ):
                stage_note = (f"ℹ 本阶段绘画动作已达标（{done_cnt}/{need}）。"
                              f"若该阶段结构基本到位，可输出 next_stage 进入 "
                              f"{STAGE_SEQ[si + 1]} 阶段。")

            images = [{"image_b64": target_b64, "mime": "image/jpeg"},
                      {"image_b64": snap_data["image_b64"], "mime": "image/png"}]
            if self.state.stage in HEATMAP_STAGES:
                hm_b64, _, _ = metrics.heatmap_b64(HEATMAP_MAX_SIDE)
                images.append({"image_b64": hm_b64, "mime": "image/png"})

            # ---- 装配上下文 → LLM ----
            text = build_user_text(
                self.state, scalars, cov, region_rows,
                feedback=feedback, stall_note=stall_note,
                sample_result=sample_result, painted=painted,
                primary=primary, stage_note=stage_note,
            )
            feedback, sample_result = [], None
            try:
                raw = self._ask(text, images, enable_thinking=self.enable_thinking)
                action = parse_action(raw)
            except ParseError as e:
                feedback = [str(e), "请严格输出单一 JSON 对象"]
                self._log(it, self.state.stage, "parse_error",
                          {"err": str(e)[:120]}, "", False, 0)
                print(f"[agent] iter {it}: 输出解析失败 - {e}")
                continue
            except VLMError as e:
                # _ask 内部已退避重试；到这表示连续多轮 API 不可用
                vlm_fail_streak += 1
                self._log(it, self.state.stage, "vlm_error",
                          {"err": str(e)[:120]}, "", False, 0)
                print(f"[agent] iter {it}: VLM 连续失败 {vlm_fail_streak} 次 - {e}")
                if vlm_fail_streak >= 3:
                    stop_reason = "vlm_failed"
                    break
                feedback = [f"VLM 调用失败: {e}，本轮跳过"]
                time.sleep(3)
                continue
            vlm_fail_streak = 0  # 成功解析，重置连续失败计数

            # ---- 动作分发 ----
            kind = action.get("action")

            # 用户确认开关：对普通工具调用前暂停等待
            if self.confirm and kind not in ("plan", "next_stage", "done"):
                tool = action.get("tool", "?")
                thought = action.get("thought", "")
                print(f"\n[agent] iter {it} [{self.state.stage}] 下一步: {tool}")
                if thought:
                    print(f"  理由: {thought}")
                print(f"  参数: {json.dumps(action.get('params', {}), ensure_ascii=False)}")
                try:
                    ans = input("[agent] 继续？(回车继续 / q 退出) ").strip().lower()
                except EOFError:
                    ans = ""
                if ans == "q":
                    stop_reason = "user_quit"
                    break

            if kind == "done":
                stop_reason = "llm_done"
                self._log(it, self.state.stage, "done", {}, action.get("thought", ""), True, 0)
                break

            if kind == "plan":
                # 只有首轮允许 plan，后续拒绝
                if it <= 1:
                    regions = action.get("regions", [])
                    if 3 <= len(regions) <= 8:
                        self.state.plan = action
                        print(f"[agent] iter {it}: plan 已更新")
                    else:
                        feedback = ["plan regions 数量必须为 3~8"]
                    continue
                else:
                    feedback = ["⚠ 首轮已输出 plan，后续轮次禁止再次输出 plan！必须输出绘画工具动作。"]
                    self._log(it, self.state.stage, "plan_reject", {}, "", False, 0)
                    print(f"[agent] iter {it}: 拒绝 plan（已过了首轮）")
                    continue

            if kind == "next_stage":
                ok_, err_ = validate_next_stage(self.state,
                                                action.get("from", ""),
                                                action.get("to", ""))
                if not ok_:
                    feedback = [err_]
                    self._log(it, self.state.stage, "next_stage",
                              {"from": action.get("from", ""), "to": action.get("to", ""),
                               "err": err_}, "", False, 0)
                    print(f"[agent] iter {it}: 阶段切换被拒 - {err_}")
                    continue
                self.state.stage = action["to"]
                # 阶段切换后以“新阶段主指标”重置停滞基准，避免跨阶段连带误判
                to_primary = ("painted" if action["to"] in ("A", "B")
                              else "matched")
                self.state.reset_stall(painted if to_primary == "painted" else cov)
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
                                 self.state.ledger.resolve(color_token) if color_token else None,
                                 color_token=color_token)
            if not ok_:
                feedback = [err_]
                self._log(it, self.state.stage, tool, {"err": err_},
                          action.get("thought", ""), False, 0)
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
            # 记录本轮为真实绘画动作（供下一轮停滞判决使用）
            self._prev_paint = tool in PAINT_TOOLS
            if tool == "sample_color":
                try:
                    sample_result = res["result"]
                except Exception:
                    pass
            print(f"[agent] iter {it}: {tool} ok ({res['elapsed_ms']}ms) "
                  f"painted={painted:.3f} cov={cov:.3f} de={scalars['delta_e_mean']}")

            # ---- 终止判定（非 done 路径）----
            stop_now, reason = self.state.conclude(False, scalars,
                                                   self.max_iterations, self.state.stage)
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