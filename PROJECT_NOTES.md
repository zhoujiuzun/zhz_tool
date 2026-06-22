# OCR Tool 项目要点（重构参考）

> 整理时间：2026-06-16
> 用途：重构 / 加功能前的现状速查。记录架构、模块职责、数据流、关键约定与已知坑点。

## 1. 项目定位

Windows 桌面常驻的**多接口 OCR 工具**。常驻系统托盘，提供两种取图方式（屏幕截图框选、剪贴板图片自动监控），把图片送到多个云 OCR 接口识别，按优先级自动选用可用接口，结果在可编辑窗口展示并支持一键复制。

- 语言/框架：Python + PyQt6
- 平台：Windows（用到 `winreg` 注册表自启、`ctypes` 单实例互斥锁）
- 打包：PyInstaller（`OCRTool.spec`，单文件、无控制台窗口）
- 入口：`main.py` → `app.tray.TrayApp`

## 2. 依赖

```
PyQt6>=6.6.0        # GUI / 托盘 / 截图 / 剪贴板
requests>=2.31.0    # 所有接口 HTTP 调用
cryptography>=42.0.0 # API Key 本地加密（Fernet）
Pillow>=10.0.0      # 百度接口的图片压缩
```

## 3. 目录结构与模块职责

```
OCR/
├── main.py                  # 入口：单实例锁 + QApplication + 全局样式 + 启动托盘
├── run.ps1                  # 开发便捷脚本：杀掉本目录旧实例后用最新代码重启(绕开单实例锁看不到改动的坑)
├── OCRTool.spec             # PyInstaller 打包配置（datas 带 test_image.png + logo.png）
├── requirements.txt
├── app/
│   ├── __init__.py          # 空
│   ├── tray.py              # ★ 核心调度：托盘菜单、OCR 线程编排、各窗口生命周期
│   ├── config.py            # 配置读写 + API Key 加密（Fernet）；默认配置/加密集从接口注册表派生
│   ├── fields.py            # 接口字段模型 Field（纯数据，零 Qt）；驱动表单渲染/加密集/已配置判断
│   ├── providers.py         # ★ OCR 接口（自描述类 + _REGISTRY 派生工厂/探测/默认/字段/已配置）
│   ├── translators.py       # ★ 翻译接口（同 providers，自描述 + _REGISTRY 派生）
│   ├── dispatch.py          # ★ 通用派发引擎 DispatchEngine（探测 + 优先级 + 回退 + 首选记忆 + 分级超时）
│   ├── engines.py           # ★ 引擎注册中心：实例化 OCR/翻译两个 DispatchEngine，对外统一入口
│   ├── screenshot.py        # 全屏截图框选控件
│   ├── clipboard_monitor.py # 剪贴板图片轮询监控
│   ├── result_window.py     # 译文窗（原文/译文双栏 + 翻译 + 复制 + 置顶）
│   ├── settings_window.py   # 设置界面：OCR/翻译接口管理 + 通用 + 宏 Tab
│   ├── autostart.py         # 开机自启（写 HKCU Run 注册表）
│   ├── pin_window.py        # 贴图浮窗（截图钉屏）
│   ├── hotkeys.py           # 全局热键设施（ctypes RegisterHotKey + 应用级事件过滤）
│   ├── macro.py             # 宏引擎：录制（pynput）+ 回放（ctypes SendInput）
│   ├── macro_tab.py         # 设置窗「宏」Tab
│   ├── window_pin.py        # ★ 窗口置顶工具：把任意外部窗口钉到最上层(拾取式 toggle)
│   ├── style.py             # ★ 主题色派生：build_style(主题色) 生成全套清新风 QSS
│   ├── logo.png             # ★ 应用图标(托盘/任务栏/各窗标题栏);换 logo = 直接覆盖此文件(单一来源)
│   └── test_image.png       # 测试连通性用的内置图片（随包分发）
└── (调试脚本 test_gemini.py / test_xunfei.py / gemini_result.txt 已于 2026-06-19 删除)
```

## 4. 内置 OCR 接口（providers.py）

所有接口继承 `OCRProvider(ABC)`，实现 `recognize(image_bytes) -> str`。基类提供 `_get_test_image()` 读内置 `test_image.png`，供设置窗「测试」按钮做真实识别连通性测试。

**自描述**：每个接口类声明 `ID` / `DISPLAY_NAME` / `PROBE_URL` / `FIELDS`（字段列表，见 `app/fields.py` 的 `Field`）。文件末尾 `_REGISTRY` 列表驱动一切派生。

| id | 名称 | 鉴权字段 | 备注 |
|----|------|---------|------|
| `mistral` | Mistral OCR 3 | `api_key` | `/v1/ocr`，返回 markdown 拼接 |
| `google` | Gemini 2.5 Flash | `api_key` | generateContent，prompt 提取文字保留排版 |
| `azure` | Azure Document Intelligence | `api_key` + `api_endpoint` | 异步轮询 Operation-Location（最多 20 次 / 每次 sleep 1s）|
| `baidu` | 百度 OCR | `api_key` + `secret_key` | 先换 token；>3MB 自动压缩；`high_accuracy` 切高精度版 |
| `tencent` | 腾讯云 OCR | `api_key` + `secret_key` | TC3-HMAC-SHA256 签名；`high_accuracy` 切 GeneralAccurate |
| `xunfei` | 讯飞 OCR | `api_key` + `secret_key` + `app_id` | HMAC-SHA256 签名，结果 base64 解码后解析 |
| (custom) | 自定义 | `url` + `request_template` + `response_path` + 可选 `api_key` | 模板用 `{{image_base64}}` 占位，`response_path` 用 `.` 分隔取值 |

- 工厂：`build_provider(cfg)` —— `type=="custom"` 走 `CustomOCR`，否则按 `id` 查 `_BUILTIN`。
- ⚠️ **自定义接口 UI 入口已移除（2026-06-19）**：设置窗 OCR 页的「添加」按钮去掉了（`_KIND_SPEC["ocr"]["allow_custom"]=False`）。原因:各商业厂家参数各异(appid/签名/轮询),通用自定义表单(单一 JSON 模板 + Bearer)表达不了,**该走内置接口**(见下条)。`CustomOCR` 类与已有自定义配置仍**保留可用**(自部署/本地 OCR 的逃生口);改回 `True` 即恢复入口。
- **添加新内置接口（自描述化后）**：① 写一个继承 `OCRProvider` 的类，声明 `ID`/`DISPLAY_NAME`/`PROBE_URL`/`FIELDS` 并实现 `recognize`；② 把类加进 `providers._REGISTRY` 列表（顺序即默认优先级）。**就这两步**——工厂表、探测 URL、默认配置、设置窗字段渲染、加密集、已配置判断全部自动派生。翻译接口同理（`translators._REGISTRY`）。

