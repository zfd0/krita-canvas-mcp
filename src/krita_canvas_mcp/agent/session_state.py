"""闭环会话状态 + 差异度量。

- CanvasMetrics: 画布 vs 目标图的标量差异(mae/rmse/psnr/ssim/delta_e/covered_pct)、
  热点、区域统计、伪彩色热力图。
- ColorLedger:   颜色账本(防调色漂移)。
- SessionState:  stage/iteration/plan/动作历史/停滞检测/终止判定。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- 差异度量

_SRGB = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)
_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)


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
        """与目标颜色接近(ΔE<阈值)的像素占比，作为进度覆盖度。"""
        return float((self.delta_e < threshold).mean())

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
    def by_regions(self, regions: list[dict], threshold: float = 6.0,
                   status_map: dict | None = None) -> list[dict]:
        """按 plan 的 regions bbox 统计覆盖度与状态。
        状态边界对齐草稿口径：≥0.9 done / ≥0.75 active / ≥0.5 warm / ≥0.3 hot / 其余 pending。
        """
        status_map = status_map or {"done": 0.9, "active": 0.75,
                                    "warm": 0.5, "hot": 0.3}
        rows = []
        for r in regions:
            x, y, w, h = r["bbox"]
            x, y = max(0, int(x)), max(0, int(y))
            w, h = min(int(w), self.delta_e.shape[1] - x), min(int(h), self.delta_e.shape[0] - y)
            if w <= 0 or h <= 0:
                rows.append({"id": r["id"], "name": r.get("name", ""),
                             "covered_pct": 0.0, "status": "pending"})
                continue
            sub = self.delta_e[y:y + h, x:x + w]
            cov = float((sub < threshold).mean())
            if cov >= status_map["done"]:
                status = "done"
            elif cov >= status_map["active"]:
                status = "active"
            elif cov >= status_map["warm"]:
                status = "warm"
            elif cov >= status_map["hot"]:
                status = "hot"
            else:
                status = "pending"
            rows.append({"id": r["id"], "name": r.get("name", ""),
                         "covered_pct": round(cov, 3), "status": status,
                         "mae": round(float(sub.mean()), 3)})
        return rows

    # ---- 热力图 ----
    def heatmap_b64(self, max_side: int = 512) -> tuple:
        """伪彩色热力图 → (b64, 缩放后的宽, 高)。"""
        hm = _heatmap_lut(self.delta_e)
        img = Image.fromarray(hm, "RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        from .vlm_client import pil_to_b64
        return pil_to_b64(img), img.size[0], img.size[1]


# ---------------------------------------------------------------- 颜色账本

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

    def resolve(self, token: str) -> str | None:
        """把 'c3' / '#AABBCC' 解析为 hex；无法解析返回 None。"""
        if not token:
            return None
        t = token.strip()
        if t.startswith("#"):
            return t.upper()
        if t.lower().startswith("c") and t[1:].isdigit():
            idx = int(t[1:])
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


STAGE_SEQ = ["A", "B", "C", "D"]
# 各阶段最少动作数（next_stage 校验用，低于该值拒绝切换）
MIN_ACTIONS = {"A": 8, "B": 8, "C": 6, "D": 6}


@dataclass
class SessionState:
    """会话状态。"""
    stage: str = "A"
    iteration: int = 0
    plan: dict | None = None
    history: list = field(default_factory=list)       # ActionRecord
    ledger: ColorLedger = field(default_factory=ColorLedger)
    stall_rounds: int = 0
    last_covered: float = 0.0
    stage_cov_anchor: dict = field(default_factory=dict)  # stage -> 切换时 covered
    stage_actions: dict = field(default_factory=lambda: {s: 0 for s in STAGE_SEQ})
    started_at: float = field(default_factory=time.time)
    out_dir: str = "outputs"

    def record_action(self, rec: ActionRecord):
        self.history.append(rec)
        # 特殊动作与失败动作不占用阶段动作配额（next_stage 门槛只数真实成功的绘画动作）
        if rec.ok and rec.tool not in ("done", "next_stage", "plan"):
            self.stage_actions[self.stage] += 1
        try:
            import os
            os.makedirs(self.out_dir, exist_ok=True)
            payload = json.dumps(rec.__dict__, ensure_ascii=False, default=str)
            with open(self._history_path(), "a", encoding="utf-8") as f:
                f.write(payload + "\n")
        except Exception:
            pass

    def _history_path(self) -> str:
        import os
        return os.path.join(self.out_dir, "action_history.jsonl")

    # ---- 停滞检测 ----
    def update_stall(self, covered: float, gain_threshold: float = 0.002) -> int:
        """更新停滞计数：covered 改善低于阈值累加，否则清零。"""
        if covered - self.last_covered < gain_threshold:
            self.stall_rounds += 1
        else:
            self.stall_rounds = 0
        self.last_covered = covered
        return self.stall_rounds

    # ---- 终止判定 ----
    def conclude(self, done: bool, metrics: dict, max_iter: int) -> tuple:
        """返回 (stop: bool, reason: str|None)。"""
        if done:
            return True, "llm_done"
        if self.iteration >= max_iter:
            return True, "max_iter"
        if metrics["delta_e_mean"] < 4.0 and metrics["ssim"] > 0.92:
            return True, "converged"
        if self.stall_rounds >= 5:
            return True, "stalled"
        return False, None

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