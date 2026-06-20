# -*- coding: utf-8 -*-
"""宏(动作序列)引擎:录制 + 回放。

- 录制:用 pynput 全局钩子捕获鼠标(移动轨迹/点击/滚轮/侧键)与键盘事件。
- 回放:用 ctypes SendInput 注入,与项目其余 Win32 调用同栈,零额外注入依赖。
- 挂在 TrayApp 上常驻;同时只跑一个(录制或回放)。

动作 schema(紧凑键名,轨迹动辄上千条,省体积):
  移动   {"t":"move",  "x":, "y":, "rel":bool, "d":delay}
  鼠标键 {"t":"btn",   "b":"left|right|middle|x1|x2", "down":bool, "x":, "y":, "d":}
  滚轮   {"t":"scroll","dx":, "dy":, "d":}
  键盘   {"t":"key",   "vk":, "down":bool, "d":}
其中 d = 距上一个动作的间隔秒数(回放据此还原原速节奏)。

高层动作(手动编辑产物):
  点击   {"t":"click", "b":, "act":"click|double|down|up", "x"?:, "y"?:, "rel":bool, "d":}
  按键   {"t":"keytap","vk":, "mods":[vk,...], "act":"tap|down|up", "d":}
  移动   {"t":"move",  "x":, "y":, "rel":bool, "d":}   (复用底层 move,可带 rel)
  滚轮   {"t":"scroll","dx":, "dy":, "d":}
  等待   {"t":"wait",  "d":, "rand":随机上限秒}
说明:
  - rel=True:坐标是「回放时当前光标位置」的偏移量(可负),否则为屏幕绝对坐标。
  - click.act:down/up 只按下/只松开;click 按下+松开;double 连点两次。
    兼容旧数据:无 act 时按 double 字段推断(double→double,否则 click)。
  - keytap.act:tap 为完整敲击(修饰键包裹);down/up 仅作用主键(不含修饰键)。
  - wait.rand:实际等待 = d + 0~rand 的随机量,用于拟人/防机械节奏。
"""
import time
import random
import ctypes
from ctypes import wintypes
from PyQt6.QtCore import QObject, QThread, pyqtSignal

user32 = ctypes.windll.user32

# ── SendInput 结构体 ──────────────────────────────────────────────────────────
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
XBUTTON1, XBUTTON2 = 0x0001, 0x0002
WHEEL_DELTA = 120

KEYEVENTF_KEYUP = 0x0002

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))


def _mouse_input(flags, data=0, dx=0, dy=0):
    extra = ctypes.c_ulong(0)
    return INPUT(type=INPUT_MOUSE, union=_INPUTunion(mi=MOUSEINPUT(
        dx, dy, data, flags, 0, ctypes.pointer(extra))))


def _key_input(vk, up):
    extra = ctypes.c_ulong(0)
    flags = KEYEVENTF_KEYUP if up else 0
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTunion(ki=KEYBDINPUT(
        vk, 0, flags, 0, ctypes.pointer(extra))))