## 5. 配置系统（config.py）

- 路径：`~/.ocr_tool/config.json`，加密密钥：`~/.ocr_tool/.key`
- API Key 用 **Fernet 对称加密**：保存时 `api_key` → `api_key_enc`，读取时解密回 `api_key`。
- 注意：**只有 `api_key` 字段被加密**，`secret_key` / `app_id` / `api_endpoint` 等仍是明文存储（重构时若要全面加密需扩展 `save_config`/`load_config`）。
- 配置结构：
```json
{
  "clipboard_monitor": false,
  "providers": [ { "id", "name", "enabled", "priority", "api_key", "type", ...接口专属字段 } ]
}
```

## 6. 调度引擎（dispatch.py + engines.py）—— 核心逻辑

> 2026-06 重构:原 `ocr_engine.py` 的模块级全局状态已抽成通用引擎类 `DispatchEngine`
> (`dispatch.py`),OCR 与翻译各持一个实例;`engines.py` 是注册中心,对外统一入口。
> 托盘/设置窗只与 `engines.py` 交互,不再 reach 进引擎内部状态(原 §9.2 技术债已消除)。

`DispatchEngine.warmup(providers_config)` 并行 HEAD 探测所有「已启用且已配置」接口的
可达性,结果存入**实例属性** `_status`(带锁 + `_ready` Event)。

`DispatchEngine.run(providers_config, invoke)` 的派发策略:
1. 等 warmup 完成（最多 3s；从未 warmup 则不空等）
2. 过滤出已启用且有 key（或 custom）的接口，按 `priority` 升序排
3. **首选记忆**:把上次成功的 `_preferred_id` 提到最前
4. **可用性分层**:warmup 完成且有结果时,把探测可达的接口排前、不可达的排后
   (**不再硬剔除**——快照会因网络环境变化过期,硬剔除会误杀实际可用接口;
   custom 视为始终可达)
5. **分级超时**：首个接口长超时（OCR 20s / 翻译 8s），其余短超时（OCR 10s / 翻译 5s）
6. 依次尝试，第一个成功的返回 `(result, name)` 并记为 preferred；全失败抛 `RuntimeError` 汇总错误

`engines.py` 对外提供:`run_ocr` / `run_translation` / `warmup_all` / `warmup_ocr` /
`warmup_translation` / `reset_all` / `get_ocr_probe_status` / `get_translation_probe_status`。
`tray._rewarmup()` 调 `warmup_all`,`重置接口状态` 调 `reset_all`——都走封装方法,不碰内部变量。

## 7. 运行时数据流

```
托盘菜单「截图识别」 → ScreenshotWidget 框选 → captured(PNG bytes)
                                                      │
剪贴板监控（800ms 轮询，md5 去重）→ image_detected ───┤
                                                      ▼
                              TrayApp._run_ocr(image_bytes)
                                  · 重新 load_config（取最新设置）
                                  · 起 OCRWorker(QThread) 后台跑 run_ocr
                                      ├─ finished(text, name) → ResultWindow 展示
                                      └─ failed(msg) → 托盘气泡报错
```

- `OCRWorker` / `ResultWindow` 实例都被存进 list 防止被 GC（`self._workers` / `self._result_windows`）。
- UI 线程不阻塞：识别在 QThread 里跑。

## 8. 各 UI 组件要点

- **托盘 tray.py**：菜单项 = 截图识别 / 截图贴图 / 翻译 / 开机自启 / 重置接口状态 / 设置 / 退出。是整个 app 的协调中心，还常驻持有宏引擎 + 全局热键管理器。
- **截图 screenshot.py**：全屏半透明遮罩 + QRubberBand 框选，处理了 `devicePixelRatio` 高 DPI 缩放与多屏/混合 DPI，Esc 取消。
- **剪贴板 clipboard_monitor.py**：QTimer 800ms 轮询，用 md5 哈希去重避免重复识别同一张图。
- **译文窗 result_window.py**：原文/译文双栏可编辑 + 翻译 + 翻译剪贴板 + 粘性目标语言 + 双复制 + 置顶切换，关闭即销毁。
- **设置窗 settings_window.py**：OCR/翻译接口共用 `ProviderTab` + `ProviderDialog`（按 kind+id 渲染字段），「测试」用 `_TestWorker(QThread)` 异步跑，连通状态轮询展示。内置接口不可删除只能禁用。另含「宏」「通用」两 Tab。**自动保存（防抖,2026-06-19）**：无底部保存按钮,各变更信号 → `_schedule`(重启 350ms 单发 `_save_timer`),停手后才 `_do_persist` 写一次盘 + emit `applied`(托盘做轻量副作用:剪贴板启停 + 宏热键重注册);接口重探测仍留到关窗一次性跑。**为何防抖而非每次微改即写**:宏文件可达数百 KB,spinbox 连点/打字会触发大量 `valueChanged`,每次都重写大文件 + 触发副作用既慢又增损坏风险。⚠️ **边界(已处理)**:① 关窗时若 timer 仍 active → `closeEvent` 先 stop+`_do_persist`,不丢未到点改动;② 宏热键/循环改动只标 `MacroTab._dirty` + emit,实际由 `flush()` 在防抖到点/切宏/关窗/录放前写;切宏在 `_on_select` 开头先 `flush()` 旧宏(`_prev_name`),不丢。⚠️ **递归坑(已修)**:`collect()` 只读不写、不发信号(曾因它回调 `_persist_macro_settings` emit `changed` 造成无限递归爆栈"改设置就崩溃")。
- **自启 autostart.py**：写 `HKCU\...\Run`；区分打包态（`sys.frozen`）与开发态（用 pythonw 避免控制台）。

## 9. 已知坑点 / 重构注意

> 多条已在 2026-06 的翻译/贴图/宏重构中解决,下面标注了现状。

