from krita import Krita, Extension

from .plugin import KritaCanvasMCP

Krita.instance().addExtension(KritaCanvasMCP(Krita.instance()))