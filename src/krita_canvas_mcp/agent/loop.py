"""闭环绘画 Agent 主循环。

用法（独立 CLI）：
    python -m krita_canvas_mcp.agent --target <目标图路径> [--max-iterations 1000]

流程契约见 docs/草稿.txt：
    O 计划阶段(plan) → next_stage 显式切换(校验) → 每轮单工具调用(硬约束校验)
    → 终止(done/max_iter)。画布不做缩放，工作坐标即画布真实坐标。
"""
from __future__ import annotations

import base64
import hashlib
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
from .session_state import (ActionRecord, CanvasMetrics, SessionState,
                            norm_color_token)
from .stage_rules import (MIN_ACTIONS, STAGE_NAMES, STAGE_SEQ, validate,
                          validate_next_stage)
from .vlm_client import VLMClient, VLMError, pil_to_b64

# 热力图注入门槛：C/D 阶段给（A/B 结构阶段看轮廓不看颜色）
HEATMAP_STAGES = {"C", "D"}


def _say(msg: str) -> None:
    """带时间戳的控制台输出（精确到秒，立即刷新）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class AgentLoop:
    """闭环绘画循环。依赖：Krita 插件 HTTP RPC 运行中 + 任意 VLM API 可用。"""

    def __init__(self, target_path: str, api_key: str | None = None,
                 model: str | None = None, base_url: str | None = None,
                 max_iterations: int = 1000,
                 out_dir: str = "outputs",
                 endpoint: str = DEFAULT_ENDPOINT,
                 max_retries: int | None = None,
                 raw_output: bool = False,
                 raw_input: bool = False,
                 confirm: bool = False,
                 enable_thinking: bool = False):
        self.target_path = target_path
        self.max_iterations = max_iterations
        self.out_dir = out_dir
        self.bridge = KritaBridge(endpoint=endpoint)
        self.vlm = VLMClient(base_url=base_url, api_key=api_key,
                             model=model)
        self.max_retries = max_retries  # VLM 调用失败重试次数；None=无限
        self.system = load_system_prompt()
        # 产物目录：每次运行放入以时间戳命名的独立子目录 outputs/<时间戳>/
        self.session_id = time.strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(out_dir, self.session_id)
        os.makedirs(self.run_dir, exist_ok=True)
        self.state = SessionState(out_dir=self.run_dir)
        self.state.session_id = self.session_id  # 动作历史带会话号，便于多轮运行区分
        # 可选开关
        self.raw_output = raw_output    # True 时打印完整模型 API 返回（JSON）
        self.raw_input = raw_input      # True 时保存并打印发往模型的完整请求
        self.confirm = confirm          # True 时每步执行前等待用户确认
        self.enable_thinking = enable_thinking  # True 时启用思考模式（Agnes 模型）
        # 提示缓存命中遥测（来自 DeepSeek usage；服务不返回时恒为 0）
        self._cache_hit = 0
        self._cache_miss = 0

    # ------------------------------------------------------------ 初始化

    def _load_target(self, work_w: int, work_h: int) -> np.ndarray:
        """加载目标图；尺寸与画布不一致时对齐到画布尺寸（坐标对齐前提）。"""
        img = Image.open(self.target_path).convert("RGB")
        if (img.width, img.height) != (work_w, work_h):
            _say(f"[agent] 目标图 {img.size} 已对齐到画布尺寸 {work_w}x{work_h}"
                  f"（过程重建以画布尺寸为准）")
            img = img.resize((work_w, work_h))
        return np.asarray(img, dtype=np.uint8)

    def _snapshot_rgb(self, work_w: int, work_h: int) -> np.ndarray:
        """拉取画布快照并解码为 numpy RGB（原始尺寸，不做缩放）。

        max_side 传画布最大边，插件端不再缩小；解码后尺寸异常时对齐到画布尺寸。
        """
        data = self.bridge.call("get_canvas_snapshot",
                                {"max_side": max(work_w, work_h)})
        b64 = data["image_b64"]
        img = Image.open(__import__("io").BytesIO(base64.b64decode(b64))).convert("RGB")
        if img.size != (work_w, work_h):
            img = img.resize((work_w, work_h))
        return np.asarray(img, dtype=np.uint8), data

    # ------------------------------------------------------------ LLM 交互

    def _ask(self, text: str, images: list[dict], prefix: str = "",
             max_retries: int | None = None,
             enable_thinking: bool = False) -> str:
        """调 VLM 取回原始文本。

        prefix: 稳定文本前缀，置于图像之前以命中前缀缓存。
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
                raw = self.vlm.chat(self.system, text, images, prefix=prefix,
                                    enable_thinking=enable_thinking)
                return raw
            except VLMError as e:
                last_err = e
                if max_retries is not None and attempt >= max_retries:
                    break
                wait = min(3 * (2 ** attempt), 60)
                total = "无限" if max_retries is None else str(max_retries)
                _say(f"[agent] VLM 调用失败({e})，{wait}s 后重试 "
                      f"({attempt + 1}/{total})")
                time.sleep(wait)
                attempt += 1
        assert last_err is not None
        raise last_err

    def _track_cache(self, it: int) -> None:
        """读取最近一次 VLM 响应 usage，累计并打印提示缓存命中情况。

        DeepSeek 在 usage 中返回 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
        其他服务（如 GLM）不返回时按 0 计且不打印，保证兼容。
        """
        usage = self.vlm.last_usage or {}
        try:
            hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        except (TypeError, ValueError):
            return
        if hit or miss:
            self._cache_hit += hit
            self._cache_miss += miss
            _say(f"[agent] iter {it}: 缓存命中 {hit} / 未命中 {miss}")

    @staticmethod
    def _decode_data_url(url: str) -> tuple[str, bytes] | None:
        """解析 data:<mime>;base64,<payload>；非 data URL 返回 None。"""
        if not url.startswith("data:") or ";base64," not in url:
            return None
        head, _, payload = url.partition(";base64,")
        try:
            return head[len("data:"):], base64.b64decode(payload)
        except (ValueError, TypeError):
            return None

    def _dump_request(self, it: int) -> None:
        """保存本轮发往模型的完整请求 JSON（debug 用）。

        图片从 data URL 解码落盘到本次运行目录，JSON 中的图片地址替换为该文件的
        相对路径（相对本 JSON 所在目录），便于直接查看请求结构。
        图片按内容哈希命名并去重：逐轮相同的目标图只保存一份。
        """
        req = self.vlm.last_request
        if not req:
            return
        out = json.loads(json.dumps(req))  # 深拷贝，避免改动影响后续请求
        for msg in out.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if part.get("type") != "image_url":
                    continue
                url = (part.get("image_url") or {}).get("url", "")
                decoded = self._decode_data_url(url)
                if decoded is None:
                    continue
                mime, raw = decoded
                ext = "jpg" if "jp" in mime else "png"
                # 按内容哈希命名：同一张图（如每轮相同的目标图）整个运行只落盘一次，
                # 后续轮次复用同一文件，避免重复占用磁盘
                name = f"img_{hashlib.md5(raw).hexdigest()[:12]}.{ext}"
                path = os.path.join(self.run_dir, name)
                if not os.path.exists(path):
                    with open(path, "wb") as f:
                        f.write(raw)
                part["image_url"]["url"] = name  # 相对路径（相对本 JSON 所在目录）
        path = os.path.join(self.run_dir, f"request_{it:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _say(f"[agent][raw-input] 本轮请求已保存 → {path}")

    # ------------------------------------------------------------ 执行

    def _digest(self, action: dict) -> dict:
        """动作参数 → 轻量摘要（历史展示用，不带完整点列）。

        对模型输出的畸形参数做容错：类型不符时返回能提取到的部分，绝不抛异常。
        """
        params = action.get("params", {})
        if not isinstance(params, dict):
            return {}
        d = {}
        try:
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
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            pass
        return d

    def _sample_target(self, x: int, y: int, radius: int = 0,
                       reduce: str = "median") -> dict:
        """从目标图(target_arr, 画布坐标)直接采样颜色，无需经过 Krita。"""
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
        - 坐标即画布真实坐标（不做缩放）；sample_color(target) 直接在目标图上采样
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
            try:
                size_f = float(params["size"])
            except (TypeError, ValueError):
                raise KritaError(ErrCode.INVALID_PARAM,
                                 f"size 必须为数字，得到 {params['size']!r}")
            self.bridge.call("set_brush_params", {"size": size_f})
            executed.append(("set_brush_size", params["size"]))

        # 3) 主工具：target 采样走本地；绘画工具坐标还原到画布真实尺寸
        t0 = time.time()
        if tool == "sample_color" and params.get("source", "target") == "target":
            try:
                sx, sy = int(params.get("x", 0)), int(params.get("y", 0))
                srad = int(params.get("radius", 0))
            except (TypeError, ValueError):
                raise KritaError(ErrCode.INVALID_PARAM,
                                 "sample_color 的 x/y/radius 必须为整数")
            result = self._sample_target(sx, sy, srad, params.get("reduce", "median"))
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
            # 无活动文档的判定需同时兼容两种表达：插件直接 raise
            # RuntimeError("NO_ACTIVE_DOCUMENT")（message 为空时 dispatcher 会把它
            # 降级为 IO_ERROR + message="NO_ACTIVE_DOCUMENT"），或带说明的
            # "NO_ACTIVE_DOCUMENT: xxx"（err_code=NO_ACTIVE_DOCUMENT、message=中文）。
            # 只比对 str(e) 会在带说明时漏判 → 直接 raise 使闭环启动即中断。
            no_doc = (getattr(e, "code", None) == ErrCode.NO_ACTIVE_DOCUMENT
                      or "NO_ACTIVE_DOCUMENT" in str(e))
            if no_doc:
                _say("[agent] 无活动文档，自动创建与目标图同尺寸画布")
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
        work_w, work_h = canvas_w, canvas_h  # 取消缩放：工作尺寸即画布真实尺寸
        self.canvas_w, self.canvas_h = canvas_w, canvas_h
        self.work_w, self.work_h = work_w, work_h
        _say(f"[agent] 画布 {canvas_w}x{canvas_h}（不缩放） | "
              f"目标 {self.target_path} | 模型 {self.vlm.model}")

        # 1) 加载目标图并对齐
        target_arr = self._load_target(work_w, work_h)
        self.target_arr = target_arr
        target_b64 = pil_to_b64(Image.fromarray(target_arr, "RGB"), "JPEG")

        # 2) 迭代循环：起始为 O 计划阶段，模型在循环内输出 plan 后切 A
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

            # ---- 观测：快照 + 差异 ----
            try:
                canvas_arr, snap_data = self._snapshot_rgb(work_w, work_h)
            except KritaError as e:
                _say(f"[agent] 快照失败: {e}，5s 后重试")
                time.sleep(5)
                continue
            except Exception as e:
                # 非 RPC 错误（base64 解码失败、返回结构缺字段等）同样降级重试，
                # 否则单帧异常会直接冒泡中断整个闭环
                _say(f"[agent] 快照解析失败({type(e).__name__}): {e}，5s 后重试")
                time.sleep(5)
                continue
            metrics = CanvasMetrics(target_arr, canvas_arr)
            scalars = metrics.scalars()
            painted = metrics.painted_pct()
            cov = metrics.covered_pct()
            # A/B 结构阶段以“已绘占比”为主指标，C/D 颜色阶段以“匹配度”为主指标
            primary = "painted" if self.state.stage in ("A", "B") else "matched"
            region_rows = metrics.by_regions(
                (self.state.plan or {}).get("regions", []), metric=primary)

            # 阶段提示：O 阶段提示切阶段；绘画阶段动作数达标后提示可推进
            stage_note = None
            if self.state.stage == "O":
                if self.state.plan:
                    stage_note = ('ℹ O 计划阶段：plan 已就绪。请输出 next_stage '
                                  '{"action":"next_stage","from":"O","to":"A",'
                                  '"stage_goals":[...],"open_issues":[...]} '
                                  '进入 A 草图阶段。')
            else:
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
                hm_b64, _, _ = metrics.heatmap_b64()
                images.append({"image_b64": hm_b64, "mime": "image/png"})

            # ---- 装配上下文 → LLM ----
            # 拆为「稳定前缀（图像之前，可命中前缀缓存）」+「变化后缀（图像之后）」
            if self.state.stage == "O" and not self.state.plan:
                # O 计划阶段：plan 专用提示（画布尺寸恒定 → 全段稳定，整体前置），
                # 仅反馈随轮变化，放到图像之后
                prefix = build_plan_prompt(work_w, work_h)
                text = ("\n⚠ 上轮输出的修正要求：\n"
                        + "\n".join(f"  - {f}" for f in feedback)) if feedback else ""
            else:
                prefix, text = build_user_text(
                    self.state, scalars, cov, region_rows,
                    feedback=feedback, sample_result=sample_result,
                    painted=painted, primary=primary, stage_note=stage_note,
                )
            feedback, sample_result = [], None
            # 请求前先输出一行：网络阻塞时（DNS/连接挂起）能立刻定位卡点，
            # 否则控制台会长时间无任何输出，看起来像“死掉”
            _say(f"[agent] iter {it} [{self.state.stage}]: 请求模型…"
                  f"（{len(images)} 张图，超时 {self.vlm._client.timeout.read:g}s）")
            try:
                raw = self._ask(text, images, prefix=prefix,
                                enable_thinking=self.enable_thinking)
                self._track_cache(it)
                if self.raw_input:
                    try:
                        self._dump_request(it)
                    except Exception as e:
                        # 调试落盘失败（磁盘/权限等）不应中断闭环
                        _say(f"[agent][raw-input] 请求落盘失败: {e}")
                if self.raw_output:
                    _say("[agent][raw] 模型 API 完整返回: "
                         + json.dumps(self.vlm.last_response, ensure_ascii=False))
                action = parse_action(raw)
            except ParseError as e:
                feedback = [str(e), "请严格输出单一 JSON 对象"]
                self._log(it, self.state.stage, "parse_error",
                          {"err": str(e)[:120]}, "", False, 0)
                _say(f"[agent] iter {it}: 输出解析失败 - {e}")
                continue
            except VLMError as e:
                # _ask 内部已退避重试；到这表示连续多轮 API 不可用
                vlm_fail_streak += 1
                self._log(it, self.state.stage, "vlm_error",
                          {"err": str(e)[:120]}, "", False, 0)
                _say(f"[agent] iter {it}: VLM 连续失败 {vlm_fail_streak} 次 - {e}")
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
                _say(f"\n[agent] iter {it} [{self.state.stage}] 下一步: {tool}")
                if thought:
                    _say(f"  理由: {thought}")
                _say(f"  参数: {json.dumps(action.get('params', {}), ensure_ascii=False)}")
                try:
                    ans = input("[agent] 继续？(回车继续 / q 退出) ").strip().lower()
                except EOFError:
                    ans = ""
                if ans == "q":
                    stop_reason = "user_quit"
                    break

            if kind == "done":
                # O 计划阶段尚未开始绘画，拒绝提前终止
                if self.state.stage == "O":
                    feedback = ["⚠ 当前是 O 计划阶段，尚未开始绘画，不能输出 done；"
                                "请先输出 plan"]
                    self._log(it, "O", "done_reject", {},
                              action.get("thought", ""), False, 0)
                    _say(f"[agent] iter {it}: 拒绝 done（仍处于 O 阶段）")
                    continue
                stop_reason = "llm_done"
                self._log(it, self.state.stage, "done", {}, action.get("thought", ""), True, 0)
                break

            if kind == "plan":
                # plan 仅允许在 O 计划阶段输出
                if self.state.stage != "O":
                    feedback = [f"⚠ plan 只能在 O 计划阶段输出！当前 stage="
                                f"{self.state.stage}，必须输出绘画工具动作。"]
                    self._log(it, self.state.stage, "plan_reject", {}, "", False, 0)
                    _say(f"[agent] iter {it}: 拒绝 plan（当前 {self.state.stage} 阶段）")
                    continue
                regions = action.get("regions", [])
                if 3 <= len(regions) <= 8:
                    self.state.plan = action
                    self._log(it, "O", "plan", {"regions": len(regions)},
                              action.get("thought", ""), True, 0)
                    _say(f"[agent] iter {it}: plan 已就绪 - "
                          f"{action.get('composition', '')[:40]}")
                else:
                    feedback = [f"plan regions 数量必须为 3~8（当前 {len(regions)}）"]
                    self._log(it, "O", "plan_reject", {"regions": len(regions)},
                              action.get("thought", ""), False, 0)
                    _say(f"[agent] iter {it}: plan 被拒 - regions 数量 {len(regions)}")
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
                    _say(f"[agent] iter {it}: 阶段切换被拒 - {err_}")
                    continue
                self.state.stage = action["to"]
                self.state.plan["stage_goals_cur"] = action.get("stage_goals", [])
                self.state.plan["open_issues"] = action.get("open_issues", [])
                _say(f"[agent] iter {it}: {action['from']}→{action['to']} "
                      f"({STAGE_NAMES.get(action['to'], '')})")
                self._log(it, action["to"], "next_stage", action, action.get("thought", ""), True, 0)
                continue

            # 普通工具调用
            tool = action.get("tool")
            try:
                stage_decl = action.get("stage", self.state.stage)
                color_token = norm_color_token(action.get("color"))
                ok_, err_ = validate(
                    stage_decl, tool, action.get("params", {}),
                    self.state.ledger.resolve(color_token) if color_token else None,
                    color_token=color_token)
                if not ok_:
                    feedback = [err_]
                    self._log(it, self.state.stage, tool, {"err": err_},
                              action.get("thought", ""), False, 0)
                    _say(f"[agent] iter {it}: 硬约束拒绝 - {err_}")
                    continue
                if stage_decl != self.state.stage:
                    feedback = [f"声明的 stage={stage_decl} 与服务端当前 "
                                f"{self.state.stage} 不符，用后者"]
                res = self._execute(action)
            except KritaError as e:
                feedback = [f"工具执行失败: {e.message}"]
                self._log(it, self.state.stage, tool, self._digest(action),
                          action.get("thought", ""), False, 0)
                _say(f"[agent] iter {it}: 执行失败 - {e.message}")
                continue
            except Exception as e:
                # 模型输出类型异常等未预期错误 → 转成反馈重试，避免中断闭环
                feedback = [f"动作处理异常({type(e).__name__}): {e}；请检查参数类型"]
                self._log(it, self.state.stage, str(tool), {},
                          action.get("thought", ""), False, 0)
                _say(f"[agent] iter {it}: 动作处理异常 - {type(e).__name__}: {e}")
                continue

            self._log(it, self.state.stage, tool, res["digest"],
                      action.get("thought", ""), True, res["elapsed_ms"],
                      color=color_token or "")
            if tool == "sample_color":
                try:
                    sample_result = res["result"]
                except Exception:
                    pass
            _say(f"[agent] iter {it}: {tool} ok ({res['elapsed_ms']}ms) "
                  f"painted={painted:.3f} cov={cov:.3f} de={scalars['delta_e_mean']}")

        # 3) 收尾：保存结果
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
            canvas_arr, snap_data = self._snapshot_rgb(int(doc["width"]),
                                                       int(doc["height"]))
            final_img = Image.fromarray(canvas_arr, "RGB")
            final_path = os.path.join(self.run_dir, "final.png")
            final_img.save(final_path)
        except Exception:
            traceback.print_exc()
            snap_data, final_path = {}, None

        cache_total = self._cache_hit + self._cache_miss
        summary = {
            "session_id": self.session_id,
            "stop_reason": reason,
            "iterations": self.state.iteration,
            "metrics": scalars,
            "prompt_cache": {
                "hit_tokens": self._cache_hit,
                "miss_tokens": self._cache_miss,
                "hit_rate": round(self._cache_hit / cache_total, 4) if cache_total else 0.0,
            },
            "stage_breakdown": dict(self.state.stage_actions),
            "history_count": len(self.state.history),
            "final_image_path": final_path,
            "target_path": self.target_path,
        }
        meta_path = os.path.join(self.run_dir, "summary.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        _say(f"[agent] 完成: stop_reason={reason} "
              f"iterations={self.state.iteration} 产物目录→{self.run_dir}")
        return summary