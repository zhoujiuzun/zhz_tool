# -*- coding: utf-8 -*-
"""检查 GitHub 新版本 + 一键更新(下载安装包 → 静默提权安装)。

- check_latest():调 GitHub Releases API 拿最新 tag,和本地 __version__ 比较;并解析出
  安装包(setup exe)的下载直链与可选的 SHA256(供一键更新下载校验)。
- Downloader:后台线程流式下载安装包,带进度回调 + 大小/PE 头/SHA256 校验。
- launch_installer():以管理员(runas,弹一次 UAC)静默运行安装包;安装包带
  CloseApplications + AppMutex,会自动关旧版、装新版、装完重启到新版。

为何不能"零交互全自动":程序装在 Program Files(写它需管理员→至少一次 UAC),且 exe
未代码签名(SmartScreen 首次运行会拦一次)。故"一键更新"= 点一下 + 过一次 UAC,
而非字面零点击。检查在后台线程进行,不卡界面;网络失败静默。
"""
import os
import re
import hashlib
import logging
import tempfile
import requests
from PyQt6.QtCore import QThread, pyqtSignal

from app.version import __version__, GITHUB_LATEST_API, GITHUB_RELEASES_URL

_log = logging.getLogger(__name__)

_TIMEOUT = 8        # 秒;GitHub API 偶尔慢,给够但不无限等
_DL_TIMEOUT = 30    # 下载单次读超时(秒);流式分块,整体可远超此值


def _parse_version(text: str):
    """把 'v1.2.3' / '1.2.3' 解析成 (1,2,3) 元组用于比较。解析不出返回 None。"""
    if not text:
        return None
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())


def _is_newer(remote: str, local: str) -> bool:
    """remote 版本是否比 local 新。任一解析失败则保守判为"不更新"。"""
    rv, lv = _parse_version(remote), _parse_version(local)
    if rv is None or lv is None:
        return False
    return rv > lv


def _pick_installer_asset(assets: list):
    """从 release 资产里挑出安装包(setup exe)的下载直链。挑不到返回 None。

    约定命名 zhz_tool_setup_v*.exe(见 build.ps1/installer)。容错:任何
    含 'setup' 的 .exe 都认;绿色版 zip 不作为一键更新目标(它不会自己装)。
    """
    for a in assets or []:
        name = (a.get("name") or "").lower()
        url = a.get("browser_download_url")
        if url and name.endswith(".exe") and "setup" in name:
            return {"name": a.get("name"), "url": url, "size": a.get("size", 0)}
    return None


def _pick_sha256(assets: list, installer_name: str) -> str:
    """从 SHA256SUMS.txt 资产里取安装包的 sha256(可选,加固下载校验)。取不到返回空串。

    SHA256SUMS.txt 每行格式:`<hex>  <filename>`(sha256sum 标准格式)。
    """
    sums_url = next((a.get("browser_download_url") for a in (assets or [])
                     if (a.get("name") or "").lower() == "sha256sums.txt"), None)
    if not sums_url:
        return ""
    try:
        txt = requests.get(sums_url, timeout=_TIMEOUT).text
    except Exception:
        return ""
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == installer_name:
            return parts[0].lower()
    return ""


