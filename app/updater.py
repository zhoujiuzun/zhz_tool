# -*- coding: utf-8 -*-
"""检查 GitHub 是否有新版本(只检查、不下载)。

调 GitHub Releases API 拿最新 release 的 tag(如 v1.0.1),和本地 __version__ 比较。
有新版 → 发信号让 UI 提示用户去 GitHub 下载。下载/安装由用户手动,不走任何自有流量。
检查在后台线程进行,不卡界面;网络失败静默(不打扰用户)。
"""
import re
import logging
import requests
from PyQt6.QtCore import QThread, pyqtSignal

from app.version import __version__, GITHUB_LATEST_API, GITHUB_RELEASES_URL

_log = logging.getLogger(__name__)

_TIMEOUT = 8        # 秒;GitHub API 偶尔慢,给够但不无限等


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


def check_latest():
    """同步查一次最新版本。返回 dict 或 None(网络/解析失败)。

    dict: {has_update, latest, current, notes, url}
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
                "notes": "", "url": GITHUB_RELEASES_URL, "no_release": True}
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.info("检查更新失败(忽略): %s", e)
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "has_update": _is_newer(tag, __version__),
        "latest": tag.lstrip("vV"),
        "current": __version__,
        "notes": (data.get("body") or "").strip(),
        # 优先用该 release 的页面;拿不到则退到 releases 列表页
        "url": data.get("html_url") or GITHUB_RELEASES_URL,
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

