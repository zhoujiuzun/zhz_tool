# -*- coding: utf-8 -*-
"""窗口置顶工具:用全局热键 toggle「当前前台窗口」的置顶状态。

交互(见 docs/CONTEXT.md「窗口置顶」):看哪个窗口就让它在前台(点一下它),按全局热键
(默认 Ctrl+Alt+T,可在通用里改)→ 该窗口被钉到最上层;再按一次 → 取消。

为何不用「点击拾取」:全屏覆盖拾取方案焦点/时序脆弱,且不透明覆盖会铺满整屏(白屏)。
PowerToys「Always on Top」即用本方案——取前台窗 + 翻转 WS_EX_TOPMOST,简单可靠。

只操作窗口层级(Win32 SetWindowPos),不取图/不识别/不读内容。
"""
import ctypes
import os
from ctypes import wintypes
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, Qt, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


user32 = ctypes.windll.user32

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
GW_HWNDNEXT = 2          # GetWindow:取 z-order 中的下一个(更下层)窗口

# ⚠️ 显式声明签名:64 位 Windows 上 HWND 是 64 位指针,ctypes 默认按 32 位 c_int
# 处理会截断句柄,导致调用静默失败。
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]   # 是否最小化
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetTopWindow.argtypes = [wintypes.HWND]      # 取 z-order 最顶窗(传 None=桌面)
user32.GetTopWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]   # 查按键瞬时状态(判断左键是否按住=拖动)
user32.GetAsyncKeyState.restype = ctypes.c_short
VK_LBUTTON = 0x01

# 前台切换事件钩子:Windows 每次切前台都同步回调,比定时器轮询可靠(无时序竞态)。
WINEVENT_OUTOFCONTEXT = 0x0000          # 回调投到本线程消息循环(GUI 线程),Qt 下安全
EVENT_SYSTEM_FOREGROUND = 0x0003
WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                   WinEventProcType, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL

# 任务栏/桌面/托盘浮层等 shell 外壳窗的类名:它们多属 explorer.exe(非本进程),但绝不该被
# 当作置顶目标。★关键:Win11 托盘"溢出区"浮层(TopLevelWindowForOverflowXamlIsland)——
# 应用图标在溢出区时,每次开菜单它都先成前台,曾污染 _last_ext_fg 导致置顶错乱(见调试)。
# 注意文件资源管理器(CabinetWClass)也是 explorer.exe,但不在此列——那是可置顶的真实窗。
_SHELL_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "WorkerW", "Progman",
    "NotifyIconOverflowWindow",               # Win10 旧版托盘溢出
    "TopLevelWindowForOverflowXamlIsland",    # ★ Win11 托盘溢出浮层(本 bug 元凶)
    "XamlExplorerHostIslandWindow",           # 任务视图/Alt-Tab 等
    "Windows.UI.Core.CoreWindow",             # 通知/开始菜单/操作中心等 shell 浮层
    "Xaml_WindowedPopupClass",
}


def _pid_of(hwnd) -> int:
    """取窗口所属进程 PID;失败返回 0。"""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _class_of(hwnd) -> str:
    """取窗口类名(用于识别任务栏/桌面等 shell 外壳窗)。"""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_of(hwnd) -> str:
    """取窗口标题(用于反馈气泡里指明操作了哪个窗);空标题返回占位串。"""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value or "(无标题窗口)"


def _is_pin_target(hwnd, own_pid) -> bool:
    """该窗口是否可作为置顶目标:有效窗 + 非本程序窗 + 非任务栏/托盘浮层等 shell 外壳窗。
    托盘菜单宿主、Win11 托盘溢出浮层都会被排除,从而回退到最后记录的真实外部窗。"""
    return bool(hwnd) and _pid_of(hwnd) != own_pid and _class_of(hwnd) not in _SHELL_CLASSES


