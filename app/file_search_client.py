# -*- coding: utf-8 -*-
"""文件搜索 GUI 侧客户端:连提权 helper 的 localhost socket,转发搜索请求。

对 SearchWindow 暴露与 FileIndex 相同的接口(__len__ / search),故搜索窗代码不必区分
"本地索引"还是"远程 helper"。helper 没起来时由上层(tray)负责拉起,见 ADR-0004。
"""
import json
import socket

from app.file_search_service import read_state

_TIMEOUT = 5          # 连接超时(短:helper 在跑就该秒连上)
_READ_TIMEOUT = 60    # 读响应超时(长:高级搜索全表扫可能数秒~十几秒,别误判断连)


class IndexClient:
    """瘦客户端:每次请求开一条短连接发一行 JSON、收一行 JSON。

    无状态、线程安全(搜索在 SearchWindow 的后台线程里调),实现简单且够快
    (本机 loopback,延迟微秒级;搜索耗时在 helper 端)。
    """

    def __init__(self):
        self._count = 0

    def _rpc(self, req):
        st = read_state()
        if not st:
            return None                      # helper 未运行
        req["token"] = st["token"]
        try:
            s = socket.create_connection(("127.0.0.1", st["port"]), timeout=_TIMEOUT)
        except OSError:
            return None
        try:
            s.settimeout(_READ_TIMEOUT)       # 连上后放大读超时:高级搜索全表扫可能数秒,别误断
            f = s.makefile("rwb")
            f.write((json.dumps(req) + "\n").encode("utf-8"))
            f.flush()
            line = f.readline()
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            s.close()

    def ping(self):
        r = self._rpc({"cmd": "ping"})
        return bool(r and r.get("ok"))

    def __len__(self):
        r = self._rpc({"cmd": "stat"})
        if r and r.get("ok"):
            self._count = r.get("count", 0)
        return self._count

    def search(self, query, limit=1000, prev=None, types=None, match_path=False, path_query=None,
               whole_word=False, case=False, drives=None):
        req = {"cmd": "search", "q": query, "limit": limit}
        if types:
            req["types"] = list(types)      # 类型 key 列表,空/None=不过滤
        if match_path:
            req["match_path"] = True        # 路径+名称搜索;缺省=普通(只名字)
            if path_query:
                req["path_query"] = path_query   # 路径过滤词(双框过滤的"路径含")
        if whole_word:
            req["whole_word"] = True        # 全字匹配(对标 Everything Match Whole Word)
        if case:
            req["case"] = True              # 区分大小写
        if drives:
            req["drives"] = list(drives)    # 限定盘符(小写字母列表);空/None=不限
        r = self._rpc(req)
        if not r or not r.get("ok"):
            return []
        # helper 回的是 [[is_dir, path], ...](JSON 无元组),转回 (is_dir, path)
        return [(bool(d), p) for d, p in r.get("results", [])]

    def advanced_search(self, cond, limit=1000, drives=None):
        """高级搜索:cond 为条件字典(见 advanced_search_window),交 helper 的 search_advanced。"""
        req = {"cmd": "advsearch", "cond": cond, "limit": limit}
        if drives:
            req["drives"] = list(drives)
        r = self._rpc(req)
        if not r or not r.get("ok"):
            return []
        return [(bool(d), p) for d, p in r.get("results", [])]

    def drives(self):
        """返回 helper 当前已索引的盘符列表(如 ['C','D']);取不到返回 []。供搜索设置抽屉勾选。"""
        r = self._rpc({"cmd": "drives"})
        if r and r.get("ok"):
            return list(r.get("drives", []))
        return []

    def shutdown_helper(self):
        """通知 helper 落盘并退出(GUI 关搜索窗时调)。"""
        self._rpc({"cmd": "shutdown"})

    def rebuild_index(self):
        """通知 helper 重新扫描磁盘并重建索引(GUI 刷新按钮调)。阻塞 ~30 秒直到重建完成。"""
        self._rpc({"cmd": "rebuild"})
