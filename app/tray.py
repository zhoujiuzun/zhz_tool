"""System tray application — main entry point for all features."""
import threading
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PyQt6.QtGui import QIcon, QPixmap, QColor, QDesktopServices
from PyQt6.QtCore import QObject, pyqtSignal, QThread, Qt, QTimer, QUrl

from app.config import (load_config, save_config, save_macro, load_macro,
                        list_macros, migrate_macro_play_hotkey)
from app.engines import run_ocr, warmup_all, reset_all
from app.screenshot import ScreenshotWidget
from app.result_window import ResultWindow
from app.settings_window import SettingsWindow
from app.clipboard_monitor import ClipboardMonitor
from app.autostart import is_autostart, set_autostart
from app.pin_window import PinWindow
from app.macro import MacroEngine
from app.hotkeys import HotkeyManager, parse_hotkey
from app.window_pin import WindowPinner
from app.style import build_style, DEFAULT_THEME
from app.version import APP_NAME
from app.updater import UpdateChecker


def _logo_path() -> str:
    """定位随包分发的 logo.png。

    打包态:文件被 spec 放进 `_MEIPASS/app/logo.png`(datas=('app/logo.png','app')),
    故 frozen 下 base 必须含 `app` 子目录——之前漏了这层导致打包后找不到、退回蓝块。
    开发态:`dirname(__file__)` 本就是 app 目录。
    """
    import sys, os
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "app")
    else:
        base = os.path.dirname(__file__)
    return os.path.join(base, "logo.png")


def _app_icon() -> QIcon:
    """加载 logo 作为应用图标;文件缺失/损坏时退回纯色方块,绝不因图标崩。"""
    icon = QIcon(_logo_path())
    if icon.isNull():
        px = QPixmap(32, 32)
        px.fill(QColor("#4A90D9"))
        icon = QIcon(px)
    return icon


class OCRWorker(QThread):
    """Run OCR in background thread to avoid blocking UI."""
    ocr_done = pyqtSignal(str, str)   # text, provider_name(避免覆盖 QThread.finished)
    failed   = pyqtSignal(str)        # error message

    def __init__(self, image_bytes: bytes, providers: list):
        super().__init__()
        self._image = image_bytes
        self._providers = providers

    def run(self):
        try:
            text, name = run_ocr(self._image, self._providers)
            self.ocr_done.emit(text, name)
        except Exception as e:
            self.failed.emit(str(e))