def _dragging() -> bool:
    """左键当前是否按住——用作"正在拖动窗口"的判据。GetAsyncKeyState 最高位=当前按下。"""
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def _is_topmost(hwnd) -> bool:
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def _set_topmost(hwnd, on: bool) -> bool:
    flag = HWND_TOPMOST if on else HWND_NOTOPMOST
    return bool(user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


GLOW_MARGIN = 10        # 辉光环向外扩出的像素(逻辑像素)
GLOW_RINGS = 6          # 辉光层数,越多越柔


class PinGlow(QWidget):
    """贴在某外部窗口外圈的主题色辉光环。透明+点击穿透+置顶,定时跟随目标窗。"""
    def __init__(self, hwnd, theme_hex: str):
        super().__init__(None)
        self._hwnd = hwnd
        self._color = QColor(theme_hex)
        self._last = None          # 上次 (x,y,w,h),用于跳过无变化 + 区分移动/缩放
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._follow)
        self._timer.start(16)      # ~60fps,拖动时辉光跟手
        self._follow()

    def set_color(self, theme_hex: str):
        self._color = QColor(theme_hex)
        self.update()

    def _follow(self):
        """跟随目标窗:取真实 rect → 外扩 margin → setGeometry(逻辑像素)。"""
        if not user32.IsWindow(self._hwnd):
            self.close()
            return
        if user32.IsIconic(self._hwnd):       # 最小化:藏辉光
            if self.isVisible():
                self.hide()
            return
        r = wintypes.RECT()
        if not user32.GetWindowRect(self._hwnd, ctypes.byref(r)):
            return
        dpr = self.devicePixelRatioF() or 1.0
        m = GLOW_MARGIN
        x = int(r.left / dpr) - m
        y = int(r.top / dpr) - m
        w = int((r.right - r.left) / dpr) + 2 * m
        h = int((r.bottom - r.top) / dpr) + 2 * m
        geo = (x, y, w, h)
        if geo != self._last:
            if self._last is not None and (w, h) == (self._last[2], self._last[3]):
                self.move(x, y)            # 仅位移:不改尺寸→不触发整层重绘,跟手
            else:
                self.setGeometry(x, y, w, h)   # 尺寸变了才重设几何(会重绘)
            self._last = geo
        if not self.isVisible():
            self.show()

    def paintEvent(self, _e):
        """画外扩的圆角辉光环:由外到内多层、逐层加深,中心镂空不挡内容。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        m = GLOW_MARGIN
        for i in range(GLOW_RINGS):
            t = i / max(1, GLOW_RINGS - 1)          # 0(外)→1(内)
            alpha = int(40 + 150 * t)               # 外淡内浓
            c = QColor(self._color)
            c.setAlpha(alpha)
            pen = QPen(c)
            pen.setWidth(2)
            p.setPen(pen)
            off = m * (1 - t) + 1                    # 由外向内收
            rect = QRectF(off, off,
                          self.width() - 2 * off, self.height() - 2 * off)
            p.drawRoundedRect(rect, 8, 8)
        p.end()


class WindowPinner(QObject):
    """外部窗口置顶 + 全局 z-order 协调者。done(msg) 供托盘弹气泡反馈。

    维护一个跨子系统的总层级(见 docs/CONTEXT.md「置顶顺序」「贴图优先级」、docs/adr/0002、0003):
      下层 = 外部置顶窗,按「置顶时间」严格排序(后钉者在更先钉者之上);
      上层 = 贴图浮窗(注册进来),按「贴图优先级」1~5(1 最高)排,同级按"最后激活";
      且**所有贴图恒高于所有外部窗**。
    程序持续重申 z-order(点较早置顶的外部窗/低优先级贴图也压不上来)。
    每个外部置顶窗外圈有主题色辉光环(PinGlow)便于识别。
    """
    done = pyqtSignal(str)

    def __init__(self, theme_hex="#6FB3EC"):
        super().__init__()
        self._order = []            # 外部置顶 HWND,按置顶时间升序:[0]=最早(最低),[-1]=最新
        self._theme = theme_hex
        self._glows = {}            # hwnd -> PinGlow
        self._pins = []             # 注册的贴图浮窗(PinWindow 引用)
        self._pin_seq = {}          # pin -> 激活序号(越大=越近被点),同级用它排"点谁谁上"
        self._seq = 0               # 激活序号计数器
        self._own_pid = os.getpid()   # 本进程 PID:用来识别"本程序自己的窗口"(如托盘菜单宿主)
        self._last_ext_fg = None      # 最后一个"非本程序"的前台窗;热键触发时若前台非法则回退到它
        # 前台切换事件钩子:每次切前台同步记下"最后的外部前台窗",彻底取代轮询取窗(无竞态)。
        # 回调对象须存活,故存成实例属性,否则被 GC 回收→钩子静默失效。
        self._winevent_proc = WinEventProcType(self._on_foreground_changed)
        self._winevent_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
            self._winevent_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        self._enforce_timer = QTimer(self)   # 持续把窗按总层级压回,维持严格上下层
        self._enforce_timer.timeout.connect(self._enforce)
        self._enforce_timer.start(40)        # 前台变化检测够快,重排只在被打乱时发生

    def set_theme(self, theme_hex: str):
        """主题色变更:更新色并刷新所有现存辉光层。"""
        self._theme = theme_hex
        for g in self._glows.values():
            g.set_color(theme_hex)

    # ── 贴图浮窗注册(由 tray 在创建 PinWindow 时调用,纳入统一 z-order)──────────
    def register_pin(self, pin):
        """登记一张贴图浮窗:接它的 activated/priority_changed/closed 信号并立刻重排。"""
        if pin in self._pins:
            return
        self._pins.append(pin)
        self._seq += 1
        self._pin_seq[pin] = self._seq
        pin.activated.connect(lambda p=pin: self._on_pin_activated(p))
        pin.priority_changed.connect(self._restack)
        pin.closed.connect(lambda p=pin: self.unregister_pin(p))
        self._restack()

    def unregister_pin(self, pin):
        self._pin_seq.pop(pin, None)
        if pin in self._pins:
            self._pins.remove(pin)
        self._restack()

    def _on_pin_activated(self, pin):
        """某贴图被点击:刷新其激活序(同级内升到最上),再重排维持跨级严格。"""
        self._seq += 1
        self._pin_seq[pin] = self._seq
        self._restack()

    def reassert(self):
        """供 tray 在"框选结束恢复隐藏的贴图"等时机调用,重新压好全栈层级。"""
        self._restack()

    def _add_glow(self, hwnd):
        if hwnd in self._glows:
            return
        try:
            self._glows[hwnd] = PinGlow(hwnd, self._theme)
        except Exception:
            pass

    def _remove_glow(self, hwnd):
        g = self._glows.pop(hwnd, None)
        if g is not None:
            try:
                g.close()
            except Exception:
                pass

    def _prune(self):
        """剔除已关闭的窗(连带其辉光),保持 _order 干净。"""
        for hwnd in [h for h in self._order if not user32.IsWindow(h)]:
            self._order.remove(hwnd)
            self._remove_glow(hwnd)

    def _desired_order(self):
        """返回期望的总层级,从低到高(最后一个应在最上)。

        外部置顶窗(按置顶时间升序) → 贴图浮窗(按优先级 5→1,同级按激活序升序)。
        于是最高优先级、最近激活的贴图在最上;所有贴图恒高于所有外部窗。
        """
        order = list(self._order)                # 下层:外部窗,[0]最早→[-1]最新
        pins = [p for p in self._pins if p.is_on_top()]
        pins.sort(key=lambda p: (-p.priority(), self._pin_seq.get(p, 0)))
        for p in pins:
            try:
                order.append(int(p.winId()))
            except Exception:
                pass
        return order

    def _zorder_ok(self, desired):
        """读系统真实 z-order,判断 desired 里的窗是否已按"低→高"正确相对排列。

        只看被管理窗之间的相对次序(中间夹着别的窗无所谓)。已正确 → 返回 True,
        _enforce 即可跳过重排,避免无谓 SetWindowPos 引起重叠区闪烁。
        """
        if len(desired) < 2:
            return True
        managed = set(desired)
        # 从最顶窗起沿 z-order 向下走,收集被管理窗的出现次序(从高到低)
        seen_high_to_low = []
        hwnd = user32.GetTopWindow(None)
        guard = 0
        while hwnd and guard < 5000:
            if hwnd in managed:
                seen_high_to_low.append(hwnd)
                if len(seen_high_to_low) == len(desired):
                    break
            hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
            guard += 1
        # 期望从高到低 = desired 反序;与实际比较
        return seen_high_to_low == desired[::-1]

    def _restack(self):
        """按期望层级从低到高依次压到 topmost 顶——最后压的落在最上。用 SWP_NOACTIVATE,不抢焦点。"""
        for hwnd in self._desired_order():
            try:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            except Exception:
                pass

    def _enforce(self):
        """定时(40ms):① 记录"最后一个非本程序前台窗"(热键触发时前台非法则回退到它);
        ② 维持跨级严格层级,但**仅在你正拖动窗口时让步**,避免拖动中与 OS 反复抢位的闪烁。

        策略(见调试结论 + 用户要求):
        - z-order 没乱 → 不动(静止时零 SetWindowPos、不闪);
        - 乱了,且**左键正按住(=你正在拖某个窗)** → 让步,不抢,拖动期间不闪;
        - 乱了,且左键没按(静止 / 仅点击使用) → **立刻压回严格序**,严格遵守优先度。
        松开左键后下一 tick 即恢复严格顺序。故"让步"只在拖动的那段时间生效;不拖动时,
        无论你正点着/用着哪个窗,优先度都严格生效(后钉的恒高于先钉的)。
        """
        fg = user32.GetForegroundWindow()
        # ① 记录外部前台窗:排除无效窗 + 本程序自己的窗(托盘菜单宿主等) + 任务栏/桌面外壳窗
        if (fg and _pid_of(fg) != self._own_pid
                and _class_of(fg) not in _SHELL_CLASSES):
            self._last_ext_fg = fg
        if not self._order and not self._pins:
            return
        self._prune()
        if self._zorder_ok(self._desired_order()):
            return                                # ② 没乱,不动
        if _dragging():
            return                                # 正在拖动 → 让步,不抢(拖动中不闪)
        self._restack()                           # 不拖动 → 严格压回优先度顺序

    def _on_foreground_changed(self, hook, event, hwnd, idobj, idchild, thread, ttime):
        """前台切换事件回调(SetWinEventHook):每次切前台同步记下"最后的外部前台窗"。

        排除本程序窗口(托盘菜单宿主/贴图/设置窗)与任务栏/桌面外壳窗。这是取窗的**主**
        机制,确定无竞态;_enforce 里的同名记录留作冗余兜底。
        """
        if hwnd and _pid_of(hwnd) != self._own_pid and _class_of(hwnd) not in _SHELL_CLASSES:
            self._last_ext_fg = hwnd

    def toggle_foreground(self):
        """翻转当前前台窗口的置顶状态(仅全局热键调用)。读真实 WS_EX_TOPMOST 判断当前态。

        按热键时焦点还在目标窗上,实时前台可信;若它不是合法目标(本程序窗/shell 浮层),
        则回退到事件钩子记录的最后一个外部前台窗 _last_ext_fg。

        置顶 = 追加到 _order 末尾(成为最新/最高);取消 = 从 _order 移除。
        对已置顶窗"取消再置顶"(双按热键)即把它刷新为最新 → 提到最上。
        """
        fg = user32.GetForegroundWindow()
        if _is_pin_target(fg, self._own_pid):
            hwnd = fg                                 # 实时前台可信
        else:
            hwnd = self._last_ext_fg                  # 前台非法(本程序窗等)→回退
        if not hwnd or not user32.IsWindow(hwnd):
            self.done.emit("没有可置顶的前台窗口。")
            return
        if _is_topmost(hwnd):
            title = _title_of(hwnd)
            ok = _set_topmost(hwnd, False)
            if hwnd in self._order:
                self._order.remove(hwnd)
            self._remove_glow(hwnd)
            self.done.emit(f"已取消置顶：{title}" if ok else "取消置顶失败。")
        else:
            title = _title_of(hwnd)
            ok = _set_topmost(hwnd, True)
            if ok:
                if hwnd in self._order:
                    self._order.remove(hwnd)
                self._order.append(hwnd)          # 最新置顶 → 最高优先级
                self._add_glow(hwnd)
                self._restack()                   # 立刻把全栈按时间序压好
                self.done.emit(f"已置顶：{title}（再点取消；先取消再置顶可提到最上）")
            else:
                self.done.emit("置顶失败(可能是系统/管理员窗口,权限不足)。")

    def unpin_all(self):
        """退出 app 前调用:解除本会话置顶过的窗口 + 移除所有辉光层 + 卸载事件钩子,不留副作用。"""
        self._enforce_timer.stop()
        if getattr(self, "_winevent_hook", None):
            try:
                user32.UnhookWinEvent(self._winevent_hook)
            except Exception:
                pass
            self._winevent_hook = None
        for hwnd in list(self._order):
            try:
                _set_topmost(hwnd, False)
            except Exception:
                pass
        for hwnd in list(self._glows):
            self._remove_glow(hwnd)
        self._order.clear()
