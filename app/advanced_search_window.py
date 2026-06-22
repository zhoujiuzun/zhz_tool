# -*- coding: utf-8 -*-
"""高级搜索对话框:Everything 风格的多条件表单,产出条件字典交 search_advanced 执行。

只放**真能过滤**的分组(后端有数据支撑):文件名(必含/短语/任一/不含 + 大小写)、搜索文件夹、
修改/创建/访问时间、大小、类型、扩展名、属性、正则、文件名长度、文件夹深度、子项名包含。
做不到的(文件内容/运行次数/重复/文件列表)不放,避免摆设。见 docs/adr/0004。

布局:浅灰底、白控件、可纵向滚动、底部固定 确定/取消。get_conditions() 收集成条件字典。
"""
import os
from PyQt6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QGroupBox, QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton,
                             QScrollArea, QSpinBox, QDateEdit, QFileDialog, QDialogButtonBox)
from PyQt6.QtCore import Qt, QDate
from app.file_index import FILE_CATEGORIES, FILE_ATTRS

_SIZE_UNITS = [("字节", 1), ("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)]


class _WordRow(QWidget):
    """一行:标签 + 输入框 + (区分大小写/全字匹配/匹配变音标记) 复选框。变音标记我们不支持,置灰。"""
    def __init__(self, label, with_word=True):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QLineEdit()
        lay.addWidget(QLabel(label), 0)
        lay.addWidget(self.edit, 1)
        self.cb_case = QCheckBox("区分大小写")
        lay.addWidget(self.cb_case)
        if with_word:
            self.cb_word = QCheckBox("全字匹配")
            lay.addWidget(self.cb_word)
        self.cb_dia = QCheckBox("匹配变音标记")
        self.cb_dia.setEnabled(False)        # 我们不支持变音匹配,置灰(诚实)
        self.cb_dia.setToolTip("暂不支持")
        lay.addWidget(self.cb_dia)

    def text(self):
        return self.edit.text().strip()


# PLACEHOLDER_DIALOG


class AdvancedSearchDialog(QDialog):
    """高级搜索对话框。exec() 返回 Accepted 时,用 get_conditions() 取条件字典。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("高级搜索")
        self.resize(720, 760)
        self.setStyleSheet("""
            QDialog { background: #f0f0f0; }
            QGroupBox { font-weight: bold; border: 1px solid #cfcfcf; border-radius: 6px;
                        margin-top: 8px; padding: 8px 10px 10px 10px; background: #f7f7f7; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QComboBox, QSpinBox, QDateEdit {
                background: #ffffff; border: 1px solid #c4c4c4; border-radius: 4px; padding: 3px 6px; }
            QLabel { color: #2c2c2c; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 可滚动的内容区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._form = QVBoxLayout(body)
        self._form.setContentsMargins(12, 12, 12, 12)
        self._form.setSpacing(8)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._build_sections()
        self._form.addStretch()

        # 底部固定 确定/取消
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        bar = QWidget()
        bar.setStyleSheet("background:#f0f0f0; border-top:1px solid #d6d6d6;")
        blay = QHBoxLayout(bar)
        blay.addStretch()
        blay.addWidget(btns)
        outer.addWidget(bar)

    def _warn_icon_label(self, text):
        """分组标题前加黄色警告图标(用于"暂以名字/路径近似"的项)。返回带 ⚠ 的标题串。"""
        return "⚠ " + text

    # PLACEHOLDER_SECTIONS

    def _build_sections(self):
        self._build_name_group()
        self._build_folder_group()
        self._build_time_size_group()
        self._build_type_ext_group()
        self._build_attr_group()
        self._build_regex_group()
        self._build_range_group()

    def _build_name_group(self):
        """一、文件名中包含有:必含单词/必含短语/任一单词/不含单词,各带选项。"""
        g = QGroupBox("文件名中包含有…")
        lay = QVBoxLayout(g)
        lay.setSpacing(5)
        self._w_all = _WordRow("必含单词(A)：")
        self._w_phrase = _WordRow("必含短语(E)：")
        self._w_any = _WordRow("任一单词(O)：")
        self._w_none = _WordRow("不含单词(N)：")
        for w in (self._w_all, self._w_phrase, self._w_any, self._w_none):
            lay.addWidget(w)
        self._form.addWidget(g)

    def _build_folder_group(self):
        """三、搜索文件夹:路径输入 + 浏览 + 含子文件夹。"""
        g = QGroupBox("搜索文件夹(L)")
        lay = QHBoxLayout(g)
        self._folder = QLineEdit()
        btn = QPushButton("浏览(W)…")
        btn.clicked.connect(self._browse_folder)
        self._inc_sub = QCheckBox("包含子文件夹")
        self._inc_sub.setChecked(True)
        lay.addWidget(self._folder, 1)
        lay.addWidget(btn)
        lay.addWidget(self._inc_sub)
        self._form.addWidget(g)

    def _browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择搜索文件夹")
        if d:
            self._folder.setText(d.replace("/", "\\"))

    # PLACEHOLDER_SECTIONS2

    def _date_range_row(self, label, warn=False):
        """一行日期区间:左复选框启用 + 起始 QDateEdit + 到 + 结束 QDateEdit。返回 (cb, d_from, d_to)。"""
        row = QHBoxLayout()
        cb = QCheckBox(self._warn_icon_label(label) if warn else label)
        d1 = QDateEdit(QDate.currentDate()); d1.setCalendarPopup(True)
        d1.setDisplayFormat("yyyy/ M/d"); d1.setEnabled(False)
        d2 = QDateEdit(QDate.currentDate()); d2.setCalendarPopup(True)
        d2.setDisplayFormat("yyyy/ M/d"); d2.setEnabled(False)
        cb.toggled.connect(d1.setEnabled)
        cb.toggled.connect(d2.setEnabled)
        row.addWidget(cb)
        row.addWidget(d1)
        row.addWidget(QLabel("到"))
        row.addWidget(d2)
        row.addStretch()
        return row, cb, d1, d2

    def _build_time_size_group(self):
        """四、时间与大小:修改/创建/访问时间区间 + 大小区间(带单位)。"""
        g = QGroupBox("时间与大小")
        lay = QVBoxLayout(g)
        r1, self._mt_cb, self._mt1, self._mt2 = self._date_range_row("修改时间(D)：")
        r3, self._ct_cb, self._ct1, self._ct2 = self._date_range_row("创建时间：", warn=True)
        r4, self._at_cb, self._at1, self._at2 = self._date_range_row("访问时间：", warn=True)
        lay.addLayout(r1); lay.addLayout(r3); lay.addLayout(r4)
        # 大小区间 + 单位
        srow = QHBoxLayout()
        self._sz_cb = QCheckBox("大小(S)：")
        self._sz_min = QSpinBox(); self._sz_min.setRange(0, 1000000); self._sz_min.setEnabled(False)
        self._sz_min_u = QComboBox(); self._sz_min_u.addItems([u[0] for u in _SIZE_UNITS])
        self._sz_min_u.setCurrentIndex(2); self._sz_min_u.setEnabled(False)
        self._sz_max = QSpinBox(); self._sz_max.setRange(0, 1000000); self._sz_max.setEnabled(False)
        self._sz_max_u = QComboBox(); self._sz_max_u.addItems([u[0] for u in _SIZE_UNITS])
        self._sz_max_u.setCurrentIndex(2); self._sz_max_u.setEnabled(False)
        for w in (self._sz_min, self._sz_min_u, self._sz_max, self._sz_max_u):
            self._sz_cb.toggled.connect(w.setEnabled)
        srow.addWidget(self._sz_cb); srow.addWidget(self._sz_min); srow.addWidget(self._sz_min_u)
        srow.addWidget(QLabel("到")); srow.addWidget(self._sz_max); srow.addWidget(self._sz_max_u)
        srow.addStretch()
        lay.addLayout(srow)
        self._form.addWidget(g)

    def _build_type_ext_group(self):
        """五~七、类型 + 扩展名。"""
        g = QGroupBox("类型与扩展名")
        lay = QGridLayout(g)
        lay.addWidget(QLabel("类型(T)："), 0, 0)
        self._type = QComboBox()
        self._type.addItem("（全部文件和文件夹）", "")
        for key, name, _e in FILE_CATEGORIES:
            self._type.addItem(name, key)
        lay.addWidget(self._type, 0, 1)
        lay.addWidget(QLabel("扩展名(X)："), 1, 0)
        self._ext = QLineEdit()
        self._ext.setPlaceholderText("多个用分号或空格分隔,如 jpg;png")
        lay.addWidget(self._ext, 1, 1)
        self._form.addWidget(g)

    # PLACEHOLDER_SECTIONS3

    def _build_attr_group(self):
        """八、属性:多个复选框纵向排列(每个=必须置位)。"""
        g = QGroupBox("属性(B)")
        grid = QGridLayout(g)
        self._attr_cbs = {}
        for n, (key, name, bit) in enumerate(FILE_ATTRS):
            cb = QCheckBox(name)
            self._attr_cbs[key] = (cb, bit)
            grid.addWidget(cb, n // 4, n % 4)      # 每行 4 个
        self._form.addWidget(g)

    def _build_regex_group(self):
        """九、匹配正则表达式:对文件名。"""
        g = QGroupBox("匹配正则表达式(R)")
        lay = QHBoxLayout(g)
        self._regex = QLineEdit()
        self._regex_case = QCheckBox("区分大小写")
        lay.addWidget(self._regex, 1)
        lay.addWidget(self._regex_case)
        self._form.addWidget(g)

    def _build_range_group(self):
        """十、数值范围:文件名长度 / 文件夹深度。"""
        g = QGroupBox("数值范围")
        lay = QVBoxLayout(g)
        # 文件名长度
        r1 = QHBoxLayout()
        self._len_cb = QCheckBox("文件名长度(M)：")
        self._len_min = QSpinBox(); self._len_min.setRange(0, 32767); self._len_min.setEnabled(False)
        self._len_max = QSpinBox(); self._len_max.setRange(0, 32767); self._len_max.setValue(255); self._len_max.setEnabled(False)
        self._len_cb.toggled.connect(self._len_min.setEnabled)
        self._len_cb.toggled.connect(self._len_max.setEnabled)
        r1.addWidget(self._len_cb); r1.addWidget(self._len_min); r1.addWidget(QLabel("到"))
        r1.addWidget(self._len_max); r1.addStretch()
        lay.addLayout(r1)
        # 文件夹深度
        r2 = QHBoxLayout()
        self._dep_cb = QCheckBox("文件夹深度(P)：")
        self._dep_min = QSpinBox(); self._dep_min.setRange(0, 255); self._dep_min.setEnabled(False)
        self._dep_max = QSpinBox(); self._dep_max.setRange(0, 255); self._dep_max.setValue(50); self._dep_max.setEnabled(False)
        self._dep_cb.toggled.connect(self._dep_min.setEnabled)
        self._dep_cb.toggled.connect(self._dep_max.setEnabled)
        r2.addWidget(self._dep_cb); r2.addWidget(self._dep_min); r2.addWidget(QLabel("到"))
        r2.addWidget(self._dep_max); r2.addStretch()
        lay.addLayout(r2)
        self._form.addWidget(g)

    # PLACEHOLDER_GETCOND

    @staticmethod
    def _date_to_unix(dateedit, end=False):
        """QDateEdit → Unix 秒。end=True 取当天 23:59:59(区间右端含当天)。"""
        import datetime
        d = dateedit.date()
        dt = datetime.datetime(d.year(), d.month(), d.day(),
                               23, 59, 59 if end else 0, 0 if end else 0)
        if not end:
            dt = datetime.datetime(d.year(), d.month(), d.day(), 0, 0, 0)
        return dt.timestamp()

    def get_conditions(self):
        """收集表单为条件字典(交 client.advanced_search → search_advanced)。空条件不放。"""
        c = {}
        # 文件名四项(共用一个区分大小写:取必含单词那行的;Everything 也是各行独立,这里简化为或)
        if self._w_all.text():
            c["name_all"] = self._w_all.text().split()
            c["name_case"] = c.get("name_case") or self._w_all.cb_case.isChecked()
        if self._w_phrase.text():
            c["name_phrase"] = self._w_phrase.text()
            c["name_case"] = c.get("name_case") or self._w_phrase.cb_case.isChecked()
        if self._w_any.text():
            c["name_any"] = self._w_any.text().split()
            c["name_case"] = c.get("name_case") or self._w_any.cb_case.isChecked()
        if self._w_none.text():
            c["name_none"] = self._w_none.text().split()
        # 文件夹
        if self._folder.text().strip():
            c["folder"] = self._folder.text().strip()
            c["include_sub"] = self._inc_sub.isChecked()
        # 时间区间
        for cb, d1, d2, key in ((self._mt_cb, self._mt1, self._mt2, "mtime"),
                                (self._ct_cb, self._ct1, self._ct2, "ctime"),
                                (self._at_cb, self._at1, self._at2, "atime")):
            if cb.isChecked():
                c[key + "_from"] = self._date_to_unix(d1)
                c[key + "_to"] = self._date_to_unix(d2, end=True)
        # 大小
        if self._sz_cb.isChecked():
            c["size_min"] = self._sz_min.value() * _SIZE_UNITS[self._sz_min_u.currentIndex()][1]
            if self._sz_max.value() > 0:
                c["size_max"] = self._sz_max.value() * _SIZE_UNITS[self._sz_max_u.currentIndex()][1]
        # 类型 / 扩展名
        t = self._type.currentData()
        if t:
            c["types"] = [t]
        if self._ext.text().strip():
            c["ext"] = [e for e in self._ext.text().replace(";", " ").split() if e]
        # 属性
        attrs = [bit for _k, (cb, bit) in self._attr_cbs.items() if cb.isChecked()]
        if attrs:
            c["attrs"] = attrs
        # 正则
        if self._regex.text().strip():
            c["regex"] = self._regex.text().strip()
            c["regex_case"] = self._regex_case.isChecked()
        # 文件名长度 / 文件夹深度
        if self._len_cb.isChecked():
            c["name_len_min"] = self._len_min.value()
            c["name_len_max"] = self._len_max.value()
        if self._dep_cb.isChecked():
            c["depth_min"] = self._dep_min.value()
            c["depth_max"] = self._dep_max.value()
        return c

