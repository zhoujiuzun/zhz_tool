# -*- coding: utf-8 -*-
"""应用内浮层提示(toast):右下角、置顶、点击穿透、自动淡出。

为什么不用托盘气泡(QSystemTrayIcon.showMessage):Win11 的专注助手/通知设置会**静默吞掉**
托盘气泡,导致 OCR「识别中」「识别失败」用户完全看不到(成功有窗、失败无声)。本控件由 app
自己绘制,不依赖系统通知,一定可见。

用法(单例,挂 TrayApp):
    toast = Toast(accent="#6FB3EC")
    toast.show_loading("识别中")          # 常驻动画点,直到下面任一被调用
    toast.show_error("识别失败:...")      # 红色,几秒后自动淡出
    toast.show_info("已完成")             # 普通,几秒后自动淡出
    toast.dismiss()                       # 立即收起
"""
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QRectF, QSize
from PyQt6.QtGui import QColor, QPainter, QFont, QFontMetrics


class Toast(QWidget):
    _LOADING_BG = QColor(40, 44, 52, 235)     # 深色:进行中
    _ERROR_BG   = QColor(180, 60, 60, 240)    # 红:失败
    _INFO_BG    = QColor(60, 120, 80, 240)    # 绿:信息/成功
    _TEXT       = QColor(245, 245, 245)
    _MARGIN     = 24        # 距屏幕右/下边缘
    _PAD_X      = 18
    _PAD_Y      = 12
    _MAX_W      = 460       # 文字最大宽度,超出换行

    def __init__(self, accent: str = "#6FB3EC"):
        super().__init__(None)
        self._accent = QColor(accent)
        self._text = ""
        self._bg = self._LOADING_BG
        self._loading = False
        self._dots = 0
        # 无边框、置顶、不抢焦点、点击穿透、工具窗(不进任务栏/Alt-Tab)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._font = QFont()
        self._font.setPointSize(11)

        # 进行中:动画点(识别中 . .. ...)。不改窗宽(按 "..." 预留),只重绘,避免抖动。
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._tick_dots)

        # 自动淡出
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(280)
        self._fade.finished.connect(self._on_fade_done)
        self._fading = False

    # ── 公开 API ──────────────────────────────────────────────────────────────
    def show_loading(self, text: str = "识别中"):
        self._enter(text, self._LOADING_BG, loading=True, auto_ms=0)

    def show_error(self, text: str, auto_ms: int = 8000):
        self._enter(text, self._ERROR_BG, loading=False, auto_ms=auto_ms)

    def show_info(self, text: str, auto_ms: int = 2500):
        self._enter(text, self._INFO_BG, loading=False, auto_ms=auto_ms)

    def set_accent(self, accent: str):
        self._accent = QColor(accent)
        if self.isVisible():
            self.update()

    def dismiss(self):
        self._dot_timer.stop()
        self._hide_timer.stop()
        self._fade.stop()
        self._fading = False
        self.hide()

    # PLACEHOLDER_INTERNAL
    def _enter(self, text, bg, loading, auto_ms):
        """切换到某种状态:设文本/颜色 → 重新排版定位 → 显示 → (可选)启动动画/自动淡出。"""
        self._text = text or ""
        self._bg = bg
        self._loading = loading
        self._dots = 0
        self._dot_timer.stop()
        self._hide_timer.stop()
        self._fade.stop()
        self._fading = False
        self.setWindowOpacity(1.0)
        self._relayout()
        self.show()
        self.raise_()
        if loading:
            self._dot_timer.start()
        if auto_ms > 0:
            self._hide_timer.start(auto_ms)

    def _measure_text(self) -> str:
        """排版用文本:进行中状态按最长(带 "...")测量,避免动画点改变窗宽导致抖动。"""
        return (self._text + "...") if self._loading else self._text

    def _relayout(self):
        """据文本算窗口尺寸(支持换行)并定位到当前屏右下角。"""
        fm = QFontMetrics(self._font)
        avail = self._MAX_W
        # 多行换行测量:flag 用 int() 包裹(PyQt6 不允许不同枚举直接 | )
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft)
        rect = fm.boundingRect(0, 0, avail, 1000, flags, self._measure_text())
        w = min(self._MAX_W, rect.width()) + 2 * self._PAD_X
        h = rect.height() + 2 * self._PAD_Y
        self.resize(int(w), int(h))
        # 定位:鼠标所在屏的「可用区」(扣掉任务栏)右下角
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        ag = screen.availableGeometry()
        x = ag.right() - self.width() - self._MARGIN
        y = ag.bottom() - self.height() - self._MARGIN
        self.move(int(x), int(y))

    def _tick_dots(self):
        self._dots = (self._dots + 1) % 4
        self.update()       # 只重绘,不改尺寸(宽度已按 "..." 预留)

    def _fade_out(self):
        self._fading = True
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_done(self):
        if self._fading:        # 仅淡出结束时真正隐藏(淡入若有则不隐)
            self.hide()
            self._fading = False

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 圆角底
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg)
        p.drawRoundedRect(self.rect(), 10, 10)
        # 左侧主题色竖条(进行中)/ 状态色已由底色表达,这里加一道强调条
        p.setBrush(self._accent if self._loading else QColor(255, 255, 255, 90))
        p.drawRoundedRect(QRectF(0, 0, 4, self.height()), 2, 2)
        # 文本(进行中追加动画点)
        text = self._text
        if self._loading:
            text = self._text + ("." * self._dots)
        p.setPen(self._TEXT)
        p.setFont(self._font)
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignVCenter) | int(Qt.AlignmentFlag.AlignLeft)
        tr = self.rect().adjusted(self._PAD_X, self._PAD_Y, -self._PAD_X, -self._PAD_Y)
        p.drawText(tr, flags, text)
        p.end()

