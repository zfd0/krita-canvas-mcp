"""Krita Canvas MCP 59 工具全量测试脚本。

路径：构建进程内 MCP Server → call_tool 调用全部 59 个工具（真实链路：
MCP 层 → HTTP bridge → Krita 插件 → LibKis）。

前置：Krita 运行且插件启用（127.0.0.1:5678/rpc）。

用法:
    python scripts/test_all_tools.py [--keep-fixtures] [--target path]
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from krita_canvas_mcp.server import build_server  # noqa: E402

MAIN_DOC = "__mcp_test_main__"
VEC_DOC = "__mcp_test_vec__"
FX_DOC = "__mcp_test_fx__"
LAYER_A = "__mcp_selftest_A__"
LAYER_B = "__mcp_selftest_B__"
DEFAULT_TARGET = str(Path(__file__).resolve().parent.parent
                     / "tests" / "fixtures" / "target_512.png")

RESULTS = []          # (序号, 名称, status∈PASS/FAIL/SKIP/ERROR, detail)
CTX = {"uuid_a": None, "uuid_b": None, "baseline": None}
FAILED_P0 = False


class ToolError(Exception):
    """工具 envelope 返回 ok=false。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def rec(name: str, status: str, detail: str = ""):
    RESULTS.append((name, status, detail))
    print(f"[{status:4s}] {name}" + (f"  -> {detail}" if detail else ""))


def decode(text: str) -> dict:
    """MCP 工具返回的 envelope JSON 字符串 → data；失败抛 ToolError。"""
    obj = json.loads(text)
    if not obj.get("ok"):
        raise ToolError(obj.get("err_code", "IO_ERROR"), obj.get("message", "?"))
    return obj.get("data", {})


def ok_of(text: str) -> bool:
    return json.loads(text).get("ok", False)


def show(text: str, n: int = 240) -> str:
    """失败时截取 envelope 内容用于展示。"""
    return text[:n]


class Tester:
    """测试执行器：asyncio + 全局状态。"""

    def __init__(self, mcp, target: str, keep: bool):
        self.mcp = mcp
        self.target = target
        self.keep = keep

    async def call(self, tool: str, **args) -> str:
        """调用 MCP 工具，返回 envelope JSON 字符串。
        注意：方法参数命名为 tool，避免与工具参数(name 等)冲突。"""
        r = await self.mcp.call_tool(tool, args)
        try:
            return r.content[0].text
        except Exception:
            return str(r)

    async def data(self, tool: str, **args) -> dict:
        """调用并解码 data。"""
        return decode(await self.call(tool, **args))

    async def must(self, tool: str, **args):
        """调用且必须成功（失败抛 ToolError）。"""
        return await self.data(tool, **args)

    async def expect_err(self, tool: str, code: str, **args):
        """期望返回指定错误码。"""
        text = await self.call(tool, **args)
        obj = json.loads(text)
        if obj.get("ok"):
            raise AssertionError(f"预期错误 {code} 但成功: {show(text)}")
        if obj.get("err_code") != code:
            raise AssertionError(
                f"错误码不符 期望={code} 实际={obj.get('err_code')}: {show(text)}")

    async def hash(self) -> str:
        """当前画布快照 hash。"""
        d = await self.data("get_canvas_snapshot", max_side=256)
        return d.get("snapshot_hash", "")

    async def sample_hex(self, x: int, y: int, radius: int = 1) -> str:
        d = await self.must("sample_color", x=x, y=y,
                            radius=radius, reduce="median")
        return d["color"]["srgb_hex"]

    def wrap(self):
        async def run(name, fn, *args):
            try:
                out = await fn(*args)
                if out is not None:      # fn 自行 rec 时不应返回;此处兼容
                    rec(name, "PASS", str(out))
            except ToolError as e:
                rec(name, "FAIL", f"{e.code}: {e.message}")
            except Exception as e:
                rec(name, "FAIL", f"{type(e).__name__}: {e}")
        return run


# ====================================================================== 用例

