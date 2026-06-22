import sys
import os
import ctypes
import faulthandler
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import qInstallMessageHandler
from app.tray import TrayApp
from app.style import build_style, DEFAULT_THEME
from app.config import load_config
from app.version import APP_NAME

_crash_file = None      # 崩溃日志文件句柄(faulthandler + 异常钩子 + Qt 消息共用)


def _install_crash_logging():
    """把崩溃现场写到 ~/.ocr_tool/crash.log。

    faulthandler 抓原生致命错误(如 0xc0000409:在非 GUI 线程碰 Qt 对象触发的 abort)的
    Python 调用栈;sys.excepthook 抓主线程未捕获异常;threading.excepthook 抓子线程异常。
    打包态(console=False)无 stderr,这是唯一能留下崩溃现场的途径。
    """
    global _crash_file
    try:
        d = os.path.expanduser("~/.ocr_tool")
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, "crash.log"), "a", encoding="utf-8", buffering=1)
        _crash_file = f
        faulthandler.enable(file=f, all_threads=True)
        import datetime, traceback, threading

        def _hook(exc_type, exc, tb):
            f.write(f"\n[{datetime.datetime.now().isoformat()}] 未捕获异常(主线程):\n")
            traceback.print_exception(exc_type, exc, tb, file=f)
            f.flush()
        sys.excepthook = _hook

        def _thook(args):
            f.write(f"\n[{datetime.datetime.now().isoformat()}] 未捕获异常(线程 {args.thread.name}):\n")
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
            f.flush()
        threading.excepthook = _thook
    except Exception:
        pass


def _filter_qt_warnings(mode, context, message):
    # 全局样式用 px 定义字号,Qt 内部复制字体时读 pointSize() 得 -1,会反复打出
    # "QFont::setPointSize: Point size <= 0" 警告。该警告无害,仅是控制台噪音,
    # 这里只静音这一条,其余 Qt 信息照常输出。
    if "Point size <= 0" in message:
        return
    # ★ Qt 在 abort(0xc0000409)前会把致命原因作为一条消息发出来。打包/pythonw 下
    # stderr=None 会把它丢掉 —— 这正是"崩了却看不到原因"的根因。故所有 Qt 消息也写崩溃日志。
    if _crash_file is not None:
        try:
            import datetime
            ctx = f" ({context.file}:{context.line})" if context and context.file else ""
            _crash_file.write(f"[{datetime.datetime.now().isoformat()}] [Qt:{int(mode)}]{ctx} {message}\n")
            _crash_file.flush()
        except Exception:
            pass
    # pythonw 下无控制台,stderr 可能为 None,写前判断避免 AttributeError
    if sys.stderr is not None:
        sys.stderr.write(message + "\n")


def main():
    # 文件搜索 helper 模式:由计划任务以管理员身份拉起(exe --file-search-helper)。
    # 这是独立提权进程,不起 GUI、不占单实例锁、不碰 Qt —— 尽早分流,避免拉起整套 GUI。
    if "--file-search-helper" in sys.argv:
        from app.file_search_service import main as helper_main
        sys.exit(helper_main())

    _install_crash_logging()   # 先装:下次闪退会在 ~/.ocr_tool/crash.log 留调用栈/Qt 致命原因
    qInstallMessageHandler(_filter_qt_warnings)   # 消息处理器依赖 _crash_file,故在其后

    # 单实例锁:已有实例在跑则直接退出。CreateMutexW 失败(句柄 0)时不拦截,照常启动。
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "OCRTool_SingleInstance")
    if mutex and kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    theme = load_config().get("theme_color", DEFAULT_THEME)
    app.setStyleSheet(build_style(theme))
    tray = TrayApp(app)
    # mutex 句柄需存活至进程结束(持有以维持单实例锁),进程退出由 OS 回收
    app._single_instance_mutex = mutex
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
