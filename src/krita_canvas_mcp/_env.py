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


def _strip_inline_comment(value: str) -> str:
    """剥离值末尾的内联注释（形如 `https://x  # 备用地址`）。

    仅当 `#` 前有空白且不在引号内时才视为注释起点，避免误伤值本身含 `#`
    的情况（如颜色 '#AABBCC'、URL 片段）。不剥离会把注释一并读进配置。
    """
    quote = ""
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].rstrip()
    return value


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
            # 先剥离内联注释：否则 `VLM_BASE_URL=https://api.deepseek.com  # 备用`
            # 会把注释读进 URL，拼出含空格的非法请求地址，请求会卡在 DNS/连接上
            # 长时间无输出，且 Ctrl+C 需等 C 层调用超时才生效
            value = _strip_inline_comment(value)
            # 剥离成对引号
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            # 环境变量优先，不覆盖已存在项
            if key and key not in os.environ:
                os.environ[key] = value
    return path