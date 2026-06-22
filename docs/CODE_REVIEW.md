# 代码审查报告 — zhz_tool 桌面工具箱

审查范围:`main.py` + `app/` 全部 28 个模块(约 10220 行),覆盖安全性、正确性、性能、健壮性、Qt 线程安全。
基线:commit `5d434b1`(v1.1.0)。

---

## 总体评价

工程质量明显高于同类个人项目:配置加密(Fernet)、原子写盘 + 损坏隔离、崩溃日志脱敏、
路径穿越防护、QThread 引用生命周期(用 `finished` 而非业务信号清理引用)、多屏 DPI 处理、
USN 增量索引等都做得专业且有注释说明权衡。下面是审查发现,按严重度排序。每条都标注了
「影响范围 / 是否确有问题 / 处置」。

---

## 高危(确认会导致功能损坏 / 崩溃)

### H1. 「刷新索引」会让提权 helper 彻底瘫痪 — 确认 BUG
- 位置:`app/file_search_service.py:307` `rebuild` 命令处理。
- 现象:
  ```python
  self._index = None              # 释放旧索引
  for ext in ['', '.pblob', '.lblob']:
      try: os.remove(_ARCHIVE + ext)
      except ...: pass
  ok = self.prepare()             # ← prepare() 第一行就 self._index.load(...)
  ```
  `prepare()` 内 `loaded = (not drives_changed) and self._index.load(_ARCHIVE)`,而
  `self._index` 此刻是 `None` → `None.load(...)` 抛 `AttributeError`。
- 影响范围:文件搜索窗「🔄 刷新索引」按钮(自研引擎)。点一次即触发。
- 后果:`AttributeError` 不被 `_handle` 的 `except OSError` 捕获 → 处理线程死亡 → 此后所有
  `search`/`stat` 都在 `len(None)` / `None.search()` 上崩,helper 名存实亡,搜索全 0,直到进程重启。
- 处置:**已修复** — 改为 `self._index = FileIndex()`(全新空索引)。归档文件已删,`load()` 会
  正常返回 False 并走全量重扫路径。

### H2. 文件搜索窗关闭时不等待后台线程 → QThread 销毁崩溃(0xc0000409)— 确认风险
- 位置:`app/search_window.py:649` `closeEvent` 只 `emit` + `super()`。
- 现象:窗口设了 `WA_DeleteOnClose`,但 `_worker / _adv_worker / _drives_worker / _rescan_worker /
  _ready_workers` 这些 `QThread` 在关窗时可能仍在运行。其中 `_rescan_worker` 阻塞约 30 秒、
  `_ready_workers` 探测可达数秒。线程仍运行时其 Python 对象随窗体被回收 → Qt 抛
  `QThread: Destroyed while thread is still running` → abort 崩溃。
- 影响范围:在「搜索中 / 刷新索引中 / 启动探测中」关闭搜索窗。
- 佐证:这正是项目在 `OCRWorker`/`_TestWorker`/`_TranslateWorker` 都用 `wait()` 防御的同一类崩溃
  (见 `tray.py:508`、`settings_window.py:305`、`result_window.py:194`),唯独搜索窗漏了。
- 处置:**已修复** — `closeEvent` 中对所有在飞 worker `wait()` 后再放行。

---

## 中危

### M1. API Key 经错误信息泄露(Google / Gemini)— 确认问题
- 位置:`app/dispatch.py:45` `humanize_error` 兜底 `return str(e)`;`providers.py:98` /
  `translators.py:149` 把 key 放在 URL 查询串 `?key={api_key}`。
- 现象:Gemini/Google 鉴权失败时 `requests` 的 `HTTPError` 文本含完整 URL,即
  `...generateContent?key=AIzaSy...`。该串经 `humanize_error` 原样返回 → 显示在托盘气泡/译文窗状态栏。
- 影响范围:用户自己的 key 显示给用户本人危害有限,但错误文本可能被截图分享、或写入其它日志,
  造成密钥外泄。`main.py` 的崩溃日志脱敏覆盖了 `key=`,但 UI 路径未脱敏。
