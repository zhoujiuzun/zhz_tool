# -*- coding: utf-8 -*-
"""文件搜索 提权 helper 的计划任务管理(Windows 计划任务实现"一生一次 UAC")。

机制(见 docs/adr/0004):
- install():创建一个"最高权限、按需触发(不自动跑)"的计划任务。这一步需管理员 → 弹一次
  UAC(经 ShellExecute runas 提权执行 schtasks /create)。
- run():`schtasks /run` 拉起该任务 → helper 以管理员身份静默启动,**不再弹 UAC**。GUI
  普通权限即可调用。
- uninstall()/is_installed():删除/查询任务。

任务只按需 /run 触发(用一个永不到来的 ONCE 时间占位,杜绝自动运行),与"关窗即退出
helper、开窗才拉起"的模型一致。
"""
import os
import sys
import ctypes
import subprocess

TASK_NAME = "zhz_tool_FileSearchHelper"
_CREATE_NO_WINDOW = 0x08000000     # 不弹黑窗


def _helper_command():
    """返回 (program, args_str):以管理员身份要运行的命令 = 本程序 + --file-search-helper。

    打包态:exe 自己;开发态:pythonw + main.py。两者都附 --file-search-helper 标志,
    由 main.py 据此进入 helper 模式(而非 GUI)。
    """
    flag = "--file-search-helper"
    if getattr(sys, "frozen", False):
        return sys.executable, flag
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    return pythonw, f'"{main_py}" {flag}'


def _schtasks(args, elevated=False):
    """跑 schtasks。elevated=True 时经 ShellExecute runas 提权(弹 UAC)。返回 True/False。"""
    if elevated:
        # runas:以管理员运行 schtasks.exe;用户在 UAC 同意后才执行
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "schtasks.exe", subprocess.list2cmdline(args), None, 0)
        return int(rc) > 32        # >32 = 成功发起(用户点了"是")
    try:
        r = subprocess.run(["schtasks.exe"] + args, creationflags=_CREATE_NO_WINDOW,
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_installed():
    """计划任务是否已注册**且指向当前程序**。

    只查"存在"不够:开发版↔打包版切换、或 dist 目录移动后,旧任务会指向失效路径,
    `/run` 静默跑不起来、GUI 永远干等。故比对任务里的 Command 是否就是当前 _helper_command()。
    不匹配(过期)→ 视为未安装,由调用方重装到正确路径。
    """
    try:
        r = subprocess.run(["schtasks.exe", "/query", "/tn", TASK_NAME, "/xml"],
                           creationflags=_CREATE_NO_WINDOW, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False                       # 任务不存在
    xml = r.stdout.decode("utf-16", "ignore") or r.stdout.decode("utf-8", "ignore")
    program, _args = _helper_command()
    return program in xml                  # 任务里的 Command 含当前程序路径 = 未过期


def run():
    """按需拉起 helper(管理员身份、静默,不弹 UAC)。任务未注册返回 False。"""
    return _schtasks(["/run", "/tn", TASK_NAME])


def uninstall():
    """删除计划任务(需管理员 → 弹一次 UAC)。"""
    return _schtasks(["/delete", "/tn", TASK_NAME, "/f"], elevated=True)


# PLACEHOLDER_INSTALL


def _build_xml():
    """生成计划任务 XML(区域无关,优于命令行 /sd 日期格式;无触发器=绝不自动跑,只靠 run())。"""
    from xml.sax.saxutils import escape
    program, args = _helper_command()
    cmd = escape(program)
    arg = escape(args)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>zhz_tool 文件搜索 helper(按需提权拉起)</Description></RegistrationInfo>\n'
        '  <Principals><Principal id="Author">'
        '<LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel>'
        '</Principal></Principals>\n'
        '  <Settings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled>'
        '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
        '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
        '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>\n'
        f'  <Actions Context="Author"><Exec><Command>{cmd}</Command>'
        f'<Arguments>{arg}</Arguments></Exec></Actions>\n'
        '</Task>\n'
    )


def install():
    """创建"最高权限、无触发器(仅按需 /run)"的计划任务。需管理员 → 弹一次 UAC。

    用 XML 方式(/create /xml)以避开区域日期格式问题。XML 临时写到 TEMP,创建后删除。
    """
    xml = _build_xml()
    xml_path = os.path.join(os.environ.get("TEMP", _dir()), "zhz_tool_task.xml")
    try:
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml)
    except OSError:
        return False
    ok = _schtasks(["/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"], elevated=True)
    return ok


def _dir():
    d = os.path.expanduser("~/.ocr_tool")
    os.makedirs(d, exist_ok=True)
    return d


