"""将 plugin/krita_canvas_mcp 部署到 Krita 的 pykrita 目录。

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
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("找不到 APPDATA 环境变量")
        return Path(appdata) / "krita" / "pykrita"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "krita" / "pykrita"
    return Path.home() / ".local" / "share" / "krita" / "pykrita"


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

    pykrita = find_pykrita_dir()
    dst = pykrita / PLUGIN_NAME

    print(f"源:   {src}")
    print(f"目标: {dst}")

    if args.dry_run:
        return

    pykrita.mkdir(parents=True, exist_ok=True)

    if dst.exists() or dst.is_symlink():
        print(f"移除旧版本: {dst}")
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)

    if args.link:
        try:
            os.symlink(src, dst, target_is_directory=True)
            print("已创建软链")
        except OSError as e:
            sys.exit(f"软链失败（Windows 需管理员权限或开发者模式）: {e}")
    else:
        shutil.copytree(src, dst)
        print("已复制")

    print("完成。重启 Krita 生效。")


if __name__ == "__main__":
    main()