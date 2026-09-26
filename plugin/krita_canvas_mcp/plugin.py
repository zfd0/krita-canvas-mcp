from krita import Extension
from .http_server import start_server
from .dispatcher import Dispatcher

class KritaCanvasMCP(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.dispatcher = None
        self.server = None

    def setup(self):
        self.dispatcher = Dispatcher()
        self.server = start_server(self.dispatcher)
        print("[krita-canvas-mcp] HTTP RPC listening on 127.0.0.1:5678")

    def createActions(self, window):
        pass