1. ✅ **已解决**:原 `providers.py` 写死的 `_TEST_IMAGE_PATH` 死代码已删,`_get_test_image` 用 `sys._MEIPASS`/`__file__` 正确定位 `test_image.png`。
2. ✅ **已解决**:原 `ocr_engine` 模块级全局状态已抽成 `DispatchEngine` 实例(`dispatch.py`),托盘经 `engines.py` 封装方法交互,不再 reach 内部变量。
3. ⚠️ **部分解决**:加密现覆盖 `api_key` + `secret_key`(`config._SECRET_FIELDS`);`app_id` / `api_endpoint` 等仍明文。
4. ✅ **已解决**:已有全局热键设施(`hotkeys.py`),宏回放用 F6/F9。截图目前仍只走托盘菜单,如需可复用同设施。
5. **无识别历史/记录**：结果不持久化。
6. **剪贴板监控用轮询**而非系统事件，800ms 延迟 + 占用。
7. ✅ **已解决**:`screenshot.py` 已遍历 `QApplication.screens()` 逐屏预抓 + 按 dpr 还原,多屏/混合 DPI 不再错位。
8. **Azure 轮询**最长约 20s 阻塞在 worker 线程，配合分级超时逻辑可能有冲突。
9. ⚠️ **部分解决**:`dispatch.humanize_error` 已把网络/HTTP 状态码分类成可读中文;业务层鉴权/配额错误仍按各接口原样透传。
10. ✅ **已清理**:原 `test_gemini.py` / `test_xunfei.py` 独立调试脚本(含硬编码真实密钥)及 `gemini_result.txt` 调试残留已于 2026-06-19 删除。如再需调试,务必用占位密钥或从配置读取,勿硬编码。

## 10. 常见扩展点速查（加功能时改哪里）

| 想加的功能 | 主要改动位置 |
|-----------|------------|
| 新 OCR 接口 | **仅 `providers.py`**：写自描述类（`ID`/`DISPLAY_NAME`/`PROBE_URL`/`FIELDS` + `recognize`）+ 加进 `_REGISTRY`。工厂/探测URL/默认配置/设置窗字段/加密集/已配置判断全自动派生 |
| 新翻译接口 | **仅 `translators.py`**：同上（`translate` + 语言码映射）+ 加进 `_REGISTRY` |
| 接口新增配置字段 | 在该接口类的 `FIELDS` 加一个 `Field`（设 `kind`/`secret`/`required`），UI/加密/校验自动跟随 |
| 全局热键截图 | 复用 `hotkeys.HotkeyManager`，接到 `tray._start_screenshot` |
| 识别历史 | 新增存储模块 + `result_window` 入口；可在 `tray._show_result` 落库 |
| 后处理管线 | `engines.run_ocr` 返回后、`tray._show_result` 之前插入 |
| 配置项扩展 | `config._default_config` + `settings_window._build_general_tab` |

## 11. 本轮重构决策（翻译 + 贴图，2026-06-16）

> 领域概念与边界以 [`CONTEXT.md`](./CONTEXT.md) 为准；本节只记**实现层面**的架构决策。

### 11.1 抽通用派发引擎（核心重构）
现有 `ocr_engine.py` 把派发逻辑和**模块级全局状态**（`_AVAILABLE_IDS` / `_preferred_id` / `_available_ready`）耦在一起，`tray._rewarmup()` 还直接 import 这些内部变量去 clear（见 §9.2 技术债）。本轮翻译功能要复用同一套派发逻辑，借此把它抽成**通用引擎类**：

- 一个 `DispatchEngine`（名字待定）封装：**优先级排序 + 失败回退 + warmup 可用性预探测 + 首选记忆 + 分级超时**。状态全部是**实例属性**，不再用模块级全局变量。
- OCR 和翻译各 `new` 一个引擎实例，分别喂入自己的「接口列表 + 探测 URL 表 + 超时参数」。
- 一举解决两件事：① §9.2 的全局状态技术债；② 避免 OCR / 翻译两份几乎一样的派发代码。
- `tray._rewarmup()` 改为调用引擎实例的方法，不再 reach 进内部变量。

### 11.2 翻译沿用 OCR 派发的哪些机制
| 机制 | OCR | 翻译 | 说明 |
|------|-----|------|------|
| 优先级排序 | ✓ | ✓ | 按 `priority` 升序 |
| 失败回退 | ✓ | ✓ | 当前接口抛错试下一个 |
| 首选记忆 | ✓ | ✓ | 记住上次成功接口，提到最前（重启重置） |
| warmup 预探测 | ✓ | ✓ | 翻译接口同样有「国内连不通」问题（DeepL/Google），值得探测 |
| 分级超时 | 20s / 10s | **8s / 5s** | 翻译是纯文字、响应快，超时**改短**，不照抄 OCR 数值 |

### 11.3 翻译接口层
- 新增 `TranslationProvider` 抽象基类 + 5 个实现：**DeepL / Google 翻译 / Gemini（复用 key）/ 百度翻译 / 有道智云**。对称现有 `providers.py` 的 `OCRProvider` 结构。
- 与 OCR 接口**独立的接口池、独立优先级、独立配置**。`config.py` 需新增翻译接口配置段（注意：当前加密只覆盖 `api_key`，翻译接口的 secret 类字段同样需要处理，见 §9.3）。
- Gemini / 百度作为翻译接口与其作为 OCR 接口是**不同条目**，key 可共用但配置互不影响。

### 11.4 译文窗（统一 OCR 结果窗与翻译窗）
- 现有 `result_window.py` 扩展为**译文窗**：上下分栏（原文/译文均可编辑）+ 翻译按钮 + 翻译剪贴板按钮 + 粘性目标语言下拉 + 双复制按钮 + 置顶可切换。
- 接收一个「初始原文」参数（可为空）：从 OCR 进入则预填识别结果，单独调用则为空。
- **粘性目标语言**：窗口初始按方向规则（非中→中、中→英）；手动选定后完全盖过规则、含中文也翻该语言；作用域 = 窗口生命周期，关窗失效。语义细节见 `CONTEXT.md`。

### 11.5 贴图浮窗（全新独立模块）
- 与识别/翻译流水线**完全不联动**。新增浮窗模块 + 托盘「截图贴图」入口（复用现有 `screenshot.py` 框选）。
- 浮窗交互：拖动 / 多张 / Ctrl+滚轮等比缩放 / 右键菜单关闭 / Esc 关闭 / 右键「浮窗设置」切置顶 / 暂不做透明度。
- **边界**：截图框选时已有浮窗不得遮挡框选区（框选期间浮窗临时下沉或不拦截鼠标）。

### 11.6 设置窗与托盘菜单变化
**设置窗（共用一个，不另开窗）**：现有「OCR 接口 / 通用」两 Tab，新增第三 Tab **「翻译接口」**，复用现有表格 + `ProviderDialog`（启用/优先级/编辑/测试连通性）。「自动翻译」开关 + 默认方向规则配置放进**「通用」Tab**。
- 注意：`ProviderDialog` 现在按 `id` 硬编码字段（baidu/tencent/xunfei 等）。翻译接口字段不同（DeepL 仅 key；Google 需 key+project；有道需 key+secret；百度翻译需 appid+key），需让对话框区分 OCR / 翻译两类来渲染对应字段。

