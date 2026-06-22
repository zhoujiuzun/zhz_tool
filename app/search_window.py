# -*- coding: utf-8 -*-
"""文件搜索窗:搜索框 + 结果列表,边打字增量搜,双击打开,右键定位/复制。

搜索查的是 FileIndex(内存索引,载自本地存档)。建索引/USN 在提权进程里做,本窗只读
索引、只搜、只展示——故本窗无需管理员权限。见 docs/adr/0004。

输入防抖:停手 ~120ms 才真正搜,避免每键都触发最坏 ~250ms 全扫卡 UI;搜索在后台线程跑。
结果上限 N:百万级匹配不全渲染,只显示前 N 条 + 提示细化关键词。
"""
import os
import subprocess
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
                             QTreeWidget, QTreeWidgetItem, QMenu, QApplication, QToolButton,
                             QPushButton)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QGuiApplication, QAction, QActionGroup
from app.file_index import FILE_CATEGORIES
from app.search_engine import ENGINE_NATIVE, ENGINE_EVERYTHING, ENGINE_LABELS

_RESULT_LIMIT = 1000          # 最多渲染条数;命中更多则提示细化
_DEBOUNCE_MS = 120            # 停手多少毫秒后才真正搜
_MAX_READY_POLLS = 150        # 就绪轮询上限(×800ms ≈ 2分钟);超时给失败提示,不无限干等


class _SearchWorker(QThread):
    """后台搜索线程:避免最坏 ~250ms 全扫卡住 UI。"""
    done = pyqtSignal(object, list)   # key=(名称,路径), results[(is_dir, path)]

    def __init__(self, engine, query, types=None, match_path=False, path_query=None,
                 whole_word=False, case=False, drives=None):
        super().__init__()
        self._engine = engine
        self._query = query
        self._types = types        # 选中的类型 key 集合;None/空=不过滤
        self._match_path = match_path   # True=路径+名称搜索(双框过滤);False=普通(只名字)
        self._path_query = path_query   # 路径过滤词(双框的"路径含");仅 match_path 时用
        self._whole_word = whole_word   # 全字匹配
        self._case = case               # 区分大小写
        self._drives = drives           # 限定盘符(小写字母集合);None/空=不限

    def run(self):
        try:
            res = self._engine.search(self._query, limit=_RESULT_LIMIT,
                                      types=self._types, match_path=self._match_path,
                                      path_query=self._path_query,
                                      whole_word=self._whole_word, case=self._case,
                                      drives=self._drives)
        except Exception:
            res = []
        self.done.emit((self._query, self._path_query or ""), res)


class _AdvWorker(QThread):
    """后台高级搜索线程:走 engine.advanced_search(条件可能要全表扫,放后台不卡 UI)。"""
    done = pyqtSignal(list)   # results[(is_dir, path)]

    def __init__(self, engine, cond, drives=None):
        super().__init__()
        self._engine = engine
        self._cond = cond
        self._drives = drives

    def run(self):
        try:
            res = self._engine.advanced_search(self._cond, limit=_RESULT_LIMIT, drives=self._drives)
        except Exception:
            res = []
        self.done.emit(res)


class _ReadyWorker(QThread):
    """后台就绪探测:engine.probe() 可能是同步 socket 往返(自研 helper 启动时等数秒),
    绝不能在 GUI 主线程做——否则启动期间拖窗口一顿一顿。放后台线程,结果用信号回主线程。

    自研引擎:probe()=索引项数(0=还没就绪,需轮询等待)。Everything:probe()=1(即时可查)。
    """
    checked = pyqtSignal(int)      # probe 结果(0=还没就绪)

    def __init__(self, engine):
        super().__init__()
        self._engine = engine

    def run(self):
        try:
            n = self._engine.probe()
        except Exception:
            n = 0
        self.checked.emit(n)


class _DrivesWorker(QThread):
    """后台拉取已索引盘符:drives() 可能是同步 socket 往返,放后台线程不卡 UI。"""
    got = pyqtSignal(list)         # 已索引盘符(大写字母列表)

    def __init__(self, engine):
        super().__init__()
        self._engine = engine

    def run(self):
        try:
            drives = self._engine.drives()
        except Exception:
            drives = []
        self.got.emit(drives or [])


