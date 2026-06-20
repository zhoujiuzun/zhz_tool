# -*- coding: utf-8 -*-
"""设置窗:管理 OCR 接口 / 翻译接口 / 通用配置。

OCR 接口与翻译接口共用 ProviderTab + ProviderDialog,通过 kind 区分
("ocr" / "translation")。两个 Tab 各自持有表格、连通状态轮询与测试。
"""
import uuid
import json
import threading
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QLineEdit, QCheckBox,
    QSpinBox, QTextEdit, QMessageBox, QHeaderView, QDialog,
    QFormLayout, QDialogButtonBox, QAbstractItemView, QColorDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from app.config import load_config, save_config
from app.version import __version__, APP_NAME, GITHUB_URL, GITHUB_RELEASES_URL
from app.updater import UpdateChecker
from app.providers import build_provider, OCRProvider, ocr_fields_for
from app.translators import build_translator, translation_fields_for
from app.dispatch import humanize_error
from app.engines import (warmup_ocr, warmup_translation,
                         get_ocr_probe_status, get_translation_probe_status)
from app.style import build_style, DEFAULT_THEME, PRESET_THEMES

# 每个 kind 的配置:数据键、warmup 函数、状态函数、字段查询、是否允许自定义接口
_KIND_SPEC = {
    "ocr": {
        "data_key": "providers",
        "warmup": warmup_ocr,
        "status": get_ocr_probe_status,
        "fields_for": ocr_fields_for,
        # 不显示「添加」按钮:各商业 OCR 厂家参数各异(appid/签名/轮询…),通用自定义表单
        # (单一 JSON 模板 + Bearer)表达不了,该走内置接口(providers.py 写类 + 入 _REGISTRY)。
        # CustomOCR 类与已有自定义配置仍保留可用,改回 True 即恢复入口。
        "allow_custom": False,
    },
    "translation": {
        "data_key": "translators",
        "warmup": warmup_translation,
        "status": get_translation_probe_status,
        "fields_for": translation_fields_for,
        "allow_custom": False,
    },
}

# probe 状态 -> (显示文本, 颜色)
_STATUS_DISPLAY = {
    "testing":     ("检测中…", "#d89614"),
    "reachable":   ("● 可连通", "#5a9a5a"),
    "unreachable": ("● 不可连通", "#d9534f"),
}

# === REORDER_TABLE ===