**托盘菜单**：现有「截图识别 / 剪贴板监控 / 开机自启 / 重置首选接口 / 设置 / 退出」：
- 新增 **「截图贴图」** 与 **「翻译」（单独调用译文窗）** 两个入口。
- 「重置首选接口」**改名「重置接口状态」，行为升级为方案乙**：同时对 **OCR + 翻译两个引擎**执行「首选记忆重置 + 可用性重探测（re-warmup）」。即点一下 = 两引擎都忘掉上次成功接口 + 重新联网测一遍可达性。
- **设置窗关闭时的自动重探测**（现 `tray._rewarmup`，原只测 OCR）**扩为两引擎都测**，使改完翻译接口 key 后关窗即自动重测。
- 实现提示：既然走通用引擎（§11.1），「重置接口状态」与「关窗自动重探测」都应是"遍历所有引擎实例调用其 reset+warmup 方法"，而非 reach 进模块级全局变量。

### 范围外明确不做（本轮）
ADR、全局热键、浮窗透明度、划词即译、智能多语言方向检测。

## 12. 宏（动作序列）功能（2026-06-18）

> 领域概念以 [`CONTEXT.md`](./CONTEXT.md)「动作序列 / 宏」条目为准；本节记实现层面。
> 这是 app 第一个与 OCR 完全无关的工具，标志定位从「OCR 工具」扩展为「桌面工具箱」。

### 12.1 形态演进
诉求起于「连点器」，逐项拷问后扩成**动作序列 / 宏**：录制 + 列表可编辑、多条命名宏库。
覆盖键盘 / 鼠标（左右中 + 侧键 + 滚轮 + 移动 + 拖拽）/ 等待，连鼠标移动轨迹一起录。

### 12.2 新增模块
| 文件 | 职责 |
|------|------|
| `app/hotkeys.py` | 全局热键设施：ctypes `RegisterHotKey` + 应用级 `QAbstractNativeEventFilter` 捕获 `WM_HOTKEY`。见 [ADR-0001](./docs/adr/0001-global-hotkey-infrastructure.md) |
| `app/macro.py` | 宏引擎：录制（pynput 全局钩子）+ 回放（ctypes `SendInput`）。挂 `TrayApp` 常驻 |
| `app/macro_tab.py` | 设置窗「宏」Tab：宏选择 / 录制 / 编辑 / 回放配置 |

### 12.3 关键架构约定
- **运行机器住 `TrayApp`（常驻、不可见、无新增托盘图标/菜单项）**：`MacroEngine` + `HotkeyManager` 是 `TrayApp` 实例属性。关设置窗回放/录制照常继续。
- **宏总开关（`macro.enabled`，默认关闭）**：宏 Tab 顶部「启用宏」复选框。关闭时 `tray._register_macro_hotkeys` **注销所有宏的回放热键 + F9**，避免常驻热键干扰用户正常键鼠；勾选即时生效（设置窗实时保存 + `applied` 触发托盘重注册）。开箱默认关闭。`_toggle_macro_play_for` / `_toggle_macro_record` 另有防御性兜底（关闭时即便回调触发也不动作）。宏 Tab 关闭时录制/回放控件相应禁用。
- **配置面是 Tab**：「宏」Tab 只编辑配置 + 触发引擎，不持有运行状态。
- **热键（2026-06-19 每宏独立改造）**：**每条宏配自己的回放热键**（存各宏文件的 `hotkey` 字段），`tray._register_macro_hotkeys` 遍历所有宏、对每条有热键的宏以 id `macro_play::<name>` 各自注册，按谁的键就回放谁（用该宏自己的循环设置）。**F9 启停录制是全局键**（`config.macro.stop_record_hotkey`），空闲按 F9 录当前选中宏、再按停。**录制期间注销所有回放热键**（`_on_macro_state` 里 state=="recording" 注销、回 idle 由 `_register_macro_hotkeys` 恢复），使「录制时其他热键失效、不干扰录制；不录制时照常有效」。录制时把全局 F9 + 本宏回放键的 vk 放进 `ignore_vks`（`_macro_control_vks`），避免物理按键被录进序列。撞键在宏 Tab 保存时即拦截（本宏键 vs 其他宏键/全局 F9）。
- **存储分离**：宏序列 + **每宏的回放热键/循环设置**存独立文件 `~/.ocr_tool/macros/<name>.json`，**不进 `config.json`**；config 的 `macro` 段只存 `enabled` + 当前选中宏名 `current` + 全局 `stop_record_hotkey`。宏内容无敏感字段，不加密。旧配置的全局 `play_hotkey`/`loop_*` 由 `config.migrate_macro_play_hotkey()` 一次性迁到 current 宏文件。
- **写盘健壮性（2026-06-19，踩坑后加固）**：宏文件可大至数百 KB（上千轨迹点）。`save_macro` 用**原子写**（临时文件 + `fsync` + `os.replace`），避免「写到一半被强杀/掉电」损坏正式文件。`load_macro` 对损坏文件（JSON 解析失败）**容错**：改名隔离成 `<name>.corrupt-<时间>.json` 留证 + 返回空壳，**绝不让单个坏文件崩掉整个 app 启动**。`list_macros` 排除 `.corrupt-*` 备份。教训:`json.dump` 直写非原子 + `load` 无 try 是启动期单点故障。
- **录制持久化双保险**：`MacroEngine.recorded` 信号被宏 Tab 和 `TrayApp` 同时接收并落盘，防止录制中关设置窗导致丢失。
- **同时只跑一个**：录制中禁回放，回放中禁录制（引擎 state 机：idle / recording / playing）。

## 16. 文件搜索·元数据 + 高级搜索（2026-06-20，C 方案）

> 在第 15 节基础上扩展:索引每条加 大小/创建·修改·访问时间/属性,支撑 Everything 式高级搜索。

### 16.1 MFT 全解析(`file_search.py`,自实现,非抄 Everything)
- 原 `FSCTL_ENUM_USN_DATA` 只给名字/父号/属性,**给不了大小和文件自身时间**。故新增直读 `$MFT`:
  `FSCTL_GET_NTFS_VOLUME_DATA` 取卷参数 → 解析 record0 的 `$DATA` data run 定位 MFT → 逐条 FILE 记录解析 `$STANDARD_INFORMATION`(3 时间+DOS属性)、`$FILE_NAME`(父号+名,取 Win32 命名空间)、`$DATA`(大小)。含 fixup(USA)校正。