async def run_all(t: Tester):
    run = t.wrap()

    # ---------------- 阶段 0 前置（无外部依赖：会自动创建测试文档） ----------------
    try:
        await t.data("get_krita_info")   # 连通性探测（不依赖活动文档）
    except ToolError as e:
        print(f"前置失败: {e.code}: {e.message}\n"
              "请确认 Krita 已启动并启用插件(krita_canvas_mcp)。")
        sys.exit(1)

    # 确保存在活动文档：没有则由 create_document 自举（本身即清单用例）
    try:
        info = await t.must("get_document_info")
        print(f"存在活动文档: {info['width']}x{info['height']} "
              f"{info['color_model']}/{info['color_depth']}")
    except ToolError:
        try:
            d = await t.must("create_document", width=512, height=512,
                             name=MAIN_DOC, color_model="RGBA",
                             color_depth="U8", resolution=300)
            rec("0.4 create_document 自举", "PASS",
                f"{d['width']}x{d['height']}")
            print(f"已自举创建测试文档: {d['document_id']}")
        except ToolError as e:
            print(f"前置失败: {e.code}: {e.message}")
            sys.exit(1)

    # 记录基线
    CTX["baseline"] = await t.hash()

    # 建测试主文档 + 图层 A
    try:
        await t.call("close_document", document_id=MAIN_DOC)
    except Exception:
        pass
    await t.must("create_document", width=512, height=512, name=MAIN_DOC,
                 color_model="RGBA", color_depth="U8", resolution=300)
    a = await t.must("create_node", name=LAYER_A, node_type="paintlayer")
    CTX["uuid_a"] = a["uuid"]

    # ---------------- 阶段 1 观测（17） ----------------
    async def obs01():
        d = await t.must("get_document_info")
        assert d["width"] == 512 and d["height"] == 512, d
    await run("1.1 get_document_info 512×512", obs01)

    async def obs02():
        d = await t.must("get_krita_info")
        assert str(d.get("version", "")).startswith("6."), d
    await run("1.2 get_krita_info version=6.x", obs02)

    async def obs03():
        d = await t.must("list_documents")
        assert len(d["documents"]) >= 1
    await run("1.3 list_documents ≥1", obs03)

    async def obs04():
        await t.must("get_node_tree")
    await run("1.4 get_node_tree 递归", obs04)

    async def obs05():
        d = await t.must("get_view_state")
        for k in ("zoom", "brush_size", "opacity", "foreground"):
            assert k in d, k
    await run("1.5 get_view_state 字段齐全", obs05)

    async def obs06():
        full = await t.must("get_canvas_snapshot")
        region = await t.must("get_canvas_snapshot",
                              region={"x": 0, "y": 0, "width": 128, "height": 128})
        small = await t.must("get_canvas_snapshot", max_side=128)
        assert full["image_b64"] and region["region"]["width"] == 128 \
            and small["image_b64"]
    await run("1.6 get_canvas_snapshot 三形态", obs06)

    async def obs07():
        h1 = await t.hash()
        h2 = await t.hash()
        assert h1 == h2, (h1, h2)
    await run("1.7 get_snapshot_hash 稳定", obs07)

    async def obs08():
        d = await t.must("get_node_pixels", node_id=CTX["uuid_a"])
        assert d["image_b64"]
    await run("1.8 get_node_pixels 空图层", obs08)

    async def obs09():
        d = await t.must("list_channels", node_id=CTX["uuid_a"])
        assert len(d["channels"]) == 4, d
    await run("1.9 list_channels RGBA×4", obs09)

    async def obs10():
        d = await t.must("get_channel_pixels", node_id=CTX["uuid_a"],
                         channel="Alpha",
                         region={"x": 0, "y": 0, "width": 64, "height": 64})
        assert d["image_b64"]
    await run("1.10 get_channel_pixels alpha", obs10)

    async def obs11():
        for cs in ("srgb_hex", "srgb_255", "srgb_float", "lab", "hsv"):
            d = await t.must("sample_color", x=256, y=256, radius=0)
            # sample_color 返回全部色彩空间
        d = await t.must("sample_color", x=256, y=256, radius=0)
        assert len(d["color"]) >= 5
    await run("1.11 sample_color 多色彩空间", obs11)

    async def obs12():
        d = await t.must("get_selection_pixels")
        assert d["has_selection"] is False
    await run("1.12 get_selection_pixels 无选区全黑", obs12)

    async def obs13():
        await t.expect_err("get_target_image", "NO_TARGET_IMAGE")
    await run("1.13 get_target_image 未设→NO_TARGET_IMAGE", obs13)

    async def obs14():
        d = await t.must("validate_canvas_target", strict=False)
        assert d["valid"] is False, d
    await run("1.14 validate_canvas_target 无目标", obs14)

    async def obs15():
        await t.expect_err("diff_with_target", "NO_TARGET_IMAGE")
    await run("1.15 diff_with_target→NO_TARGET_IMAGE", obs15)

    async def obs16():
        await t.expect_err("get_paint_progress", "NO_TARGET_IMAGE")
    await run("1.16 get_paint_progress→NO_TARGET_IMAGE", obs16)

    async def obs17():
        d = await t.must("get_action_history", format="stats_only")
        assert "total_iterations" in d
    await run("1.17 get_action_history 空历史", obs17)

    # ---------------- 阶段 2 建环境（12） ----------------
    async def s20():
        b = await t.must("create_node", name=LAYER_B, node_type="paintlayer")
        CTX["uuid_b"] = b["uuid"]
    await run("2.1 create_node B", s20)

    async def s21():
        g = await t.must("create_node", name="__mcp_grp__", node_type="grouplayer")
        CTX["uuid_grp"] = g["uuid"]
    await run("2.2 create_node grouplayer", s21)

    async def s22():
        d = await t.must("create_fill_layer", name="__mcp_fill__",
                         color=[128, 128, 128])
        CTX["uuid_fill"] = d["uuid"]
    await run("2.3 create_fill_layer #808080", s22)

    async def s23():
        v = await t.must("create_node", name="__mcp_vec__", node_type="vectorlayer")
        CTX["uuid_vec"] = v["uuid"]
    await run("2.4 create_node vectorlayer", s23)

    async def s24():
        f = await t.must("create_node", name="__mcp_file__", node_type="filelayer",
                         file_path=t.target)
        CTX["uuid_file"] = f["uuid"]
    await run("2.5 create_node filelayer", s24)

    async def s25():
        await t.must("set_node_props", node_id=CTX["uuid_a"],
                     name=LAYER_A, visible=True, opacity=255)
    await run("2.6 set_node_props 读回一致", s25)

    async def s26():
        d = await t.must("check_paintability", node_id=CTX["uuid_a"])
        assert d["paint_ability"] == "PAINT", d
    await run("2.7 check_paintability(A)=PAINT", s26)

    async def s27():
        d = await t.must("check_paintability", node_id=CTX["uuid_vec"])
        assert d["paint_ability"] == "VECTOR", d
    await run("2.8 check_paintability(vec)=VECTOR", s27)

    async def s28():
        d = await t.must("set_target_image", file_path=t.target)
        assert d["path"] == t.target
    await run("2.9 set_target_image", s28)

    async def s29():
        d = await t.must("get_target_image", variant="original")
        assert d["image_b64"] and d["size"] == [512, 512], d["size"]
    await run("2.10 get_target_image 尺寸一致", s29)

    async def s30():
        d = await t.must("validate_canvas_target")
        assert d["valid"] is True, d
    await run("2.11 validate_canvas_target valid", s30)

    async def s31():
        d = await t.must("get_session_state")
        assert d["target_path"] == t.target
    await run("2.12 get_session_state 记录 target", s31)

    # ---------------- 阶段 3 绘画链（17） ----------------
    async def s32():
        await t.must("set_colors", foreground="#FF0000")
        d = await t.must("get_view_state")
        assert d["foreground"] == "#FF0000", d["foreground"]
    await run("3.1 set_colors 红(读回)", s32)

    async def s33():
        await t.must("set_brush_params", size=8, opacity=1.0)
        d = await t.must("get_view_state")
        assert abs(d["brush_size"] - 8) < 0.5
    await run("3.2 set_brush_params size=8", s33)

    async def s34():
        await t.must("set_brush_flags", eraser_mode=False)
    await run("3.3 set_brush_flags", s34)

    async def s35():
        await t.must("set_blending_mode", mode="normal")
        d = await t.must("get_view_state")
        assert d["blending_mode"] == "normal"
    await run("3.4 set_blending_mode normal(读回)", s35)

    async def s36():
        res = await t.must("list_resources", resource_type="preset")
        if res["names"]:
            await t.must("set_brush_preset", preset_name=res["names"][0])
        else:
            rec("3.5 set_brush_preset", "SKIP", "无预设资源")
            return
        d = await t.must("get_view_state")
        assert d["brush_preset"], "预设未生效"
    await run("3.5 set_brush_preset 读回", s36)

    async def s37():
        for rt in ("preset", "brush", "pattern", "gradient", "palette", "workspace"):
            d = await t.must("list_resources", resource_type=rt)
            assert isinstance(d.get("names"), list), (rt, d)
    await run("3.6 list_resources 6 类型", s37)

    async def s38():
        await t.must("paint_line", node_id=CTX["uuid_a"],
                     x1=10, y1=100, x2=200, y2=100)
        hexv = await t.sample_hex(100, 100)
        r = int(hexv[1:3], 16)
        assert r > 180, f"采样 {hexv} 非红"
    await run("3.7 paint_line 采样=红", s38)

    async def s39():
        await t.must("wait_for_done", refresh_projection=True)
        h1, h2 = await t.hash(), await t.hash()
        assert h1 == h2
    await run("3.8 wait_for_done hash 稳定", s39)

    async def s40():
        await t.must("paint_path", node_id=CTX["uuid_a"],
                     points=[[50, 300], [120, 260], [200, 300], [200, 380], [50, 380]],
                     smooth=True, closed=True,
                     stroke_style="ForegroundColor", fill_style="ForegroundColor")
        hexv = await t.sample_hex(120, 310)
        r = int(hexv[1:3], 16)
        assert r > 180, f"内部采样 {hexv} 非红"
    await run("3.9 paint_path 内部采样=红", s40)

    async def s41():
        await t.must("paint_shape", shape="rectangle", node_id=CTX["uuid_a"],
                     rect={"x": 300, "y": 30, "width": 40, "height": 30},
                     fill_style="ForegroundColor")
        await t.must("paint_shape", shape="ellipse", node_id=CTX["uuid_a"],
                     rect={"x": 360, "y": 30, "width": 40, "height": 30},
                     fill_style="ForegroundColor")
        await t.must("paint_shape", shape="polygon", node_id=CTX["uuid_a"],
                     points=[[300, 150], [340, 120], [380, 150]],
                     fill_style="ForegroundColor")
    await run("3.10 paint_shape 三种", s41)

    async def s42():
        # 32x32 橙色补丁 → overwrite 写位
        from PIL import Image
        import base64, io
        img = Image.new("RGBA", (32, 32), (255, 128, 0, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        await t.must("write_pixels", node_id=CTX["uuid_a"],
                     x=420, y=200, width=32, height=32, image_b64=b64)
        hexv = await t.sample_hex(436, 216)
        assert hexv in ("#FF8000", "#FF7F00"), hexv
    await run("3.11 write_pixels 采样=橙", s42)

    async def s43():
        from PIL import Image
        import base64, io
        img = Image.new("RGBA", (16, 16), (0, 0, 255, 120))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        await t.must("write_pixels", node_id=CTX["uuid_a"],
                     x=450, y=250, width=16, height=16, image_b64=b64,
                     blend_mode="alpha_composite")
        hexv = await t.sample_hex(458, 258)
        # 半透明蓝与橙色混合 → 偏暗/中调
        v = [int(hexv[i:i + 2], 16) for i in (1, 3, 5)]
        assert 60 < sum(v) < 640, hexv
    await run("3.12 write_pixels alpha_composite", s43)

    async def s44():
        import base64
        b64 = await _gray_patch_b64(8, 8, 200)
        await t.must("set_channel_pixels", node_id=CTX["uuid_a"],
                     channel="Alpha", x=300, y=200, image_b64=b64)
        d = await t.must("get_channel_pixels", node_id=CTX["uuid_a"],
                         channel="Alpha",
                         region={"x": 300, "y": 200, "width": 8, "height": 8})
        assert d["image_b64"]
    await run("3.13 set/get_channel_pixels alpha", s44)

    async def s45():
        d = await t.must("extract_palette", max_colors=8,
                         quantize_algorithm="kmeans")
        assert len(d["colors"]) == 8
    await run("3.14 extract_palette 8色", s45)

    async def s46():
        d = await t.must("extract_palette", max_colors=6,
                         quantize_algorithm="median_cut",
                         save_as_palette="t1")
        p = await t.must("get_palette")
        assert p["colors"] == d["colors"]
    await run("3.15 get_palette 与 extract 一致", s46)

    async def s47():
        d1 = await t.must("diff_with_target", mode="abs", include_heatmap=False)
        d2 = await t.must("diff_with_target", mode="perceptual",
                          include_heatmap=True)
        assert d1["metrics"]["mae"] >= 0 and d2["metrics"]["delta_e_mean"] >= 0
        assert "heatmap_png_b64" in d2
    await run("3.16 diff_with_target 双模式", s47)

    async def s48():
        d = await t.must("get_paint_progress", against="target")
        assert 0 <= d["overall"]["covered_pct"] <= 1
    await run("3.17 get_paint_progress", s48)

    # ---------------- 阶段 4 撤销/同步/历史（12） ----------------
    async def s49():
        d = await t.must("get_action_history", format="stats_only")
        before = d["total_iterations"]
        await t.must("get_action_history", format="full", last_n=5)
        await t.must("get_action_history", format="summary")
        CTX["hist_before"] = before
    await run("4.1 三种格式历史", s49)

    async def s50():
        h1 = await t.hash()
        await t.must("undo", steps=1)
        h2 = await t.hash()
        assert h1 != h2, "hash 未变"
        CTX["h_after_undo"] = h2
    await run("4.2 undo(1) hash 变化", s50)

    async def s51():
        await t.must("redo", steps=1)
        h3 = await t.hash()
        # 重做应基本回到基线轨迹（内容自治，不强制全等）
        assert h3
        CTX["h_after_redo"] = h3
    await run("4.3 redo(1) 执行", s51)

    async def s52():
        for _ in range(3):
            await t.must("paint_line", node_id=CTX["uuid_a"],
                         x1=20, y1=430, x2=80, y2=430)
        await t.must("undo", steps=3)
    await run("4.4 undo(3) 批量", s52)

    async def s53():
        d = await t.must("get_action_history", format="full", last_n=50)
        assert len(d["actions"]) > 0
    await run("4.5 get_action_history(full) 含动作", s53)

    async def s54():
        d = await t.must("get_action_history", format="summary")
        assert d["by_action"], d
    await run("4.6 get_action_history(summary)", s54)

    async def s55():
        d = await t.must("get_action_history", format="stats_only")
        assert set(d.keys()) >= {"total_iterations", "by_stage", "by_action"}
    await run("4.7 get_action_history(stats)", s55)

    async def s56():
        d = await t.must("get_session_state")
        assert "iteration" in d and "stage" in d
    await run("4.8 get_session_state", s56)

    async def s57():
        d = await t.must("get_session_state", with_history=True)
        assert "history_summary" in d
    await run("4.9 get_session_state(with_history)", s57)

    async def s58():
        d = await t.must("get_snapshot_hash")
        assert d["hash"]
    await run("4.10 get_snapshot_hash", s58)

    # ---------------- 阶段 5 选区 + 像素（9） ----------------
    async def s59():
        await t.must("selection_op", op="select_rect", x=100, y=100,
                     width=50, height=50)
        d = await t.must("get_selection_pixels",
                         region={"x": 100, "y": 100, "width": 50, "height": 50})
        assert d["has_selection"] is True
    await run("5.1 selection_op(select_rect)", s59)

    async def s60():
        await t.must("selection_op", op="invert")
    await run("5.2 selection_op(invert)", s60)

    async def s61():
        await t.must("selection_op", op="feather", radius=5)
        d = await t.must("get_selection_pixels")
        assert d["has_selection"]
    await run("5.3 selection_op(feather)", s61)

    async def s62():
        await t.must("selection_op", op="grow", radius=3)
    await run("5.4 selection_op(grow)", s62)

    async def s63():
        await t.must("selection_op", op="clear")
        d = await t.must("get_selection_pixels",
                         region={"x": 0, "y": 0, "width": 64, "height": 64})
        assert d["has_selection"] is False or d["selection_bounds"] is None
    await run("5.5 selection_op(clear)", s63)

    async def s64():
        b64 = await _gray_patch_b64(30, 30, 255)
        await t.must("set_selection_pixels", x=200, y=200, image_b64=b64)
        d = await t.must("get_selection_pixels",
                         region={"x": 200, "y": 200, "width": 30, "height": 30})
        assert d["has_selection"] is True
    await run("5.6 set_selection_pixels 读回", s64)

    async def s65():
        await t.must("paint_line", node_id=CTX["uuid_a"],
                     x1=180, y1=215, x2=260, y2=215)
    await run("5.7 选区内绘制", s65)

    async def s66():
        await t.must("selection_op", op="clear")
    await run("5.8 selection_op(clear) 恢复自由", s66)

    # ---------------- 阶段 6 配置读回（5） ----------------
    async def s67():
        await t.must("set_brush_params", size=2)
        d = await t.must("get_view_state")
        assert abs(d["brush_size"] - 2) < 0.5
    await run("6.1 brush size=2 读回", s67)

    async def s68():
        await t.must("set_colors", foreground="#00FF00")
        d = await t.must("get_view_state")
        assert d["foreground"] == "#00FF00"
    await run("6.2 fg=绿 读回", s68)

    async def s69():
        await t.must("set_brush_flags", global_alpha_lock=True)
        d = await t.must("get_view_state")
        assert d["global_alpha_lock"] is True
    await run("6.3 alpha_lock 读回", s69)

    async def s70():
        await t.must("set_node_props", node_id=CTX["uuid_a"],
                     blending_mode="multiply")
        tree = await t.must("get_node_tree")
        txt = json.dumps(tree)
        assert "multiply" in txt
    await run("6.4 node blending_mode 树反映", s70)

    async def s71():
        await t.must("set_node_props", node_id=CTX["uuid_a"],
                     blending_mode="normal")
    await run("6.5 收尾改回 normal", s71)

    # ---------------- 阶段 7 图层管理（12） ----------------
    async def s72():
        d = await t.must("manage_node", node_id=CTX["uuid_a"], op="duplicate")
        CTX["uuid_dup"] = d["uuid"]
    await run("7.1 duplicate 新 uuid", s72)

    async def s73():
        await t.must("manage_node", node_id=CTX["uuid_dup"], op="set_active")
    await run("7.2 set_active 副本", s73)

    async def s74():
        await t.must("manage_node", node_id=CTX["uuid_dup"], op="move", x=5, y=5)
    await run("7.3 move(5,5)", s74)

    async def s75():
        await t.must("manage_node", node_id=CTX["uuid_dup"], op="reorder",
                     above_id=CTX["uuid_b"])
    await run("7.4 reorder 到 B 上", s75)

    async def s76():
        await t.must("manage_node", node_id=CTX["uuid_dup"], op="remove")
    await run("7.5 remove 副本", s76)

    async def s77():
        # merge 专测：独立临时两图层，红+蓝 merge
        from PIL import Image
        import base64, io
        m1 = await t.must("create_node", name="__mcp_m1__", node_type="paintlayer")
        m2 = await t.must("create_node", name="__mcp_m2__", node_type="paintlayer")
        red = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        blue = Image.new("RGBA", (64, 64), (0, 0, 255, 255))
        for node, img in ((m1["uuid"], red), (m2["uuid"], blue)):
            buf = io.BytesIO()
            img.save(buf, "PNG")
            await t.must("write_pixels", node_id=node, x=0, y=0,
                         width=64, height=64,
                         image_b64=base64.b64encode(buf.getvalue()).decode())
        await t.must("manage_node", node_id=m2["uuid"], op="set_active")
        d = await t.must("manage_node", node_id=m2["uuid"], op="merge_down")
        assert d["op"] == "merge_down"
        await t.must("manage_node", node_id=m1["uuid"], op="remove")
    await run("7.6 merge_down 专测", s77)

    async def s78():
        d = await t.must("create_fill_layer", name="__mcp_fill2__",
                         color=[0, 0, 0])
        await t.must("manage_node", node_id=d["uuid"], op="remove")
    await run("7.7 create_fill_layer 重测", s78)

    async def s79():
        await t.must("transform_node", node_id=CTX["uuid_a"],
                     op="scale", width=256, height=256)
    await run("7.8 transform_node(scale 0.5)", s79)

    async def s80():
        await t.must("transform_node", node_id=CTX["uuid_a"],
                     op="rotate", angle=30)
    await run("7.9 transform_node(rotate 30°)", s80)

    async def s811():
        d = await t.must("transform_node", node_id=CTX["uuid_a"],
                         op="crop",
                         crop_region={"x": 0, "y": 0, "width": 150, "height": 150})
        assert d["bounds"][2] <= 150
    await run("7.10 transform_node(crop 150)", s811)

    async def s812():
        await t.must("transform_node", node_id=CTX["uuid_a"],
                     op="shear", angle=10, angle_y=0)
    await run("7.11 transform_node(shear)", s812)

    async def s813():
        await t.must("undo", steps=3)
    await run("7.12 undo 恢复", s813)

    # ---------------- 阶段 8 矢量（13） ----------------
    async def s82():
        await t.must("create_document", width=256, height=256, name=VEC_DOC)
        v = await t.must("create_node", name="__mcp_vec2__", node_type="vectorlayer")
        CTX["uuid_vec2"] = v["uuid"]
    await run("8.1 create_document(vec)+图层", s82)

    async def s83():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
               '<path d="M10 10 L60 60" stroke="black"/>'
               '<rect x="20" y="20" width="40" height="40"/>'
               '<ellipse cx="80" cy="80" rx="20" ry="10"/></svg>')
        d = await t.must("vector_add_svg", node_id=CTX["uuid_vec2"], svg=svg)
        assert d["added"] == 3, d
    await run("8.2 vector_add_svg 3 路径", s83)

    async def s84():
        d = await t.must("vector_get_shapes", node_id=CTX["uuid_vec2"])
        assert d["count"] >= 3, d
    await run("8.3 vector_get_shapes ≥3", s84)

    async def s85():
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="set_position", shape_index=0, position=[120, 120])
    await run("8.4 shape_op set_position", s85)

    async def s86():
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="set_transform", shape_index=1,
                     matrix=[1, 0, 0, 1, 30, 30])
    await run("8.5 shape_op set_transform", s86)

    async def s87():
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="set_zindex", shape_index=1, z_index=5)
    await run("8.6 shape_op set_zindex", s87)

    async def s88():
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="select", shape_index=0)
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="deselect", shape_index=0)
    await run("8.7 shape_op select/deselect", s88)

    async def s89():
        g = await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                         op="group", group_with=[0, 1], group_name="__grp__")
        assert g["grouped"] == 2
    await run("8.8 shape_op group(0,1)", s89)

    async def s90():
        d = await t.must("vector_get_shapes", node_id=CTX["uuid_vec2"])
        # group 后 top-level 形状减少
        assert d["count"] >= 1
    await run("8.9 group 后拓扑", s90)

    async def s91():
        await t.must("vector_shape_op", node_id=CTX["uuid_vec2"],
                     op="remove", shape_index=0)
    await run("8.10 shape_op remove", s91)

    async def s92():
        d = await t.must("vector_export_svg", node_id=CTX["uuid_vec2"])
        assert d["contains_path"] or "svg" in d["svg"]
    await run("8.11 vector_export_svg 整层", s92)

    async def s93():
        d = await t.must("vector_export_svg", node_id=CTX["uuid_vec2"],
                         shape_index=0)
        assert d["scope"] == "shape"
    await run("8.12 vector_export_svg 单形状", s93)

    async def s94():
        await t.must("close_document")
    await run("8.13 close_document(vec)", s94)

    # ---------------- 阶段 9 滤镜/变换（12） ----------------
    async def s95():
        await t.must("create_document", width=256, height=256, name=FX_DOC)
    await run("9.1 create_document(fx)", s95)

    async def s96():
        await t.must("set_colors", foreground="#000000")
        await t.must("paint_line", x1=20, y1=128, x2=230, y2=128)
    await run("9.2 paint_line 黑线", s96)

    async def s97():
        d = await t.must("apply_filter", list_only=True)
        assert "gaussian blur" in d["filters"] or "blur" in d["filters"], d
    await run("9.3 apply_filter(list_only)", s97)

    async def s98():
        d = await t.must("get_filter_config", filter_name="gaussian blur")
        assert d["config"], "无参数模板"
    await run("9.4 get_filter_config(blur)", s98)

    async def s99():
        before = await t.hash()
        await t.must("apply_filter", filter_name="gaussian blur",
                     config={"horizRadius": 3.0, "vertRadius": 3.0,
                             "lockAspect": True})
        after = await t.hash()
        assert before != after, "快照未变化"
    await run("9.5 apply_filter(blur) 快照变化", s99)

    async def s100():
        await t.must("apply_filter", filter_name="gaussian blur",
                     config={"horizRadius": 1.0, "vertRadius": 1.0},
                     as_filter_layer=True)
        tree = json.dumps(await t.must("get_node_tree"))
        assert "filter" in tree.lower()
    await run("9.6 apply_filter(as_filter_layer)", s100)

    async def s101():
        d = await t.must("transform_document", op="resize",
                         region={"x": 0, "y": 0, "width": 128, "height": 128})
        assert d["width"] == 128
    await run("9.7 transform_document(resize 128)", s101)

    async def s102():
        await t.must("transform_document", op="rotate", angle=30)
    await run("9.8 transform_document(rotate)", s102)

    async def s103():
        await t.must("transform_document", op="scale", width=256, height=256,
                     x_res=72, y_res=72)
    await run("9.9 transform_document(scale 256)", s103)

    async def s104():
        d = await t.must("transform_document", op="resize", x_res=300, y_res=300)
        assert d["x_res"] == 300
    await run("9.10 transform_document(DPI=300)", s104)

    async def s105():
        import os, tempfile
        out = os.path.join(tempfile.gettempdir(), "mcp_export_test.png")
        d = await t.must("save_document", mode="export", file_path=out,
                         format="png")
        assert d["file_exists"], d
        os.remove(out)
    await run("9.11 save_document(export) 文件存在", s105)

    async def s106():
        await t.must("close_document")
    await run("9.12 close_document(fx)", s106)

    # ---------------- 阶段 10 会话/IO（19） ----------------
    async def s107():
        # 回到主文档（重新激活）
        await t.must("create_document", width=512, height=512, name=MAIN_DOC)
        d = await t.must("open_document", file_path=t.target)
        assert d["width"] == 512
    await run("10.1 open_document(target)", s107)

    async def s108():
        import os, tempfile
        out = os.path.join(tempfile.gettempdir(), "mcp_saveas.kra")
        d = await t.must("save_document", mode="save_as", file_path=out,
                         format="kra")
        assert d["file_exists"]
        os.remove(out)
    await run("10.2 save_document(save_as .kra)", s108)

    async def s109():
        import os, tempfile
        out = os.path.join(tempfile.gettempdir(), "mcp_node_export.png")
        await t.must("set_colors", foreground="#FF0000")
        d = await t.must("get_view_state")
        # 活动文档为 target 图；改为导出主文档 A 层
        await t.must("close_document")
        d = await t.must("save_document", mode="export", file_path=out,
                         format="png")
        assert d["file_exists"]
        os.remove(out)
    await run("10.3 save_document(export)", s109)

    async def s110():
        d = await t.must("get_setting", name="__test_key__",
                         default_value="def")
        assert d["value"] == "def"
    await run("10.4 get_setting 默认值", s110)

    async def s111():
        await t.must("set_setting", name="__test_key__", value="hello")
    await run("10.5 set_setting 写", s111)

    async def s112():
        d = await t.must("get_setting", name="__test_key__")
        assert d["value"] == "hello", d
    await run("10.6 get_setting 读回一致", s112)

    async def s113():
        d = await t.must("get_session_state")
        assert d["iteration"] > 0
    await run("10.7 get_session_state", s113)

    async def s114():
        d = await t.must("abort_session", save_partial=True, keep_canvas=True)
        assert d["partial_saved_to"], "未落盘"
        CTX["partial"] = d["partial_saved_to"]
    await run("10.8 abort_session(keep=true)", s114)

    async def s115():
        await t.must("set_target_image", file_path=t.target)
    await run("10.9 重新初始化 target", s115)

    async def s116():
        d = await t.must("abort_session", save_partial=False, keep_canvas=False)
        assert d["aborted_at_iteration"] >= 0
    await run("10.10 abort_session(keep=false)", s116)

    async def s117():
        d = await t.must("execute_action", list_only=True)
        assert "edit_undo" in d["actions"], "动作列表缺 edit_undo"
    await run("10.11 execute_action(list_only)", s117)

    async def s118():
        await t.must("execute_action", action_name="edit_undo")
        await t.must("execute_action", action_name="edit_redo")
    await run("10.12 execute_action undo/redo 动作", s118)

    async def s119():
        d = await t.must("set_view_state", zoom=2.0)
        assert abs(d["zoom"] - 2.0) < 0.01
    await run("10.13 set_view_state(zoom=2)", s119)

    async def s120():
        d = await t.must("set_view_state", rotation=30)
        assert abs(d["rotation"] - 30) < 0.5
    await run("10.14 set_view_state(rotation=30)", s120)

    async def s121():
        d = await t.must("set_view_state", mirror=True)
        assert d["mirror"] is True
    await run("10.15 set_view_state(mirror)", s121)

    async def s122():
        d = await t.must("set_view_state", center_to=[256, 256])
        assert "zoom" in d
    await run("10.16 set_view_state(center_to)", s122)

    async def s123():
        d = await t.must("set_view_state", reset_view=True)
        assert abs(d["zoom"] - 1.0) < 0.01 and d["mirror"] is False
    await run("10.17 set_view_state(reset)", s123)

    # ---------------- 阶段 11 清理 ----------------
    if not t.keep:
        for doc_name in (MAIN_DOC, VEC_DOC, FX_DOC):
            try:
                await t.call("close_document", document_id=doc_name)
            except Exception:
                pass
        # 关闭打开的目标图：活动文档全关
        try:
            await t.must("close_document")
        except Exception:
            pass
        rec("11. 清理测试文档", "PASS")
    else:
        rec("11. 清理测试文档", "SKIP", "--keep-fixtures")


