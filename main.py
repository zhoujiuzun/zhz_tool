import sys
import ctypes
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import qInstallMessageHandler
from app.tray import TrayApp
from app.style import build_style, DEFAULT_THEME
from app.config import load_config
from app.version import APP_NAME


def _filter_qt_warnings(mode, context, message):
    # 全局样式用 px 定义字号,Qt 内部复制字体时读 pointSize() 得 -1,会反复打出
    # "QFont::setPointSize: Point size <= 0" 警告。该警告无害,仅是控制台噪音,
    # 这里只静音这一条,其余 Qt 信息照常输出。
    if "Point size <= 0" in message:
        return
    # pythonw 下无控制台,stderr 可能为 None,写前判断避免 AttributeError
    if sys.stderr is not None:
        sys.stderr.write(message + "\n")


def main():
    qInstallMessageHandler(_filter_qt_warnings)

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
