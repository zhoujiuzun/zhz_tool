# -*- coding: utf-8 -*-
"""贴图浮窗:把一张截图钉成置顶浮窗,用于并排对照不同页面。

与识别/翻译流水线完全独立。交互:
  - 拖动:按住浮窗任意位置拖动
  - 缩放:鼠标在浮窗上时 Ctrl + 滚轮 等比缩放
  - 关闭:右键菜单「关闭」,或按 Esc
  - 置顶:右键菜单「浮窗设置 → 始终置顶」可切换(默认置顶)
"""
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QMenu, QGraphicsDropShadowEffect
from PyQt6.QtCore import Qt, QPoint, QRect, QSize, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QColor, QActionGroup


class PinWindow(QWidget):
    closed = pyqtSignal()
    activated = pyqtSignal()        # 被点击(获得焦点)→ 协调者据此做同级"点谁谁上"
    priority_changed = pyqtSignal() # 优先级改了 → 协调者据此重排 z-order

    _MIN_SCALE = 0.1
    _MAX_SCALE = 8.0

    PRIORITY_MIN = 1                 # 1=最高
    PRIORITY_MAX = 5                 # 5=最低
    PRIORITY_DEFAULT = 3

    # 浅蓝色发光阴影:四周留透明边距供其渲染,鼠标悬停时模糊半径变大→发光面积扩大。
    _MARGIN = 32           # 透明边距,需 ≥ 悬停模糊半径,否则发光会被窗口边缘裁掉
    _BLUR_NORMAL = 16.0
    _BLUR_HOVER = 28.0
    _GLOW_COLOR = QColor(96, 165, 250)   # 浅蓝

    def __init__(self, image_bytes: bytes, rect: QRect = None):
        super().__init__()
        self._always_on_top = True
        self._priority = self.PRIORITY_DEFAULT   # 贴图优先级 1~5(1 最高),见 docs/CONTEXT.md「贴图优先级」
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 透明背景,使边距区只显示阴影发光,不显示窗口底色
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        img = QImage.fromData(image_bytes, "PNG")
        self._base_pixmap = QPixmap.fromImage(img)
        # 损坏/非 PNG 数据会得到 null pixmap,放任会显示空白窗且无提示。用 1x1 占位兜底。
        if self._base_pixmap.isNull():
            self._base_pixmap = QPixmap(1, 1)
            self._base_pixmap.fill(QColor(0, 0, 0, 0))
        self._scale = 1.0

        # 截图裁出的 pixmap 是物理像素(逻辑尺寸 × 缩放比),贴图要还原成框选时的
        # 逻辑大小/位置。dpr 由物理宽与框选逻辑宽之比推得,避免在高 DPI 屏上偏大。
        if rect is not None and rect.width() > 0:
            self._base_logical = rect.size()
            self._dpr = self._base_pixmap.width() / rect.width()
            # 窗口含边距,左上角需向外偏 margin,使图片本体落在框选原位
            self._target_pos = rect.topLeft() - QPoint(self._MARGIN, self._MARGIN)
        else:
            self._base_logical = self._base_pixmap.size()
            self._dpr = 1.0
            self._target_pos = None

        layout = QVBoxLayout(self)
        # 四周留出边距给发光阴影
        layout.setContentsMargins(self._MARGIN, self._MARGIN, self._MARGIN, self._MARGIN)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

        # 浅蓝发光阴影(offset 0 → 四周均匀光晕)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setColor(self._GLOW_COLOR)
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(self._BLUR_NORMAL)
        self._label.setGraphicsEffect(self._glow)

        # 悬停时模糊半径动画过渡
        self._glow_anim = QPropertyAnimation(self._glow, b"blurRadius", self)
        self._glow_anim.setDuration(140)
        self._glow_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._drag_offset = QPoint()
        self._apply_scale()
        if self._target_pos is not None:
            self.move(self._target_pos)

    def showEvent(self, e):
        # 首次显示后再次定位,防止窗口管理器在 show 时挪动位置
        if self._target_pos is not None:
            self.move(self._target_pos)
        super().showEvent(e)

    # ── 悬停:发光面积变化 ────────────────────────────────────────────────────
    def _animate_glow(self, target: float):
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow.blurRadius())
        self._glow_anim.setEndValue(target)
        self._glow_anim.start()

    def enterEvent(self, e):
        self._animate_glow(self._BLUR_HOVER)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._animate_glow(self._BLUR_NORMAL)
        super().leaveEvent(e)

    # ── 拖动 ────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self.activated.emit()        # 任意键按下都算"激活",协调者据此把本窗提到同级最上
        if e.button() == Qt.MouseButton.LeftButton:
            # 记录鼠标相对窗口左上角的偏移(全局坐标 - 窗口坐标)
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    # ── 缩放:Ctrl + 滚轮 ───────────────────────────────────────────────────
    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            factor = 1.1 if delta > 0 else (1 / 1.1)
            new_scale = max(self._MIN_SCALE, min(self._MAX_SCALE, self._scale * factor))
            if new_scale != self._scale:
                self._scale = new_scale
                self._apply_scale()
            e.accept()
        else:
            e.ignore()

    def _apply_scale(self):
        # 逻辑目标宽 = 框选逻辑宽 × 缩放;物理宽 = 逻辑宽 × dpr(保证高 DPI 下清晰)
        logical_w = max(1, int(self._base_logical.width() * self._scale))
        physical_w = max(1, int(logical_w * self._dpr))
        scaled = self._base_pixmap.scaledToWidth(physical_w, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(self._dpr)
        self._label.setPixmap(scaled)
        # 窗口 = 贴图逻辑尺寸 + 四周边距,使贴图本体与框选区大小一致
        logical_h = max(1, round(scaled.height() / self._dpr))
        self.setFixedSize(QSize(logical_w + 2 * self._MARGIN, logical_h + 2 * self._MARGIN))

    # ── 右键菜单 ────────────────────────────────────────────────────────────
    def contextMenuEvent(self, e):
        self.activated.emit()        # 右键也算激活
        menu = QMenu(self)
        top_act = menu.addAction("始终置顶")
        top_act.setCheckable(True)
        top_act.setChecked(self._always_on_top)
        top_act.triggered.connect(self._toggle_on_top)

        # 优先度二级菜单:1~5,1 最高、5 最低,当前档打勾(radio 互斥)
        prio_menu = menu.addMenu("优先度")
        grp = QActionGroup(prio_menu)
        grp.setExclusive(True)
        for lv in range(self.PRIORITY_MIN, self.PRIORITY_MAX + 1):
            if lv == self.PRIORITY_MIN:
                text = f"{lv}（最高）"
            elif lv == self.PRIORITY_MAX:
                text = f"{lv}（最低）"
            else:
                text = str(lv)
            act = prio_menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(lv == self._priority)
            act.triggered.connect(lambda _=False, v=lv: self.set_priority(v))
            grp.addAction(act)

        reset_act = menu.addAction("重置大小")
        reset_act.triggered.connect(self._reset_size)
        menu.addSeparator()
        close_act = menu.addAction("关闭")
        close_act.triggered.connect(self.close)
        menu.exec(e.globalPos())

    def priority(self) -> int:
        return self._priority

    def set_priority(self, level: int):
        """设贴图优先级(1~5),变化则发信号让协调者重排 z-order。"""
        level = max(self.PRIORITY_MIN, min(self.PRIORITY_MAX, int(level)))
        if level != self._priority:
            self._priority = level
            self.priority_changed.emit()

    def _toggle_on_top(self, checked: bool):
        self._always_on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()   # 改 flags 后需重新 show
        self.priority_changed.emit()   # 进/出置顶层 → 协调者重排(非置顶的不参与)

    def is_on_top(self) -> bool:
        return self._always_on_top

    def _reset_size(self):
        self._scale = 1.0
        self._apply_scale()

    # ── Esc 关闭 ────────────────────────────────────────────────────────────
    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)