- **★ 严重内存坑(已修)**:初版 `_read_mft_bytes` 一次性把整个 5.8GB MFT 读进内存 + 5.4M 个 dict 节点 → 吃到 7GB 卡死。改成 `_iter_mft_records` **流式逐段读、读一段吐一段记录**,节点用**元组**而非 dict。峰值 7GB→4.4GB,能正常完成。
- `entries_and_graph_full`:一次解析同产 (带元数据条目, FRN图),供建索引 + USN 增量,省一次解析。`metadata_of_path`:`GetFileAttributesExW` 给 USN 增量的变动文件补元数据。
- 实测 C 盘:全解析 542 万条 ~57s(纯名字 ENUM 是 22s)。

### 16.2 索引存元数据 + 存档 v02(`file_index.py`)
- FileIndex 加并行数组 `_size/_mtime/_ctime/_atime/_attr`,与 `_paths` 对齐;`_delta` 改 7 元组。
- 存档魔数升 **ZHZIDX02**(旧 v01 自动作废重扫),用 `array` 紧凑存定长元数据。实测 622MB→808MB,载档 ~10s。
- `search_advanced(cond)`:条件字典 AND 组合——必含/短语/任一/不含词、路径、正则、扩展名、类型、属性位、大小区间、3 种时间区间、文件名长度、文件夹深度。有文本词时用 blob 粗筛,纯元数据条件全表扫(可接受,高级搜索非边打边出)。

### 16.3 高级搜索对话框(`advanced_search_window.py`)
- Everything 风格中文表单:浅灰底白控件、可纵向滚动、底部固定 确定/取消。由搜索窗「高级搜索」按钮唤起(`_open_advanced` → `_AdvWorker` 后台跑 → 渲染)。
- 只放有数据支撑的分组(见 16.4);`get_conditions()` 收集成条件字典,经 IPC(`advsearch` 命令 / `IndexClient.advanced_search`)交 helper。

### 16.4 范围外/诚实边界
- **做不到(无数据/引擎,未做)**:文件内容检索、运行次数/日期、跨盘重复检测、外部文件列表(.efu)。变音匹配复选框置灰。
- 代价(C 方案既定):首次全扫 ~60s、存档 808MB、载档 ~10s、扫描内存峰值 ~4.4GB——"秒开"相应变 ~10s。
- 打包改 **onedir**(`OCRTool.spec` COLLECT)治"分钟级启动":单文件每次解压 56MB 被杀软重扫,onedir 不解压、只首次扫。产物为 `dist/zhz_tool/` 文件夹。

### 12.4 已知坑点 / 后续
1. **回放绝对坐标按录制时分辨率**存储,换分辨率/缩放/多屏会错位(不做自动缩放,仅存分辨率备校验)。**相对坐标**(§12.6)可绕开此问题:以回放时光标为原点偏移,不绑死屏幕点。
2. **录制轨迹不可逐条编辑**:连续 move 折叠成一行「移动轨迹 N 点」,可整段删,不能改单条。手动添加的单条 move/click/keytap/wait/scroll 可逐条编辑。
3. **新依赖 `pynput`**(破了「零依赖纯 ctypes」旧约,仅录制全局钩子用它)。打包 `OCRTool.spec` 已加 `hiddenimports=['pynput.keyboard._win32','pynput.mouse._win32']`。
4. 倍速回放未做。随机延迟(防机械节奏)已做(§12.6)。

### 12.5 手动编辑（手搓宏，2026-06-18 增补）
录制产物是底层动作（上千 move 点、按下/抬起分开），无法手写。为支持「不录制、自己编排」，新增**高层动作**层:

| 高层动作 | schema | 引擎处理（`macro._do_action`） |
|---------|--------|------------------------------|
| 鼠标点击 | `{"t":"click","b":,"double":bool,"x"?:,"y"?:,"d":}` | 可选移动到坐标(缺省落当前光标)→ 按下+抬起;double 连点两次 |
| 按键 | `{"t":"keytap","vk":,"mods":[vk,...],"d":}` | 按下修饰键→主键按下→主键抬起→逆序抬修饰键 |
| 等待 | `{"t":"wait","d":}` | 空操作,时长由 `d` 在回放循环 sleep 体现 |
| 移动 | `{"t":"move","x":,"y":,"d":}` | 复用底层 move(单条不折叠) |
| 滚轮 | `{"t":"scroll","dx":,"dy":,"d":}` | 复用底层 scroll |

- **与录制混用**:高层动作和录制的底层动作存同一份 `actions` 文件、走同一引擎,可混排。
- **UI(主从布局)**:宏 Tab 用 `QStackedWidget` 分两页——
  - **管理页**:启用开关、宏选择器、录制/回放、循环、热键 + 「✎ 编辑动作」按钮(显示「共 N 条动作」)。点编辑动作切到编辑页。
  - **编辑页**:顶部「← 返回」+ 宏名;左侧动作列表(添加/删除/↑/↓/清空),右侧 `ActionEditor` 内联编辑器。
  - **`ActionEditor`**:类型选择用**分段按钮**(`SegmentedButtons`,5 个互斥可勾选按钮平铺,点一下即切——不用下拉框,高频操作更快);各类型字段用 `QStackedWidget` 切页,**实时回写**——选中左侧某条即载入(`load_action`),改任意字段(`changed` 信号)立即写盘并更新左侧那行文字,无需「确定」。坐标含「抓取光标」按钮(点后 1.5s 读当前光标坐标填入)。载入时用 `_loading` 标志抑制 `changed`,避免回写风暴。
- **`d` 字段语义统一**:无论录制还是手动,`d` 都是「执行这条动作**前**的等待秒数」。手动动作的「前置等待」即写入 `d`。
- **显示**:`_one_line` 覆盖全部高层类型;`_summarize` 改为单条 move 单独成行(显示坐标),仅连续多条 move 才折叠成「移动 N 点」。
- **限制**:折叠的录制轨迹段仍不可逐条编辑(选中时右侧编辑器显示占位提示「不可逐条编辑,可整段删除」)。录制的底层 `btn`/`key` 单条若被选中,编辑器也显示不可编辑占位。

> ⚠️ §12.5 描述的「实时回写」交互已被 §12.6 重构取代,保留本节作历史记录。

### 12.6 编辑区重构(2026-06-19)
把「主从实时回写」改成「分模块 + 显式添加/更新」,并修掉实时回写带来的隐患。