# 鼠标键 → (down标志, up标志, mouseData)
_BTN_FLAGS = {
    "left":   (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "right":  (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "x1":     (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "x2":     (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


def _resolve_xy(a: dict):
    """解析动作的目标坐标。rel=True 时以当前光标为原点偏移,否则为绝对屏幕坐标。

    返回 (x, y);动作未带坐标时返回 None。
    """
    if a.get("x") is None or a.get("y") is None:
        return None
    x, y = int(a["x"]), int(a["y"])
    if a.get("rel"):
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x + x, pt.y + y
    return x, y


def _click_act(a: dict) -> str:
    """取 click 动作的子动作。兼容旧数据(无 act 字段时按 double 推断)。"""
    act = a.get("act")
    if act:
        return act
    return "double" if a.get("double") else "click"


def _do_action(a: dict):
    """回放单个动作(注入到当前光标处或绝对/相对坐标)。

    底层动作(录制产物):move / btn(按下或抬起,分开) / scroll / key(按下或抬起,分开)。
    高层动作(手动编辑产物,一行即一个完整意图):
      click  {"t":"click","b":,"act":"click|double|down|up","x"?:,"y"?:,"rel":}
      keytap {"t":"keytap","vk":,"mods":[vk,...],"act":"tap|down|up"}
      wait   {"t":"wait"}                                  纯等待(时长由 d/rand 体现)
    """
    t = a.get("t")
    if t == "move":
        xy = _resolve_xy(a) or (int(a.get("x", 0)), int(a.get("y", 0)))
        user32.SetCursorPos(xy[0], xy[1])
    elif t == "btn":
        # 先把光标移到记录坐标,再按/抬,保证落点准确
        xy = _resolve_xy(a)
        if xy is not None:
            user32.SetCursorPos(xy[0], xy[1])
        down_f, up_f, data = _BTN_FLAGS.get(a.get("b", "left"), _BTN_FLAGS["left"])
        _send(_mouse_input(down_f if a.get("down", True) else up_f, data))
    elif t == "scroll":
        if a.get("dy"):
            _send(_mouse_input(MOUSEEVENTF_WHEEL, int(a["dy"]) * WHEEL_DELTA))
        if a.get("dx"):
            _send(_mouse_input(MOUSEEVENTF_HWHEEL, int(a["dx"]) * WHEEL_DELTA))
    elif t == "key":
        _send(_key_input(int(a["vk"]), up=not a.get("down", True)))
    elif t == "click":
        # 高层点击:可选坐标(绝对/相对)→ 按子动作按下/松开
        xy = _resolve_xy(a)
        if xy is not None:
            user32.SetCursorPos(xy[0], xy[1])
        down_f, up_f, data = _BTN_FLAGS.get(a.get("b", "left"), _BTN_FLAGS["left"])
        act = _click_act(a)
        if act == "down":
            _send(_mouse_input(down_f, data))
        elif act == "up":
            _send(_mouse_input(up_f, data))
        else:  # click / double
            for _ in range(2 if act == "double" else 1):
                _send(_mouse_input(down_f, data))
                _send(_mouse_input(up_f, data))
    elif t == "keytap":
        # 高层按键:tap=修饰键包裹完整敲击;down/up 仅作用主键
        vk = int(a["vk"])
        act = a.get("act", "tap")
        if act == "down":
            _send(_key_input(vk, up=False))
        elif act == "up":
            _send(_key_input(vk, up=True))
        else:  # tap:依次按下修饰键 → 主键按下 → 抬主键 → 逆序抬修饰键
            mods = [int(v) for v in a.get("mods", [])]
            for m in mods:
                _send(_key_input(m, up=False))
            _send(_key_input(vk, up=False))
            _send(_key_input(vk, up=True))
            for m in reversed(mods):
                _send(_key_input(m, up=True))
    elif t == "wait":
        pass  # 等待时长由动作的 d/rand 字段在回放循环里 sleep 体现


def _pynput_btn_name(button) -> str:
    """pynput Button → 我们的鼠标键名。"""
    s = str(button)  # 形如 "Button.left" / "Button.x1"
    name = s.split(".")[-1]
    return {"left": "left", "right": "right", "middle": "middle",
            "x1": "x1", "x2": "x2"}.get(name, name)


def _pynput_vk(key):
    """pynput Key/KeyCode → 虚拟键码 vk。取不到返回 None。"""
    try:
        if hasattr(key, "vk") and key.vk is not None:
            return key.vk
        if hasattr(key, "value") and getattr(key.value, "vk", None) is not None:
            return key.value.vk
    except Exception:
        pass
    return None


class _Recorder:
    """用 pynput 全局钩子录制。录到的事件追加到 self.actions。

    ignore_vks:不录进序列的键 vk 集合(回放/停止录制等控制热键,否则会被录进去)。
    """
    def __init__(self, ignore_vks=None):
        from pynput import mouse, keyboard
        self._mouse_mod = mouse
        self._kbd_mod = keyboard
        self._ignore_vks = set(ignore_vks or ())
        self.actions = []
        self._last_t = None
        self._m_listener = None
        self._k_listener = None

    def _delay(self) -> float:
        now = time.time()
        d = 0.0 if self._last_t is None else now - self._last_t
        self._last_t = now
        return round(d, 4)

    def _on_move(self, x, y):
        self.actions.append({"t": "move", "x": x, "y": y, "d": self._delay()})

    def _on_click(self, x, y, button, pressed):
        self.actions.append({"t": "btn", "b": _pynput_btn_name(button),
                             "down": pressed, "x": x, "y": y, "d": self._delay()})

    def _on_scroll(self, x, y, dx, dy):
        self.actions.append({"t": "scroll", "dx": dx, "dy": dy, "d": self._delay()})

    def _on_press(self, key):
        vk = _pynput_vk(key)
        if vk is None or vk in self._ignore_vks:
            return
        self.actions.append({"t": "key", "vk": vk, "down": True, "d": self._delay()})

    def _on_release(self, key):
        vk = _pynput_vk(key)
        if vk is None or vk in self._ignore_vks:
            return
        self.actions.append({"t": "key", "vk": vk, "down": False, "d": self._delay()})

    def start(self):
        self._last_t = None
        self._m_listener = self._mouse_mod.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._k_listener = self._kbd_mod.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self._m_listener.start()
        self._k_listener.start()

    def stop(self):
        if self._m_listener:
            self._m_listener.stop()
        if self._k_listener:
            self._k_listener.stop()
        self._m_listener = self._k_listener = None
        return self.actions


class _Player(QThread):
    """在后台线程按原速回放动作序列,支持一次/固定次数/无限循环。"""
    finished_all = pyqtSignal()

    def __init__(self, actions: list, loop_mode: str, loop_count: int):
        super().__init__()
        self._actions = actions
        self._loop_mode = loop_mode
        self._loop_count = max(1, loop_count)
        self._stop = False

    def stop(self):
        self._stop = True

    def _sleep(self, secs: float):
        """可被 stop 打断的分段睡眠(长等待时也能及时停)。"""
        end = time.time() + secs
        while not self._stop:
            remaining = end - time.time()
            if remaining <= 0:
                break
            # max(0,…) 防御:while 判断与本行之间时间可能已越过 end,
            # 不夹一下会传负值给 time.sleep 抛 ValueError,直接打挂回放线程。
            time.sleep(max(0.0, min(0.02, remaining)))

    def run(self):
        loops = 0
        try:
            while not self._stop:
                had_delay = False
                for a in self._actions:
                    if self._stop:
                        break
                    d = a.get("d", 0) or 0
                    rand = a.get("rand", 0) or 0
                    if rand > 0:
                        d += random.uniform(0, rand)   # 随机抖动:拟人/防机械节奏
                    if d > 0:
                        had_delay = True
                        self._sleep(d)
                    if self._stop:
                        break
                    try:
                        _do_action(a)
                    except Exception:
                        pass  # 单个动作失败不该中断整条回放
                loops += 1
                if self._loop_mode == "once":
                    break
                if self._loop_mode == "count" and loops >= self._loop_count:
                    break
                # infinite/count 续轮:若整轮零延迟,夹 1ms 防 100% CPU 空转
                if not had_delay and not self._stop:
                    time.sleep(0.001)
        finally:
            # 无论正常结束还是异常,都必须发信号,否则引擎状态会卡在 "playing"
            self.finished_all.emit()


class MacroEngine(QObject):
    """宏引擎:挂 TrayApp 常驻。统一管录制与回放,同时只跑一个。

    信号供 UI 实时反映状态:
      state_changed(str): "idle" | "recording" | "playing"
      recorded(list, list): 录制结束,(actions, [屏幕宽,高])
    """
    state_changed = pyqtSignal(str)
    recorded = pyqtSignal(list, list)

    def __init__(self):
        super().__init__()
        self._state = "idle"
        self._recorder = None
        self._player = None

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, s: str):
        self._state = s
        self.state_changed.emit(s)

    # ── 录制 ────────────────────────────────────────────────────────────────
    def start_record(self, ignore_vks=None):
        if self._state != "idle":
            return False
        self._recorder = _Recorder(ignore_vks=ignore_vks)
        self._recorder.start()
        self._set_state("recording")
        return True

    def stop_record(self):
        if self._state != "recording" or self._recorder is None:
            return
        actions = self._recorder.stop()
        self._recorder = None
        self._set_state("idle")
        w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        self.recorded.emit(actions, [w, h])

    # ── 回放 ────────────────────────────────────────────────────────────────
    def start_play(self, actions: list, loop_mode: str = "once", loop_count: int = 1):
        if self._state != "idle" or not actions:
            return False
        self._player = _Player(actions, loop_mode, loop_count)
        self._player.finished_all.connect(self._on_play_finished)
        self._set_state("playing")
        self._player.start()
        return True

    def stop_play(self):
        if self._player is not None:
            self._player.stop()

    def _on_play_finished(self):
        p = self._player
        self._player = None
        if p is not None:
            p.wait(500)
            p.deleteLater()
        self._set_state("idle")

    # ── 启停 toggle(供 F6 热键与 Tab 开关共用) ──────────────────────────────
    def toggle_play(self, actions: list, loop_mode: str = "once", loop_count: int = 1):
        """正在回放则停;空闲则用给定序列开始回放。录制中则忽略。"""
        if self._state == "playing":
            self.stop_play()
        elif self._state == "idle":
            self.start_play(actions, loop_mode, loop_count)

    # ── 退出清理 ──────────────────────────────────────────────────────────────
    def shutdown(self):
        """app 退出前调用:停录制、停回放并等回放线程真正结束,
        避免 QThread 在仍运行时被销毁(崩溃),以及 pynput listener 残留。"""
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass
            self._recorder = None
        p = self._player
        if p is not None:
            p.stop()
            p.wait(2000)   # 最长等 2s 让回放线程退出 run()
            self._player = None
        self._state = "idle"

