"""Clipboard monitor — watches for new images and triggers OCR."""
import hashlib
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QBuffer, QIODevice
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage


class ClipboardMonitor(QObject):
    image_detected = pyqtSignal(bytes)  # PNG bytes

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_image_hash = None
        self._timer = QTimer(self)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._check)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def is_running(self) -> bool:
        return self._timer.isActive()

    def _check(self):
        clipboard = QApplication.clipboard()
        img = clipboard.image()
        if img.isNull():
            return
        h = hashlib.md5(img.constBits().tobytes(), usedforsecurity=False).digest()
        if h == self._last_image_hash:
            return
        self._last_image_hash = h
        buf = QBuffer()
        if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
            return
        if not img.save(buf, "PNG"):
            return  # 编码失败:不向下游发空数据
        data = bytes(buf.data())
        if data:
            self.image_detected.emit(data)
