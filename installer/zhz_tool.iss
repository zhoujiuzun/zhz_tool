; ── zhz_tool 安装脚本 (Inno Setup 6) ─────────────────────────────────────────
; 把 onedir 打包产物 dist\zhz_tool\ 装成单个 setup.exe。
; 安装到 Program Files(全机器、需管理员):exe 落在非用户可写目录,堵死提权风险(审查 M3)。
; 运行时仍是 onedir(启动快、躲杀软反复扫描),与原分发方式一致。
;
; 编译:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\zhz_tool.iss
; 产物:  dist\zhz_tool_setup_v#.#.#.exe

#define MyAppName "zhz工具箱"
#define MyAppExeName "zhz_tool.exe"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "zhoujiuzun"
#define MyAppURL "https://github.com/zhoujiuzun/zhz_tool"
; 计划任务名:与 app/file_search_task.py 的 TASK_NAME 必须一致(卸载时清理它)
#define FileSearchTask "zhz_tool_FileSearchHelper"
; 单实例互斥名:与 main.py 的 CreateMutexW 名一致(让安装器能检测到程序在运行)
#define AppMutex "OCRTool_SingleInstance"

[Setup]
; AppId 是升级的唯一标识,一经发布不可改(改了会被当成不同程序、不覆盖旧版)
AppId={{8F3A2C71-5E94-4B2D-9A6F-1C7D0E2B4A88}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\zhz_tool
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 装到 Program Files → 需要管理员
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=zhz_tool_setup_v{#MyAppVersion}
SetupIconFile=..\app\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装/卸载前若程序在跑,提示并自动关闭(配合下面 [Code] 的 taskkill 兜底)
CloseApplications=yes
AppMutex={#AppMutex}

[Languages]
; Inno 6 未自带简体中文 .isl;先用英文标准向导保证可编译。
; 如需全中文向导:下载官方 ChineseSimplified.isl 放到本目录,改为
;   Name: "cn"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
en.LaunchAfter=立即运行 {#MyAppName}
en.MakeDesktopIcon=创建桌面快捷方式
en.AskRemoveData=是否同时删除你的配置和索引数据?%n%n包括 API Key、宏、文件索引等(位于用户目录 .ocr_tool)。%n选「否」可保留,重装后设置仍在。

[Tasks]
Name: "desktopicon"; Description: "{cm:MakeDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 把整个 onedir 产物递归装进去(zhz_tool.exe + _internal\)
Source: "..\dist\zhz_tool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成可勾选立即运行(普通权限运行,不带管理员——与日常使用一致)
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchAfter}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
{ 安装/卸载前强制结束仍在跑的程序与文件搜索 helper,避免文件被占用导致写入失败。
  CloseApplications + AppMutex 已能优雅关 GUI,这里 taskkill 兜底(含提权 helper)。}
procedure KillRunning;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/c taskkill /f /im {#MyAppExeName} /t',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunning;
  Result := '';
end;

{ 卸载时:① 关程序 → ② 删提权计划任务(卸载以管理员运行,schtasks 可删)
  → ③ 删开机自启注册表项 → ④ 可选删用户数据(默认保留)。}
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
  dataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    KillRunning;
    { 删文件搜索提权计划任务(名字须与 file_search_task.py 的 TASK_NAME 一致) }
    Exec(ExpandConstant('{cmd}'), '/c schtasks /delete /tn "{#FileSearchTask}" /f',
         '', SW_HIDE, ewWaitUntilTerminated, rc);
    { 删开机自启注册表项(app/autostart.py 写在 HKCU\...\Run 下,值名 OCRTool) }
    RegDeleteValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Run', 'OCRTool');
    { 可选:删用户配置/索引数据(默认保留;弹框问,选「是」才删)。
      静默卸载(/SILENT)下不弹框、一律保留数据,避免静默场景误删。 }
    if (not UninstallSilent) and
       (MsgBox(ExpandConstant('{cm:AskRemoveData}'), mbConfirmation, MB_YESNO) = IDYES) then
    begin
      dataDir := ExpandConstant('{%USERPROFILE}\.ocr_tool');
      if DirExists(dataDir) then
        DelTree(dataDir, True, True, True);
    end;
  end;
end;
