from enum import Enum


class ErrCode(str, Enum):
    # Krita 侧
    NO_ACTIVE_DOCUMENT = "NO_ACTIVE_DOCUMENT"
    INVALID_NODE = "INVALID_NODE"
    UNPAINTABLE = "UNPAINTABLE"
    INVALID_PARAM = "INVALID_PARAM"
    ACTION_NOT_FOUND = "ACTION_NOT_FOUND"
    FILTER_NOT_FOUND = "FILTER_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    IO_ERROR = "IO_ERROR"

    # 会话/闭环
    NO_TARGET_IMAGE = "NO_TARGET_IMAGE"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    INVALID_REGION = "INVALID_REGION"

    # 通信
    KRITA_NOT_CONNECTED = "KRITA_NOT_CONNECTED"

    # 开发期
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class KritaError(Exception):
    """统一错误类型。所有工具应抛出它，由 envelope 层序列化。"""

    def __init__(self, code: ErrCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"ok": False, "err_code": self.code.value, "message": self.message}