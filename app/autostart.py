"""Auto-start on Windows login via registry."""
import sys
import os
import logging
import winreg

_log = logging.getLogger(__name__)

APP_NAME = "OCRTool"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_cmd() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # development: use pythonw to avoid a console window
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    return f'"{pythonw}" "{script}"'


def set_autostart(enabled: bool) -> bool:
    """写入/删除开机自启注册表项。成功返回 True,失败返回 False(不抛异常)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_cmd())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        _log.warning("写入开机自启注册表失败:%s", e)
        return False


def is_autostart() -> bool:
    """是否已设置开机自启,且注册的命令与当前程序路径一致(路径变化视为未启用)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return value == _launch_cmd()
    except FileNotFoundError:
        return False
    except OSError as e:
        _log.warning("读取开机自启注册表失败:%s", e)
        return False