**动机(重构前的问题)**:
1. **切类型串值(数据损坏)**:旧 `ActionEditor.load_action` 只填当前类型页,其余页留着上一条动作的旧值;编辑时手动切类型 → `get_action` 读到从未为本动作填过的残留数据,把无关动作的值串进来。
2. **每动一下读写整文件**:旧「改字段即 `changed`→`_persist_actions`」每次都 `load_macro`(读+解析整 JSON)+`save_macro`(写),拖 spinbox/连按时一秒几十次,大宏卡顿。
3. 选中不可编辑动作时 `_editing_idx` 仍指向它,靠「禁用控件不发信号」兜底,脆弱。
4. keytap 未设键存成 `vk=0` 无意义空动作。

**重构后**:
- **左侧:3 列表格**(序号/功能/参数,`QTableWidget`),替代旧 `QListWidget`。`_summarize` 改返回 `(功能, 参数, start, end)` 四元组。连续录制 move 仍折叠成一行「移动轨迹 N 点」。
- **右侧:分模块编辑区**(§12.7 后改为「全模块纵向展开」,见下)。
- **添加/更新模型(去实时回写)**:编辑器是**纯表单**,无 `changed` 信号。新建态点「添加」追加到末尾;选中既有可编辑动作 → 回填 + 按钮变「✓ 更新此动作」(+「取消编辑」)。只在点按钮时 dump+整批写盘一次。
- **对称 load/dump(根治串值)**:每个模块字段独立、互不共享,从根上杜绝旧设计「切类型串值」。
- **校验**:模块 `validate()` 拦截 keytap 未设键等,失败发 `invalid` 信号。

**schema 扩展(macro.py,全部「只加字段」向后兼容)**:
| 动作 | 新增 | 语义 |
|------|------|------|
| `click` | `act` ∈ {click,double,down,up} | 鼠标按下/松开/单击/双击。**兼容旧**:无 act 按 `double` 推断 |
| `click`/`move` | `rel` (默认 false) | true=坐标为「回放时光标位置」的偏移(可负),否则绝对屏幕坐标 |
| `keytap` | `act` ∈ {tap,down,up} | tap=修饰键包裹完整敲击;down/up 仅作用主键 |
| `wait` | `rand` (默认 0) | 实际等待 = d + uniform(0, rand),拟人/防机械节奏 |

引擎 `_do_action` 加 `_resolve_xy`(rel 时 `GetCursorPos`+偏移)与 act 分支;`_Player.run` 的 sleep 加 rand 抖动。旧宏文件无新字段 → `.get` 兜底,完全兼容。

**范围外(本轮不做)**:查找颜色(无取色能力 + 需条件分支,破坏「线性序列」定义)、输入到剪贴板(需放宽宏领域定义为非纯输入事件,留待单议)。

### 12.7 右侧全模块展开 + 紧凑无滚动(2026-06-19)
把右侧从「分段切换器(一次只显示一个模块)」改成「所有模块紧凑横排、一页全展示(无滚动)」。

**动机**:用户希望一眼看全所有可加动作、各模块就地添加,不必点切换按钮;且**不要滚动条**,
要求极致空间利用,5 个模块在设置窗一页内全部容下。

**实现**:
- 删 `SegmentedButtons`(及其 `QButtonGroup` 导入、`_MODULES` 常量)——成为死代码。
- 新增 `_Module(QWidget)` 基类:**紧凑横排条**(窄标签 64px + 字段竖排 1-2 横排行 +
  行尾紧凑图标按钮 ＋/✓/✕,语义靠 tooltip)+ 校验,发 `add_requested(dict)` /
  `update_requested(dict)` / `cancel_requested()` / `invalid(str)`。
  子类各实现 `_build_fields(box)/load/dump/validate/reset`(box 为字段竖排 QVBoxLayout)。
- 5 个子类:`MouseModule`(点击/按下/松开/双击)/ `KeyboardModule`(敲击/按下/松开)/
  `MoveModule` / `ScrollModule` / `WaitModule`。各模块**字段独立**,天然无串值。字段横排紧凑,
  长说明移 tooltip;坐标行用紧凑 `_make_pos_row`(窄 spin + 单字「抓」按钮)。
- `ActionEditor` 改为容器:**无滚动**,5 模块紧凑条纵向直铺 + 模块间 `QFrame` HLine 分隔,
  一页全展示(适配设置窗 560×480)。透传各模块信号。
  - `begin_add()`:全模块回添加态、解禁。
  - `begin_edit(a)`:按 `a["t"]` 路由到对应模块进更新态,其余模块禁用添加;
    不可编辑类型(录制底层 btn/key)返回 False。
- `MacroTab`:右侧去掉统一底部按钮行;连 4 个编辑器信号到 `_on_module_add` /
  `_on_module_update` / `_cancel_edit` / `_on_module_invalid`;选中不可编辑行时用
  `_edit_hint` 标签提示。左侧表格 + 删/清/移 + `_persist` + schema/引擎 **全部不变**。
- `style.py`:加 `#modRow`/`#modName`/`#modAdd`/`#modSep` 紧凑样式(SpinBox 不覆盖
  padding,避免压掉箭头按钮的右侧留白)。

### 12.8 统一底部按钮 + 编辑可改类型(2026-06-19)
把「每模块各自一个添加按钮」合并成「功能编辑区底部唯一按钮」,并放开编辑时的类型锁定。

**诉求**:① 编辑某条动作时可把它改成别的类型(鼠标条目改成键盘/移动等);
② 五个添加按钮合并成一个放底部;③ 省下行内按钮空间,使进编辑时窗口不必变大。

**新交互模型(激活模块 + 底部唯一按钮)**:
- 5 模块仍全展开。点进哪个模块(获焦/点击该行)→ 该模块**激活**(左色条高亮),
  底部唯一按钮就对**当前激活模块**操作。
- 新建态:底部 = 「＋ 添加动作」→ 把激活模块 dump 的动作追加到列表。
- 编辑态(选中左侧可编辑动作):该动作载入其模块并激活,底部 = 「✓ 更新此动作」+
  「取消编辑」。**不再锁类型**——可点别的模块切到其字段再点更新 → 那条被替换成新类型;
  前置延迟 `d` 由 `ActionEditor._edit_d` 统一保管,切类型也保留。

**实现**:
- `_Module`:删每行 add/update/cancel 按钮及 `add_requested/update_requested/
  cancel_requested/invalid` 信号、`_on_add/_on_update/enter_edit/exit_edit/set_add_enabled`。
  改为只发 `activated` 信号(给自身+所有子控件装 `eventFilter`,捕 `FocusIn`/
  `MouseButtonPress`)+ `set_active(on)`(置动态属性 `active` + repolish 高亮)。
