# -*- coding: utf-8 -*-
"""全局版本号：软件唯一的版本来源。

发版流程：改这里的 __version__ → 打包 → 在 GitHub 建同名 tag/release（如 v1.0.1）。
软件用它和 GitHub 最新 release 比较，判断是否有新版（见 app/updater.py）。
"""

__version__ = "1.2.0"

# 软件名 + 开源地址（关于页、更新检查共用，单一来源）
APP_NAME = "zhz_tool"
GITHUB_OWNER = "zhoujiuzun"
GITHUB_REPO = "zhz_tool"
GITHUB_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_RELEASES_URL = f"{GITHUB_URL}/releases"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