class ReorderTableWidget(QTableWidget):
    """拖动行首 ☰ 手柄(优先级列)来调整该接口的优先级顺序。"""
    rows_reordered = pyqtSignal(int, int)  # from_visual_row, to_visual_row
    HANDLE_COL = 1  # 仅从该列发起的拖动才触发重排

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.verticalHeader().setVisible(False)
        self.viewport().setMouseTracking(True)
        self._drag_allowed = False

    def _on_handle(self, pos) -> bool:
        idx = self.indexAt(pos)
        return idx.isValid() and idx.column() == self.HANDLE_COL

    def mousePressEvent(self, event):
        self._drag_allowed = self._on_handle(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            if self._on_handle(event.position().toPoint()):
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def startDrag(self, supportedActions):
        if not self._drag_allowed:
            return
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        try:
            super().startDrag(supportedActions)
        finally:
            self.viewport().unsetCursor()

    def dropEvent(self, event):
        src = self.currentRow()
        dst = self.indexAt(event.position().toPoint()).row()
        if dst < 0:
            dst = self.rowCount() - 1
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        self._drag_allowed = False
        if src >= 0 and dst >= 0 and src != dst:
            self.rows_reordered.emit(src, dst)


# === TEST_WORKER ===


class _TestWorker(QThread):
    done = pyqtSignal(str)  # 空串 = 成功,非空 = 错误信息

    def __init__(self, cfg: dict, kind: str):
        super().__init__()
        self._cfg = cfg
        self._kind = kind

    def run(self):
        try:
            if self._kind == "translation":
                build_translator(self._cfg).translate("hello", "zh")
            else:
                build_provider(self._cfg).recognize(OCRProvider._get_test_image())
            self.done.emit("")
        except Exception as e:
            self.done.emit(humanize_error(e))


class ProviderDialog(QDialog):
    """添加/编辑接口。字段表驱动:照接口自己声明的 FIELDS 通用渲染,
    不再按 id 硬编码分支。加接口只需在 providers/translators 里声明 FIELDS。

    通用字段(名称/优先级)所有接口都有,固定渲染;接口专属字段由 FIELDS 决定。
    """
    def __init__(self, kind: str, cfg: dict = None, parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setWindowTitle("编辑接口" if cfg else "添加自定义接口")
        self.resize(420, 380)
        self._cfg = cfg or {"id": str(uuid.uuid4()), "type": "custom", "enabled": True, "priority": 10}
        self._fields = _KIND_SPEC[kind]["fields_for"](self._cfg)
        self._widgets = {}   # field.key -> 控件

        form = QFormLayout(self)
        self._name = QLineEdit(self._cfg.get("name", ""))
        form.addRow("名称", self._name)

        # 照 FIELDS 顺序逐个渲染接口专属字段(kind 决定控件类型)
        for f in self._fields:
            widget = self._make_widget(f)
            self._widgets[f.key] = widget
            if f.kind == "checkbox":
                form.addRow("", widget)   # 复选框文字自带,标签列留空
            else:
                form.addRow(f.label, widget)

        self._priority = QSpinBox()
        self._priority.setRange(1, 99)
        self._priority.setValue(self._cfg.get("priority", 10))
        form.addRow("优先级 (数字越小越优先)", self._priority)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _make_widget(self, f):
        """按 Field.kind 造对应控件并填入当前值。这是 kind→Qt 控件映射的唯一处。"""
        if f.kind == "checkbox":
            w = QCheckBox(f.label)
            w.setChecked(bool(self._cfg.get(f.key, False)))
            return w
        if f.kind == "multiline":
            w = QTextEdit()
            w.setPlainText(self._cfg.get(f.key, ""))
            w.setMaximumHeight(80)
            if f.placeholder:
                w.setPlaceholderText(f.placeholder)
            return w
        # password / text / url 都是单行
        w = QLineEdit(self._cfg.get(f.key, ""))
        if f.kind == "password":
            w.setEchoMode(QLineEdit.EchoMode.Password)
        if f.placeholder:
            w.setPlaceholderText(f.placeholder)
        return w

    def _widget_value(self, f):
        """从控件取值(字符串去空白;复选框取布尔)。"""
        w = self._widgets[f.key]
        if f.kind == "checkbox":
            return w.isChecked()
        if f.kind == "multiline":
            return w.toPlainText().strip()
        return w.text().strip()

    def _on_accept(self):
        """保存前校验:名称非空;url 字段合法;multiline 字段为合法 JSON。
        早校验,避免错误拖到真正识别时才以异常形式爆出。校验规则由字段 kind 派生,
        不再针对具体接口硬编码。"""
        if not self._name.text().strip():
            QMessageBox.warning(self, "提示", "请填写接口名称。")
            return
        for f in self._fields:
            val = self._widget_value(f)
            if f.kind == "url":
                if not val:
                    QMessageBox.warning(self, "提示", f"请填写 {f.label}。")
                    return
                if not (val.startswith("http://") or val.startswith("https://")):
                    QMessageBox.warning(self, "提示", f"{f.label} 必须以 http:// 或 https:// 开头。")
                    return
            elif f.kind == "multiline" and val:
                try:
                    json.loads(val.replace("{{image_base64}}", ""))
                except json.JSONDecodeError as e:
                    QMessageBox.warning(self, "提示", f"{f.label} 不是合法 JSON:{e}")
                    return
        self.accept()

    def get_config(self) -> dict:
        cfg = dict(self._cfg)
        cfg["name"] = self._name.text().strip()
        cfg["priority"] = self._priority.value()
        for f in self._fields:
            cfg[f.key] = self._widget_value(f)
        return cfg


# === PROVIDER_TAB ===


class ProviderTab(QWidget):
    """一个接口池(OCR 或翻译)的管理页:表格 + 增删改测 + 连通状态轮询。"""
    changed = pyqtSignal()   # 任何配置变更(勾选/优先级/增删改)→ 供设置窗实时写盘

    def __init__(self, kind: str, providers: list, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._spec = _KIND_SPEC[kind]
        self._providers = providers   # 直接引用 config 中的列表,原地改
        self._test_worker = None
        self._refreshing = False      # _refresh_table 重建表格时抑制 itemChanged 误触

        vbox = QVBoxLayout(self)
        hint = QLabel("按住 ☰ 标记上下拖动，即可调整该接口的优先级顺序")
        hint.setObjectName("info")
        vbox.addWidget(hint)

        self._table = ReorderTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["启用", "优先级", "名称", "类型", "连通状态"])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.rows_reordered.connect(self._on_rows_reordered)
        self._table.itemChanged.connect(self._on_item_changed)   # 启用勾选即时写盘
        vbox.addWidget(self._table)
        self._refresh_table()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self._edit_provider)
        btn_row.addWidget(edit_btn)
        if self._spec["allow_custom"]:
            add_btn = QPushButton("添加")
            add_btn.setObjectName("primary")
            add_btn.setToolTip("添加自定义接口")
            add_btn.clicked.connect(self._add_custom)
            btn_row.addWidget(add_btn)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._delete_provider)
        test_btn = QPushButton("测试")
        test_btn.setToolTip("测试选中接口的连通性")
        test_btn.clicked.connect(self._test_provider)
        recheck_btn = QPushButton("重测")
        recheck_btn.setToolTip("重新检测全部接口连通状态")
        recheck_btn.clicked.connect(self._recheck_reachability)
        for b in (del_btn, test_btn, recheck_btn):
            btn_row.addWidget(b)
        vbox.addLayout(btn_row)

        self._test_status = QLabel("")
        self._test_status.setObjectName("info")
        self._test_status.setWordWrap(True)
        vbox.addWidget(self._test_status)

        # 连通状态实时轮询
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status_column)
        self._status_timer.start()
        self._refresh_status_column()

    def stop_timer(self):
        self._status_timer.stop()
        # 关窗前等运行中的测试线程结束,避免 QThread 随 Tab 析构时仍在跑而崩溃
        w = self._test_worker
        if w is not None and w.isRunning():
            w.wait(3000)

    # ── 表格 ───────────────────────────────────────────────────────────────
    def _refresh_table(self):
        self._refreshing = True   # 重建期间 setCheckState 会触发 itemChanged,先抑制
        try:
            ordered = sorted(self._providers, key=lambda x: x.get("priority", 99))
            self._table.setRowCount(len(ordered))
            for i, p in enumerate(ordered):
                chk = QTableWidgetItem()
                chk.setCheckState(Qt.CheckState.Checked if p.get("enabled", True) else Qt.CheckState.Unchecked)
                chk.setData(Qt.ItemDataRole.UserRole, p["id"])
                self._table.setItem(i, 0, chk)

                prio = QTableWidgetItem(f"{i + 1}    ☰")
                prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                prio.setForeground(QColor("#b07d4a"))
                prio.setToolTip("按住拖动以调整优先级")
                self._table.setItem(i, 1, prio)

                self._table.setItem(i, 2, QTableWidgetItem(p.get("name", "")))
                self._table.setItem(i, 3, QTableWidgetItem("自定义" if p.get("type") == "custom" else "内置"))
                self._table.setItem(i, 4, QTableWidgetItem(""))
        finally:
            self._refreshing = False
        self._refresh_status_column()

    def _on_item_changed(self, item):
        """启用勾选变化:即时同步到数据并 emit changed(供设置窗实时写盘)。"""
        if self._refreshing or item.column() != 0:
            return
        self.sync_enabled()
        self.changed.emit()


    def _on_rows_reordered(self, src: int, dst: int):
        ordered = sorted(self._providers, key=lambda x: x.get("priority", 99))
        if not (0 <= src < len(ordered)) or not (0 <= dst < len(ordered)):
            return
        moved = ordered.pop(src)
        ordered.insert(dst, moved)
        for i, p in enumerate(ordered):
            p["priority"] = i + 1
        self._refresh_table()
        self.changed.emit()

    def _refresh_status_column(self):
        status = self._spec["status"]()
        for i in range(self._table.rowCount()):
            chk = self._table.item(i, 0)
            if chk is None:
                continue
            pid = chk.data(Qt.ItemDataRole.UserRole)
            cell = self._table.item(i, 4)
            if cell is None:
                cell = QTableWidgetItem("")
                self._table.setItem(i, 4, cell)
            st = status.get(pid)
            if st in _STATUS_DISPLAY:
                text, color = _STATUS_DISPLAY[st]
                cell.setText(text)
                cell.setForeground(QColor(color))
            else:
                cell.setText("—")
                cell.setForeground(QColor("#888888"))

    # ── 操作 ───────────────────────────────────────────────────────────────
    def sync_enabled(self):
        """把表格里的勾选状态同步回数据。"""
        for i in range(self._table.rowCount()):
            chk = self._table.item(i, 0)
            pid = chk.data(Qt.ItemDataRole.UserRole)
            enabled = chk.checkState() == Qt.CheckState.Checked
            p = next((p for p in self._providers if p["id"] == pid), None)
            if p:
                p["enabled"] = enabled

    def _recheck_reachability(self):
        self.sync_enabled()
        threading.Thread(target=self._spec["warmup"],
                         args=(self._providers,), daemon=True).start()
        self._refresh_status_column()

    def _selected_provider(self):
        row = self._table.currentRow()
        if row < 0:
            return None
        pid = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((p for p in self._providers if p["id"] == pid), None)

    def _edit_provider(self):
        cfg = self._selected_provider()
        if not cfg:
            return
        dlg = ProviderDialog(self._kind, dict(cfg), self)
        if dlg.exec():
            new_cfg = dlg.get_config()
            idx = next(i for i, p in enumerate(self._providers) if p["id"] == cfg["id"])
            self._providers[idx] = new_cfg
            self._refresh_table()
            self.changed.emit()

    def _add_custom(self):
        dlg = ProviderDialog(self._kind, parent=self)
        if dlg.exec():
            self._providers.append(dlg.get_config())
            self._refresh_table()
            self.changed.emit()

    def _delete_provider(self):
        cfg = self._selected_provider()
        if not cfg:
            return
        if cfg.get("type") == "builtin":
            QMessageBox.warning(self, "提示", "内置接口不能删除，只能禁用。")
            return
        self._providers[:] = [p for p in self._providers if p["id"] != cfg["id"]]
        self._refresh_table()
        self.changed.emit()

    def _test_provider(self):
        cfg = self._selected_provider()
        if not cfg:
            return
        if self._test_worker is not None:
            return
        self._test_status.setStyleSheet("")   # 复位上次的红/绿色
        self._test_status.setText("测试中…")
        self._test_worker = _TestWorker(cfg, self._kind)
        self._test_worker.done.connect(self._on_test_done)
        # 用 QThread.finished(线程真正结束)清理引用,避免在 done 回调里就丢引用、
        # 线程仍运行时被 GC 触发销毁崩溃。
        self._test_worker.finished.connect(self._cleanup_test_worker)
        self._test_worker.start()

    def _cleanup_test_worker(self):
        w = self._test_worker
        self._test_worker = None
        if w is not None:
            w.deleteLater()

    def _on_test_done(self, err: str):
        if err:
            self._test_status.setStyleSheet("color: #d9534f;")
            self._test_status.setText(f"❌ {err}")
        else:
            self._test_status.setStyleSheet("color: #5a9a5a;")
            self._test_status.setText("✅ 连通正常")


