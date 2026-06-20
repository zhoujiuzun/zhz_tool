# -*- coding: utf-8 -*-
"""全局热键设施(Windows).

用 Win32 RegisterHotKey + 应用级 QAbstractNativeEventFilter 捕获 WM_HOTKEY。
零新依赖(仅 ctypes)。详见 docs/adr/0001-global-hotkey-infrastructure.md。

两个 PyQt6 专属坑(违反则热键静默失效):
  1. 必须用应用级 native event filter,不能用 widget 的 nativeEvent
     —— hwnd=None 的 RegisterHotKey 把 WM_HOTKEY 投到线程消息队列,而非某个窗口。
  2. nativeEventFilter 返回值必须是 (bool, int) 元组(PyQt6 改了签名)。
"""
import ctypes
from ctypes import wintypes
from PyQt6.QtCore import QAbstractNativeEventFilter

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000   # 避免按住时反复触发

# 修饰键名 → MOD 标志
_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN,
}

# 键名 → 虚拟键码(virtual-key code)。覆盖功能键、字母、数字。
_VK = {f"f{i}": 0x70 + (i - 1) for i in range(1, 13)}          # F1..F12 = 0x70..0x7B
_VK.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})  # A..Z = 0x41..0x5A
_VK.update({str(d): 0x30 + d for d in range(10)})               # 0..9 = 0x30..0x39
_VK.update({
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "pagedown": 0x22,
})


def parse_hotkey(text: str):
    """把 'F6' / 'Ctrl+Shift+K' 解析成 (modifiers, vk)。失败返回 None。"""
    if not text:
        return None
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            key = p
    if key is None:
        return None
    vk = _VK.get(key) or _VK.get(key.upper())
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


class HotkeyManager(QAbstractNativeEventFilter):
    """注册/注销全局热键,把 WM_HOTKEY 路由到回调。

    用法:
        mgr = HotkeyManager()
        app.installNativeEventFilter(mgr)        # 必须应用级
        mgr.register("play", "F6", on_toggle)
        ...
        mgr.unregister_all()                     # 退出前
    """
    def __init__(self):
        super().__init__()
        self._next_id = 1
        self._by_name = {}    # name -> (hotkey_id, callback)
        self._by_id = {}      # hotkey_id -> callback

    def register(self, name: str, hotkey_text: str, callback) -> bool:
        """注册一个命名热键。同名先注销旧的。成功返回 True。

        失败原因通常是热键被别的程序占用(GetLastError=1409),
        此时返回 False,由调用方提示用户改键。
        """
        self.unregister(name)
        parsed = parse_hotkey(hotkey_text)
        if parsed is None:
            return False
        mods, vk = parsed
        hk_id = self._next_id
        self._next_id += 1
        if not user32.RegisterHotKey(None, hk_id, mods, vk):
            return False
        self._by_name[name] = (hk_id, callback)
        self._by_id[hk_id] = callback
        return True

    def unregister(self, name: str):
        entry = self._by_name.pop(name, None)
        if entry:
            hk_id, _ = entry
            user32.UnregisterHotKey(None, hk_id)
            self._by_id.pop(hk_id, None)

    def unregister_all(self):
        for hk_id in list(self._by_id):
            user32.UnregisterHotKey(None, hk_id)
        self._by_id.clear()
        self._by_name.clear()

    # ── 坑 1+2:应用级 filter + (bool, int) 返回值 ──────────────────────────
    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                cb = self._by_id.get(msg.wParam)
                if cb:
                    cb()
                    return True, 0
        return False, 0