- `ActionEditor`:底部加唯一按钮行(添加/更新/取消,按 `_editing` 切换);跟踪 `_active`
  模块(默认鼠标),各模块 `activated` → `_set_active`(清他人高亮);`_on_add`/`_on_update`
  对激活模块校验+dump+发对外信号;`begin_edit(a)` 载入+激活+记 `_edit_d`,**不再禁用其他模块**。
- `MacroTab`:信号名不变,连接照旧;不再管按钮(已移进 ActionEditor)。
- `style.py`:`#modRow[active="true"]` 醒目高亮(粗左色条 + 整圈暖边框 + 浅暖底 + 标签变深)。
- **激活可见性增强**:底部按钮文字带当前激活模块名(「＋ 添加:🖱 鼠标」/「✓ 更新为:⌨ 键盘」),
  随激活切换实时更新(`_refresh_buttons`),让「将要添加/更新哪个动作」一目了然。

### 12.9 一个外框包全部模块 + 激活单一高亮(2026-06-19)
诉求(逐步收敛):5 个模块要突出当前激活项,但**不要每块各自的边框/阴影**(碎而乱);
最终要「**一个方框把所有模块整体包起来** + 激活行单一高亮」。

**设计(一个盒子 + 内部扁平行 + 单一高亮)**:
- 5 个模块装进**一个白底圆角边框盒子** `#modBox`(与左侧动作列表盒对称,视觉平衡)。
  底部统一按钮行在盒子**外、下方**。
- 盒内每行(`#modRow`)**无独立边框**,仅靠细底分隔线区隔;末行标 `lastRow` 去掉底线
  (避免悬在盒底边之上)。
- **激活**:单一高亮 = 一道左色条(`#b07d4a`) + 一层轻暖底(`#f3e8d6`) + 标签主色加深。
  同时只一行高亮。无卡片边框、无发光阴影。
- (中途曾试「每模块独立卡片 + `QGraphicsDropShadowEffect` 发光」,因多卡多阴影杂乱,弃用。)

**改动文件**:`macro_tab.py`(`ActionEditor` 把 5 模块包进 `#modBox`、末行标 `lastRow`;
`_Module` 无图形效果,`set_active` 仅切动态属性)、`style.py`(`#modBox` 外框 + `#modRow`
内部行 + 单一激活高亮)。schema/引擎/左侧表格/信号/按钮带名 **不变**。

