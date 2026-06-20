# -*- coding: utf-8 -*-
"""全局样式:由单一「主题色」派生整套清新风配色,生成 QSS。

用户在「设置→通用」选一个主题色,本模块据它派生 背景/按钮/悬停/选中/边框/滚动条 等
全部衍生色(正文文字固定深灰,保证任何主题色下可读)。见 CONTEXT.md「主题色」。
"""
from string import Template
from PyQt6.QtGui import QColor

# 默认主题色:logo 家族的浅蓝(清新,白字按钮仍可读)。用户可在通用里改。
DEFAULT_THEME = "#6FB3EC"

# 清新预设色(通用里一键选用):湖蓝(默认)/薄荷/藕粉/灰蓝/青/雅紫
PRESET_THEMES = [
    ("湖蓝", "#6FB3EC"),
    ("薄荷", "#5BD0AC"),
    ("藕粉", "#EE9DB0"),
    ("灰蓝", "#86A6C6"),
    ("青色", "#4ECEDC"),
    ("雅紫", "#AE94E0"),
]

# 正文/标题文字固定色(不随主题变,保可读)
_TEXT = "#2c2c2c"


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _hsl(h: float, s: float, l: float) -> str:
    """按 HSL(0~1)造色,返回 #rrggbb。"""
    return QColor.fromHslF(_clamp(h), _clamp(s), _clamp(l)).name()


def _derive(theme_hex: str) -> dict:
    """从主题色派生整套配色。无彩色输入(白/灰/黑,色相未定义)走中性灰阶,
    不再被误钳成红色(Qt 对白/灰返回 h=-1)。"""
    base = QColor(theme_hex)
    if not base.isValid():
        base = QColor(DEFAULT_THEME)
    h, s, _l, _a = base.getHslF()
    achromatic = h < 0 or s < 0.06   # 白/灰/黑:无色相
    sf = 0.0 if achromatic else 1.0  # 无彩色时各处饱和度压成 0,走灰
    if achromatic:
        h, s = 0.0, 0.0              # 不强行兜底饱和度
    else:
        s = _clamp(s, 0.35, 0.95)    # 饱和度兜底:太灰的输入也给点色相感

    # 强调色:亮度钳在可读区间(白字按钮要够深)
    accent_l = _clamp(min(_l, 0.55), 0.42, 0.55)
    accent_s = 0.0 if achromatic else max(s, 0.55)
    accent          = _hsl(h, accent_s, accent_l)
    accent_hover    = _hsl(h, accent_s, accent_l + 0.08)
    accent_pressed  = _hsl(h, accent_s, accent_l - 0.08)

    return {
        "accent": accent, "accent_hover": accent_hover, "accent_pressed": accent_pressed,
        "text": _TEXT,
        "surface": "#ffffff",                 # 输入框/表格/面板:纯白
        "bg":        _hsl(h, min(s, 0.55), 0.965),  # 主背景:极淡蓝(取代米白)
        "bg_alt":    _hsl(h, min(s, 0.45), 0.93),   # 普通按钮/表头/滚动槽/spin按钮
        "bg_alt2":   _hsl(h, min(s, 0.45), 0.88),   # 按钮悬停/Tab 未选中
        "bg_alt3":   _hsl(h, min(s, 0.45), 0.83),   # 按钮按下/spin按钮悬停
        "border":      _hsl(h, min(s, 0.40), 0.82),  # 主边框
        "border_soft": _hsl(h, min(s, 0.30), 0.92),  # 内部细分隔线
        "selected":    _hsl(h, min(s, 0.50), 0.90),  # 选中行/激活行底
        "scroll":       _hsl(h, min(s, 0.35), 0.80),
        "scroll_hover": _hsl(h, min(s, 0.40), 0.70),
        "arrow":     _hsl(h, min(s, 0.45), 0.50),    # spinbox 箭头
        "muted":     _hsl(h, 0.12 * sf, 0.55),       # info/未选中 Tab 文字
        "label":     _hsl(h, 0.25 * sf, 0.42),       # 面板标题/模块名(偏深的弱化色)
        "danger": "#d9534f", "danger_hover": "#e06560",  # 危险色不随主题
    }


