"""Screenshot capture widget."""
from PyQt6.QtWidgets import QWidget, QApplication, QRubberBand
from PyQt6.QtCore import Qt, QRect, QPoint, QSize, QBuffer, QIODevice, pyqtSignal
from PyQt6.QtGui import QColor, QPainter


class ScreenshotWidget(QWidget):
    captured = pyqtSignal(bytes, QRect)  # emits PNG bytes + selection rect (logical screen coords)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 关窗即销毁,避免每次截图累积旧实例(各自持一份全屏 pixmap)占内存
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # 预抓每块屏的物理像素 pixmap(各带自己的 dpr),覆盖整个虚拟桌面。
        # 先抓后显遮罩,保证截图里不含暗色蒙层,也避开 hide() 异步生效的竞态。
        vrect = QRect()
        self._shots = []   # [(screen, pixmap, geometry)]
        for s in QApplication.screens():
            geo = s.geometry()
            vrect = vrect.united(geo)
            shot = s.grabWindow(0)
            shot.setDevicePixelRatio(s.devicePixelRatio())
            self._shots.append((s, shot, geo))
        self.setGeometry(vrect)
        self._virtual_origin = vrect.topLeft()
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._origin = QPoint()
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)

    def paintEvent(self, _):
        painter = QPainter(self)
        # 把每块屏的截图画到对应位置(控件坐标 = 屏几何 - 虚拟原点),dpr 已设故按逻辑尺寸绘
        for _s, shot, geo in self._shots:
            painter.drawPixmap(geo.topLeft() - self._virtual_origin, shot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))

    def mousePressEvent(self, e):
        self._origin = e.pos()
        self._rubber.setGeometry(QRect(self._origin, QSize()))
        self._rubber.show()

    def mouseMoveEvent(self, e):
        self._rubber.setGeometry(QRect(self._origin, e.pos()).normalized())

    def mouseReleaseEvent(self, e):
        rect = QRect(self._origin, e.pos()).normalized()
        self.hide()
        if rect.width() > 5 and rect.height() > 5:
            # 选区是本控件(虚拟桌面)内坐标,换算成全局屏幕逻辑坐标
            global_rect = rect.translated(self._virtual_origin)
            # 按选区左上角所在的屏取对应预抓 pixmap 及其 dpr,多屏/混合 DPI 不再错位
            shot, geo, ratio = self._shot_for(global_rect.topLeft())
            local = global_rect.translated(-geo.topLeft())   # 该屏内逻辑坐标
            scaled = QRect(
                int(local.x() * ratio), int(local.y() * ratio),
                int(local.width() * ratio), int(local.height() * ratio)
            )
            cropped = shot.copy(scaled)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            cropped.save(buffer, "PNG")
            # 发出的 rect 用全局逻辑坐标,贴图据此还原原位原大小
            self.captured.emit(bytes(buffer.data()), global_rect)
        self.close()

    def _shot_for(self, global_pt: QPoint):
        """返回包含 global_pt 的屏的 (pixmap, geometry, dpr);找不到则用第一块兜底。"""
        for s, shot, geo in self._shots:
            if geo.contains(global_pt):
                return shot, geo, s.devicePixelRatio()
        s, shot, geo = self._shots[0]
        return shot, geo, s.devicePixelRatio()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
