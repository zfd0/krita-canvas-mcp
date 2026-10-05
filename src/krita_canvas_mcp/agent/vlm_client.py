"""通用 VLM（视觉语言模型）客户端 —— OpenAI Chat Completions 兼容。

闭环绘画的大脑：每轮把【目标图 + 画布快照 + 差异热力图】作为多模态内容，
连同会话状态文本一起发给任意 OpenAI 兼容的 VLM，取回本轮动作 JSON。

配置无内置默认值，必须来自项目根 .env（模块加载时自动读取）或进程环境变量，
缺失任一项（base_url / api_key / model）都会抛出 VLMConfigError：

    VLM_BASE_URL   （必填，如 https://open.bigmodel.cn/api/paas/v4）
    VLM_API_KEY    （必填；本地免 key 服务可显式留空 VLM_API_KEY=，亦兼容 GLM_API_KEY）
    VLM_MODEL      （必填，如 glm-4.6v-flash）

base_url 约定：到协议 + 主机（可含 /v1 等前缀），不含 /chat/completions，
客户端内部自动拼上 /chat/completions。
"""
import base64
import os

import httpx

from .._env import load_dotenv

# 模块导入即加载 .env（默认读取项目根 .env；环境变量优先于 .env）
load_dotenv()


class VLMError(Exception):
    """VLM API 调用失败。携带可读信息，供 loop 重试/降级。"""


class VLMConfigError(Exception):
    """VLM 配置缺失（.env / 环境变量未提供必需项）。"""


def _require(key: str, example: str, allow_empty: bool = False) -> str:
    """读取必需配置项；缺失则抛出带修复提示的 VLMConfigError。

    :param key:          环境变量名
    :param example:      .env 中的示例值（用于错误提示）
    :param allow_empty:  True 表示允许显式空值（如 VLM_API_KEY= 表示免 key）
    """
    val = os.environ.get(key)
    if val is None or (val.strip() == "" and not allow_empty):
        raise VLMConfigError(
            f"缺少 VLM 配置项 {key}。请在项目根 .env 中设置，例如：\n"
            f"  {key}={example}\n"
            f"（也可通过环境变量 {key} 提供）")
    return val


def resolve_base_url() -> str:
    """base_url：环境变量 / .env 必须提供，缺失报错。"""
    return _require("VLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")


def resolve_api_key() -> str:
    """api_key：VLM_API_KEY > GLM_API_KEY(兼容)；显式空值表示本地免 key。"""
    val = os.environ.get("VLM_API_KEY")
    if val is None:
        val = os.environ.get("GLM_API_KEY")
    if val is None:
        raise VLMConfigError(
            "缺少 VLM 配置项 VLM_API_KEY。请在项目根 .env 中设置，例如：\n"
            "  VLM_API_KEY=你的密钥\n"
            "（本地免 key 服务可显式留空：VLM_API_KEY=）")
    return val


def resolve_model() -> str:
    """model：环境变量 / .env 必须提供，缺失报错。"""
    return _require("VLM_MODEL", "glm-4.6v-flash")


class VLMClient:
    """通用多模态 VLM 客户端。每次 chat() 是无状态单轮调用（上下文由调用方显式组装）。"""

    def __init__(self, base_url: str | None = None,
                 api_key: str | None = None, model: str | None = None,
                 temperature: float = 0.3, max_tokens: int = 2048,
                 timeout: float = 120.0):
        self.base_url = (base_url or resolve_base_url()).rstrip("/")
        self.api_key = api_key or resolve_api_key()
        self.model = model or resolve_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout)
        # 最近一次成功请求的 usage（含 DeepSeek 提示缓存命中字段），供遥测读取
        self.last_usage: dict = {}

    @property
    def endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    @staticmethod
    def _image_part(image_b64: str, mime: str = "image/png") -> dict:
        """base64 图像 → OpenAI content 块。"""
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_b64}"}}

    def chat(self, system: str, text: str, images: list[dict] | None = None,
             prefix: str = "", enable_thinking: bool = False) -> str:
        """一次多模态对话。
        :param system: system prompt
        :param text:   用户文本（每轮变化的后缀：会话状态+指令）
        :param images: [{"image_b64": ..., "mime": ...}, ...] 按序插入文本前
        :param prefix: 稳定文本前缀，置于图像之前（提升前缀缓存命中率）
        :param enable_thinking: 是否启用思考模式（Agnes 模型专用）
        :return: assistant 文本内容
        """
        content = []
        if prefix:
            # 稳定前缀置于图像之前：是唯一可命中前缀缓存的部分
            content.append({"type": "text", "text": prefix})
        content += [self._image_part(im["image_b64"], im.get("mime", "image/png"))
                    for im in (images or [])]
        if text:
            # O 阶段无反馈时可能为空串，跳过空文本块以免部分服务拒收
            content.append({"type": "text", "text": text})

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # Agnes 模型支持思考模式
        if enable_thinking and "agnes" in self.model.lower():
            body["chat_template_kwargs"] = {"enable_thinking": True}

        try:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            resp = self._client.post(
                self.endpoint,
                headers=headers,
                json=body,
            )
        except httpx.HTTPError as e:
            raise VLMError(f"VLM 网络错误: {e}")

        if resp.status_code != 200:
            raise VLMError(f"VLM HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        self.last_usage = data.get("usage") or {}
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise VLMError(f"VLM 响应结构异常: {data.get('error', data)}")

    @staticmethod
    def encode_image(path: str) -> tuple:
        """本地图片文件 → (image_b64, mime)。仅支持 png/jpeg。"""
        with open(path, "rb") as f:
            raw = f.read()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return base64.b64encode(raw).decode("ascii"), mime


def pil_to_b64(img, fmt: str = "PNG") -> str:
    """PIL Image → base64 字符串。"""
    import io
    buf = io.BytesIO()
    img.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")