# QSS 模板:${...} 占位由 _derive 的派生色填充(QSS 的 {} 字面量不受 Template 影响)。
_TEMPLATE = Template("""
QWidget {
    background-color: ${bg};
    color: ${text};
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}
QTextEdit, QLineEdit, QSpinBox {
    background-color: ${surface};
    border: 1px solid ${border};
    border-radius: 6px;
    padding: 6px 8px;
    color: ${text};
}
QTextEdit:focus, QLineEdit:focus { border: 1px solid ${accent}; }
QPushButton {
    background-color: ${bg_alt};
    border: 1px solid ${border};
    border-radius: 6px;
    padding: 6px 16px;
    color: ${text};
}
QPushButton:hover   { background-color: ${bg_alt2}; }
QPushButton:pressed { background-color: ${bg_alt3}; }
QPushButton#primary {
    background-color: ${accent};
    color: #ffffff;
    font-weight: bold;
    border: none;
}
QPushButton#primary:hover  { background-color: ${accent_hover}; }
QPushButton#primary:pressed{ background-color: ${accent_pressed}; }
QPushButton#danger {
    background-color: ${danger};
    color: #ffffff;
    border: none;
}
QPushButton#danger:hover { background-color: ${danger_hover}; }
QTableWidget {
    background-color: ${surface};
    border: 1px solid ${border};
    border-radius: 6px;
    gridline-color: ${border_soft};
}
QTableWidget::item:selected { background-color: ${selected}; color: ${text}; }
QHeaderView::section {
    background-color: ${bg_alt};
    color: ${muted};
    border: none;
    border-bottom: 1px solid ${border};
    padding: 6px;
    font-weight: bold;
}
QTabWidget::pane {
    border: 1px solid ${border};
    border-radius: 6px;
    background-color: ${bg};
}
QTabBar::tab {
    background-color: ${bg_alt2};
    color: ${muted};
    padding: 8px 24px;
    border-radius: 6px 6px 0 0;
    border: 1px solid ${border};
    border-bottom: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: ${bg};
    color: ${accent};
    font-weight: bold;
    border-bottom: 2px solid ${accent};
}
QTabBar::tab:hover:!selected { background-color: ${bg_alt3}; color: ${text}; }
QLabel#info  { color: ${muted}; font-size: 11px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid ${border};
    border-radius: 4px;
    background: ${surface};
}
QCheckBox::indicator:checked {
    background-color: ${accent}; border-color: ${accent};
    image: url("${check_icon}");   /* 勾选态再叠一个白色勾号,颜色之外多一重视觉提示 */
}
QScrollBar:vertical { background: ${bg_alt}; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: ${scroll}; border-radius: 4px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: ${scroll_hover}; }
QDialog { background-color: ${bg}; }
QMessageBox { background-color: ${bg}; }

/* ── SpinBox 上下箭头按钮 ───────────────────────────── */
QSpinBox { padding: 4px 22px 4px 8px; }
QSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 18px;
    border-left: 1px solid ${border}; border-top-right-radius: 6px; background-color: ${bg_alt};
}
QSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
    border-left: 1px solid ${border}; border-bottom-right-radius: 6px; background-color: ${bg_alt};
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background-color: ${bg_alt3}; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background-color: ${accent}; }
QSpinBox::up-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid ${arrow};
}
QSpinBox::down-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid ${arrow};
}
QSpinBox::up-arrow:hover   { border-bottom-color: ${accent}; }
QSpinBox::down-arrow:hover { border-top-color: ${accent}; }
QSpinBox::up-arrow:pressed   { border-bottom-color: #ffffff; }
QSpinBox::down-arrow:pressed { border-top-color: #ffffff; }

/* ── 分段按钮 ─────────── */
QPushButton#segment { padding: 6px 4px; border-radius: 6px; }
QPushButton#segment:checked {
    background-color: ${accent}; color: #ffffff; font-weight: bold; border: none;
}
QPushButton#segment:checked:hover { background-color: ${accent_hover}; }

/* ── 紧凑工具按钮 ───────── */
QPushButton#toolbtn { padding: 6px 0; border-radius: 6px; font-size: 14px; }
QPushButton#toolbtn[kind="primary"] {
    background-color: ${accent}; color: #ffffff; font-weight: bold; border: none;
}
QPushButton#toolbtn[kind="primary"]:hover  { background-color: ${accent_hover}; }
QPushButton#toolbtn[kind="primary"]:pressed { background-color: ${accent_pressed}; }

/* ── 宏编辑页主从布局 ─────────── */
QLabel#editTitle { color: ${text}; font-size: 15px; font-weight: bold; }
QWidget#panel { border: 1px solid ${border}; border-radius: 8px; }
QLabel#panelHeader {
    color: ${label}; font-size: 12px; font-weight: bold;
    padding: 2px 2px 6px 2px; border-bottom: 1px solid ${border_soft};
}
QWidget#panel QListWidget {
    background-color: ${surface}; border: 1px solid ${border}; border-radius: 6px;
}
QWidget#panel QListWidget::item { padding: 6px 6px; border-bottom: 1px solid ${border_soft}; }
QWidget#panel QListWidget::item:selected {
    background-color: ${selected}; color: ${text}; border-left: 3px solid ${accent};
}

/* ── 宏功能编辑区:模块盒 ───── */
QWidget#modBox { background-color: ${surface}; border: 1px solid ${border}; border-radius: 8px; }
QWidget#modRow {
    background-color: transparent; border: none;
    border-left: 3px solid transparent; border-bottom: 1px solid ${border_soft};
}
QWidget#modRow[active="true"] { background-color: ${selected}; border-left: 3px solid ${accent}; }
QWidget#modRow[lastRow="true"] { border-bottom: none; }
QLabel#modName { color: ${label}; font-size: 12px; font-weight: bold; }
QWidget#modRow[active="true"] QLabel#modName { color: ${accent_pressed}; }
QWidget#modRow QComboBox, QWidget#modRow QPushButton, QWidget#modRow QLineEdit { padding: 3px 6px; }
""")


def _check_icon_path() -> str:
    """确保 app 目录下有一个白色勾号 SVG(勾选态复选框用),返回 QSS 可用的正斜杠路径。
    纯白勾号与主题色无关,生成一次即可。"""
    import os
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        '<path d="M3.5 8.5 L6.5 11.5 L12.5 4.5" stroke="#ffffff" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
    )
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_check_mark.svg")
    try:
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != svg:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
    except OSError:
        return ""   # 写不了就退回纯色填充,不影响其余样式
    return path.replace("\\", "/")   # QSS url() 需正斜杠


def build_style(theme_hex: str = DEFAULT_THEME) -> str:
    """按主题色生成完整 QSS。供 main.py 启动与设置窗改色时调用。"""
    vals = _derive(theme_hex)
    vals["check_icon"] = _check_icon_path()
    return _TEMPLATE.substitute(vals)