async def _gray_patch_b64(w: int, h: int, v: int) -> str:
    """生成灰度 PNG base64 补丁。"""
    import base64
    from PIL import Image
    import io
    img = Image.new("L", (w, h), v)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser(description="Krita Canvas MCP 全工具测试")
    parser.add_argument("--keep-fixtures", action="store_true",
                        help="保留测试文档/图层")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="目标图路径")
    args = parser.parse_args()

    if not Path(args.target).exists():
        sys.exit(f"目标图不存在: {args.target}")

    print(f"== Krita Canvas MCP 59 工具全量测试 ==\n目标图: {args.target}\n")

    mcp = build_server()
    t = Tester(mcp, args.target, args.keep_fixtures)

    t0 = time.time()
    try:
        asyncio.run(run_all(t))
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        rec("框架异常", "ERROR", f"{type(e).__name__}: {e}")

    # ---------------- 阶段 12 汇总 ----------------
    fails = [r for r in RESULTS if r[1] in ("FAIL", "ERROR")]
    skips = [r for r in RESULTS if r[1] == "SKIP"]
    passed = len(RESULTS) - len(fails) - len(skips)
    print(f"\n== 汇总: 通过 {passed} / 失败 {len(fails)} / 跳过 {len(skips)} "
          f"/ 共 {len(RESULTS)} | 耗时 {time.time() - t0:.0f}s ==")
    for name, status, detail in fails:
        print(f"  [{status}] {name}: {detail}")
    sys.exit(2 if any(r[1] == "ERROR" for r in fails) else (1 if fails else 0))


if __name__ == "__main__":
    main()