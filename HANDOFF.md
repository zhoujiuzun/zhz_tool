# 宏功能 — 续作交接 (HANDOFF)

> 写于 2026-06-18,关机前。重启后读这一份即可快速接上。
> 配套文档:[CONTEXT.md](./CONTEXT.md)「动作序列/宏」条目、[PROJECT_NOTES.md](./PROJECT_NOTES.md) §12、[docs/adr/0001-global-hotkey-infrastructure.md](./docs/adr/0001-global-hotkey-infrastructure.md)。

## 当前状态:✅ 功能已完成 + 已自查修复 + 已通过自动化验证

「连点器」诉求经逐项拷问后扩成 **动作序列 / 宏工具**(录制 + 列表可编辑、多条命名宏)。
代码全部写完,自查发现的 6 个问题已修,自动化测试全绿。**只差你本人在真机上点一遍。**

---

## 一、做了什么(功能定稿)

| 维度 | 决定 |
|------|------|
| 形态 | 录制 + 列表可编辑;多条命名宏库 |
| 录制 | pynput 全局钩子;**连鼠标移动轨迹一起录**;轨迹在列表折叠成一条「移动 N 点」 |
| 动作类型 | 鼠标(左/中/右/侧键x1x2、双击、滚轮、移动、拖拽)+ 键盘 + 等待 |
| 回放热键 | **F6** 单键 toggle 启停(可配),常驻注册 |
| 停止录制 | **F9** 专属热键(可配),**仅录制期间注册**,结束即注销 |
| 循环 | 一次 / 固定次数 / 无限 |
| 回放速度 | 原速(按录制真实间隔) |
| 坐标 | 绝对坐标 + 存录制时分辨率(换分辨率会错位,v1 不自动缩放) |
| UI | 设置窗新增「宏」Tab;**托盘零新增图标、零新增菜单项** |
| 引擎 | 挂 `TrayApp`(常驻不可见);关设置窗回放/录制继续 |
| 并发 | 同时只跑一个(state: idle/recording/playing) |

访问路径:**右键托盘 → 设置 → 宏 Tab**。

---

## 二、新增/改动文件

| 文件 | 内容 |
|------|------|
| `app/hotkeys.py` | 新增。全局热键(ctypes RegisterHotKey + 应用级 QAbstractNativeEventFilter) |
| `app/macro.py` | 新增。宏引擎:录制(pynput)+ 回放(ctypes SendInput)+ MacroEngine |
| `app/macro_tab.py` | 新增。设置窗「宏」Tab |
| `app/config.py` | 加 `macro` 配置段 + 宏文件读写(`list_macros`/`load_macro`/`save_macro`/`delete_macro`) |
| `app/settings_window.py` | 接入「宏」Tab;加 `WA_DeleteOnClose` |
| `app/tray.py` | 实例化 MacroEngine + HotkeyManager;注册 F6/F9;退出 shutdown |
| `main.py` | (此前)加了 QFont 警告静音的消息处理器 |
| `requirements.txt` | 加 `pynput>=1.7` |
| `OCRTool.spec` | 加 `hiddenimports=['pynput.keyboard._win32','pynput.mouse._win32']` |
| `CONTEXT.md` / `PROJECT_NOTES.md §12` / `docs/adr/0001` | 文档 |

存储:宏序列存独立文件 `~/.ocr_tool/macros/<name>.json`(不进 config.json);
config 的 `macro` 段只存当前选中宏名 + 热键 + 循环设置。

---

## 三、自查修复的 6 个问题(已全部修 + 验证)

1. **`_sleep` 负值崩溃(严重)**:`time.sleep` 可能收到负值→ValueError→回放线程死→状态卡在 playing→F6 再也启动不了。已加 `max(0.0,…)`。
2. **`run()` 无异常保护(严重)**:已用 `try/finally` 保证 `finished_all` 必发。
3. **退出竞态崩溃**:`_quit` 没等回放线程结束。已加 `MacroEngine.shutdown()`(停录制/回放 + wait 线程)。
4. **设置窗信号累积**:无 `WA_DeleteOnClose`,旧 MacroTab 不析构、信号累积。已加。
5. **控制热键被录进宏**:原只滤 F9,按 F6 会被录入。已改 `_Recorder` 忽略一组 vk(F6+F9)。
6. **零延迟无限循环 100% CPU**:已加 1ms 守卫。

验证:6 文件全编译;9 项测试全绿(含异常恢复、零延迟不崩、shutdown);TrayApp 启动+设置窗+关闭+退出路径通过。

---

## 四、⚠️ 重启后第一件事

**有 3 个残留 python 进程**(6/17–6/18 启动)占着单实例互斥锁 `OCRTool_SingleInstance`。
不杀掉的话,新启动的实例会**静默退出**(`main.py` 检测到锁就 sys.exit)。

重启通常会清掉它们;若没有,先确认:
```powershell
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, StartTime
```
确属残留再 `Stop-Process -Id <id>`(别误杀你自己开的工具)。

---

## 五️、续作待办(按优先级)

### P0 — 你本人必须真机验证(我无法自动化,会乱动真实键鼠)
- [ ] 启动 `python main.py`,右键托盘 → 设置 → 宏 Tab
- [ ] 新建一条宏 → 点「● 录制」→ 操作几下(点击/打字/滚轮/拖拽)→ 按 **F9** 停止
- [ ] 确认动作列表出现内容(轨迹折叠成「移动 N 点」)
- [ ] 选循环方式 → 点「▶ 回放」或按 **F6** → 确认动作被复现
- [ ] 关掉设置窗后按 F6 → 确认仍能回放(机器住托盘、关窗不停)
- [ ] 测 F6 占用提示:若 F6 被别的程序占,启动时应弹气泡提示改键

### P1 — 已知限制,看你要不要补
- [ ] 回放前「分辨率不符」提示(现在只存分辨率,不校验不缩放)
- [ ] 录制时若想排除更多修饰键场景(Ctrl+Shift+F9 这类组合键当停止键时,Ctrl/Shift 仍会被录)

### P2 — 可选增强
- [ ] 倍速/压缩等待回放
- [ ] 动作逐条编辑(现在轨迹段只能整段删,不能改单条)
- [ ] 防误触/随机抖动(反检测)

---

## 六、关键架构备忘(改代码前必读)

- **运行机器住 `TrayApp`**:`self._macro_engine` + `self._hotkey_mgr` 是 TrayApp 实例属性,常驻、不可见。
- **热键必须应用级**:`app.installNativeEventFilter(mgr)`,不能用 widget 的 nativeEvent(见 ADR-0001)。PyQt6 的 `nativeEventFilter` 返回值必须是 `(bool, int)`。
- **录制持久化双保险**:`MacroEngine.recorded` 信号被宏 Tab 和 TrayApp 同时接收落盘(防关窗丢失)。
- **保存协调**:宏 Tab 的 `collect()` 只返回 dict 不写盘,由 `SettingsWindow._save` 统一写一次,避免两头写打架。
- **F9 仅录制期注册**:`tray._on_macro_state` 里 state=="recording" 时注册、否则注销,避免长期占用 F9。
