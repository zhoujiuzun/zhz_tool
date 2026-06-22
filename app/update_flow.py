# -*- coding: utf-8 -*-
"""一键更新编排:发现新版 → 对话框确认 → 进度下载 → 静默提权安装 → 退出旧版。

为何单独成模块:托盘(开机静默检查)与设置窗(关于页手动检查)都要走这套流程,集中一处
避免两边各写一遍。所有 UI(对话框/进度条)都在主线程;下载在 Downloader 后台线程,
经信号回主线程,不卡界面。

流程(见 updater.py 的限制说明):
  has_update 且有安装包资产 → 弹「发现新版,是否更新?」对话框
    用户点「立即更新」→ 进度条下载安装包 → 校验通过 → runas 静默装(过一次 UAC)
      → 退出本程序(安装包会替换文件并重启到新版)
    用户点「稍后」→ 关闭,不打扰
  无安装包资产(只有 zip 等)→ 退化为「打开 GitHub 页」让用户手动下
"""
from PyQt6.QtWidgets import QMessageBox, QProgressDialog
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

from app.updater import Downloader, launch_installer
from app.version import APP_NAME


class UpdateFlow:
    """挂在 TrayApp 上,持有下载线程与进度框引用(防 GC)。一次只跑一个更新流程。

    quit_fn:退出整个程序的回调(装好后调它,让安装包替换文件 + 重启)。
    parent:对话框/进度框的父窗(可 None;托盘场景传 None,设置窗场景传窗体)。
    """
    def __init__(self, quit_fn, parent=None):
        self._quit_fn = quit_fn
        self._parent = parent
        self._dl = None
        self._progress = None
        self._busy = False          # 防重入:更新流程进行中,再次触发直接忽略

    def offer(self, result: dict):
        """收到 check_latest() 结果后调用。有新版才弹框;无安装包资产则退化为打开网页。"""
        if self._busy or not result or not result.get("has_update"):
            return
        latest, current = result.get("latest", "?"), result.get("current", "?")
        notes = result.get("notes") or ""
        asset_url = result.get("asset_url") or ""

        box = QMessageBox(self._parent)
        box.setWindowTitle("发现新版本")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"发现新版本 v{latest}(当前 v{current})。")
        if asset_url:
            box.setInformativeText("点「立即更新」自动下载并安装(需确认一次管理员授权,安装完自动重启)。")
            update_btn = box.addButton("立即更新", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        else:
            # 没有安装包资产(理论上不该发生):退化为打开网页手动下
            box.setInformativeText("请前往 GitHub 下载新版本。")
            update_btn = box.addButton("前往下载", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        if notes:
            box.setDetailedText(notes)
        box.exec()
        if box.clickedButton() is not update_btn:
            return                                  # 用户选了「稍后」

        if not asset_url:
            QDesktopServices.openUrl(QUrl(result.get("url", "")))
            return
        self._start_download(result)

    # PLACEHOLDER_DOWNLOAD
    def _start_download(self, result: dict):
        """开进度框 + 后台下载线程。"""
        self._busy = True
        self._progress = QProgressDialog("正在下载新版本…", "取消", 0, 100, self._parent)
        self._progress.setWindowTitle("更新")
        self._progress.setWindowModality(Qt.WindowModality.NonModal)
        self._progress.setMinimumDuration(0)       # 立刻显示,不等 4 秒默认延迟
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setValue(0)

        self._dl = Downloader(
            url=result["asset_url"],
            expected_size=result.get("asset_size", 0),
            sha256=result.get("sha256", ""),
            name=result.get("asset_name") or "zhz_tool_setup.exe")
        self._dl.progress.connect(self._on_progress)
        self._dl.error.connect(self._on_error)
        self._dl.done.connect(self._on_done)
        # 进度框「取消」→ 通知下载线程停
        self._progress.canceled.connect(self._on_cancel)
        self._dl.start()

    def _on_progress(self, pct: int):
        if self._progress is not None:
            self._progress.setValue(pct)

    def _on_cancel(self):
        if self._dl is not None:
            self._dl.cancel()
        # 不在此清理引用:等线程 done/error 信号回来再清(避免线程仍跑就被 GC)
        self._busy = False

    def _on_error(self, msg: str):
        if self._progress is not None:
            self._progress.close()
        QMessageBox.warning(self._parent, "更新失败",
                            f"下载更新失败:{msg}\n\n你可以稍后重试,或到 GitHub 手动下载。")
        self._cleanup()

    def _on_done(self, path: str):
        if self._progress is not None:
            self._progress.close()
        if not path:
            self._cleanup()                        # 失败/取消:error 信号已处理或用户主动取消
            return
        # 下好了:启动静默安装(弹一次 UAC)。成功发起后退出本程序,让安装包替换文件并重启。
        if launch_installer(path):
            box = QMessageBox(self._parent)
            box.setWindowTitle("正在更新")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(f"{APP_NAME} 即将关闭并完成更新,稍候会自动重启到新版本。")
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            self._cleanup()
            self._quit_fn()                        # 退出旧版,放行安装包替换正在占用的文件
        else:
            QMessageBox.warning(
                self._parent, "更新已取消",
                "未获得管理员授权,更新已取消。下次可重试,或到 GitHub 手动下载安装。")
            self._cleanup()

    def _cleanup(self):
        self._busy = False
        self._progress = None
        # _dl 留到其 finished 后由 Qt 回收;这里不主动 del,避免线程仍在收尾时被释放
        self._dl = None