# === SETTINGS_WINDOW ===


class SettingsWindow(QWidget):
    applied = pyqtSignal()   # 任何设置实时写盘后发射 → 托盘据此应用轻量副作用(剪贴板/热键)

    def __init__(self, macro_engine=None, hotkey_mgr=None):
        super().__init__()
        self._loaded = False   # 构造期护栏:控件初始 setChecked/接线时不触发写盘
        self.setWindowTitle("OCR / 翻译 设置")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        # 关窗即销毁:① 旧 MacroTab 随之析构,断开其与引擎的信号连接,避免多次
        # 开关设置窗导致连接累积;② destroyed 确定性触发,使关窗后的热键重注册及时。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 644 = 原 560 +15%,给动作编辑区更宽裕的横向空间;仍 > 内部布局最小宽度 512,
        # 横向比例可正常生效(不会被子控件顶宽)
        self.resize(644, 480)
        self._data = load_config()
        self._macro_tab = None

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        tabs = self._tabs

        self._ocr_tab = ProviderTab("ocr", self._data.setdefault("providers", []))
        self._tr_tab = ProviderTab("translation", self._data.setdefault("translators", []))
        tabs.addTab(self._ocr_tab, "OCR 接口")
        tabs.addTab(self._tr_tab, "翻译接口")
        # 宏 Tab 需要常驻引擎与热键管理器(由托盘传入);缺失则不显示该 Tab
        if macro_engine is not None and hotkey_mgr is not None:
            from app.macro_tab import MacroTab
            self._macro_tab = MacroTab(macro_engine, hotkey_mgr)
            tabs.addTab(self._macro_tab, "宏")
        tabs.addTab(self._build_general_tab(), "通用")
        tabs.addTab(self._build_about_tab(), "关于")
        self._apply_tab_visibility()   # 按 feature_visibility 初始显隐(通用永远在)

        # 防抖保存:各变更信号 → _schedule(重启 350ms timer),停手后才 _do_persist 写一次。
        # 避免每次微改(spinbox 跳一下/打一个字)都重写大宏文件 + 反复触发副作用。
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self._do_persist)

        self._ocr_tab.changed.connect(self._schedule)
        self._tr_tab.changed.connect(self._schedule)
        if self._macro_tab is not None:
            self._macro_tab.changed.connect(self._schedule)

        hint = QLabel("设置改完即时生效并自动保存,直接关闭窗口即可。")
        hint.setObjectName("info")
        layout.addWidget(hint)

        self._loaded = True   # 至此控件初值都已设好,后续真实交互才触发写盘

    def closeEvent(self, event):
        # 关窗时若有未到点的防抖改动,立即落盘一次,绝不丢
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._do_persist()
        self._ocr_tab.stop_timer()
        self._tr_tab.stop_timer()
        super().closeEvent(event)

    # ── 通用 Tab ───────────────────────────────────────────────────────────
    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._cb_monitor = QCheckBox("开启剪贴板监控（自动识别剪贴板图片）")
        self._cb_monitor.setChecked(self._data.get("clipboard_monitor", False))
        self._cb_monitor.toggled.connect(self._schedule)
        form.addRow(self._cb_monitor)
        self._cb_auto_tr = QCheckBox("识别后自动翻译（默认关闭，可在译文窗手动翻译）")
        self._cb_auto_tr.setChecked(self._data.get("auto_translate", False))
        self._cb_auto_tr.toggled.connect(self._schedule)
        form.addRow(self._cb_auto_tr)
        hint = QLabel("默认方向：非中文→中文，中文→英文；译文窗可临时切换目标语言。")
        hint.setObjectName("info")
        hint.setWordWrap(True)
        form.addRow(hint)

        # ── 主题色 ──
        self._theme_color = self._data.get("theme_color", DEFAULT_THEME)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(6)
        # 当前色:圆形只读指示,始终显示当前主题色(含自定义色),圆形以区别于方形预设
        self._theme_current = QLabel()
        self._theme_current.setFixedSize(22, 22)
        self._theme_current.setToolTip("当前主题色")
        theme_row.addWidget(self._theme_current)

        # 自定义:彩虹渐变 + “＋”,一眼区别于纯色预设方块,点开取色盘选任意色
        self._theme_swatch = QPushButton("＋")
        self._theme_swatch.setFixedSize(30, 24)
        self._theme_swatch.setToolTip("自定义颜色（打开取色盘）")
        self._theme_swatch.setStyleSheet(
            "QPushButton{border:1px solid #999; border-radius:4px; color:#2c2c2c;"
            " font-weight:bold; background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            " stop:0 #ff8a8a, stop:0.2 #ffcf8a, stop:0.4 #f4f08a,"
            " stop:0.6 #8af0b0, stop:0.8 #8ac6ff, stop:1 #d28aff);}"
            "QPushButton:hover{border:1px solid #555;}")
        self._theme_swatch.clicked.connect(self._pick_theme)
        theme_row.addWidget(self._theme_swatch)

        theme_row.addSpacing(4)
        preset_lbl = QLabel("预设")        # 小标签,点明后面是一键预设色
        preset_lbl.setObjectName("info")
        theme_row.addWidget(preset_lbl)

        self._preset_btns = []             # 记下预设块,便于高亮当前选中项
        for name, hexv in PRESET_THEMES:        # 预设清新色,点一下即用
            b = QPushButton()
            b.setFixedSize(22, 22)
            b.setToolTip(name)
            b.clicked.connect(lambda _=False, c=hexv: self._set_theme(c))
            self._preset_btns.append((b, hexv))
            theme_row.addWidget(b)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(lambda: self._set_theme(DEFAULT_THEME))
        theme_row.addWidget(reset_btn)
        theme_row.addStretch()
        form.addRow("主题色", self._wrap(theme_row))
        self._refresh_swatch()

        # ── 窗口置顶热键 ──
        from app.macro_tab import HotkeyEdit
        self._wintop_key = HotkeyEdit(self._data.get("window_top_hotkey", "Ctrl+Alt+T"))
        self._wintop_key.changed.connect(self._on_wintop_key_changed)
        form.addRow("窗口置顶热键", self._wintop_key)
        wt_hint = QLabel("看哪个窗口就按此热键把它钉到最上层，再按一次取消。")
        wt_hint.setObjectName("info")
        wt_hint.setWordWrap(True)
        form.addRow(wt_hint)

        # ── 功能可见性 ──
        vis_hint = QLabel("功能可见性（取消勾选可隐藏对应的菜单入口/设置页，减少干扰）：")
        vis_hint.setObjectName("info")
        vis_hint.setWordWrap(True)
        form.addRow(vis_hint)
        self._vis_boxes = {}
        fv = self._data.get("feature_visibility", {})
        for key, label in (("ocr", "OCR 识别（截图识别 + OCR 接口页）"),
                           ("translate", "翻译（翻译菜单 + 翻译接口页）"),
                           ("macro", "宏（宏设置页）"),
                           ("pin", "截图贴图"),
                           ("window_top", "窗口置顶"),
                           ("autostart", "开机自启动"),
                           ("reset_engine", "重置接口状态")):
            cb = QCheckBox(label)
            cb.setChecked(fv.get(key, True))
            cb.toggled.connect(lambda _checked, k=key: self._on_vis_toggled(k))
            self._vis_boxes[key] = cb
            form.addRow(cb)
        return w

    def _build_about_tab(self) -> QWidget:
        """关于页:软件名 + 版本号 + GitHub 开源地址 + 检查更新。"""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size:18px; font-weight:bold;")
        layout.addWidget(title)

        layout.addWidget(QLabel(f"版本：v{__version__}"))

        link = QLabel(f'开源地址：<a href="{GITHUB_URL}">{GITHUB_URL}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(link)

        desc = QLabel("本软件已在 GitHub 开源，欢迎下载、反馈与贡献。新版本请到上方仓库的 Releases 页下载。")
        desc.setObjectName("info")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        row = QHBoxLayout()
        self._update_btn = QPushButton("检查更新")
        self._update_btn.clicked.connect(self._check_update_manual)
        row.addWidget(self._update_btn)
        self._update_status = QLabel("")
        self._update_status.setObjectName("info")
        row.addWidget(self._update_status)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return w

    def _check_update_manual(self):
        """点「检查更新」:后台查 GitHub 最新版,结果回 _on_update_checked。"""
        self._update_btn.setEnabled(False)
        self._update_status.setText("正在检查…")
        self._uc = UpdateChecker()
        self._uc.result_ready.connect(self._on_update_checked)
        self._uc.start()

    def _on_update_checked(self, result):
        """检查结果回调(主线程)。result: dict 或 None(网络失败)。"""
        self._update_btn.setEnabled(True)
        if result is None:
            self._update_status.setText("检查失败，请检查网络后重试。")
            return
        if result["has_update"]:
            self._update_status.setText(f"发现新版本 v{result['latest']}（当前 v{result['current']}）")
            box = QMessageBox(self)
            box.setWindowTitle("发现新版本")
            box.setText(f"有新版本 v{result['latest']} 可用（当前 v{result['current']}）。\n是否前往 GitHub 下载？")
            notes = result.get("notes") or ""
            if notes:
                box.setDetailedText(notes)
            go = box.addButton("前往下载", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is go:
                QDesktopServices.openUrl(QUrl(result["url"]))
        elif result.get("no_release"):
            self._update_status.setText(f"仓库暂无发布版本（当前 v{result['current']}）。")
        else:
            self._update_status.setText(f"已是最新版本（v{result['current']}）。")

    @staticmethod
    def _wrap(layout) -> QWidget:
        """把一个 layout 包成 widget,便于放进 QFormLayout 的一行。"""
        c = QWidget()
        c.setLayout(layout)
        return c

    def _refresh_swatch(self):
        """圆点显示当前主题色;命中的预设块加粗描边高亮,便于区分谁被选中。"""
        cur = (self._theme_color or "").lower()
        self._theme_current.setStyleSheet(
            f"background-color:{self._theme_color}; border:1px solid #999; border-radius:11px;")
        for b, hexv in getattr(self, "_preset_btns", []):
            on = hexv.lower() == cur
            b.setStyleSheet(
                f"background-color:{hexv}; border-radius:4px;"
                + (" border:2px solid #2c2c2c;" if on else " border:1px solid #999;"))

    def _pick_theme(self):
        """打开系统取色盘选任意主题色。"""
        col = QColorDialog.getColor(QColor(self._theme_color), self, "选择主题色")
        if col.isValid():
            self._set_theme(col.name())

    def _set_theme(self, hexv: str):
        """设主题色:即时全局预览 + 写数据 + 防抖落盘(托盘 applied 再统一重套)。"""
        self._theme_color = hexv
        self._data["theme_color"] = hexv
        self._refresh_swatch()
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().setStyleSheet(build_style(hexv))   # 即时预览,所有窗口换色
        self._schedule()

    def _on_wintop_key_changed(self):
        """窗口置顶热键改了:写 data + 防抖落盘(托盘 applied 再重注册该全局热键)。"""
        self._data["window_top_hotkey"] = self._wintop_key.value().strip()
        self._schedule()

    def _on_vis_toggled(self, key: str):
        """功能可见性变更:即时显隐对应 Tab + 写数据 + 防抖落盘(托盘 applied 重建菜单)。"""
        self._data.setdefault("feature_visibility", {})[key] = self._vis_boxes[key].isChecked()
        self._apply_tab_visibility()
        self._schedule()

    def _apply_tab_visibility(self):
        """按 feature_visibility 显隐设置窗的 Tab(通用 Tab 永远显示)。

        仅 ocr/translate/macro 有对应 Tab;pin/window_top/autostart/reset_engine 只影响托盘菜单。
        """
        fv = self._data.get("feature_visibility", {})
        mapping = [("ocr", self._ocr_tab), ("translate", self._tr_tab)]
        if self._macro_tab is not None:
            mapping.append(("macro", self._macro_tab))
        for key, widget in mapping:
            idx = self._tabs.indexOf(widget)
            if idx >= 0:
                self._tabs.setTabVisible(idx, fv.get(key, True))

    # ── 防抖保存 ─────────────────────────────────────────────────────────────
    def _schedule(self):
        """有变更:重启防抖 timer。连续改只在停手 350ms 后落一次盘。

        构造期(_loaded=False)由护栏挡掉,避免控件初值/接线触发空写。
        """
        if not self._loaded:
            return
        self._save_timer.start()

    def _do_persist(self):
        """防抖到点(或关窗/即时场景)真正写盘 + 通知托盘应用副作用。"""
        self._ocr_tab.sync_enabled()
        self._tr_tab.sync_enabled()
        self._data["clipboard_monitor"] = self._cb_monitor.isChecked()
        self._data["auto_translate"] = self._cb_auto_tr.isChecked()
        # 宏:先 flush 当前宏文件(热键/循环),再取全局段写 config
        if self._macro_tab is not None:
            self._macro_tab.flush()
            self._data["macro"] = self._macro_tab.collect()
        save_config(self._data)
        self.applied.emit()