class TrayApp(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self._cfg = load_config()
        self._workers = []           # keep references alive
        self._result_windows = []
        self._pin_windows = []       # 贴图浮窗,保持引用
        self._screenshot_widget = None
        self._settings_win = None
        self._warmup_lock = threading.Lock()   # 串行化 warmup,避免并发改 providers 状态

        # Warmup: probe all OCR + translation providers in background
        self._rewarmup()

        # Tray icon + 全局应用图标(任务栏/各窗标题栏共用)
        icon = _app_icon()
        app.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, app)
        self._tray.setToolTip(APP_NAME)
        # 窗口置顶工具(全局热键触发 + 置顶窗辉光/优先级协调)
        self._window_pinner = WindowPinner(self._cfg.get("theme_color", DEFAULT_THEME))
        self._window_pinner.done.connect(self._on_pin_done)
        self._build_menu()
        self._tray.show()

        # Clipboard monitor
        self._monitor = ClipboardMonitor()
        self._monitor.image_detected.connect(self._on_clipboard_image)
        if self._cfg.get("clipboard_monitor"):
            self._monitor.start()

        # 宏引擎 + 全局热键(常驻 TrayApp,不可见、无新增托盘图标/菜单项)
        migrate_macro_play_hotkey()   # 一次性:旧全局回放热键/循环 → current 宏自己的文件
        self._macro_engine = MacroEngine()
        self._macro_engine.recorded.connect(self._on_macro_recorded)
        self._macro_engine.state_changed.connect(self._on_macro_state)
        self._macro_play_names = set()   # 已注册回放热键的宏名(用于重注册前注销)
        self._hotkey_mgr = HotkeyManager()
        app.installNativeEventFilter(self._hotkey_mgr)   # 必须应用级,见 ADR-0001
        self._register_macro_hotkeys()

        # 窗口置顶全局热键(默认 Ctrl+Alt+T,可在通用里改);WindowPinner 已在菜单前创建
        self._register_window_top_hotkey()
        self._register_file_search_hotkey()   # 文件搜索全局热键(默认关闭)
        self._search_win = None               # 当前文件搜索窗(单例)

        # 启动后延迟 5 秒静默检查更新:有新版才弹气泡,无新版/网络失败均不打扰
        self._update_checkers = []          # 持有 UpdateChecker 引用,防被 GC
        QTimer.singleShot(5000, lambda: self._check_update(silent=True))

    # ── Menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self):
        """按 feature_visibility 条件加菜单项。「设置」「退出」为红线项,永远显示。"""
        vis = self._cfg.get("feature_visibility", {})
        menu = QMenu()

        if vis.get("ocr", True):
            menu.addAction("截图识别").triggered.connect(self._start_screenshot)
        if vis.get("pin", True):
            menu.addAction("截图贴图").triggered.connect(self._start_pin)
        # 「置顶当前窗口」菜单项已移除:菜单触发取窗不可靠,改为只用全局热键(Ctrl+Alt+T)置顶。
        if vis.get("translate", True):
            menu.addAction("翻译").triggered.connect(self._open_translate)
        if vis.get("file_search", True):
            menu.addAction("文件搜索").triggered.connect(self._open_file_search)

        menu.addSeparator()

        if vis.get("autostart", True):
            autostart_act = menu.addAction("开机自启动")
            autostart_act.setCheckable(True)
            autostart_act.setChecked(is_autostart())
            autostart_act.triggered.connect(self._on_autostart_toggled)
            self._autostart_act = autostart_act
        else:
            self._autostart_act = None
        if vis.get("reset_engine", True):
            menu.addAction("重置接口状态").triggered.connect(self._reset_engine_state)

        menu.addSeparator()
        menu.addAction("设置").triggered.connect(self._open_settings)       # 红线:常显
        if vis.get("file_search", True):
            menu.addAction("停止后台索引").triggered.connect(self._stop_helper)
        menu.addSeparator()
        menu.addAction("退出").triggered.connect(self._quit)                # 红线:常显

        self._tray.setContextMenu(menu)

    def _check_update(self, silent=True):
        """后台查 GitHub 最新版。silent=True(开机自动)时只在有新版才提示;
        失败/无新版均不打扰。结果回 _on_update_checked。"""
        uc = UpdateChecker()
        self._update_checkers.append(uc)        # 持有引用,防线程对象被 GC
        uc.result_ready.connect(lambda r: self._on_update_checked(r, silent))
        # ★ 引用清理挂到 QThread 自带的 finished(run() 真正返回后才发),而非 result_ready。
        #   否则在 run() 内 emit 的 result_ready 回调里删引用 → 线程未结束就被 GC →
        #   Qt qFatal「QThread: Destroyed while thread is still running」→ 0xc0000409 闪退。
        uc.finished.connect(lambda c=uc: self._update_checkers.remove(c)
                            if c in self._update_checkers else None)
        uc.start()

    def _on_update_checked(self, result, silent):
        """检查结果(主线程)。有新版弹气泡,点击气泡打开 release 页。"""
        if not result or not result.get("has_update"):
            return                              # 无新版 / 失败 → 静默(开机检查不打扰)
        self._pending_update_url = result["url"]
        self._tray.messageClicked.connect(self._open_update_url)
        self._tray.showMessage(
            f"{APP_NAME} 有新版本",
            f"发现新版本 v{result['latest']}（当前 v{result['current']}），点此前往 GitHub 下载。",
            QSystemTrayIcon.MessageIcon.Information, 8000)

    def _open_update_url(self):
        """点击更新气泡:打开 GitHub release 页;只触发一次后断开连接。"""
        url = getattr(self, "_pending_update_url", None)
        if url:
            QDesktopServices.openUrl(QUrl(url))
        try:
            self._tray.messageClicked.disconnect(self._open_update_url)
        except Exception:
            pass

    def _toggle_window_top(self):
        """toggle 当前前台窗口的置顶状态(全局热键触发)。"""
        self._window_pinner.toggle_foreground()

    def _register_window_top_hotkey(self):
        """注册/重注册窗口置顶全局热键(默认 Ctrl+Alt+T,可配,留空则注销)。

        随 app 常驻(不受宏总开关影响);窗口置顶模块被隐藏时也注销。
        """
        self._cfg = load_config()
        key = (self._cfg.get("window_top_hotkey", "Ctrl+Alt+T") or "").strip()
        vis = self._cfg.get("feature_visibility", {}).get("window_top", True)
        if not key or not vis:
            self._hotkey_mgr.unregister("window_top")
            return
        if not self._hotkey_mgr.register("window_top", key, self._toggle_window_top):
            self._tray.showMessage(
                "窗口置顶", f"热键「{key}」注册失败(可能被占用),请在设置→通用改键。",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _register_file_search_hotkey(self):
        """注册/重注册文件搜索全局热键(默认关闭=空;可在通用里自定义)。

        留空或模块隐藏则注销。键按下=打开文件搜索窗(等同点托盘「文件搜索」)。
        """
        self._cfg = load_config()
        key = (self._cfg.get("file_search_hotkey", "") or "").strip()
        vis = self._cfg.get("feature_visibility", {}).get("file_search", True)
        if not key or not vis:
            self._hotkey_mgr.unregister("file_search")
            return
        if not self._hotkey_mgr.register("file_search", key, self._open_file_search):
            self._tray.showMessage(
                "文件搜索", f"热键「{key}」注册失败(可能被占用),请在设置→通用改键。",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _on_pin_done(self, msg: str):
        if msg:   # 空串=静默
            self._tray.showMessage("窗口置顶", msg,
                                   QSystemTrayIcon.MessageIcon.Information, 2500)

    # ── Screenshot (OCR) ───────────────────────────────────────────────────────
    def _start_screenshot(self):
        self._begin_capture(self._run_ocr)

    # ── Screenshot (Pin / 贴图) ──────────────────────────────────────────────────
    def _start_pin(self):
        self._begin_capture(self._create_pin)

    def _begin_capture(self, on_captured):
        """发起框选。框选前临时隐藏所有贴图浮窗,避免遮挡框选区;框选结束后恢复。"""
        # 已有截图窗在进行中则忽略重复触发,避免叠多个全屏遮罩
        if self._screenshot_widget is not None:
            return
        hidden = [w for w in self._pin_windows if w.isVisible()]
        for w in hidden:
            w.hide()

        # 用每次捕获独立的闭包守卫(而非实例属性),避免重入时第二次把标志重置、
        # 导致第一次的恢复逻辑被误判已执行。
        restored = {"done": False}

        def restore():
            if restored["done"]:
                return
            restored["done"] = True
            for w in hidden:
                w.show()
            if hidden:
                self._window_pinner.reassert()   # 重新 show 会打乱层级,按总顺序压回

        def handle(image_bytes, rect):
            restore()
            on_captured(image_bytes, rect)

        def on_destroyed():
            restore()
            self._screenshot_widget = None

        # 延迟到下一次事件循环再建窗,让 hide() 先生效,避免 processEvents 重入
        def create():
            self._screenshot_widget = ScreenshotWidget()
            # WA_DeleteOnClose 确保 close()(无论框选成功还是 Esc 取消)都会触发
            # destroyed,从而恢复浮窗;否则取消路径下浮窗会一直隐藏。
            self._screenshot_widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._screenshot_widget.captured.connect(handle)
            self._screenshot_widget.destroyed.connect(on_destroyed)
            self._screenshot_widget.show()

        QTimer.singleShot(50, create)

    def _create_pin(self, image_bytes: bytes, rect):
        win = PinWindow(image_bytes, rect)
        self._pin_windows.append(win)
        win.closed.connect(lambda w=win: self._drop_from(self._pin_windows, w))
        win.show()                       # 先 show,winId 才有效
        self._window_pinner.register_pin(win)   # 纳入统一 z-order(贴图层恒高于外部置顶窗)

    # ── Translate (单独调用译文窗) ───────────────────────────────────────────────
    def _open_translate(self):
        win = ResultWindow(initial_text="", provider_name=None)
        self._result_windows.append(win)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        win.destroyed.connect(lambda *_: self._drop_from(self._result_windows, win))
        win.show()

    # ── File Search ───────────────────────────────────────────────────────────
    def _open_file_search(self):
        """打开文件搜索窗。首次会装提权计划任务(弹一次 UAC),之后静默拉起 helper。

        关窗时通知 helper 落盘并退出(按搜索窗启停,见 ADR-0004)。
        """
        if getattr(self, "_search_win", None) is not None:
            self._search_win.raise_()
            self._search_win.activateWindow()
            return
        from app import search_engine as se

        self._cfg = load_config()
        kind = se.default_engine_kind(self._cfg.get("file_search_engine", ""))
        engine, actual_kind, err = self._make_search_engine(kind)
        if engine is None:
            self._tray.showMessage("文件搜索", err or "搜索服务启动失败,已取消。",
                                   QSystemTrayIcon.MessageIcon.Warning, 4000)
            return

        from app.search_window import SearchWindow
        win = SearchWindow(engine, self._make_search_engine,
                           everything_available=se.everything_available())
        self._search_win = win
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        win.closed.connect(self._on_search_closed)
        win.destroyed.connect(lambda *_: setattr(self, "_search_win", None))
        win.show()

    def _make_search_engine(self, kind):
        """造引擎并处理生命周期(供 _open_file_search 初次 + SearchWindow 切换时调)。

        返回 (engine, actual_kind, err)。Everything 不可用会回退自研。自研引擎需确保提权 helper
        在跑(首次装计划任务弹一次 UAC);Everything 不碰 helper。切到非自研时把自研 helper 落盘退出。
        持久化用户选择(actual_kind)到 config(见 ADR-0005)。
        """
        from app import search_engine as se
        from app import file_search_task as task
        engine, actual_kind, fell_back = se.make_engine(kind)
        if actual_kind == se.ENGINE_NATIVE:
            # 自研:确保提权 helper 在跑。★ 不在此 probe(会卡主线程);交给 SearchWindow 的
            # _poll_ready 后台线程探测,未就绪时自动安装+拉起 helper(见 _open_file_search 注释)。
            # 首次装计划任务会弹一次 UAC(task.install),这里只确保任务已安装。
            if not task.is_installed():
                if not task.install():          # 弹一次 UAC;拒绝/失败则中止
                    return None, None, "需要安装搜索服务(管理员权限)才能使用,已取消。"
            # 静默拉起 helper(如果还没跑);不阻塞等待就绪(SearchWindow 会轮询)
            task.run()
        else:
            # 切到 Everything:把自研 helper 落盘退出(零后台,见 ADR-0004/0005)
            try:
                from app.file_search_client import IndexClient
                IndexClient().shutdown_helper()
            except Exception:
                pass
        # 持久化用户实际所用引擎
        if self._cfg.get("file_search_engine", "") != actual_kind:
            self._cfg["file_search_engine"] = actual_kind
            try:
                save_config(self._cfg)
            except Exception:
                pass
        err = "Everything 未运行,已回退自研引擎。" if fell_back else None
        return engine, actual_kind, err

    def _on_search_closed(self):
        """搜索窗关闭:根据 keep_helper_alive 决定是否落盘退出 helper(见配置)。Everything 无需收尾。"""
        if not self._cfg.get("keep_helper_alive", True):
            # 用户选择零后台:通知自研 helper 落盘退出
            try:
                from app.file_search_client import IndexClient
                IndexClient().shutdown_helper()
            except Exception:
                pass
        # else: helper 常驻,下次打开搜索窗即用(索引已在内存)
        self._search_win = None

    # ── Clipboard ─────────────────────────────────────────────────────────────
    def _on_clipboard_image(self, image_bytes: bytes):
        self._tray.showMessage("OCR", "检测到剪贴板图片，识别中…", QSystemTrayIcon.MessageIcon.Information, 2000)
        self._run_ocr(image_bytes, notify=False)

    # ── OCR dispatch ─────────────────────────────────────────────────────────
    def _run_ocr(self, image_bytes: bytes, rect=None, notify: bool = True):
        self._cfg = load_config()  # reload in case settings changed
        # 主动截图路径给「识别中」反馈,否则首个接口超时可达 20s,用户框选完会以为没反应。
        # 剪贴板路径已自带 toast,传 notify=False 避免重复弹。
        if notify:
            self._tray.showMessage("OCR", "识别中…", QSystemTrayIcon.MessageIcon.Information, 1500)
        worker = OCRWorker(image_bytes, self._cfg.get("providers", []))
        worker.ocr_done.connect(self._show_result)
        worker.failed.connect(self._show_error)
        # 用真正的 QThread.finished(run() 返回、线程已结束后才发)做清理,
        # 而非业务信号——否则线程仍在跑就丢引用,可能被 GC 触发销毁崩溃。
        worker.finished.connect(lambda w=worker: self._drop_worker(w))
        self._workers.append(worker)
        worker.start()

    def _drop_worker(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    @staticmethod
    def _drop_from(lst: list, item):
        """从引用列表里安全移除一个窗口/对象(幂等)。"""
        if item in lst:
            lst.remove(item)

    def _show_result(self, text: str, provider: str):
        win = ResultWindow(initial_text=text, provider_name=provider,
                           auto_translate=self._cfg.get("auto_translate", False))
        self._result_windows.append(win)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        win.destroyed.connect(lambda *_: self._drop_from(self._result_windows, win))
        win.show()

    def _show_error(self, msg: str):
        self._tray.showMessage("OCR 失败", msg, QSystemTrayIcon.MessageIcon.Critical, 4000)

    # ── Settings ──────────────────────────────────────────────────────────────
    def _open_settings(self):
        # 已打开则激活已有窗口,不重复创建(否则旧窗失去引用、残留或被 GC)
        existing = getattr(self, "_settings_win", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        self._settings_win = SettingsWindow(macro_engine=self._macro_engine,
                                            hotkey_mgr=self._hotkey_mgr)
        self._settings_win.applied.connect(self._on_settings_applied)
        self._settings_win.destroyed.connect(self._on_settings_closed)
        self._settings_win.show()

    def _on_settings_applied(self, *_):
        """设置实时变更后的轻量副作用:剪贴板启停 + 宏热键重注册 + 重建菜单 + 重套主题样式。

        重探测(warmup,联网,贵)不在此做——仍留到关窗 _on_settings_closed 一次性跑,
        避免每改一项就联网。
        """
        self._cfg = load_config()
        self._sync_clipboard_monitor()
        self._register_macro_hotkeys()
        self._register_window_top_hotkey()   # 用户可能改了置顶热键或隐藏了该模块
        self._register_file_search_hotkey()  # 用户可能改了文件搜索热键或隐藏了该模块
        self._build_menu()   # 功能可见性可能变了,按最新配置重建托盘菜单
        # 主题色可能变了:对整个 app 重套样式,所有窗口即时换色
        self._app.setStyleSheet(build_style(self._cfg.get("theme_color", DEFAULT_THEME)))
        self._window_pinner.set_theme(self._cfg.get("theme_color", DEFAULT_THEME))

    def _on_settings_closed(self, *_):
        self._settings_win = None
        self._rewarmup()
        self._sync_clipboard_monitor()   # 用户可能改了剪贴板监控开关,即时启停
        self._register_macro_hotkeys()   # 用户可能改了回放/录制热键或开关,重注册

    def _sync_clipboard_monitor(self):
        """按最新配置即时启停剪贴板监控,避免改了开关要重启才生效。

        在主线程调用(设置窗 destroyed),且 _rewarmup 已刷新 self._cfg。
        """
        want = self._cfg.get("clipboard_monitor", False)
        if want and not self._monitor.is_running():
            self._monitor.start()
        elif not want and self._monitor.is_running():
            self._monitor.stop()

    def _on_autostart_toggled(self, checked: bool):
        """写注册表;失败则回滚勾选态并提示,避免菜单显示与实际不一致。"""
        if not set_autostart(checked):
            if self._autostart_act is not None:
                self._autostart_act.setChecked(not checked)
            self._tray.showMessage(
                "开机自启动", "设置失败(可能被安全策略限制),请检查权限。",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _stop_helper(self):
        """手动停止后台索引 helper(释放内存)。下次打开文件搜索会重新拉起并扫描。"""
        try:
            from app.file_search_client import IndexClient
            if IndexClient().ping():
                IndexClient().shutdown_helper()
                self._tray.showMessage(
                    "后台索引", "已停止(释放约 50MB 内存)。下次打开文件搜索将重新扫描。",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
            else:
                self._tray.showMessage(
                    "后台索引", "未在运行,无需停止。",
                    QSystemTrayIcon.MessageIcon.Information, 2000)
        except Exception as e:
            self._tray.showMessage(
                "后台索引", f"停止失败:{e}",
                QSystemTrayIcon.MessageIcon.Warning, 3000)

    def _quit(self):
        self._hotkey_mgr.unregister_all()
        self._macro_engine.shutdown()   # 停录制/回放并等线程结束,避免销毁崩溃
        self._window_pinner.unpin_all()  # 解除所有外部窗口置顶,不留副作用
        try:
            from app.file_search_client import IndexClient
            IndexClient().shutdown_helper()   # 通知文件搜索 helper 落盘退出,不留提权进程
        except Exception:
            pass
        try:
            self._monitor.stop()        # 停剪贴板轮询定时器
        except Exception:
            pass
        # 等在飞的 OCR worker 结束,避免 QThread 仍运行时随进程销毁而崩溃
        for w in list(self._workers):
            if w.isRunning():
                w.wait(2000)
        self._app.quit()

    # ── 宏:全局热键 + 回放/录制 ────────────────────────────────────────────────
    def _macro_control_vks(self, name: str) -> set:
        """录制 name 这条宏时要忽略的控制键 vk 集合 = 该宏自己的回放热键 + 全局 F9。

        否则按这两个键(停止录制 / 它自己的回放键)会被 pynput 底层钩子录进宏序列。
        """
        cfg = load_config().get("macro", {})
        keys = [cfg.get("stop_record_hotkey", "F9"), load_macro(name).get("hotkey", "")]
        vks = set()
        for key in keys:
            parsed = parse_hotkey(key) if key else None
            if parsed:
                vks.add(parsed[1])
        return vks

    def _register_macro_hotkeys(self):
        """注册/重注册宏热键:每条宏各自的回放热键 + 全局 F9 录制启停。

        - 宏总开关关闭:全部注销,不抢键。
        - 录制进行中:不注册任何回放热键(录制时其他热键失效,不干扰录制)。
        - F9 录制键:随总开关常驻(空闲时要能按它开录)。
        每条宏的回放热键以 id `macro_play::<name>` 注册;撞键/被占则气泡提示该宏。
        """
        self._cfg = load_config()
        macro_cfg = self._cfg.get("macro", {})

        # 先注销上轮注册的所有回放热键(用跟踪集合,避免遗漏)
        for name in self._macro_play_names:
            self._hotkey_mgr.unregister(f"macro_play::{name}")
        self._macro_play_names = set()

        if not macro_cfg.get("enabled", False):
            self._hotkey_mgr.unregister("macro_rec")
            return

        # F9 启停录制:常驻
        rec_key = macro_cfg.get("stop_record_hotkey", "F9")
        if not self._hotkey_mgr.register("macro_rec", rec_key, self._toggle_macro_record):
            self._tray.showMessage(
                "宏", f"录制热键「{rec_key}」注册失败(可能被其他程序占用),请在设置中改键。",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

        # 录制期间:不注册任何回放热键
        if self._macro_engine.state == "recording":
            return

        # 遍历所有宏,各自注册其回放热键(空热键跳过)
        for name in list_macros():
            key = (load_macro(name).get("hotkey") or "").strip()
            if not key:
                continue
            ok = self._hotkey_mgr.register(
                f"macro_play::{name}", key,
                lambda n=name: self._toggle_macro_play_for(n))
            if ok:
                self._macro_play_names.add(name)
            else:
                self._tray.showMessage(
                    "宏", f"宏「{name}」的热键「{key}」注册失败(可能被占用或与其他宏冲突)。",
                    QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _toggle_macro_play_for(self, name: str):
        """某条宏的回放热键回调:录制中忽略;回放中→停;空闲→回放 name(用它自己的循环设置)。"""
        if self._macro_engine.state == "recording":
            return
        if not load_config().get("macro", {}).get("enabled", False):
            return  # 总开关关闭:防御性兜底
        # 正在回放:按任意回放键即停止(同时只跑一个)
        if self._macro_engine.state == "playing":
            self._macro_engine.stop_play()
            self._tray.showMessage("宏", "■ 已停止回放。",
                                   QSystemTrayIcon.MessageIcon.Information, 1500)
            return
        macro = load_macro(name)
        actions = macro.get("actions", [])
        if not actions:
            self._tray.showMessage("宏", f"宏「{name}」没有动作。",
                                   QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        self._tray.showMessage("宏", f"▶ 开始回放宏「{name}」…",
                               QSystemTrayIcon.MessageIcon.Information, 1500)
        self._macro_engine.toggle_play(actions, macro.get("loop_mode", "once"),
                                       macro.get("loop_count", 1))

    def _toggle_macro_record(self):
        """录制启停热键(F9)回调:空闲→开始录制当前宏;录制中→停止;回放中→忽略。"""
        if self._macro_engine.state == "playing":
            return  # 回放中不录制
        cfg = load_config().get("macro", {})
        if not cfg.get("enabled", False):
            return  # 总开关关闭:防御性兜底(正常此时热键已未注册)
        if self._macro_engine.state == "recording":
            self._macro_engine.stop_record()
            self._tray.showMessage("宏", "■ 已停止录制。",
                                   QSystemTrayIcon.MessageIcon.Information, 1500)
            return
        # 空闲:开始录制当前选中宏。把回放/录制两个控制键放进 ignore,免被录进序列。
        name = cfg.get("current", "")
        if not name:
            self._tray.showMessage("宏", "尚未选择宏,请在设置→宏中选择或新建。",
                                   QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        try:
            self._macro_engine.start_record(ignore_vks=self._macro_control_vks(name))
        except Exception as e:
            self._tray.showMessage("宏", f"无法开始录制:{e}",
                                   QSystemTrayIcon.MessageIcon.Warning, 4000)
            return
        rec_key = cfg.get("stop_record_hotkey", "F9")
        self._tray.showMessage("宏", f"● 开始录制宏「{name}」…再按 {rec_key} 停止。",
                               QSystemTrayIcon.MessageIcon.Information, 2000)

    def _on_macro_state(self, state: str):
        """录制期间屏蔽所有回放热键,使录制不受其他热键干扰;录制结束恢复。

        F9(录制启停)由 _register_macro_hotkeys 常驻,这里不动它——它正是停止录制的键。
        """
        if state == "recording":
            # 录制时其他热键失效:注销所有已注册的回放热键
            for name in self._macro_play_names:
                self._hotkey_mgr.unregister(f"macro_play::{name}")
            self._macro_play_names = set()
        else:
            # 回到空闲/回放:按当前配置恢复各宏回放热键(总开关关则不注册)
            self._register_macro_hotkeys()

    def _on_macro_recorded(self, actions: list, screen: list):
        """托盘层也持久化录制结果,防止录制中关了设置窗导致丢失。

        先读回宏保留其 hotkey/loop 等每宏字段,只更新 actions/screen,避免冲掉热键。
        """
        name = load_config().get("macro", {}).get("current", "")
        if name:
            macro = load_macro(name)
            macro["name"] = name
            macro["screen"] = screen
            macro["actions"] = actions
            save_macro(name, macro)

    def _reset_engine_state(self):
        """重置接口状态(方案乙):两引擎都忘掉首选 + 重新探测可达性。"""
        reset_all()
        self._rewarmup()
        self._tray.showMessage("OCR", "已重置接口状态,正在重新检测可达性…",
                               QSystemTrayIcon.MessageIcon.Information, 2000)

    def _rewarmup(self):
        self._cfg = load_config()
        cfg = self._cfg

        def run():
            # 串行化:多次触发(启动/关设置窗/重置)时排队执行,不并发改 providers 状态
            with self._warmup_lock:
                warmup_all(cfg)

        threading.Thread(target=run, daemon=True).start()
