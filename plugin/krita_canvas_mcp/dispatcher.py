# 关键点：LibKis 必须在 Krita 主线程执行。这里的 dispatcher 把 HTTP 请求排进队列，主线程用 QTimer 消费。
from PyQt5.QtCore import QTimer, QObject
from PyQt5.QtWidgets import QApplication
from queue import Queue, Empty
import threading
import traceback

from .handlers import HANDLERS

class Dispatcher(QObject):
    def __init__(self):
        super().__init__()
        self.queue: Queue = Queue()
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(20)  # 50Hz 消费

    def submit(self, method: str, params: dict) -> dict:
        """从 HTTP 线程调用；阻塞等主线程结果"""
        event = threading.Event()
        box = {"result": None}
        self.queue.put((method, params, box, event))
        if not event.wait(timeout=60):
            return {"ok": False, "err_code": "TIMEOUT", "message": "主线程处理超时"}
        return box["result"]

    def _tick(self):
        try:
            while True:
                method, params, box, event = self.queue.get_nowait()
                try:
                    handler = HANDLERS.get(method)
                    if handler is None:
                        box["result"] = {"ok": False,
                                         "err_code": "ACTION_NOT_FOUND",
                                         "message": f"未知方法 {method}"}
                    else:
                        data = handler(params)
                        box["result"] = {"ok": True, "data": data}
                except Exception as e:
                    traceback.print_exc()
                    box["result"] = {"ok": False, "err_code": "IO_ERROR",
                                     "message": str(e)}
                finally:
                    event.set()
        except Empty:
            pass