- 处置:**已修复** — `humanize_error` 返回前对 `key=/token/authorization/api_key` 做脱敏。

### M2. 提权 helper 的 IPC 仅靠「同用户可读的 token」鉴权 — 设计风险(记录,未改)
- 位置:`file_search_service.py` token 写入 `~/.ocr_tool/file_search_helper.json`(同用户可读),
  helper 以管理员(High IL)运行,GUI(Medium IL)连 `127.0.0.1` 随机端口。
- 问题:任何**同用户**的中完整性进程都能读到 token、连上管理员 helper,调用
  `search/advsearch/stat/drives/rebuild/shutdown`。
  - 无任意命令执行 / 任意写文件(协议是封闭白名单),不构成提权。
  - 但构成**信息泄露**:普通进程可借管理员 helper 枚举全盘(含本不可读目录)的路径与元数据;
    `shutdown`/`rebuild` 可被滥用做轻量 DoS。
- 影响范围:仅在攻击者已能在本机以同用户身份运行代码时成立(此时威胁模型本就很宽)。
- 处置:**记录为可接受风险**。彻底修复需给状态文件 + 命名管道加 ACL(限定 High IL 或校验对端
  进程),改动较大。建议:① 对 `rebuild/shutdown` 这类副作用命令额外校验对端进程为本程序;
  ② 长期改用带 ACL 的命名管道。当前桌面单用户场景下风险低,本次不改以免引入回归。

### M3. 计划任务指向的 exe 若位于用户可写目录,存在提权风险 — 记录
- 位置:`file_search_task.py:_helper_command()` 用 `sys.executable`,计划任务以
  `HighestAvailable` 运行它。
- 问题:若程序被放在**非特权用户可写**的目录(如桌面、下载夹),低权限攻击者替换 exe 后,
  下次 `/run` 即以管理员执行其代码 → 本地提权。这是「可写目录 + 提权计划任务」的经典组合。
- 影响范围:取决于用户把程序装在哪。装在 `Program Files`(仅管理员可写)则无此问题。
- 处置:**记录 + 建议**。建议在 README/安装引导中提示安装到 `Program Files`;或 `install()` 前
  校验 exe 所在目录的 ACL。本次不改(属部署约定,非代码缺陷)。

---

## 低危 / 改进项(记录,本次不改)

- **L1 性能**:`file_index.py` 的 name/size/path/ext/mtime 排序索引全部 `if False` 禁用
  (注释说明是内存换速度的有意取舍)。后果:无名字种子词的高级搜索退化为对数百万条目的
  Python 全表扫。已有防抖 + 后台线程 + 结果上限兜底,体验可接受。属已知权衡,不改。
- **L2**:`providers.CustomOCR` 对自定义 URL 不拦内网/回环(注释已说明:桌面场景 SSRF 模型不成立,
  且连本地自部署是核心用法)。合理,不改。
- **L3**:`XunfeiOCR` 在 `app_id` 为空时仍会发请求(应在 required 校验拦下,且 `is_configured`
  已要求 app_id)。无实际触发路径,不改。
- **L4**:`tray._on_update_checked` 每次有新版都 `messageClicked.connect`;`_open_update_url` 自身
  会 disconnect,但理论上多次检查间隔内可能重复连接。影响极小,不改。
- **L5**:`file_search.py` 的硬链接 FRN 图(`build_graph`)只存单一主名,会话内增量对非主名硬链接
  路径不完整(注释已说明靠重扫兜底)。罕见,不改。

---

## 已修复清单(本次)

| 编号 | 文件 | 修改 |
|---|---|---|
| H1 | `app/file_search_service.py` | rebuild 时 `self._index = FileIndex()` 而非 `None` |
| H2 | `app/search_window.py` | `closeEvent` 等待所有在飞 QThread 结束 |
| M1 | `app/dispatch.py` | `humanize_error` 返回前脱敏 key/token/authorization |

修复均为最小改动、不改变正常路径行为,且与项目既有防御模式(QThread `wait()`、日志脱敏)一致。
