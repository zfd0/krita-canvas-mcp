"""闭环会话状态 + 差异度量。

- CanvasMetrics: 画布 vs 目标图的标量差异(mae/rmse/psnr/ssim/delta_e/covered_pct)、
  热点、区域统计、伪彩色热力图。
- ColorLedger:   颜色账本(防调色漂移)。
- SessionState:  stage/iteration/plan/动作历史。

阶段序列为 O(计划)→A(草图)→B(线稿)→C(填色)→D(光影)。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .stage_rules import STAGE_SEQ

# ---------------------------------------------------------------- 差异度量

_SRGB = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)
_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)

# “已下笔”判定：像素任一通道比纯白暗超过该值即视为画布上已落笔
INK_LEVEL = 20
# 目标前景判定：目标像素任一通道比纯白暗超过该值视为前景内容（排除白背景）
FG_LEVEL = 20

# 计入阶段动作配额的"真实绘画动作"；sample_color/set_* 等辅助动作不占门槛
PAINT_TOOLS = {"paint_path", "paint_line", "paint_shape", "write_pixels"}

# 区域状态阈值：匹配度（C/D，ΔE<6 占比）与已绘占比（A/B）分开，
# 因为 A/B 阶段线条笔画天然只覆盖区域内一小部分像素，用匹配度阈值会永远 pending
STATUS_THRESHOLDS = {
    "matched": {"done": 0.9, "active": 0.75, "warm": 0.5, "hot": 0.3},
    "painted": {"done": 0.30, "active": 0.18, "warm": 0.08, "hot": 0.03},
}


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """形状 (...,3) 的 uint8 RGB → CIELAB(D65)。向量化。"""
    rgb = np.clip(rgb, 0, 255).astype(np.float64) / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92,
                   ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _SRGB.T
    xyz = xyz / _WHITE

    def _f(t):
        return np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16.0 / 116.0)

    fx, fy, fz = _f(xyz[..., 0]), _f(xyz[..., 1]), _f(xyz[..., 2])
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return np.stack([L, a, b], axis=-1)


def _rgb_to_gray(rgb: np.ndarray) -> np.ndarray:
    """uint8 RGB → 灰度(0-1)。"""
    return (rgb.astype(np.float64) @ np.array([0.299, 0.587, 0.114])) / 255.0


def _block_ssim(a: np.ndarray, b: np.ndarray, win: int = 8) -> float:
    """简化 SSIM：8x8 均匀窗口分块取均值。a/b 为 uint8 RGB (H,W,3)。"""
    ga, gb = _rgb_to_gray(a), _rgb_to_gray(b)
    h, w = ga.shape
    h = h - h % win
    w = w - w % win
    if h < win or w < win:
        return 1.0
    ga = ga[:h, :w].reshape(h // win, win, w // win, win).transpose(0, 2, 1, 3)
    gb = gb[:h, :w].reshape(h // win, win, w // win, win).transpose(0, 2, 1, 3)
    mu1, mu2 = ga.mean(axis=(2, 3)), gb.mean(axis=(2, 3))
    var1 = ga.var(axis=(2, 3)) + 1e-10
    var2 = gb.var(axis=(2, 3)) + 1e-10
    cov = (ga * gb).mean(axis=(2, 3)) - mu1 * mu2
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    s = ((2 * mu1 * mu2 + c1) * (2 * cov + c2)) / \
        ((mu1 ** 2 + mu2 ** 2 + c1) * (var1 + var2 + c2))
    return float(s.mean())


def _heatmap_lut(delta_e: np.ndarray) -> np.ndarray:
    """delta_e 图 → 伪彩色 RGB(蓝→青→绿→黄→红)。uint8 (H,W,3)。误差上限按 30 截断。"""
    t = np.clip(delta_e / 30.0, 0.0, 1.0) * 4.0
    seg = np.clip(t, 0, 4).astype(int)
    f = np.clip(t - seg, 0, 1)[..., None]
    # 关键色 (B,G,R) 次序
    cols = np.array([[66, 100, 255], [0, 210, 255], [0, 210, 80],
                     [255, 230, 0], [255, 30, 0]], dtype=np.float64)
    i = np.clip(seg, 0, 3)
    rgb = cols[i] * (1 - f) + cols[i + 1] * f
    return rgb.astype(np.uint8)


class CanvasMetrics:
    """单帧差异计算与序列化。"""

    def __init__(self, target_rgb: np.ndarray, canvas_rgb: np.ndarray):
        assert target_rgb.shape == canvas_rgb.shape, "target/canvas 尺寸不一致"
        self.target = target_rgb
        self.canvas = canvas_rgb
        lab_t = _rgb_to_lab(target_rgb.astype(np.float64))
        lab_c = _rgb_to_lab(canvas_rgb.astype(np.float64))
        self.delta_e = np.sqrt(((lab_t - lab_c) ** 2).sum(axis=-1))
        self._target_lab = lab_t
        # 已下笔掩码：相对纯白的暗化像素（笔画落在白底画布上的位置）
        self.ink_mask = ((255 - canvas_rgb.astype(np.int16).min(axis=-1))
                         > INK_LEVEL)
        # 目标前景掩码：目标非白像素。白背景与白画布天然 ΔE≈0，若计入
        # 匹配度会虚高（未动笔的区域也显示 0.8+），故匹配统计只看前景
        self.fg_mask = ((255 - target_rgb.astype(np.int16).min(axis=-1))
                        > FG_LEVEL)

    # ---- 标量 ----
    def scalars(self) -> dict:
        diff = self.canvas.astype(np.float64) - self.target.astype(np.float64)
        mae = float(np.abs(diff).mean())
        rmse = float(np.sqrt((diff ** 2).mean()))
        psnr = float(20 * np.log10(255.0 / (rmse + 1e-9)))
        ssim = _block_ssim(self.target, self.canvas)
        de = self.delta_e
        return {
            "mae": round(mae, 4), "rmse": round(rmse, 4),
            "psnr": round(psnr, 2), "ssim": round(ssim, 4),
            "delta_e_mean": round(float(de.mean()), 2),
            "delta_e_p95": round(float(np.percentile(de, 95)), 2),
        }

    def covered_pct(self, threshold: float = 6.0) -> float:
        """全图匹配度：目标前景像素中 ΔE 低于阈值的占比。

        只统计目标非白（前景内容）像素——白背景与白画布天然匹配，
        计入会虚高（未动笔的区域也显示 0.8+），对 C/D 阶段有误导性。
        衡量“颜色贴合”，C/D 阶段的主指标。
        """
        fg = self.fg_mask
        if not fg.any():
            return 1.0
        return float((self.delta_e[fg] < threshold).mean())

    def painted_pct(self) -> float:
        """全图已绘占比：画布上相对白底已落笔的像素占比。

        A/B 结构阶段的主指标——灰/蓝细线对目标色 ΔE 很大，匹配度几乎不动，
        而已绘占比能如实反映“结构是否在推进”。
        """
        return float(self.ink_mask.mean())

    # ---- 热点 ----
    def hotspots(self, top_k: int = 8, block: int = 64) -> list[dict]:
        """按块聚合 ΔE 均值，取差异最大的 top_k 块。"""
        de = self.delta_e
        h, w = de.shape
        bh, bw = block, block
        nh, nw = (h + bh - 1) // bh, (w + bw - 1) // bw
        out = []
        for i in range(nh):
            for j in range(nw):
                sub = de[i * bh:(i + 1) * bh, j * bw:(j + 1) * bw]
                out.append({
                    "x": j * bw, "y": i * bh,
                    "radius": block // 2,
                    "severity": round(float(sub.mean()), 2),
                })
        out.sort(key=lambda v: -v["severity"])
        return out[:top_k]

    # ---- 区域统计 ----
    def by_regions(self, regions: list[dict], metric: str = "matched",
                   threshold: float = 6.0) -> list[dict]:
        """按 plan 的 regions bbox 统计进度与状态。

        metric="matched"：以 ΔE<threshold 占比为主指标（C/D 颜色贴合）；
        metric="painted"：以已绘像素占比为主指标（A/B 结构推进）。
        行内同时保留 covered_pct(匹配) 与 painted_pct(已绘)，主指标决定 status。
        """
        status_map = STATUS_THRESHOLDS.get(metric, STATUS_THRESHOLDS["matched"])
        rows = []

        def _status(pct: float) -> str:
            if pct >= status_map["done"]:
                return "done"
            if pct >= status_map["active"]:
                return "active"
            if pct >= status_map["warm"]:
                return "warm"
            if pct >= status_map["hot"]:
                return "hot"
            return "pending"

        for r in regions:
            rid, name = r.get("id", "?"), r.get("name", "")
            bbox = r.get("bbox")
            # 防御：容错模型偶发的双层嵌套 [[x,y,w,h]]
            if (isinstance(bbox, (list, tuple)) and len(bbox) == 1
                    and isinstance(bbox[0], (list, tuple))):
                bbox = bbox[0]
            try:
                x, y, w, h = bbox
            except (TypeError, ValueError):
                rows.append({"id": rid, "name": name, "covered_pct": 0.0,
                             "painted_pct": 0.0, "status": "pending"})
                continue
            x, y = max(0, int(x)), max(0, int(y))
            w, h = min(int(w), self.delta_e.shape[1] - x), min(int(h), self.delta_e.shape[0] - y)
            if w <= 0 or h <= 0:
                rows.append({"id": rid, "name": name, "covered_pct": 0.0,
                             "painted_pct": 0.0, "status": "pending"})
                continue
            sub_de = self.delta_e[y:y + h, x:x + w]
            sub_fg = self.fg_mask[y:y + h, x:x + w]
            # 匹配度只看目标前景像素（排除与该区域重叠的白背景，避免虚高）
            cov = (float((sub_de[sub_fg] < threshold).mean())
                   if sub_fg.any() else 0.0)
            ink = float(self.ink_mask[y:y + h, x:x + w].mean())
            primary = ink if metric == "painted" else cov
            rows.append({"id": rid, "name": name,
                         "covered_pct": round(cov, 3),
                         "painted_pct": round(ink, 3),
                         "status": _status(primary),
                         "mae": round(float(sub_de.mean()), 3)})
        return rows

    # ---- 热力图 ----
    def heatmap_b64(self, max_side: int | None = None) -> tuple:
        """伪彩色热力图 → (b64, 宽, 高)。max_side=None 时不缩放（与画布同尺寸）。"""
        hm = _heatmap_lut(self.delta_e)
        img = Image.fromarray(hm, "RGB")
        w, h = img.size
        if max_side is not None and max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        from .vlm_client import pil_to_b64
        return pil_to_b64(img), img.size[0], img.size[1]


# ---------------------------------------------------------------- 颜色账本

def norm_color_token(value) -> str | None:
    """把模型给出的 color 字段归一化为可解析的令牌字符串。

    兼容多种写法：'#RRGGBB' / 'cN' / [r,g,b] / [[r,g,b]] / {'r':..,'g':..,'b':..}。
    归一化后统一交给上层做 resolve/校验，避免 list/dict 直接进入字符串处理而崩溃。
    无法识别时返回 None（由上层按“无颜色”或报错处理）。
    """
    if isinstance(value, str):
        return value.strip() or None
    nums = None
    if isinstance(value, (list, tuple)):
        nums = list(value)
        if len(nums) == 1 and isinstance(nums[0], (list, tuple)):
            nums = list(nums[0])
    elif isinstance(value, dict):
        nums = [value.get("r"), value.get("g"), value.get("b")]
    if nums is not None and len(nums) >= 3:
        try:
            r, g, b = (int(round(float(nums[i]))) for i in range(3))
        except (TypeError, ValueError):
            return None
        return "#%02X%02X%02X" % (max(0, min(255, r)),
                                  max(0, min(255, g)),
                                  max(0, min(255, b)))
    return None


@dataclass
class ColorLedger:
    """颜色账本：登记每个使用过的颜色及次数，输出 c1..cN 编号(按频率降序)。"""
    _usage: dict = field(default_factory=dict)  # hex(大写) -> count

    def record(self, color, name: str = ""):
        hexv = self._norm(color)
        if hexv:
            self._usage[hexv] = self._usage.get(hexv, 0) + 1

    @staticmethod
    def _norm(color) -> str:
        if isinstance(color, str) and color.startswith("#"):
            return color.upper()
        return ""

    def resolve(self, token) -> str | None:
        """把 'c3' / '#AABBCC' / RGB 列表 解析为 hex；无法解析返回 None。"""
        t = norm_color_token(token)
        if not t:
            return None
        if t.startswith("#"):
            h = t[1:]
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
                return "#" + h.upper()
            return None
        low = t.lower()
        if low.startswith("c") and low[1:].isdigit():
            idx = int(low[1:])
            ranked = sorted(self._usage.items(), key=lambda kv: -kv[1])
            if 1 <= idx <= len(ranked):
                return ranked[idx - 1][0]
        return None

    def lines(self, limit: int = 20) -> list[str]:
        """账本文本行：'c1 #F5D5C0 ×12'。"""
        ranked = sorted(self._usage.items(), key=lambda kv: -kv[1])[:limit]
        return [f"c{i + 1} {hexv} ×{cnt}" for i, (hexv, cnt) in enumerate(ranked)]


# ---------------------------------------------------------------- 会话状态

@dataclass
class ActionRecord:
    """单轮动作记录。"""
    iter: int
    stage: str
    tool: str
    params_digest: dict
    thought: str
    ok: bool
    elapsed_ms: int
    color: str = ""


@dataclass
class SessionState:
    """会话状态。"""
    stage: str = "O"  # 初始为 O 计划阶段
    iteration: int = 0
    plan: dict | None = None
    history: list = field(default_factory=list)       # ActionRecord
    ledger: ColorLedger = field(default_factory=ColorLedger)
    stage_actions: dict = field(default_factory=lambda: {s: 0 for s in STAGE_SEQ})
    started_at: float = field(default_factory=time.time)
    out_dir: str = "outputs"
    session_id: str = ""  # 会话号（同一 jsonl 跨多次运行累积，靠它区分）

    def record_action(self, rec: ActionRecord):
        self.history.append(rec)
        # 仅真实绘画动作计入阶段配额；sample_color/set_* 等辅助动作与失败动作不占门槛
        if rec.ok and rec.tool in PAINT_TOOLS:
            self.stage_actions[self.stage] += 1
        try:
            import os
            os.makedirs(self.out_dir, exist_ok=True)
            payload = json.dumps({**rec.__dict__, "session": self.session_id},
                                 ensure_ascii=False, default=str)
            with open(self._history_path(), "a", encoding="utf-8") as f:
                f.write(payload + "\n")
        except Exception:
            pass

    def _history_path(self) -> str:
        import os
        return os.path.join(self.out_dir, "action_history.jsonl")

    # ---- 摘要 ----
    def recent_summary(self, n: int = 5) -> list[str]:
        """近 n 步动作摘要行（不带完整 points）。"""
        out = []
        for rec in self.history[-n:]:
            d = rec.params_digest
            bits = [f"#{rec.iter} {rec.tool}"]
            if d.get("points_count"):
                bits.append(f"points={d['points_count']}")
            if d.get("bbox"):
                bits.append(f"bbox={d['bbox']}")
            if rec.color:
                bits.append(f"color={rec.color}")
            if rec.thought:
                bits.append(f"thought=\"{rec.thought[:24]}\"")
            out.append("  " + " ".join(bits))
        return out