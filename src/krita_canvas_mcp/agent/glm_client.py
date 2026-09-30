"""GLM 多模态视觉模型客户端。

闭环绘画的大脑：每轮把【目标图 + 画布快照 + 差异热力图】作为多模态内容，
连同会话状态文本一起发给 GLM-4.6V(-flash)，取回本轮动作 JSON。

API: POST https://open.bigmodel.cn/api/paas/v4/chat/completions
"""
import base64

import httpx

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 默认模型与临时密钥（可用环境变量 GLM_API_KEY 覆盖）
DEFAULT_MODEL = "glm-4.6v-flash"
DEFAULT_API_KEY = "92d35395bf2d47f5990e7cb517630bdc.Mkc182jGW7U1Oy4C"


class GLMError(Exception):
    """GLM API 调用失败。携带可读信息，供 loop 重试/降级。"""


class GLMVisionClient:
    """多模态会话客户端。每次 chat() 是无状态单轮调用（上下文由调用方显式组装）。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 temperature: float = 0.3, max_tokens: int = 2048,
                 timeout: float = 120.0):
        import os
        self.api_key = api_key or os.environ.get("GLM_API_KEY") or DEFAULT_API_KEY
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout)

    @staticmethod
    def _image_part(image_b64: str, mime: str = "image/png") -> dict:
        """base64 图像 → GLM content 块。"""
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_b64}"}}

    def chat(self, system: str, text: str, images: list[dict] | None = None) -> str:
        """一次多模态对话。
        :param system: system prompt
        :param text:   用户文本（会话状态+指令）
        :param images: [{"image_b64": ..., "mime": ...}, ...] 按序插入文本前
        :return: assistant 文本内容
        """
        content = [self._image_part(im["image_b64"], im.get("mime", "image/png"))
                   for im in (images or [])]
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
        try:
            resp = self._client.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise GLMError(f"GLM 网络错误: {e}")

        if resp.status_code != 200:
            raise GLMError(f"GLM HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise GLMError(f"GLM 响应结构异常: {data.get('error', data)}")

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