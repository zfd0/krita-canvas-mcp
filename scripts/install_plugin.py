"""将 plugin/krita_canvas_mcp 部署到 Krita 的 pykrita 目录。

Krita 插件发现机制（见 krita-6.0.4 源码
plugins/extensions/pykrita/plugin/PythonPluginManager.cpp `scanPlugins()`）：
    - 仅扫描 pykrita 目录第一层的 `pykrita/*.desktop` 文件（不递归子目录）
    - desktop 的 X-KDE-Library 指向模块名，模块查 `pykrita/<模块名>/__init__.py`

因此正确布局为：
    %APPDATA%\\krita\\pykrita\\krita_canvas_mcp.desktop   ← desktop 放根目录
    %APPDATA%\\krita\\pykrita\\krita_canvas_mcp\\...      ← 模块目录（含 __init__.py）
（本插件无菜单动作，故不需要 .action 文件与 actions 目录。）

用法:
    python scripts/install_plugin.py            # 复制（默认）
    python scripts/install_plugin.py --link     # 软链（开发模式）
    python scripts/install_plugin.py --dry-run  # 只打印路径
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "krita_canvas_mcp"


def find_pykrita_dir() -> Path:
    """定位 Krita 用户资源目录下的 pykrita 目录。"""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("找不到 APPDATA 环境变量")
        return Path(appdata) / "krita" / "pykrita"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "krita" / "pykrita"
    return Path.home() / ".local" / "share" / "krita" / "pykrita"


def _rm(path: Path):
    """删除文件/软链/目录（存在时）。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description="安装 Krita Canvas MCP 插件")
    parser.add_argument("--link", action="store_true",
                        help="用软链代替复制（开发模式）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    src = project_root / "plugin" / PLUGIN_NAME
    if not src.is_dir():
        sys.exit(f"找不到插件源码: {src}")
    src_desktop = src / f"{PLUGIN_NAME}.desktop"
    if not src_desktop.is_file():
        sys.exit(f"找不到插件描述文件: {src_desktop}")

    pykrita = find_pykrita_dir()
    dst_module = pykrita / PLUGIN_NAME
    dst_desktop = pykrita / f"{PLUGIN_NAME}.desktop"

    print(f"源目录:      {src}")
    print(f"模块目标:    {dst_module}")
    print(f"desktop 目标: {dst_desktop}  (必须位于 pykrita 根目录)")

    if args.dry_run:
        return

    pykrita.mkdir(parents=True, exist_ok=True)

    # 1) 模块目录
    print(f"移除旧模块: {dst_module}")
    _rm(dst_module)

    # 2) desktop 文件（根目录，旧版本可能埋藏在别处，一并清除）
    _rm(dst_desktop)

    if args.link:
        try:
            os.symlink(src, dst_module, target_is_directory=True)
            print("模块已软链")
        except OSError as e:
            sys.exit(f"软链失败（Windows 需管理员权限或开发者模式）: {e}")
        try:
            os.symlink(src_desktop, dst_desktop)
            print("desktop 已软链")
        except OSError:
            shutil.copy2(src_desktop, dst_desktop)
            print("desktop 已复制（软链失败回退）")
    else:
        shutil.copytree(src, dst_module,
                        ignore=shutil.ignore_patterns("*.desktop"))
        shutil.copy2(src_desktop, dst_desktop)
        print("模块与 desktop 已复制")

    print("完成。重启 Krita → 设置→配置 Krita→Python 插件管理器"
          "→ 勾选 Krita Canvas MCP → 再重启。")
    print("插件启用后控制台应输出: "
          "[krita-canvas-mcp] HTTP RPC listening on 127.0.0.1:5678")


if __name__ == "__main__":
    main()