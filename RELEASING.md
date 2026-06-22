# 发版手册(RELEASING）

本项目用 GitHub Releases 分发,客户端自带「检查更新」(只检测、不自动装,见 `app/updater.py`)。
发一个新版本 = **改版本号 → 建 Release → 跑构建脚本上传**,共两步。

---

## 一次发版的完整流程

### 第 0 步:准备
- 确认要发的代码都已合进 `main` 并测过。
- 装好工具:Python 环境(含 PyInstaller)、[Inno Setup 6](https://www.innosetup.com/)。

### 第 1 步:改版本号(唯一来源)
编辑 `app/version.py`:
```python
__version__ = "1.1.2"   # 改成新版本号
```
> 这是**版本号的唯一来源**。zip 名、安装包名、安装包内版本、更新检测全都从它派生,
> 不要在别处再写一遍。

版本号规则(语义化):
- 改 bug → 末位 +1(`1.1.1` → `1.1.2`)
- 加功能 → 中位 +1(`1.1.1` → `1.2.0`)
- 大改/不兼容 → 首位 +1(`1.1.1` → `2.0.0`)

提交并推送:
```bash
git add app/version.py
git commit -m "chore: bump version to 1.1.2"
git push origin main
```

### 第 2 步:在 GitHub 建 Release(含发布说明)
在 https://github.com/zhoujiuzun/zhz_tool/releases 点 **Draft a new release**:
- **Tag**:`v1.1.2`(必须是 `v` + 版本号,和 `version.py` 对应;Target 选 `main`)
- **Title**:`v1.1.2 - 一句话说明`
- **说明**:写清这版改了什么(参考下面模板)。**发布说明由你掌控,脚本不会改它。**

> 也可以先不写说明、存成 Draft;但 `build.bat -Publish` 要求 tag 已存在(Draft 也算存在)。

### 第 3 步:构建并上传
```bat
build.bat -Publish
```
脚本会:读 `version.py` 版本号 → PyInstaller 打包 → 打绿色版 zip → 编译安装包 →
上传 `zhz_tool_setup_v1.1.2.exe` + `zhz_tool_v1.1.2.zip` 到 `v1.1.2` Release(`--clobber` 覆盖同名)。

> tag 不存在会**直接报错退出**,绝不擅自创建 Release —— 这是防误发的安全设计。
> 所以第 2 步必须先做。

发布后,把 Release 从 Draft 改为正式发布(如果第 2 步存的是 Draft),客户端即可检测到新版。

---

## 构建脚本用法速查

```bat
build.bat                 只构建(打包 + zip + 安装包),不发布。产物在 dist\
build.bat -SkipBuild      复用已有 PyInstaller 结果,只重打 zip + 安装包(调试打包用,省时间)
build.bat -Publish        构建后上传到「已存在」的 v<ver> Release
```
命令行也可直接:`powershell -ExecutionPolicy Bypass -File build.ps1 -Publish`

产物(都在 `dist\`,已被 .gitignore,不进仓库):
- `zhz_tool\` — onedir 目录产物(运行时实际用的)
- `zhz_tool_v<ver>.zip` — 绿色版(解压即用)
- `zhz_tool_setup_v<ver>.exe` — 安装版(装到 Program Files)

---

## 坑 & 注意事项(都是实际踩过的)

1. **`setup.exe` 编译报 "output file in use (32)"**
   多为杀毒软件正在扫描刚生成的 exe(瞬时文件锁)。等几秒重跑,或先手动删
   `dist\zhz_tool_setup_v<ver>.exe` 再跑即可。不是脚本问题。

2. **改了 `build.ps1` 后中文变乱码 / 解析报错**
   `build.ps1` 必须保存为 **UTF-8 with BOM**。Windows PowerShell 5.1 无 BOM 时按
   GBK 读,中文注释会让它解析崩。用支持 BOM 的编辑器保存,别用记事本另存为无 BOM 的 UTF-8。

3. **老的「绿色版(zip)」用户升到「安装版」**
   会出现两份程序并存。发布说明里要提醒他们:①删掉旧解压文件夹;②在「设置→通用」
   重新勾选开机自启(安装位置变了,旧自启失效)。文件搜索的提权计划任务会自愈,无需手动。
   (这条已写进 v1.1.1 发布说明,可复用。)

4. **`version.py` 和 tag 必须对应**
   `version.py` 写 `1.1.2`,Release tag 就必须是 `v1.1.2`。否则:① 安装包名/版本对不上;
   ② `-Publish` 找不到 tag 报错。

5. **更新是「只检测不自动装」**
   客户端发现新版只弹气泡 + 跳转 Release 页,用户手动下载安装。这是有意设计,
   不要期望它自动替换文件。

---

## 发布说明模板

```markdown
本次为 XXX 版本,建议所有用户更新。

## 📥 下载安装(二选一)
- **zhz_tool_setup_v<ver>.exe(推荐)** — 安装程序,双击装好,自动建快捷方式。已装旧版可直接覆盖。
- zhz_tool_v<ver>.zip — 绿色版,解压后双击 zhz_tool.exe。

## 更新内容
- 新增:……
- 修复:……
- 优化:……
```
