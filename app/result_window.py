# -*- coding: utf-8 -*-
"""译文窗:承载「原文 → 译文」的统一窗口。

OCR 识别后展示 与 单独调用翻译 用同一个窗。
- 上下分栏:上原文框、下译文框,均可编辑。
- 操作:翻译、翻译剪贴板、粘性目标语言下拉、复制原文/复制译文、置顶切换。
- 粘性目标语言:打开时为「自动」(按方向规则);手动选定具体语言后完全盖过规则,
  作用域 = 本窗生命周期,关窗即失效。
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                             QPushButton, QLabel, QComboBox, QApplication,
                             QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from app.config import load_config
from app.engines import run_translation, resolve_target_lang
from app.translators import LANG_NAMES, translation_is_configured


class _TranslateWorker(QThread):
    done = pyqtSignal(str, str)   # 译文, 接口名
    failed = pyqtSignal(str)

    def __init__(self, text: str, target_lang: str, translators: list):
        super().__init__()
        self._text = text
        self._target = target_lang
        self._translators = translators

    def run(self):
        try:
            translated, name = run_translation(self._text, self._target, self._translators)
            self.done.emit(translated, name)
        except Exception as e:
            self.failed.emit(str(e))


class ResultWindow(QWidget):
    """译文窗。initial_text 为空 = 单独调用;非空 = 从 OCR 进入(预填原文)。

    provider_name: OCR 来源接口名(从 OCR 进入时显示);单独调用时为 None。
    auto_translate: 从 OCR 进入且开启自动翻译时为 True,构造后立即翻译一次。
    """
    def __init__(self, initial_text: str = "", provider_name: str = None,
                 auto_translate: bool = False):
        super().__init__()
        self.setWindowTitle("OCR / 翻译")
        self._always_on_top = True
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.resize(520, 560)
        self.setContentsMargins(16, 16, 16, 16)
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 顶部信息栏 + 置顶切换
        top = QHBoxLayout()
        self._info = QLabel(f"识别来源:{provider_name}" if provider_name else "翻译")
        self._info.setObjectName("info")
        top.addWidget(self._info)
        top.addStretch()
        self._pin_btn = QPushButton("已置顶")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(True)
        self._pin_btn.setFixedWidth(80)
        self._pin_btn.clicked.connect(self._toggle_pin)
        top.addWidget(self._pin_btn)
        layout.addLayout(top)

        # 原文区
        layout.addWidget(QLabel("原文"))
        self.editor = QTextEdit()
        self.editor.setPlainText(initial_text)
        layout.addWidget(self.editor)

        # 原文操作行:复制原文 / 翻译剪贴板 / 目标语言 / 翻译
        src_row = QHBoxLayout()
        copy_src = QPushButton("复制原文")
        copy_src.clicked.connect(lambda: self._copy(self.editor))
        clip_btn = QPushButton("翻译剪贴板")
        clip_btn.clicked.connect(self._translate_clipboard)
        src_row.addWidget(copy_src)
        src_row.addWidget(clip_btn)
        src_row.addStretch()
        src_row.addWidget(QLabel("目标语言"))
        self._lang = QComboBox()
        self._lang.addItem("自动", None)   # 首项:按方向规则
        for code, label in LANG_NAMES.items():
            self._lang.addItem(label, code)
        src_row.addWidget(self._lang)
        translate_btn = QPushButton("翻译")
        translate_btn.setObjectName("primary")
        translate_btn.clicked.connect(self._translate)
        src_row.addWidget(translate_btn)
        layout.addLayout(src_row)

        # 译文区
        layout.addWidget(QLabel("译文"))
        self.translation = QTextEdit()
        layout.addWidget(self.translation)

        # 译文操作行:复制按钮右对齐单独一行
        dst_row = QHBoxLayout()
        dst_row.addStretch()
        copy_dst = QPushButton("复制译文")
        copy_dst.setObjectName("primary")
        copy_dst.clicked.connect(lambda: self._copy(self.translation))
        dst_row.addWidget(copy_dst)
        layout.addLayout(dst_row)

        # 状态/错误信息:单独一行,自动换行,且宽度不向布局索取
        # (否则长错误信息会把整个窗口撑宽)。
        self._status = QLabel("")
        self._status.setObjectName("info")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Preferred)
        layout.addWidget(self._status)

        if auto_translate and initial_text.strip():
            self._translate()

    # ── 置顶切换 ────────────────────────────────────────────────────────────
    def _toggle_pin(self, checked: bool):
        self._always_on_top = checked
        self._pin_btn.setText("已置顶" if checked else "未置顶")
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()   # 改 flags 后需重新 show

    # ── 复制 ────────────────────────────────────────────────────────────────
    def _copy(self, edit: QTextEdit):
        QApplication.clipboard().setText(edit.toPlainText())

    # ── 翻译剪贴板 ──────────────────────────────────────────────────────────
    def _translate_clipboard(self):
        text = QApplication.clipboard().text()
        if not text.strip():
            self._status.setText("剪贴板没有文字")
            return
        self.editor.setPlainText(text)
        self._translate()

    # ── 翻译 ────────────────────────────────────────────────────────────────
    def _resolve_target(self, text: str) -> str:
        chosen = self._lang.currentData()    # None = 自动
        return chosen if chosen else resolve_target_lang(text)

    def _translate(self):
        if self._worker is not None:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            self._status.setText("没有可翻译的原文")
            return
        cfg = load_config()
        translators = cfg.get("translators", [])
        # 与派发引擎的「已配置」判定保持一致:用 translation_is_configured 检查必填字段
        # (如百度翻译要 api_key+app_id),不在此处用更松的条件误判可用。
        if not any(t.get("enabled", True) and translation_is_configured(t)
                   for t in translators):
            self._status.setText("未配置可用的翻译接口,请在设置中填写")
            return
        self._status.setText("翻译中…")
        target = self._resolve_target(text)
        self._worker = _TranslateWorker(text, target, translators)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        # 用 QThread 内置 finished(run 返回后才发)清理引用,避免线程未结束就被回收
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_done(self, translated: str, name: str):
        self.translation.setPlainText(translated)
        self._status.setText(f"译自:{name}")

    def _on_failed(self, msg: str):
        self._status.setText(f"翻译失败:{msg}")

    def _on_worker_finished(self):
        w = self._worker
        self._worker = None
        if w is not None:
            w.deleteLater()

    def closeEvent(self, e):
        # 翻译进行中关窗:等线程真正结束再放行,避免 QThread 随窗(WA_DeleteOnClose)销毁崩溃
        w = self._worker
        if w is not None and w.isRunning():
            w.wait(3000)
        super().closeEvent(e)
