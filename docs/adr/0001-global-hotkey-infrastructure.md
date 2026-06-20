# 1. 全局热键设施(用 ctypes RegisterHotKey + 应用级 native event filter)

状态:已接受(2026-06-18)

## 背景

连点器的「跟随光标」模式会劫持鼠标——启动后无法再用鼠标点界面上的按钮来停止。因此连点器需要一个**全局热键**(窗口不在焦点也能启停)。这是本项目第一次需要系统级热键;以后的「全局热键截图」也会复用同一套设施。

Qt 本身**不提供**跨平台的全局热键 API。可选路线:

1. 引入第三方库(`pynput` / `keyboard` / `qtkeybind`)——换取跨平台,但增加依赖,`keyboard` 在部分环境还需管理员权限。
2. 直接用 Win32 `RegisterHotKey` + ctypes——零新依赖(项目已用 ctypes 做单实例锁),仅限 Windows(本项目本就只支持 Windows)。

## 决策

走路线 2:ctypes 调 `RegisterHotKey(None, id, MOD_NOREPEAT|mods, vk)`,并在 `QApplication` 上安装一个**应用级** `QAbstractNativeEventFilter` 捕获 `WM_HOTKEY`。

两个 PyQt6 专属坑必须遵守,否则热键静默失效:

- **必须用应用级 filter,不能用 widget 的 `nativeEvent`**。`hwnd=None` 的 `RegisterHotKey` 把 `WM_HOTKEY` 投递到**线程消息队列**而非某个窗口,挂在 widget 上的 `nativeEvent` 收不到。`app.installNativeEventFilter(...)` 才能可靠捕获。
- **`nativeEventFilter` 返回值必须是 `(bool, int)` 元组**(PyQt6 改了签名,PyQt5 是单值)。返回裸 `bool` 会让事件处理出错。

注册时机:`TrayApp` 构造时从 config 读热键并注册;设置窗保存后,若热键变更则先 `UnregisterHotKey` 再重注册。退出时 `UnregisterHotKey` 清理。

## 后果

- **好**:零新依赖;热键设施可被连点器与未来的全局热键截图共用;输入注入用同栈的 `SendInput`,一致。
- **代价**:仅 Windows(可接受,项目定位即 Windows);热键可能被别的程序占用——`RegisterHotKey` 返回 0(`GetLastError`=1409 `ERROR_HOTKEY_ALREADY_REGISTERED`),所以热键必须**可配置**,让用户改键自救。
- **约束**:`WM_HOTKEY` 投递到注册它的线程,注册必须在主 GUI 线程进行。
