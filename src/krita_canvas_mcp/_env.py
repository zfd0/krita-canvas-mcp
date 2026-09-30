"""轻量 .env 加载（无第三方依赖）。

解析 `KEY=VALUE` / `export KEY=VALUE` / `# 注释` / 引号包裹，写入 os.environ。
已存在的环境变量优先，不被 .env 覆盖（符合 参数 > 环境变量 > .env > 默认 的优先级）。
"""
from __future__ import annotations

import os


def load_dotenv(path: str | None = None) -> str | None:
    """加载 .env 到 os.environ，返回实际加载的文件路径（未找到返回 None）。

    查找顺序：显式 path > 环境变量 KRITA_ENV_FILE > 从当前工作目录向上逐级找 .env。
    """
    if path is None:
        path = os.environ.get("KRITA_ENV_FILE")

    if path:
        return _parse(path) if os.path.isfile(path) else None

    # 从 cwd 向上逐级查找 .env
    cur = os.getcwd()
    while True:
        candidate = os.path.join(cur, ".env")
        if os.path.isfile(candidate):
            return _parse(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:  # 已到文件系统根
            return None
        cur = parent


def _parse(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            # 剥离成对引号
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            # 环境变量优先，不覆盖已存在项
            if key and key not in os.environ:
                os.environ[key] = value
    return path