# -*- coding: utf-8 -*-
"""宏 Tab:多条命名宏的选择/录制/编辑/回放配置面。

职责边界:本 Tab 只负责「编辑配置 + 触发引擎」。真正的运行机器(录制钩子、
回放线程、状态)住在 TrayApp 持有的 MacroEngine 里——关掉设置窗,回放/录制
照常继续,F6/F9 任何时候可控。详见 PROJECT_NOTES 宏功能段。
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QListWidget, QSpinBox, QMessageBox, QInputDialog, QFormLayout,
    QCheckBox, QDoubleSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent
from app.config import (load_config, save_config, list_macros, load_macro,
                        save_macro, delete_macro)
from app.hotkeys import parse_hotkey
import ctypes
import ctypes.wintypes


def _build_qt_keymap() -> dict:
    """Qt 键码 → parse_hotkey 能识别的键名(F1-12 / A-Z / 0-9 / 常用功能键)。"""
    m = {}
    for i in range(1, 13):
        m[getattr(Qt.Key, f"Key_F{i}")] = f"F{i}"
    for c in range(ord("A"), ord("Z") + 1):
        m[getattr(Qt.Key, f"Key_{chr(c)}")] = chr(c)
    for d in range(10):
        m[getattr(Qt.Key, f"Key_{d}")] = str(d)
    m.update({
        Qt.Key.Key_Space: "Space", Qt.Key.Key_Return: "Enter",
        Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Tab: "Tab",
        Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End",
        Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Delete: "Delete",
        Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown",
    })
    return m


_QT_KEYMAP = _build_qt_keymap()


# vk → 可读名(用于 keytap 动作的显示)。覆盖 F1-12 / A-Z / 0-9 / 常用功能键。
def _build_vk_names() -> dict:
    m = {}
    for i in range(1, 13):
        m[0x70 + (i - 1)] = f"F{i}"
    for c in range(ord("A"), ord("Z") + 1):
        m[c] = chr(c)
    for d in range(10):
        m[0x30 + d] = str(d)
    m.update({0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc",
              0x24: "Home", 0x23: "End", 0x2D: "Insert", 0x2E: "Delete",
              0x21: "PageUp", 0x22: "PageDown",
              0x11: "Ctrl", 0x10: "Shift", 0x12: "Alt", 0x5B: "Win"})
    return m


_VK_NAMES = _build_vk_names()

# 修饰键 MOD 标志 → 对应的修饰键 vk(keytap 回放时按真实键)
_MOD_VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10, 0x0008: 0x5B}  # Ctrl/Alt/Shift/Win


def _hotkey_to_keytap(text: str):
    """把 "Ctrl+Shift+A" 解析成 {"vk":主键vk, "mods":[修饰键vk,...]}。失败返回 None。"""
    parsed = parse_hotkey(text)
    if parsed is None:
        return None
    mods_flag, vk = parsed
    mods = [mvk for flag, mvk in _MOD_VK.items() if mods_flag & flag]
    return {"vk": vk, "mods": mods}


def _keytap_label(a: dict) -> str:
    """keytap 动作 → 可读组合键文本,如 "Ctrl+Shift+A"。"""
    parts = [_VK_NAMES.get(int(m), str(m)) for m in a.get("mods", [])]
    parts.append(_VK_NAMES.get(int(a.get("vk", 0)), str(a.get("vk"))))
    return "+".join(parts)


class HotkeyEdit(QPushButton):
    """点击后捕获按键的热键设置按钮(替代手输文本框)。

    交互:点击 → 进入捕获态("按下快捷键…")→ 按下想要的键即记录。
    - 任意单键(功能键 F1-F12、数字、字母)都可直接作为热键,也可配修饰键。
    - 用单个普通字母/数字作热键时,该键的正常输入会被全局热键吞掉,需自行斟酌。
    - Esc 取消捕获,恢复原值。
    产出文本与 hotkeys.parse_hotkey 兼容(如 "1" / "F6" / "Ctrl+Shift+P")。
    """
    changed = pyqtSignal()   # 值被重新捕获后发出,供撞键检查

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent)
        self._value = value or ""
        self._capturing = False
        self.setCheckable(True)
        self.clicked.connect(self._on_clicked)
        self._render()

    def value(self) -> str:
        return self._value

    def set_value(self, v: str):
        """外部设置值(不发 changed,用于载入既有动作)。"""
        self._value = v or ""
        if not self._capturing:
            self._render()

    def _render(self):
        if self._capturing:
            self.setText("按下快捷键…(Esc 取消)")
        else:
            self.setText(self._value or "点击设置")
        self.setChecked(self._capturing)

    def _on_clicked(self):
        if not self._capturing:
            self._begin()
        else:
            self._cancel()

    def _begin(self):
        self._capturing = True
        self.grabKeyboard()
        self._render()

    def _cancel(self):
        self._end()

    def _end(self):
        if self._capturing:
            self._capturing = False
            self.releaseKeyboard()
        self._render()

    def focusOutEvent(self, event):
        # 失焦兜底:确保不会一直占着键盘
        if self._capturing:
            self._end()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._end()
            return
        # 单按修饰键不算完整热键,继续等主键
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                   Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return
        name = _QT_KEYMAP.get(key)
        if name is None:
            return  # 不支持的键,继续等
        mods = event.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        # 单键(含数字/字母/功能键)均可直接作为热键。
        # 注意:用单个普通字母/数字时,全局热键会"吞掉"该键的正常输入,自行斟酌。
        parts.append(name)
        combo = "+".join(parts)
        if parse_hotkey(combo) is None:
            self.setText("无法识别,请重按")
            return
        self._value = combo
        self._end()
        self.changed.emit()

    def keyReleaseEvent(self, event):
        if self._capturing:
            return
        super().keyReleaseEvent(event)


_BTN_NAMES = {"left": "左键", "right": "右键", "middle": "中键", "x1": "侧键1", "x2": "侧键2"}
_CLICK_ACT_NAMES = {"click": "单击", "double": "双击", "down": "按下", "up": "松开"}
_KEY_ACT_NAMES = {"tap": "敲击", "down": "按下", "up": "松开"}


def _click_act_of(a: dict) -> str:
    """click 动作的 act 子动作。兼容旧数据(无 act 时按 double 推断)。"""
    act = a.get("act")
    if act:
        return act
    return "double" if a.get("double") else "click"


def _fmt_delay(a: dict) -> str:
    d = a.get("d", 0) or 0
    return f"（等 {d:g}s）" if d else ""


def _func_and_params(a: dict) -> tuple:
    """把一条动作拆成 (功能, 参数) 两列文本,供左侧表格展示。"""
    t = a.get("t")
    if t == "move":
        rel = "(相对)" if a.get("rel") else ""
        return f"🖱 移动{rel}", f"({a.get('x')},{a.get('y')}){_fmt_delay(a)}"
    if t == "btn":
        bn = _BTN_NAMES.get(a.get("b"), a.get("b"))
        act = "按下" if a.get("down") else "抬起"
        return f"🖱 {bn}{act}", f"@({a.get('x')},{a.get('y')})"
    if t == "scroll":
        return "🖱 滚轮", f"dx={a.get('dx')} dy={a.get('dy')}{_fmt_delay(a)}"
    if t == "key":
        act = "按下" if a.get("down") else "抬起"
        return f"⌨ 键{act}", f"vk={a.get('vk')}"
    if t == "click":
        bn = _BTN_NAMES.get(a.get("b"), a.get("b"))
        kind = _CLICK_ACT_NAMES.get(_click_act_of(a), "单击")
        if a.get("x") is not None and a.get("y") is not None:
            rel = "(相对)" if a.get("rel") else ""
            pos = f"{rel}({a.get('x')},{a.get('y')})"
        else:
            pos = "当前光标"
        return f"🖱 {bn}{kind}", f"{pos}{_fmt_delay(a)}"
    if t == "keytap":
        act = _KEY_ACT_NAMES.get(a.get("act", "tap"), "敲击")
        return f"⌨ 按键{act}", f"{_keytap_label(a)}{_fmt_delay(a)}"
    if t == "wait":
        d = a.get("d", 0) or 0
        rand = a.get("rand", 0) or 0
        extra = f" + 0~{rand:g}s 随机" if rand else ""
        return "⏱ 等待", f"{d:g}s{extra}"
    return str(t), ""


def _one_line(a: dict) -> str:
    func, params = _func_and_params(a)
    return f"{func} {params}".strip()


def _summarize(actions: list) -> list:
    """把动作序列压成表格行,连续的(底层)move 折叠成一条「移动轨迹 N 点」。

    返回 [(功能, 参数, 起始动作下标, 结束动作下标+1)],下标范围用于删除整段。
    高层动作(click/keytap/wait/scroll,以及单独一条 move)各占一行,可逐条编辑。
    """
    rows = []
    i, n = 0, len(actions)
    while i < n:
        a = actions[i]
        if a.get("t") == "move":
            j = i
            while j < n and actions[j].get("t") == "move":
                j += 1
            if j - i == 1:
                func, params = _func_and_params(a)
                rows.append((func, params, i, j))
            else:
                rows.append(("🖱 移动轨迹", f"{j - i} 点", i, j))
            i = j
        else:
            func, params = _func_and_params(a)
            rows.append((func, params, i, i + 1))
            i += 1
    return rows


EDITABLE_TYPES = ("click", "keytap", "wait", "move", "scroll")

# 鼠标子动作:显示名 → click.act
_MOUSE_ACTS = [("单击", "click"), ("双击", "double"), ("按下", "down"), ("松开", "up")]
# 键盘子动作:显示名 → keytap.act
_KEY_ACTS = [("敲击", "tap"), ("按下", "down"), ("松开", "up")]
_MOUSE_BTNS = [("左键", "left"), ("右键", "right"), ("中键", "middle"),
               ("侧键1", "x1"), ("侧键2", "x2")]


class _Module(QWidget):
    """右侧一个独立动作模块:紧凑横排条(窄标签 + 字段)。

    子类实现 _build_fields(box) / load(a) / dump()->dict / validate()->(ok,msg) / reset()。
    box 是字段竖排容器(QVBoxLayout),子类往里加 QHBoxLayout 横排行。
    模块各自独立,字段互不共享。添加/更新由 ActionEditor 底部唯一按钮统一驱动:
    点进哪个模块(获焦/点击该行)→ 发 activated → 成为「当前激活模块」,底部按钮对它操作。
    """
    activated = pyqtSignal()

    TYPE = ""     # 该模块产出的动作 t
    TITLE = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modRow")
        # QWidget 子类默认不绘制样式表的 background/border,必须开 WA_StyledBackground,
        # 否则激活高亮的底色与左色条根本画不出来(肉眼看不到高亮)。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._cur_d = 0.0   # 编辑既有动作时保留其前置延迟;新建为 0
        # 扁平行:[窄标签][字段竖排],无边框无阴影——5 行同住右侧一个面板框内
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 5, 8, 5)
        root.setSpacing(6)

        label = QLabel(self.TITLE)
        label.setObjectName("modName")
        label.setFixedWidth(64)
        label.setWordWrap(True)
        root.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)

        fields = QVBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(3)
        self._build_fields(fields)
        root.addLayout(fields, 1)

        # 给自身 + 所有子控件装事件过滤器:任意处获焦/点击 → 激活本模块
        self.installEventFilter(self)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.activated.emit()
        return super().eventFilter(obj, event)

    def set_active(self, on: bool):
        """设激活态:动态属性 active 驱动 QSS 高亮(单一左色条 + 轻底色)。"""
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    # ── 子类实现 ──────────────────────────────────────────────────────────
    def _build_fields(self, form):
        raise NotImplementedError

    def load(self, a: dict):
        pass

    def dump(self) -> dict:
        raise NotImplementedError

    def validate(self):
        return True, ""

    def reset(self):
        pass


class MouseModule(_Module):
    TYPE = "click"
    TITLE = "🖱 鼠标"

    def _build_fields(self, box):
        # 行1:动作 + 按键 + 指定坐标勾选
        r1 = QHBoxLayout(); r1.setContentsMargins(0, 0, 0, 0); r1.setSpacing(4)
        self._act = QComboBox()
        for label, val in _MOUSE_ACTS:
            self._act.addItem(label, val)
        self._act.setFixedWidth(76)
        self._btn = QComboBox()
        for label, val in _MOUSE_BTNS:
            self._btn.addItem(label, val)
        self._btn.setFixedWidth(76)
        self._use_pos = QCheckBox("坐标")
        self._use_pos.setToolTip("勾选=点在指定坐标;不勾=点在当前光标处")
        r1.addWidget(self._act)
        r1.addWidget(self._btn)
        r1.addWidget(self._use_pos)
        r1.addStretch()
        box.addLayout(r1)
        # 行2:坐标 + 抓 + 相对
        r2 = QHBoxLayout(); r2.setContentsMargins(0, 0, 0, 0); r2.setSpacing(4)
        pos_row, self._x, self._y, self._grab = _make_pos_row(0, 0)
        self._rel = QCheckBox("相对")
        self._rel.setToolTip("相对坐标:以回放时光标位置为原点偏移")
        r2.addWidget(pos_row)
        r2.addWidget(self._rel)
        r2.addStretch()
        box.addLayout(r2)
        self._use_pos.toggled.connect(self._sync_pos)
        self._sync_pos(False)

    def _sync_pos(self, on: bool):
        for w in (self._x, self._y, self._grab, self._rel):
            w.setEnabled(on)

    def reset(self):
        self._act.setCurrentIndex(0)
        self._btn.setCurrentIndex(0)
        self._use_pos.setChecked(False)
        self._x.setValue(0)
        self._y.setValue(0)
        self._rel.setChecked(False)
        self._sync_pos(False)

    def load(self, a: dict):
        self._act.setCurrentIndex(max(0, self._act.findData(_click_act_of(a))))
        self._btn.setCurrentIndex(max(0, self._btn.findData(a.get("b", "left"))))
        has_pos = a.get("x") is not None and a.get("y") is not None
        self._use_pos.setChecked(has_pos)
        self._x.setValue(int(a.get("x") or 0))
        self._y.setValue(int(a.get("y") or 0))
        self._rel.setChecked(bool(a.get("rel")))
        self._sync_pos(has_pos)

    def dump(self) -> dict:
        act = {"t": "click", "b": self._btn.currentData(),
               "act": self._act.currentData(), "d": round(self._cur_d, 3)}
        if self._use_pos.isChecked():
            act["x"] = self._x.value()
            act["y"] = self._y.value()
            act["rel"] = self._rel.isChecked()
        return act


class KeyboardModule(_Module):
    TYPE = "keytap"
    TITLE = "⌨ 键盘"

    def _build_fields(self, box):
        r = QHBoxLayout(); r.setContentsMargins(0, 0, 0, 0); r.setSpacing(4)
        self._edit = HotkeyEdit("")
        self._edit.setToolTip("点按钮后按下想要的键(可带 Ctrl/Alt/Shift)")
        self._act = QComboBox()
        for label, val in _KEY_ACTS:
            self._act.addItem(label, val)
        self._act.setFixedWidth(76)
        self._act.setToolTip("敲击=按下并松开;按下/松开仅作用主键,不含修饰键")
        r.addWidget(self._edit, 1)
        r.addWidget(self._act)
        box.addLayout(r)

    def reset(self):
        self._edit.set_value("")
        self._act.setCurrentIndex(0)

    def load(self, a: dict):
        self._edit.set_value(_keytap_label(a))
        self._act.setCurrentIndex(max(0, self._act.findData(a.get("act", "tap"))))

    def dump(self) -> dict:
        kt = _hotkey_to_keytap(self._edit.value()) or {"vk": 0, "mods": []}
        return {"t": "keytap", "vk": kt["vk"], "mods": kt["mods"],
                "act": self._act.currentData(), "d": round(self._cur_d, 3)}

    def validate(self):
        if not self._edit.value().strip():
            return False, "请先设置按键组合(点「点击设置」后按键)。"
        return True, ""


class MoveModule(_Module):
    TYPE = "move"
    TITLE = "🖱 移动"

    def _build_fields(self, box):
        r = QHBoxLayout(); r.setContentsMargins(0, 0, 0, 0); r.setSpacing(4)
        pos_row, self._x, self._y, _ = _make_pos_row(0, 0)
        self._rel = QCheckBox("相对")
        self._rel.setToolTip("相对坐标:以回放时光标位置为原点偏移")
        r.addWidget(pos_row)
        r.addWidget(self._rel)
        r.addStretch()
        box.addLayout(r)

    def reset(self):
        self._x.setValue(0)
        self._y.setValue(0)
        self._rel.setChecked(False)

    def load(self, a: dict):
        self._x.setValue(int(a.get("x", 0)))
        self._y.setValue(int(a.get("y", 0)))
        self._rel.setChecked(bool(a.get("rel")))

    def dump(self) -> dict:
        return {"t": "move", "x": self._x.value(), "y": self._y.value(),
                "rel": self._rel.isChecked(), "d": round(self._cur_d, 3)}


class ScrollModule(_Module):
    TYPE = "scroll"
    TITLE = "🖱 滚轮"

    def _build_fields(self, box):
        r = QHBoxLayout(); r.setContentsMargins(0, 0, 0, 0); r.setSpacing(4)
        self._dy = QSpinBox()
        self._dy.setRange(-100, 100)
        self._dy.setFixedWidth(72)
        self._dy.setToolTip("垂直滚动:正=上,负=下")
        self._dx = QSpinBox()
        self._dx.setRange(-100, 100)
        self._dx.setFixedWidth(72)
        self._dx.setToolTip("水平滚动:正=右,负=左")
        r.addWidget(QLabel("垂直"))
        r.addWidget(self._dy)
        r.addWidget(QLabel("水平"))
        r.addWidget(self._dx)
        r.addStretch()
        box.addLayout(r)

    def reset(self):
        self._dy.setValue(0)
        self._dx.setValue(0)

    def load(self, a: dict):
        self._dy.setValue(int(a.get("dy", 0)))
        self._dx.setValue(int(a.get("dx", 0)))

    def dump(self) -> dict:
        return {"t": "scroll", "dx": self._dx.value(), "dy": self._dy.value(),
                "d": round(self._cur_d, 3)}


class WaitModule(_Module):
    TYPE = "wait"
    TITLE = "⏱ 等待"

    def _build_fields(self, box):
        r = QHBoxLayout(); r.setContentsMargins(0, 0, 0, 0); r.setSpacing(4)
        self._secs = QDoubleSpinBox()
        self._secs.setRange(0.0, 3600.0)
        self._secs.setDecimals(3)
        self._secs.setSingleStep(0.1)
        self._secs.setFixedWidth(88)
        self._secs.setToolTip("等待时长(秒)")
        self._rand = QDoubleSpinBox()
        self._rand.setRange(0.0, 3600.0)
        self._rand.setDecimals(3)
        self._rand.setSingleStep(0.1)
        self._rand.setFixedWidth(88)
        self._rand.setToolTip("随机上限(秒):实际等待 = 时长 + 0~此值的随机量(0=不随机)")
        r.addWidget(QLabel("时长"))
        r.addWidget(self._secs)
        r.addWidget(QLabel("± 随机"))
        r.addWidget(self._rand)
        r.addStretch()
        box.addLayout(r)

    def reset(self):
        self._secs.setValue(0.0)
        self._rand.setValue(0.0)

    def load(self, a: dict):
        self._secs.setValue(float(a.get("d", 0) or 0))
        self._rand.setValue(float(a.get("rand", 0) or 0))

    def dump(self) -> dict:
        return {"t": "wait", "d": round(self._secs.value(), 3),
                "rand": round(self._rand.value(), 3)}


class ActionEditor(QWidget):
    """右侧功能编辑区:所有模块全展开(无切换器、无滚动),底部一个统一按钮。

    点进哪个模块(获焦/点击)→ 该模块「激活」(高亮),底部按钮对当前激活模块操作。
    新建态底部为「添加动作」;编辑态(begin_edit)为「更新此动作」+「取消编辑」,
    且编辑期可切到别的模块再更新 = 把该条改成别的类型。

    对外信号:add_requested(dict) / update_requested(dict) / cancel_requested() / invalid(str)。
    """
    add_requested = pyqtSignal(dict)
    update_requested = pyqtSignal(dict)
    cancel_requested = pyqtSignal()
    invalid = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modules = [MouseModule(), KeyboardModule(), MoveModule(),
                         ScrollModule(), WaitModule()]
        self._by_type = {m.TYPE: m for m in self._modules}
        self._active = self._modules[0]   # 当前激活模块(底部按钮的作用对象)
        self._editing = False             # 是否在编辑既有动作
        self._edit_d = 0.0                # 编辑期保管的前置延迟(切类型也保留)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # 一个白底圆角边框盒子,把 5 个模块行整个包进来(与左侧列表盒对称)
        box = QWidget()
        box.setObjectName("modBox")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)   # 同上:否则白底+边框画不出
        box_lay = QVBoxLayout(box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(0)
        for i, m in enumerate(self._modules):
            m.activated.connect(lambda mod=m: self._set_active(mod))
            if i == len(self._modules) - 1:
                m.setProperty("lastRow", "true")   # 末行去掉底分隔线,避免悬空
            box_lay.addWidget(m)
        box_lay.addStretch()
        root.addWidget(box, 1)

        # 底部统一按钮行(在盒子外、下方),节省每行按钮空间,进编辑时窗口不必变大
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addStretch()
        self._cancel_btn = QPushButton("取消编辑")
        self._cancel_btn.clicked.connect(self.cancel_requested.emit)
        self._add_btn = QPushButton("＋ 添加动作")
        self._add_btn.setObjectName("primary")
        self._add_btn.clicked.connect(self._on_add)
        self._update_btn = QPushButton("✓ 更新此动作")
        self._update_btn.setObjectName("primary")
        self._update_btn.clicked.connect(self._on_update)
        for b in (self._cancel_btn, self._add_btn, self._update_btn):
            bar.addWidget(b)
        root.addLayout(bar)

        self._set_active(self._modules[0])
        self.begin_add()

    def _set_active(self, mod):
        self._active = mod
        for m in self._modules:
            m.set_active(m is mod)
        self._refresh_buttons()

    def _refresh_buttons(self):
        """按钮文字带上当前激活模块名,让「将要添加/更新哪个动作」一目了然。"""
        name = self._active.TITLE if self._active else ""
        self._add_btn.setText(f"＋ 添加：{name}")
        self._update_btn.setText(f"✓ 更新为：{name}")

    def _on_add(self):
        ok, msg = self._active.validate()
        if not ok:
            self.invalid.emit(msg)
            return
        self._active._cur_d = 0.0   # 手动新增无前置延迟
        self.add_requested.emit(self._active.dump())

    def _on_update(self):
        ok, msg = self._active.validate()
        if not ok:
            self.invalid.emit(msg)
            return
        self._active._cur_d = self._edit_d   # 切类型也保留原前置延迟
        self.update_requested.emit(self._active.dump())

    def _set_buttons(self, editing: bool):
        self._editing = editing
        self._add_btn.setVisible(not editing)
        self._update_btn.setVisible(editing)
        self._cancel_btn.setVisible(editing)
        self._refresh_buttons()

    def begin_add(self):
        """新建态:底部显示「添加动作」(不重置字段,便于连续添加)。"""
        self._editing = False
        self._set_buttons(editing=False)

    def begin_edit(self, a: dict) -> bool:
        """编辑态:载入 a 到其模块并激活,底部显示「更新/取消」。
        不锁类型——可切到别的模块再更新 = 改类型。不可编辑类型返回 False。"""
        m = self._by_type.get(a.get("t"))
        if m is None:
            return False
        self._edit_d = float(a.get("d", 0) or 0)
        m._cur_d = self._edit_d
        m.load(a)
        self._set_active(m)
        self._set_buttons(editing=True)
        return True

    def reset(self):
        for m in self._modules:
            m.reset()
        self._set_active(self._modules[0])
        self.begin_add()


def _make_pos_row(x: int, y: int):
    """构造紧凑的 [X spin][Y spin][抓] 行,返回 (row_widget, x_spin, y_spin, grab_btn)。"""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(3)
    # 屏幕坐标 4-5 位足够,限窄宽,紧凑横排
    xs = QSpinBox(); xs.setRange(0, 100000); xs.setValue(int(x or 0)); xs.setFixedWidth(70)
    ys = QSpinBox(); ys.setRange(0, 100000); ys.setValue(int(y or 0)); ys.setFixedWidth(70)
    grab = QPushButton("抓")
    grab.setFixedWidth(28)
    grab.setToolTip("点击后 1.5 秒内把鼠标移到目标位置,自动填入坐标")

    def do_grab():
        grab.setText("…")
        grab.setEnabled(False)

        def capture():
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            xs.setValue(pt.x); ys.setValue(pt.y)
            grab.setText("抓"); grab.setEnabled(True)
        QTimer.singleShot(1500, capture)

    grab.clicked.connect(do_grab)
    lx = QLabel("X"); ly = QLabel("Y")
    h.addWidget(lx); h.addWidget(xs)
    h.addWidget(ly); h.addWidget(ys)
    h.addWidget(grab)
    return row, xs, ys, grab


class MacroTab(QWidget):
    """engine: TrayApp 持有的 MacroEngine;hotkey_mgr: HotkeyManager。"""
    changed = pyqtSignal()   # 全局宏设置变更(总开关/选中/F9/每宏热键/循环)→ 供设置窗实时写盘 + tray 重注册热键

    def __init__(self, engine, hotkey_mgr, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._hotkey_mgr = hotkey_mgr
        self._cfg = load_config().get("macro", {})
        self._rows = []        # _summarize 的结果,行→动作下标范围
        self._actions = []     # 当前选中宏的动作序列
        self._editing_idx = None  # 当前在右侧编辑的既有动作下标(None=新建态)
        self._loading_macro = False  # 载入选中宏的设置时抑制 persist 回写
        self._last_play_key = ""     # 上次被接受的本宏回放热键(撞键时回退用)
        self._prev_name = ""         # flush() 写盘的目标宏名(切宏前仍指向旧宏)
        self._dirty = False          # 当前宏的热键/循环有未落盘改动

        # 两个页面:管理页(总览)+ 编辑页(主从布局)
        self._pages = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._pages)
        self._pages.addWidget(self._build_manage_page())  # 0
        self._pages.addWidget(self._build_edit_page())    # 1

        self._sync_loop_visibility()
        # 初始化:填充宏列表 + 接引擎状态/录制结果信号 + 初始按钮态
        self._reload_macro_list()
        self._engine.state_changed.connect(self._on_state)
        self._engine.recorded.connect(self._on_recorded)
        self._refresh_buttons()
        self._check_hotkey_conflict()

    # ── 管理页(总览) ───────────────────────────────────────────────────────
    def _build_manage_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        # 宏总开关:关闭时不注册回放热键,避免干扰正常键鼠操作
        self._enabled = QCheckBox("启用宏(开启后才注册回放热键,避免平时误触)")
        self._enabled.setChecked(self._cfg.get("enabled", False))
        self._enabled.toggled.connect(self._on_enabled_toggled)
        root.addWidget(self._enabled)

        # 宏选择器 + 新建/删除
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("宏"))
        self._combo = QComboBox()
        self._combo.currentTextChanged.connect(self._on_select)
        sel_row.addWidget(self._combo, 1)
        new_btn = QPushButton("新建")
        new_btn.clicked.connect(self._new_macro)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._delete_macro)
        sel_row.addWidget(new_btn)
        sel_row.addWidget(del_btn)
        root.addLayout(sel_row)

        # 动作概览:条数
        self._count_label = QLabel("共 0 条动作")
        self._count_label.setObjectName("info")
        root.addWidget(self._count_label)

        # 录制 / 回放 / 编辑 控制(同一排)
        ctrl_row = QHBoxLayout()
        self._record_btn = QPushButton("● 录制")
        self._record_btn.clicked.connect(self._toggle_record)
        self._play_btn = QPushButton("▶ 回放")
        self._play_btn.setObjectName("primary")
        self._play_btn.clicked.connect(self._toggle_play)
        edit_btn = QPushButton("✎ 编辑动作")
        edit_btn.clicked.connect(self._enter_edit_mode)
        ctrl_row.addWidget(self._record_btn)
        ctrl_row.addWidget(self._play_btn)
        ctrl_row.addWidget(edit_btn)
        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # 循环 + 热键设置(回放热键 + 循环均**每条宏各自独立**,随选中宏载入/存盘)
        form = QFormLayout()
        loop_row = QHBoxLayout()
        self._loop = QComboBox()
        self._loop.addItem("播放一次", "once")
        self._loop.addItem("固定次数", "count")
        self._loop.addItem("无限(按热键停)", "infinite")
        self._loop.setCurrentIndex(0)
        self._loop.currentIndexChanged.connect(self._sync_loop_visibility)
        self._loop.currentIndexChanged.connect(self._persist_macro_settings)
        loop_row.addWidget(self._loop)
        self._count = QSpinBox()
        self._count.setRange(1, 99999)
        self._count.setValue(1)
        self._count.valueChanged.connect(self._persist_macro_settings)
        loop_row.addWidget(self._count)
        loop_row.addStretch()
        form.addRow("循环", loop_row)

        self._play_key = HotkeyEdit("")
        self._play_key.changed.connect(self._on_play_key_changed)
        form.addRow("本宏回放热键", self._play_key)
        self._stop_key = HotkeyEdit(self._cfg.get("stop_record_hotkey", "F9"))
        self._stop_key.changed.connect(self._check_hotkey_conflict)
        form.addRow("启停录制热键(全局)", self._stop_key)
        root.addLayout(form)

        self._status = QLabel("")
        self._status.setObjectName("info")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch()
        return page

    # ── 编辑页(主从:左动作列表 / 右编辑器) ──────────────────────────────────
    def _build_edit_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        # 留正常边距(与管理页一致),否则顶部「返回 + 标题」会贴到窗口最上沿
        root.setContentsMargins(9, 9, 9, 9)
        root.setSpacing(10)

        # 顶部条:返回 + 标题
        top = QHBoxLayout()
        back_btn = QPushButton("←  返回")
        back_btn.clicked.connect(self._exit_edit_mode)
        top.addWidget(back_btn)
        self._edit_title = QLabel("")
        self._edit_title.setObjectName("editTitle")
        top.addWidget(self._edit_title, 1)
        root.addLayout(top)

        # 主体:左动作列表面板 + 右编辑器面板
        body = QHBoxLayout()
        body.setSpacing(6)

        # 左:动作序列面板(3 列表格:序号 / 功能 / 参数)
        left_panel = QWidget()
        left_panel.setObjectName("panel")
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(6, 6, 6, 6)
        left.addWidget(self._panel_header("宏列表"))
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["#", "功能", "参数"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumWidth(180)
        self._table.currentCellChanged.connect(
            lambda cur, *_: self._on_list_row_changed(cur))
        left.addWidget(self._table, 1)
        # 工具条:删除 / 上移 / 下移 / 清空(添加/更新在右侧编辑区)
        lbtns = QHBoxLayout()
        lbtns.setSpacing(4)
        del_act = QPushButton("删")
        del_act.setObjectName("toolbtn")
        del_act.setToolTip("删除选中")
        del_act.clicked.connect(self._delete_selected)
        up_act = QPushButton("↑")
        up_act.setObjectName("toolbtn")
        up_act.setToolTip("上移")
        up_act.clicked.connect(lambda: self._move_row(-1))
        down_act = QPushButton("↓")
        down_act.setObjectName("toolbtn")
        down_act.setToolTip("下移")
        down_act.clicked.connect(lambda: self._move_row(1))
        clear_act = QPushButton("清")
        clear_act.setObjectName("toolbtn")
        clear_act.setToolTip("清空全部")
        clear_act.clicked.connect(self._clear_actions)
        for b in (del_act, up_act, down_act, clear_act):
            b.setFixedWidth(30)
            lbtns.addWidget(b)
        lbtns.addStretch()
        left.addLayout(lbtns)
        body.addWidget(left_panel, 2)

        # 右:功能编辑区(所有模块纵向全展开,各自带添加按钮)
        right_panel = QWidget()
        right_panel.setObjectName("panel")
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(6, 6, 6, 6)
        right.addWidget(self._panel_header("功能编辑区"))
        # 选中不可编辑动作时的提示(平时为空)
        self._edit_hint = QLabel("")
        self._edit_hint.setObjectName("info")
        self._edit_hint.setWordWrap(True)
        self._edit_hint.setVisible(False)
        right.addWidget(self._edit_hint)
        self._editor = ActionEditor()
        self._editor.add_requested.connect(self._on_module_add)
        self._editor.update_requested.connect(self._on_module_update)
        self._editor.cancel_requested.connect(self._cancel_edit)
        self._editor.invalid.connect(self._on_module_invalid)
        right.addWidget(self._editor, 1)
        body.addWidget(right_panel, 3)

        root.addLayout(body, 1)
        return page

    @staticmethod
    def _panel_header(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("panelHeader")
        return lbl


    def _enter_edit_mode(self):
        if not self._combo.currentText():
            self._status.setText("请先新建或选择一个宏。")
            return
        self._edit_title.setText(f"编辑宏「{self._combo.currentText()}」的动作")
        self._render_actions()
        self._begin_add_mode()
        self._pages.setCurrentIndex(1)

    def _exit_edit_mode(self):
        self._pages.setCurrentIndex(0)
        self._update_count_label()

    # ── 编辑/新建态切换 ──────────────────────────────────────────────────────
    def _begin_add_mode(self):
        """回到「新建」态:清左侧选中,所有模块回添加态。"""
        self._editing_idx = None
        self._edit_hint.setVisible(False)
        self._table.blockSignals(True)
        self._table.setCurrentCell(-1, -1)
        self._table.clearSelection()
        self._table.blockSignals(False)
        self._editor.begin_add()

    # ── 宏列表 ────────────────────────────────────────────────────────────
    def _reload_macro_list(self):
        self._combo.blockSignals(True)
        self._combo.clear()
        names = list_macros()
        self._combo.addItems(names)
        current = self._cfg.get("current", "")
        if current in names:
            self._combo.setCurrentText(current)
        self._combo.blockSignals(False)
        self._on_select(self._combo.currentText())

    def _on_select(self, name: str):
        # 切宏前先把旧宏(此刻控件仍是它的值)未落盘的改动 flush,避免切走即丢
        self.flush()
        if name:
            macro = load_macro(name)
            self._actions = macro.get("actions", [])
            # 载入该宏自己的回放热键 + 循环设置(抑制 persist,避免载入即回写)
            self._loading_macro = True
            try:
                self._play_key.set_value(macro.get("hotkey", ""))
                self._last_play_key = macro.get("hotkey", "")
                self._loop.setCurrentIndex(max(0, self._loop.findData(macro.get("loop_mode", "once"))))
                self._count.setValue(int(macro.get("loop_count", 1) or 1))
                self._sync_loop_visibility()
            finally:
                self._loading_macro = False
        else:
            self._actions = []
            self._loading_macro = True
            try:
                self._play_key.set_value("")
                self._last_play_key = ""
                self._loop.setCurrentIndex(0)
                self._count.setValue(1)
            finally:
                self._loading_macro = False
        self._prev_name = name   # flush 的目标随之指向新宏
        self._render_actions()
        # 当前选中宏变了 → 全局 current 需写盘(emit;载入/初始化期由设置窗 _loaded 护栏挡掉)
        self.changed.emit()

    def _new_macro(self):
        name, ok = QInputDialog.getText(self, "新建宏", "宏名称")
        name = name.strip()
        if not ok or not name:
            return
        if name in list_macros():
            QMessageBox.warning(self, "提示", "已存在同名宏。")
            return
        save_macro(name, {"name": name, "screen": [0, 0], "actions": []})
        self._reload_macro_list()
        self._combo.setCurrentText(name)

    def _delete_macro(self):
        name = self._combo.currentText()
        if not name:
            return
        if QMessageBox.question(self, "删除", f"确定删除宏「{name}」?") \
                != QMessageBox.StandardButton.Yes:
            return
        delete_macro(name)
        self._reload_macro_list()

    # ── 动作列表 ──────────────────────────────────────────────────────────
    def _render_actions(self, keep_row: int = None):
        self._rows = _summarize(self._actions)
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._rows))
        for i, (func, params, _, _) in enumerate(self._rows):
            num = QTableWidgetItem(str(i + 1))
            num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 0, num)
            self._table.setItem(i, 1, QTableWidgetItem(func))
            self._table.setItem(i, 2, QTableWidgetItem(params))
        self._table.blockSignals(False)
        self._update_count_label()
        if keep_row is not None and 0 <= keep_row < len(self._rows):
            self._table.setCurrentCell(keep_row, 1)

    def _update_count_label(self):
        if hasattr(self, "_count_label"):
            self._count_label.setText(f"共 {len(self._actions)} 条动作")

    def _on_list_row_changed(self, row: int):
        """左侧选中变化:可编辑动作 → 对应模块进更新态;否则提示 + 回添加态。"""
        if row < 0 or row >= len(self._rows):
            self._begin_add_mode()
            return
        _, _, start, end = self._rows[row]
        if end - start != 1:
            # 折叠的录制轨迹段:不可逐条编辑
            self._editing_idx = None
            self._editor.begin_add()
            self._show_edit_hint(f"这是录制的移动轨迹段({end - start} 点),"
                                 "不可逐条编辑,可整段删除。")
            return
        a = self._actions[start]
        self._editing_idx = start
        if not self._editor.begin_edit(a):
            # 录制的底层单条 btn/key:不可编辑
            self._editing_idx = None
            self._editor.begin_add()
            self._show_edit_hint("这是录制的底层动作,不支持逐条编辑,可整段删除。")
            return
        self._edit_hint.setVisible(False)

    def _show_edit_hint(self, msg: str):
        self._edit_hint.setText(msg)
        self._edit_hint.setVisible(True)

    def _persist_actions(self):
        """把当前动作序列写回选中宏的文件(整批写一次,非每字段写)。"""
        name = self._combo.currentText()
        if not name:
            return
        macro = load_macro(name)
        macro["actions"] = self._actions
        save_macro(name, macro)

    def _on_module_add(self, action: dict):
        """某模块点「添加」:把该动作追加到序列末尾,停在新建态便于连续添加。"""
        if not self._combo.currentText():
            self._status.setText("请先新建或选择一个宏。")
            return
        self._actions.append(action)
        self._persist_actions()
        self._render_actions()
        self._begin_add_mode()
        self._status.setText("已添加一条动作。")

    def _on_module_update(self, action: dict):
        """某模块点「更新此动作」:写回正在编辑的那条。"""
        if self._editing_idx is None or not (0 <= self._editing_idx < len(self._actions)):
            return
        idx = self._editing_idx
        self._actions[idx] = action
        self._persist_actions()
        # 重渲染并定位回该动作所在行(保持选中 = 留在更新态)
        self._rows = _summarize(self._actions)
        row = next((i for i, (_, _, s, e) in enumerate(self._rows) if s <= idx < e), None)
        self._render_actions(keep_row=row)
        self._status.setText("已更新此动作。")

    def _on_module_invalid(self, msg: str):
        self._status.setText(f"⚠ {msg}")

    def _cancel_edit(self):
        """取消编辑既有动作:回到新建态(不改序列)。"""
        self._begin_add_mode()

    def _delete_selected(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rows):
            return
        _, _, start, end = self._rows[row]
        del self._actions[start:end]
        self._persist_actions()
        new_rows = _summarize(self._actions)
        if new_rows:
            self._render_actions(keep_row=min(row, len(new_rows) - 1))
            self._on_list_row_changed(self._table.currentRow())
        else:
            self._render_actions()
            self._begin_add_mode()

    def _move_row(self, delta: int):
        """上移/下移选中行(整段移动,与相邻段交换)。"""
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rows):
            return
        tgt = row + delta
        if tgt < 0 or tgt >= len(self._rows):
            return
        _, _, s1, e1 = self._rows[row]
        _, _, s2, e2 = self._rows[tgt]
        seg1 = self._actions[s1:e1]
        seg2 = self._actions[s2:e2]
        if delta < 0:   # 与上一段交换
            self._actions[s2:e1] = seg1 + seg2
        else:           # 与下一段交换
            self._actions[s1:e2] = seg2 + seg1
        self._persist_actions()
        self._render_actions(keep_row=tgt)
        self._on_list_row_changed(self._table.currentRow())

    def _clear_actions(self):
        if not self._actions:
            return
        if QMessageBox.question(self, "清空", "清空当前宏的所有动作?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._actions = []
        self._persist_actions()
        self._render_actions()
        self._begin_add_mode()

    # ── 录制 / 回放 ────────────────────────────────────────────────────────
    def _toggle_record(self):
        if self._engine.state == "recording":
            self._engine.stop_record()
            return
        if self._engine.state != "idle":
            return
        if not self._combo.currentText():
            self._status.setText("请先新建或选择一个宏。")
            return
        # 录制时把控制热键排除,否则按它们会被录进序列:全局 F9 + 本宏自己的回放热键(若设了)
        ignore = set()
        for text in (self._stop_key.value().strip(), self._play_key.value().strip()):
            if not text:
                continue
            parsed = parse_hotkey(text)
            if parsed:
                ignore.add(parsed[1])
        self._save_settings()  # 录制前先把热键等设置存好
        try:
            self._engine.start_record(ignore_vks=ignore)
        except Exception as e:
            # pynput 钩子起不来(权限/环境受限)等:给反馈而不是静默/崩溃
            self._status.setText(f"⚠ 无法开始录制:{e}")

    def _toggle_play(self):
        if self._engine.state == "playing":
            self._engine.stop_play()
            return
        if self._engine.state != "idle":
            return
        if not self._actions:
            self._status.setText("当前宏没有动作,先录一段。")
            return
        self._save_settings()
        self._engine.toggle_play(self._actions,
                                 self._loop.currentData(), self._count.value())

    def _on_recorded(self, actions: list, screen: list):
        """引擎录制结束回调:存进当前宏文件并刷新列表。

        先读回宏(保留其 hotkey/loop 等每宏字段),只更新 actions/screen,避免冲掉热键。
        """
        name = self._combo.currentText()
        if not name:
            return
        self._actions = actions
        macro = load_macro(name)
        macro["name"] = name
        macro["screen"] = screen
        macro["actions"] = actions
        save_macro(name, macro)
        self._render_actions()

    def _on_state(self, state: str):
        self._refresh_buttons()
        if state == "recording":
            stop_text = self._stop_key.value().strip() or "F9"
            self._status.setText(f"录制中… 按 {stop_text} 停止录制(鼠标键盘正在被记录)")
        elif state == "playing":
            self._status.setText("回放中… 按回放热键或点回放按钮停止")
        else:
            self._status.setText("")

    def _refresh_buttons(self):
        st = self._engine.state
        on = self._enabled.isChecked()
        self._record_btn.setText("■ 停止录制" if st == "recording" else "● 录制")
        self._play_btn.setText("■ 停止回放" if st == "playing" else "▶ 回放")
        # 总开关关闭时禁用录制/回放(但运行中仍允许停止);
        # 总开关开启时:录制中禁用回放,回放中禁用录制。
        self._record_btn.setEnabled((on or st == "recording") and st != "playing")
        self._play_btn.setEnabled((on or st == "playing") and st != "recording")

    def _on_enabled_toggled(self, checked: bool):
        """总开关切换:即时更新控件可用态 + emit changed(设置窗写盘、托盘即时注册/注销热键)。"""
        self._refresh_buttons()
        if checked:
            self._status.setText("宏已启用,回放热键即时生效。")
        else:
            self._status.setText("宏已关闭,已不再注册回放热键,不干扰正常操作。")
        self.changed.emit()

    # ── 设置 ──────────────────────────────────────────────────────────────
    def _sync_loop_visibility(self):
        self._count.setVisible(self._loop.currentData() == "count")

    def _other_macro_hotkeys(self) -> dict:
        """除当前选中宏外,其他宏已占用的回放热键 {小写键: 宏名}。"""
        cur = self._combo.currentText()
        out = {}
        for n in list_macros():
            if n == cur:
                continue
            hk = (load_macro(n).get("hotkey") or "").strip()
            if hk:
                out[hk.lower()] = n
        return out

    def _on_play_key_changed(self):
        """本宏回放热键变更:撞键(与全局 F9 或其他宏)则拒绝并回退,否则存进本宏文件。"""
        if self._loading_macro:
            return
        new = self._play_key.value().strip()
        if new:
            stop = self._stop_key.value().strip()
            if stop and new.lower() == stop.lower():
                self._play_key.set_value(self._last_play_key)
                self._status.setText(f"⚠ 「{new}」已是启停录制热键,换一个键。")
                return
            clash = self._other_macro_hotkeys().get(new.lower())
            if clash:
                self._play_key.set_value(self._last_play_key)
                self._status.setText(f"⚠ 「{new}」已被宏「{clash}」占用,换一个键。")
                return
        self._last_play_key = new
        self._persist_macro_settings()
        if self._status.text().startswith("⚠"):
            self._status.setText("")

    def _persist_macro_settings(self):
        """本宏热键/循环变更:只标脏 + emit changed(走设置窗防抖),不立即写盘。

        实际落盘由 flush() 在防抖到点/切宏/关窗/录放前统一做,避免每次微改都重写大文件。
        """
        if self._loading_macro:
            return
        self._dirty = True
        self.changed.emit()

    def flush(self):
        """把当前控件的热键/循环写回 _prev_name 的宏文件(仅当有脏改动)。

        _prev_name 指向「控件当前所属的宏」——切宏时在载入新宏前先 flush,故此处写的是旧宏。
        """
        if not self._dirty:
            return
        name = self._prev_name
        self._dirty = False
        if not name:
            return
        macro = load_macro(name)
        macro["hotkey"] = self._play_key.value().strip()
        macro["loop_mode"] = self._loop.currentData()
        macro["loop_count"] = self._count.value()
        save_macro(name, macro)

    def _check_hotkey_conflict(self):
        """全局 F9(启停录制)变更:与任何宏的回放热键撞键则提示(仅警告,仍生效)。"""
        s = self._stop_key.value().strip()
        self.changed.emit()   # F9 变了:写盘 + 托盘按新 F9 重注册录制键
        if not s:
            return
        for n in list_macros():
            hk = (load_macro(n).get("hotkey") or "").strip()
            if hk and hk.lower() == s.lower():
                self._status.setText(f"⚠ 启停录制热键「{s}」与宏「{n}」的回放热键相同,建议改一个。")
                return
        if self._status.text().startswith("⚠"):
            self._status.setText("")

    def _macro_settings(self) -> dict:
        """全局宏设置(总开关 + 当前选中宏 + 全局 F9)。回放热键/循环已下放到各宏文件。"""
        return {
            "enabled": self._enabled.isChecked(),
            "current": self._combo.currentText(),
            "stop_record_hotkey": self._stop_key.value().strip() or "F9",
        }

    def _save_settings(self):
        """录制/回放前立即落盘(不防抖,马上要用):本宏热键/循环 flush + 全局段写 config。"""
        self.flush()
        data = load_config()
        data["macro"] = self._macro_settings()
        save_config(data)

    def collect(self) -> dict:
        """供设置窗写全局段时调用,只返回全局 dict。

        **不可在此调 _persist_macro_settings()**:它会 emit changed → 设置窗 _persist
        → 又调本 collect → 无限递归爆栈崩溃。每宏文件的热键/循环已由控件变更回调即时写盘,
        这里无需再写。
        """
        return self._macro_settings()