> ⚠️ **Qt 坑(高亮一度不可见的根因)**:`QWidget` 子类默认**不绘制**样式表的
> `background-color`/`border`。`_Module` 与 `#modBox` 都是 QWidget 子类,必须
> `setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)`,否则激活底色与外框
> 根本画不出来(属性值正确但肉眼无高亮)。验证 UI 视觉**别只断言属性**——要 `render()`
> 到 QPixmap 抓像素比色(本轮即如此验证:激活行采样到 #f3e8d6 才算数)。

### 范围外明确不做（宏本轮）
倍速 / 压缩等待、分辨率自适应缩放、跨平台。

## 13. 主题色 + 窗口置顶 + 功能可见性（2026-06-19）

> 领域概念见 [`CONTEXT.md`](./CONTEXT.md)「主题色 / 窗口置顶 / 功能可见性」三条。本节记实现层面。

### 13.1 主题色（清新风 + 单色派生 + 可自定义）
- **`style.py`**：原静态 `STYLE` 常量 → `build_style(theme_hex)` 函数。`_derive(hex)` 用 `QColor` HSL
  从单一主题色派生整套色:强调色亮度钳在 0.40~0.52(白字按钮可读)、背景拉到极淡(L≈0.96 取代米白)、
  hover/pressed/选中/边框/滚动条/箭头按档位生成。**正文固定深灰 `#2c2c2c` 不随主题变**(可读硬边界)。
  QSS 模板用 `string.Template` 的 `${}` 占位(QSS 自身的 `{}` 不受影响)。
- `DEFAULT_THEME = "#4A90D9"`(logo 家族中蓝);`PRESET_THEMES` 6 个清新预设。
- `config.py` 加 `theme_color`;`main.py` 启动 `build_style(load_config 的 theme_color)`。
- 设置窗通用 Tab:色块按钮(开 `QColorDialog`)+ 预设小色块 + 「恢复默认」;选定即
  `QApplication.setStyleSheet(build_style(色))` **全局即时预览** + 防抖落盘;托盘 `applied` 再统一重套。

### 13.2 窗口置顶（外部窗口,新模块 `app/window_pin.py`）
- 与贴图区别:贴图钉**自己的静态截图**;此工具把**别的程序活窗口**提到顶层。是并列的独立桌面小工具。
- **机制(PowerToys 做法)**:全局热键(默认 `Ctrl+Alt+T`,`config.window_top_hotkey`,可配/留空禁用)
  → `WindowPinner.toggle_foreground()`:`GetForegroundWindow()` 取前台窗 → 读 `WS_EX_TOPMOST`
  (`GetWindowLongW`+`GWL_EXSTYLE`)判当前态 → `SetWindowPos(HWND_TOPMOST/NOTOPMOST,
  NOMOVE|NOSIZE|NOACTIVATE)` 翻转 → `done` 信号弹托盘气泡。**仅全局热键一个入口**(原托盘菜单「置顶当前窗口」项因菜单触发取窗不可靠已移除,2026-06-19)。
- 热键经 `tray._register_window_top_hotkey`(随 app 常驻,不受宏开关影响)注册;隐藏该模块/留空则注销。
- 退出前 `unpin_all()` 解除本会话置顶过的窗,不留副作用。
- **⚠️ 踩坑(已修)**:① 初版用「全屏覆盖拾取」:为捕获点击去掉 `WA_TranslucentBackground` →
  全屏窗不透明 → 全局样式铺满整屏 → **白屏**;且覆盖拾取焦点/时序脆弱。② Win32 函数未声明
  `restype` → 64 位 **HWND 被截断**成 32 位 → 置顶静默失败。改用「热键 toggle 前台窗」(无覆盖,
  不可能白屏)+ 显式声明所有 Win32 签名,两症齐解。

### 13.3 功能可见性（按模块隐藏菜单入口 + 设置 Tab）
- `config.feature_visibility`:7 模块布尔(ocr/translate/macro/pin/window_top/autostart/reset_engine),默认全开;`load_config` 逐键补全(将来加模块兼容)。
- `tray._build_menu` 按可见性条件加菜单项;**红线**「设置」「退出」无条件常加(全关也只剩这俩,不死锁)。
- 设置窗通用 Tab 7 复选框;`_apply_tab_visibility` 用 `QTabWidget.setTabVisible` 显隐 OCR/翻译/宏 Tab,
  **通用 Tab 永远可见**(红线,所有开关都在这,故任何隐藏都可逆)。
- 改动经 `applied` 信号 → 托盘 `_build_menu` 重建菜单。

### 范围外明确不做（本轮）
主题色不联动 logo 重绘;窗口置顶不做视觉边框提示/不持久化(重启不恢复上次置顶的窗);功能可见性不级联到热键注册(宏热键仍按 enabled 走)。

## 14. 改名 + 更新检查（2026-06-19）

### 14.1 软件改名为 zhz_tool
- 单一来源 `app/version.py`:`__version__` + `APP_NAME="zhz_tool"` + GitHub 常量。
- 三处引用 `APP_NAME`:`tray` 托盘提示、`main` 应用显示名(`setApplicationName`/`setApplicationDisplayName`)、`OCRTool.spec` 的 `name='zhz_tool'`(打包出 `zhz_tool.exe`)。
- **未改的内部标识**(改了有副作用):单实例锁 `OCRTool_SingleInstance`、自启注册表键 `APP_NAME="OCRTool"`(autostart.py)、spec 文件名/build 目录名。窗口标题(「OCR / 翻译」等)是功能标题,非软件名,未动。
- exe 图标:`app/logo.ico`(由 logo.png 垫成正方形后转多尺寸生成),spec 加 `icon='app/logo.ico'`。换 logo 需重跑 png→ico 再打包。

### 14.2 更新检查（只提示,不下载）
- `app/updater.py`:`check_latest()` 调 `GITHUB_LATEST_API` 取最新 release tag → `_is_newer` 按语义化版本比;`UpdateChecker(QThread)` 后台跑、`result_ready` 信号回主线程(每次新建实例,避开 moveToThread 复用 affinity 坑)。网络失败返回 None,静默。
- 设置窗「关于」Tab(`_build_about_tab`):软件名 + 版本 + 可点 GitHub 链接 + 「检查更新」按钮(`_check_update_manual`→`_on_update_checked`,有新版弹 QMessageBox 跳转,无新版/失败给文字反馈)。
- 托盘:`__init__` 末尾 `QTimer.singleShot(5000, ...)` 启动静默查;`_on_update_checked(silent)` **仅有新版才** `showMessage` 气泡,`messageClicked`→开 release 页。`_update_checkers` list 持有引用防 GC。
- **零自有流量**:走 GitHub API + 用户手动到 GitHub 下载,不碰 COS/CDN。
- 发版流程:改 `__version__` → 打包 → GitHub 建 Release(tag `vX.Y.Z` + 上传 exe + 写正文)。**当前仓库无 release,故查更新显示"已是最新"属正常**。

## 15. 文件搜索(Everything 式,MFT+USN+提权 helper)(2026-06-20)

> 领域概念见 CONTEXT.md「文件搜索」;架构决策与否决备选见 docs/adr/0004。本节记实现层。

### 15.1 模块分工
- `file_search.py`:纯 ctypes 读 NTFS。`build_graph` 走 `FSCTL_ENUM_USN_DATA` 全量枚举 → `{idx:(parent,is_dir,name)}` FRN 图(键掩低 48 位);`_entries_from_graph`/`_path_of` 据父链重建完整路径(根 idx=5 不在枚举结果里,顶层目录父=5 时仍要拼自己名字——曾漏 `Windows` 层的 bug);`query_journal`/`read_changes` 读 USN 增量;`apply_changes` 把增量应用到图、按 idx 归并去重(一次建文件产生多条 USN 记录,只按"最终是否存在"算一次)返回 (added, removed)。
- `file_index.py`:`FileIndex` 内存搜索 + 二进制存档。路径转小写 UTF-8 拼成 **bytes blob**(`bytes.find` 比 str 快)+ offsets 二分定位;`_scan_blob` 是热路径(★留作 ctypes 调 C 的替换点)。增量叠加层:`apply_delta(added,removed)` 用 `_delta`(新增)+ `_dead`(墓碑)避免 3.3s 全量重建。存档 `save/load` 只存原始路径(小写 blob 载入时重建)。实测 539 万项:扫 22s、建索引 3.3s、存档 653MB、搜索最坏 ~250ms(罕见词全扫)。
- `file_search_service.py`:**提权 helper 进程**。`Helper.prepare` 载存档/全量建 + 建 FRN 图 + USN 补课;`_usn_loop` 每 2s 补增量;`serve` 绑 127.0.0.1 随机端口、token 鉴权、换行 JSON 协议(ping/stat/search/shutdown);`shutdown` 落盘 + 记 USN 位置 + **关 socket 唤醒阻塞的 accept()**(否则进程不退)。
- `file_search_client.py`:`IndexClient` GUI 侧瘦客户端,接口与 FileIndex 同(`__len__`/`search`),每次短连接发一行 JSON;helper 未起时 ping 返回 False。
- `file_search_task.py`:计划任务管理。`install` 用 **XML 方式**(`/create /xml`,避开区域日期格式坑)创建"最高权限 + 无触发器(只按需 /run)"任务,经 ShellExecute `runas` 提权(一次 UAC);`run` 用 `schtasks /run` 静默拉起(不再 UAC);`is_installed`/`uninstall`。
- `search_window.py`:`SearchWindow`,搜索框 + QTreeWidget(名称|路径)。输入防抖 120ms、后台线程搜(`_SearchWorker`)、就绪轮询(helper 建索引期间禁搜显"正在启动…")、前 1000 条上限、双击打开、右键(打开/打开所在文件夹/复制路径/复制文件名)、`closed` 信号。

### 15.2 接入与生命周期
- `main.py`:最前面识别 `--file-search-helper` → 走 `file_search_service.main()`(独立提权进程,不起 GUI/不占单实例锁)。
- `tray._open_file_search`:ping → 没起则 `task.install()`(首次,弹 UAC)+ `task.run()` 静默拉起 → 开 `SearchWindow(IndexClient())`;`closed`/`_quit` 时 `shutdown_helper()`(关窗即退提权进程,零后台)。单例 `_search_win`。
- 热键 `file_search_hotkey`(默认空=关,可在通用自定义),`_register_file_search_hotkey` 仿窗口置顶热键;可见性 `feature_visibility.file_search`。

### 15.3 范围外/已知边界(本轮)
- 仅 NTFS;仅系统盘 C(多盘 `_DRIVE` 留扩展点);搜索最坏 ~250ms 纯 Python(C 加速接口已留 `_scan_blob`,实测嫌慢再上);目录改名的子孙路径过期靠下次开窗补课兜底(会话中罕见)。计划任务首次装弹一次 UAC、杀软可能对"最高权限任务"敏感。