def check_latest():
    """同步查一次最新版本。返回 dict 或 None(网络/解析失败)。

    dict: {has_update, latest, current, notes, url, asset_url, asset_name, asset_size, sha256}
    asset_* / sha256 供一键更新下载用;无安装包资产时它们为空(回退到「打开网页」)。
    """
    try:
        resp = requests.get(
            GITHUB_LATEST_API, timeout=_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"})
    except Exception as e:
        _log.info("检查更新失败(网络问题,忽略): %s", e)
        return None
    # 404 = 该仓库还没发布过任何 release。这是正常状态(不是失败),按"无新版"处理。
    if resp.status_code == 404:
        return {"has_update": False, "latest": __version__, "current": __version__,
                "notes": "", "url": GITHUB_RELEASES_URL, "no_release": True,
                "asset_url": "", "asset_name": "", "asset_size": 0, "sha256": ""}
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.info("检查更新失败(忽略): %s", e)
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    assets = data.get("assets") or []
    installer = _pick_installer_asset(assets)
    sha256 = _pick_sha256(assets, installer["name"]) if installer else ""
    return {
        "has_update": _is_newer(tag, __version__),
        "latest": tag.lstrip("vV"),
        "current": __version__,
        "notes": (data.get("body") or "").strip(),
        # 优先用该 release 的页面;拿不到则退到 releases 列表页
        "url": data.get("html_url") or GITHUB_RELEASES_URL,
        # 一键更新用:安装包直链 + 大小 + sha256(可能为空)
        "asset_url": installer["url"] if installer else "",
        "asset_name": installer["name"] if installer else "",
        "asset_size": installer["size"] if installer else 0,
        "sha256": sha256,
    }


class UpdateChecker(QThread):
    """后台线程查更新,结果经信号回主线程。每次检查新建一个实例,避免复用 affinity 问题。

        self._uc = UpdateChecker()
        self._uc.finished.connect(on_result)   # on_result(dict|None),自动队列回主线程
        self._uc.start()
    """
    result_ready = pyqtSignal(object)   # 传 check_latest() 的返回(dict 或 None)

    def run(self):
        self.result_ready.emit(check_latest())


class Downloader(QThread):
    """后台流式下载安装包到临时目录,带进度 + 校验。结果经信号回主线程。

    progress(int): 0~100 百分比(总大小未知时不发)。
    done(str): 成功=本地文件路径;失败=空串(error 信号带原因)。
    error(str): 失败原因(可读)。
    """
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, url: str, expected_size: int = 0, sha256: str = "", name: str = "zhz_tool_setup.exe"):
        super().__init__()
        self._url = url
        self._expected = expected_size or 0
        self._sha = (sha256 or "").lower()
        self._name = name
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            path = self._download()
        except Exception as e:
            self.error.emit(_humanize_dl_error(e))
            self.done.emit("")
            return
        if path:
            self.done.emit(path)

    def _download(self):
        # 下到系统临时目录(用户可写、无需提权);文件名带版本,避免与旧的撞
        dst = os.path.join(tempfile.gettempdir(), self._name)
        h = hashlib.sha256()
        got = 0
        with requests.get(self._url, stream=True, timeout=_DL_TIMEOUT) as r:
            r.raise_for_status()
            total = self._expected or int(r.headers.get("Content-Length", 0) or 0)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if self._cancel:
                        self.error.emit("已取消下载")
                        self.done.emit("")
                        return ""
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if total > 0:
                        self.progress.emit(min(100, int(got * 100 / total)))
        # ── 校验:大小 + PE 头 + (可选)sha256 ──
        if self._expected and got != self._expected:
            os.remove(tmp)
            raise RuntimeError(f"下载不完整({got}/{self._expected} 字节)")
        with open(tmp, "rb") as f:
            head = f.read(2)
        if head != b"MZ":                    # Windows PE 可执行文件魔数;先关文件再删(Win 不可删占用文件)
            os.remove(tmp)
            raise RuntimeError("下载的文件不是有效的安装程序")
        if self._sha:
            if h.hexdigest().lower() != self._sha:
                os.remove(tmp)
                raise RuntimeError("安装包校验失败(SHA256 不匹配),为安全起见已删除")
        os.replace(tmp, dst)                     # 校验通过才落到最终名
        return dst


def _humanize_dl_error(e: Exception) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return "下载超时,请检查网络后重试"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "无法连接下载服务器,请检查网络"
    return str(e) or e.__class__.__name__


def launch_installer(path: str) -> bool:
    """以管理员(runas,弹一次 UAC)静默运行安装包。发起成功返回 True。

    安装包(Inno)带 CloseApplications + AppMutex:会关掉正在跑的本程序,替换文件后重启到新版。
    /VERYSILENT 静默装、/SUPPRESSMSGBOXES 抑制弹框、/NORESTART 不重启系统(程序自身重启由 iss 的 [Run] 负责)。
    """
    import ctypes
    params = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, params, None, 1)
        return int(rc) > 32        # >32 = 用户在 UAC 点了"是",已发起
    except Exception as e:
        _log.warning("启动安装程序失败: %s", e)
        return False