# PLACEHOLDER_WINDOW


class SearchWindow(QWidget):
    """文件搜索窗。engine: 搜索引擎(自研/Everything,见 app/search_engine);二者接口一致。

    engine_factory(kind) -> (engine, actual_kind, err):切换引擎时调(由 tray 提供,负责
    helper 生命周期 + 持久化 + 回退)。everything_available: Everything 当前是否可用(决定引擎
    选项是否置灰)。closed 信号:窗口关闭时发出,供自研提权进程据此停 USN / 退出(见 ADR-0004)。
    """
    closed = pyqtSignal()

    def __init__(self, engine, engine_factory, everything_available=False):
        super().__init__()
        self._engine = engine
        self._engine_factory = engine_factory
        self._everything_available = everything_available
        self._worker = None
        self._pending = None        # 防抖期间最后一次待搜的 query
        self._count = 0             # 索引项数缓存(就绪时填),清空搜索框时显示,避免阻塞往返
        self.setWindowTitle("文件搜索")
        self.resize(720, 520)
        self.setContentsMargins(12, 12, 12, 12)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 搜索设置(搜索设置抽屉):盘符过滤 / 全字匹配 / 区分大小写。默认:全盘、关、关。
        self._whole_word = False        # 全字匹配
        self._case = False              # 区分大小写
        self._drives = set()            # 勾选的盘符(小写);空=全盘(不过滤)
        self._all_drives = []           # helper 已索引的盘符(大写),就绪后填

        # 模式切换排:普通搜索 / 路径+名称搜索 / 高级搜索;右侧「搜索设置」抽屉。默认普通。
        self._match_path = False        # 当前是否"路径+名称"模式
        layout.addLayout(self._build_mode_bar())

        # 搜索框 + 类型抽屉(横排)
        top = QHBoxLayout()
        top.setSpacing(6)
        self._box = QLineEdit()
        self._box.setPlaceholderText("输入文件名 / 文件夹名 关键词(空格分隔=同时包含)")
        self._box.setClearButtonEnabled(True)
        self._box.textChanged.connect(self._on_text_changed)
        top.addWidget(self._box)
        top.addWidget(self._build_type_button())
        layout.addLayout(top)

        # 路径过滤框:仅"路径+名称"模式显示;与名称框 AND 过滤。默认隐藏。
        self._path_box = QLineEdit()
        self._path_box.setPlaceholderText("路径含(限定所在目录,空格分隔=同时包含)")
        self._path_box.setClearButtonEnabled(True)
        self._path_box.textChanged.connect(self._on_text_changed)
        self._path_box.setVisible(False)
        layout.addWidget(self._path_box)

        # 结果列表:文件名 | 路径
        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["名称", "路径"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemActivated.connect(self._on_activate)   # 双击/回车
        self._tree.setColumnWidth(0, 240)
        layout.addWidget(self._tree)

        # 状态栏
        self._status = QLabel("正在启动搜索服务…")
        self._status.setObjectName("info")
        layout.addWidget(self._status)

        # 防抖定时器
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._do_search)

        # 就绪轮询:helper 首次建索引要 ~20s,期间禁搜并提示;就绪后放开。
        # 探测(len=socket 往返,可能等到超时)在后台线程做,绝不卡主线程(否则启动期拖窗口卡)。
        self._ready = False
        self._ready_workers = []      # 持有探测线程引用,finished 后才移除(防运行中被 GC)
        self._ready_polls = 0         # 已轮询次数;超时(见 _MAX_READY_POLLS)给明确失败提示,不无限干等
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(800)
        self._ready_timer.timeout.connect(self._poll_ready)
        self._box.setEnabled(False)
        self._ready_timer.start()
        QTimer.singleShot(0, self._poll_ready)      # 立刻先探一次(已就绪则秒开)

        self._box.setFocus()

    def _poll_ready(self):
        """派一个后台线程探测 helper 是否就绪;不在主线程阻塞。已有探测在飞则跳过本次。

        超过 _MAX_READY_POLLS 仍没起来 → 停止干等,给明确失败提示(否则永远"正在启动服务")。
        """
        if self._ready or any(w.isRunning() for w in self._ready_workers):
            return
        self._ready_polls += 1
        if self._ready_polls > _MAX_READY_POLLS:
            self._ready_timer.stop()
            self._status.setText("搜索服务启动失败:请重试,或检查是否被安全软件拦截。")
            return
        w = _ReadyWorker(self._engine)
        self._ready_workers.append(w)
        w.checked.connect(self._on_ready_checked)
        w.finished.connect(lambda x=w: self._ready_workers.remove(x)
                           if x in self._ready_workers else None)
        w.start()

    def _on_ready_checked(self, n):
        """就绪探测结果(主线程)。probe>0 视为可用(自研=索引项数;Everything=1)。"""
        if self._ready or n <= 0:
            return
        self._ready = True
        self._count = n               # 缓存项数,供清空搜索框时显示,避免再做阻塞 socket 往返
        self._ready_timer.stop()
        self._box.setEnabled(True)
        # 如果是刷新索引后就绪,显示"索引已更新"提示
        if hasattr(self, '_is_refreshing') and self._is_refreshing:
            self._status.setText(f"✅ 索引已更新！共 {n:,} 项,输入关键词开始搜索")
            self._is_refreshing = False
        else:
            self._status.setText(self._engine.ready_text(n))
        self._box.setFocus()
        # 拉已索引盘符填充「搜索设置」抽屉(后台线程,不卡主线程)
        self._drives_worker = _DrivesWorker(self._engine)
        self._drives_worker.got.connect(self._populate_drives)
        self._drives_worker.start()
        if self._box.text().strip():
            self._do_search()

    # PLACEHOLDER_METHODS

    def _build_mode_bar(self) -> QHBoxLayout:
        """搜索框上方一排模式按钮:普通搜索 / 路径+名称搜索 / 高级搜索(占位)。

        互斥单选(checkable),默认普通搜索。高级搜索暂禁用(待实现)。
        右侧:搜索设置抽屉 + 刷新索引按钮(自研引擎可见)。
        """
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._mode_btns = {}
        modes = [("normal", "普通搜索", True),
                 ("path", "路径+名称搜索", True),
                 ("advanced", "高级搜索", True)]
        for key, label, enabled in modes:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setEnabled(enabled)
            b.clicked.connect(lambda _checked=False, k=key: self._switch_mode(k))
            self._mode_btns[key] = b
            bar.addWidget(b)
        bar.addWidget(self._build_settings_button())   # 搜索设置抽屉
        # 刷新索引按钮:仅自研引擎可见(Everything 实时监控无需手动刷新)
        from app.search_engine import ENGINE_NATIVE
        if self._engine.kind == ENGINE_NATIVE:
            refresh_btn = QPushButton("🔄 刷新索引")
            refresh_btn.setToolTip("重新扫描磁盘,更新文件列表(新增/删除/改名)")
            refresh_btn.clicked.connect(self._refresh_index)
            bar.addWidget(refresh_btn)
            self._refresh_btn = refresh_btn
        else:
            self._refresh_btn = None
        bar.addStretch()
        self._mode_btns["normal"].setChecked(True)   # 默认普通搜索
        return bar

    def _build_settings_button(self) -> QToolButton:
        """「⚙ 搜索设置」抽屉:盘符多选(默认全勾)+ 全字匹配 + 区分大小写。任一项变化即重搜。

        样式与「类型」抽屉一致。盘符项在 helper 就绪、拿到已索引盘符后填充(_populate_drives)。
        """
        btn = QToolButton()
        btn.setText("⚙ 搜索设置")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QToolButton {
                padding: 5px 14px; border: 1px solid #c9c9c9; border-radius: 6px;
                background: #fafafa;
            }
            QToolButton:hover { background: #f0f0f0; border-color: #9aa0a6; }
            QToolButton::menu-indicator {
                subcontrol-position: right center; subcontrol-origin: padding; right: 6px;
            }
            QMenu { padding: 6px; border: 1px solid #c9c9c9; border-radius: 8px; background: #ffffff; }
            QMenu::item {
                padding: 7px 28px 7px 12px; border-radius: 5px; margin: 1px 2px; font-size: 13px;
            }
            QMenu::item:selected { background: #e8f0fe; color: #1a73e8; }
            QMenu::indicator { width: 16px; height: 16px; left: 6px; }
            QMenu::separator { height: 1px; background: #ececec; margin: 5px 8px; }
        """)
        menu = QMenu(btn)
        # ── 引擎分组(单选:自研 / Everything)──
        eng_header = QAction("搜索引擎", menu)
        eng_header.setEnabled(False)
        menu.addAction(eng_header)
        self._engine_group = QActionGroup(menu)
        self._engine_group.setExclusive(True)
        self._engine_actions = {}       # kind -> QAction
        for kind, label in ((ENGINE_NATIVE, "自研引擎"), (ENGINE_EVERYTHING, "Everything 引擎")):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(self._engine.kind == kind)
            if kind == ENGINE_EVERYTHING and not self._everything_available:
                act.setEnabled(False)            # 不可用置灰
                act.setText("Everything 引擎(需先安装并运行 Everything)")
            act.triggered.connect(lambda _c=False, k=kind: self._on_engine_selected(k))
            self._engine_group.addAction(act)
            menu.addAction(act)
            self._engine_actions[kind] = act
        menu.addSeparator()
        # ── 盘符分组(就绪后填充)──
        self._drive_header = QAction("盘符(就绪后可选)", menu)
        self._drive_header.setEnabled(False)
        menu.addAction(self._drive_header)
        self._drive_actions = {}        # letter(大写) -> QAction(可勾选)
        self._drive_menu = menu         # 留引用,_populate_drives 往里插盘符项
        self._drive_sep = menu.addSeparator()
        # ── 匹配选项 ──
        self._act_word = QAction("全字匹配", menu)
        self._act_word.setCheckable(True)
        self._act_word.triggered.connect(self._on_settings_changed)
        menu.addAction(self._act_word)
        self._act_case = QAction("区分大小写", menu)
        self._act_case.setCheckable(True)
        self._act_case.triggered.connect(self._on_settings_changed)
        menu.addAction(self._act_case)
        btn.setMenu(menu)
        self._settings_btn = btn
        return btn

    def _populate_drives(self, drives):
        """helper 就绪后,用其已索引盘符填充盘符勾选项(默认全勾=不过滤)。在盘符分隔线前插入。"""
        self._all_drives = list(drives)
        if not drives:
            return
        self._drive_header.setText("盘符")
        for letter in drives:
            act = QAction(f"{letter}:", self._drive_menu)
            act.setCheckable(True)
            act.setChecked(True)            # 默认全勾
            act.triggered.connect(self._on_settings_changed)
            self._drive_menu.insertAction(self._drive_sep, act)
            self._drive_actions[letter] = act
        self._on_settings_changed()         # 同步初始状态(全勾→全盘)

    def _on_engine_selected(self, kind):
        """搜索设置里切换引擎(见 ADR-0005):换后端 + 重置就绪态 + 重新探测/取盘符/重搜。

        engine_factory 负责 helper 生命周期(切自研拉起 helper、切走落盘退出)+ 持久化 + 回退。
        自研 helper 安装被拒(返回 None)→ 还原单选到当前引擎,不切。
        """
        if kind == self._engine.kind:
            return
        old = self._engine
        engine, actual_kind, err = self._engine_factory(kind)
        if engine is None:
            # 切换失败(如自研 helper 安装被拒):还原单选,提示
            self._engine_actions[old.kind].setChecked(True)
            if err:
                self._status.setText(err)
            return
        # 旧引擎收尾(自研→落盘退出;Everything→无操作)。若回退到原引擎则别把自己关了。
        if old.kind != actual_kind:
            try:
                old.shutdown()
            except Exception:
                pass
        self._engine = engine
        self._engine_actions[actual_kind].setChecked(True)   # 回退时同步到实际引擎
        # 清空旧引擎的盘符菜单项(避免混在一起)
        for act in list(self._drive_actions.values()):
            self._drive_menu.removeAction(act)
        self._drive_actions.clear()
        self._all_drives = []
        self._drives = set()
        self._drive_header.setText("盘符(载入中…)")
        # 重置就绪态,重新走"轮询就绪 → 取盘符"流程(自研需等 helper,Everything 立即就绪)
        self._ready = False
        self._count = 0
        self._tree.clear()
        self._box.setEnabled(False)
        self._status.setText(("已回退自研引擎,正在启动…" if err else
                              f"已切换到 {ENGINE_LABELS.get(actual_kind, actual_kind)},正在启动…"))
        self._ready_polls = 0
        self._ready_timer.start()
        QTimer.singleShot(0, self._poll_ready)

    def _on_settings_changed(self, _checked=False):
        """搜索设置变化:更新状态 + 按钮提示 + 立即重搜。"""
        self._whole_word = self._act_word.isChecked()
        self._case = self._act_case.isChecked()
        checked = {l.lower() for l, a in self._drive_actions.items() if a.isChecked()}
        # 全勾(或一个都没填充)= 不过滤(空集);否则限定勾选盘符
        all_letters = {l.lower() for l in self._drive_actions}
        self._drives = set() if (not checked or checked == all_letters) else checked
        # 按钮提示:有任一非默认设置时加 ●
        active = self._whole_word or self._case or bool(self._drives)
        self._settings_btn.setText("⚙ 搜索设置 ●" if active else "⚙ 搜索设置")
        self._do_search()

    def _refresh_index(self):
        """手动刷新索引:通知自研 helper 重新扫描磁盘,更新索引。用于文件有变化(新增/删除/改名)时主动更新。

        仅自研引擎可用(Everything 实时监控无需手动刷新)。操作:禁用搜索框+按钮 → 通知 helper 重扫 →
        重新轮询就绪态 → 完成后恢复 UI。重扫耗时 ~30 秒。
        """
        from app.file_search_client import IndexClient
        # 重入保护:旧 worker 仍在跑时不再起新的(按钮虽已同步禁用,这里兜底防止
        # 经其他路径重复触发导致旧 QThread 引用被覆盖、信号重复触发)。
        old = getattr(self, "_rescan_worker", None)
        if old is not None and old.isRunning():
            return
        self._is_refreshing = True  # 标记为刷新状态,就绪后显示"索引已更新"
        if self._refresh_btn:
            self._refresh_btn.setEnabled(False)
            self._refresh_btn.setText("🔄 扫描中...")
        self._box.setEnabled(False)
        self._ready = False
        self._ready_polls = 0
        self._status.setText("正在重新扫描磁盘(约 1-2 分钟)...")
        # 通知 helper 重建索引(后台线程,避免主线程卡)
        def _do_rescan():
            try:
                IndexClient().rebuild_index()  # 阻塞 ~30 秒
            except Exception:
                pass
        from PyQt6.QtCore import QThread, pyqtSignal
        class _RescanWorker(QThread):
            done = pyqtSignal()
            def run(self):
                _do_rescan()
                self.done.emit()
        self._rescan_worker = _RescanWorker()
        self._rescan_worker.done.connect(self._on_rescan_done)
        self._rescan_worker.start()

    def _on_rescan_done(self):
        """重扫完成:恢复按钮 + 重新探测就绪态。"""
        if self._refresh_btn:
            self._refresh_btn.setEnabled(True)
            self._refresh_btn.setText("🔄 刷新索引")
        self._status.setText("✅ 扫描完成！正在加载索引...")
        self._ready_timer.start()
        QTimer.singleShot(0, self._poll_ready)

    def _switch_mode(self, key):
        """切换搜索模式:普通/路径+名称切换显隐路径框并重搜;高级=弹对话框执行条件搜索。"""
        if key == "advanced":
            self._mode_btns["advanced"].setChecked(False)   # 高级是动作不是常驻态,不保持选中
            self._open_advanced()
            return
        for k, b in self._mode_btns.items():
            b.setChecked(k == key)
        new_match_path = (key == "path")
        if new_match_path != self._match_path:
            self._match_path = new_match_path
            self._path_box.setVisible(new_match_path)   # 路径模式才显示"路径含"框
            if not new_match_path:
                self._path_box.clear()                  # 切回普通:清掉路径过滤词
            self._do_search()                           # 模式变了立即重搜

    def _open_advanced(self):
        """弹高级搜索对话框;确定后后台执行条件搜索并展示结果。"""
        from app.advanced_search_window import AdvancedSearchDialog
        dlg = AdvancedSearchDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        cond = dlg.get_conditions()
        if not cond:
            self._status.setText("未设置任何高级条件。")
            return
        self._status.setText("高级搜索中…")
        self._adv_worker = _AdvWorker(self._engine, cond, drives=(self._drives or None))
        self._adv_worker.done.connect(self._on_adv_results)
        self._adv_worker.start()

    def _on_adv_results(self, results):
        self._render(results)

    # 各类型的 emoji 图标(美化菜单项;key 与 FILE_CATEGORIES 对应)
    _TYPE_ICONS = {"folder": "📁", "audio": "🎵", "archive": "🗜", "document": "📄",
                   "executable": "⚙", "image": "🖼", "video": "🎬"}

    def _build_type_button(self) -> QToolButton:
        """搜索栏后的「类型」抽屉:一个下拉,内含各文件类型的可勾选项(带图标),多选过滤。

        默认全不勾 = 不过滤(显示所有类型)。勾选若干则只显示这些类型。
        """
        btn = QToolButton()
        btn.setText("类型")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QToolButton {
                padding: 5px 14px; border: 1px solid #c9c9c9; border-radius: 6px;
                background: #fafafa;
            }
            QToolButton:hover { background: #f0f0f0; border-color: #9aa0a6; }
            QToolButton::menu-indicator {
                subcontrol-position: right center; subcontrol-origin: padding; right: 6px;
            }
            QMenu { padding: 6px; border: 1px solid #c9c9c9; border-radius: 8px; background: #ffffff; }
            QMenu::item {
                padding: 7px 28px 7px 12px; border-radius: 5px; margin: 1px 2px; font-size: 13px;
            }
            QMenu::item:selected { background: #e8f0fe; color: #1a73e8; }
            QMenu::indicator { width: 16px; height: 16px; left: 6px; }
            QMenu::separator { height: 1px; background: #ececec; margin: 5px 8px; }
        """)
        menu = QMenu(btn)
        self._type_actions = {}        # key -> QAction(可勾选)
        for key, name, _exts in FILE_CATEGORIES:
            icon = self._TYPE_ICONS.get(key, "")
            act = QAction(f"{icon}  {name}", menu)
            act.setCheckable(True)
            act.triggered.connect(self._on_type_changed)
            menu.addAction(act)
            self._type_actions[key] = act
        menu.addSeparator()
        clear = QAction("✕  清除筛选", menu)
        clear.triggered.connect(self._clear_types)
        menu.addAction(clear)
        btn.setMenu(menu)
        self._type_btn = btn
        return btn

    def _selected_types(self):
        """返回当前勾选的类型 key 集合;空 = 不过滤。"""
        return {k for k, a in self._type_actions.items() if a.isChecked()}

    def _on_type_changed(self, _checked=False):
        """类型勾选变化:更新按钮文字提示 + 立即重搜。"""
        sel = self._selected_types()
        self._type_btn.setText(f"类型({len(sel)})" if sel else "类型")
        self._do_search()

    def _clear_types(self):
        """清除所有类型勾选,恢复不过滤。"""
        for a in self._type_actions.values():
            a.setChecked(False)
        self._type_btn.setText("类型")
        self._do_search()

    def _on_text_changed(self, _text):
        """每次改动只重启防抖定时器;停手 _DEBOUNCE_MS 后才真搜。"""
        self._debounce.start()

    def _do_search(self):
        name_q = self._box.text().strip()
        path_q = self._path_box.text().strip() if self._match_path else ""
        key = (name_q, path_q)
        # 两个框都空 → 清空结果、提示;不发搜索
        if not name_q and not path_q:
            self._tree.clear()
            self._status.setText(self._engine.ready_text(self._count))
            return
        if self._worker is not None and self._worker.isRunning():
            self._pending = key        # 上一搜还没完,记下待搜,完事再补
            return
        self._status.setText("搜索中…")
        self._worker = _SearchWorker(self._engine, name_q, types=self._selected_types(),
                                     match_path=self._match_path, path_query=path_q,
                                     whole_word=self._whole_word, case=self._case,
                                     drives=(self._drives or None))
        self._worker.done.connect(self._on_results)
        self._worker.start()

    def _on_results(self, key, results):
        # 若搜索期间用户又改了词(名称或路径),丢弃本次结果,补搜最新的
        cur = (self._box.text().strip(),
               self._path_box.text().strip() if self._match_path else "")
        if cur != key:
            self._pending = None
            self._do_search()
            return
        self._pending = None
        self._render(results)

    def _render(self, results):
        self._tree.clear()
        items = []
        for is_dir, path in results:
            name = os.path.basename(path.rstrip("\\")) or path
            it = QTreeWidgetItem([("📁 " if is_dir else "📄 ") + name, path])
            it.setData(0, Qt.ItemDataRole.UserRole, path)
            items.append(it)
        self._tree.addTopLevelItems(items)
        n = len(results)
        if n >= _RESULT_LIMIT:
            self._status.setText(f"匹配较多,仅显示前 {_RESULT_LIMIT} 项,请细化关键词")
        else:
            self._status.setText(f"找到 {n} 项")

    def _selected_paths(self):
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self._tree.selectedItems()]

    def _on_activate(self, item, _col):
        """双击/回车:用默认程序打开该文件/文件夹。"""
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self._open_path(path)

    @staticmethod
    def _open_path(path):
        try:
            os.startfile(path)         # 默认程序打开(文件)或资源管理器打开(文件夹)
        except Exception:
            pass

    @staticmethod
    def _reveal_in_explorer(path):
        """在资源管理器中打开所在文件夹并选中该项。"""
        try:
            subprocess.run(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            pass

    def _on_context_menu(self, pos):
        paths = self._selected_paths()
        if not paths:
            return
        menu = QMenu(self)
        menu.addAction("打开").triggered.connect(lambda: [self._open_path(p) for p in paths])
        menu.addAction("打开所在文件夹").triggered.connect(
            lambda: self._reveal_in_explorer(paths[0]))
        menu.addSeparator()
        menu.addAction("复制路径").triggered.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(paths)))
        menu.addAction("复制文件名").triggered.connect(
            lambda: QGuiApplication.clipboard().setText(
                "\n".join(os.path.basename(p.rstrip("\\")) for p in paths)))
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def closeEvent(self, e):
        self.closed.emit()
        # 窗口设了 WA_DeleteOnClose:关窗即销毁。若后台 QThread 仍在跑,其 Python 对象随窗体
        # 回收会触发 Qt「QThread: Destroyed while thread is still running」abort 崩溃。
        # 与 OCRWorker/_TestWorker/_TranslateWorker 同一防御:逐个等其结束再放行。
        # _rescan_worker 阻塞约 30s、_ready/drives 探测数秒,故给足超时。
        self._stop_workers()
        super().closeEvent(e)

    def _stop_workers(self):
        """等所有在飞后台线程结束(防 WA_DeleteOnClose 下 QThread 运行中被销毁崩溃)。"""
        workers = [getattr(self, n, None) for n in
                   ("_worker", "_adv_worker", "_drives_worker", "_rescan_worker")]
        workers += list(getattr(self, "_ready_workers", []))
        for w in workers:
            try:
                if w is not None and w.isRunning():
                    w.wait(3000)
            except RuntimeError:
                pass   # 底层 C++ 对象可能已被回收,忽略
