# -*- coding: utf-8 -*-
"""文件搜索 提权后台进程(helper)+ 其 IPC 协议。

架构(见 docs/adr/0004):GUI 普通权限,helper 提权(经计划任务静默拉起)。helper 常驻
内存持有 FRN 图 + FileIndex,后台线程定期补 USN 增量;通过 **localhost TCP** 对 GUI 提供
搜索服务。GUI 是瘦客户端(file_search_client),自己不读 MFT、不提权。

为何要 helper 提权:读 MFT/USN 必须管理员;GUI 不该提权(安全 + 不破坏拖拽)。
按搜索窗启停:GUI 开窗 → 拉起 helper;关窗 → 通知 helper 落盘并退出(ADR-0004)。

协议:127.0.0.1 上换行分隔的 JSON。每条请求带 token(写在用户态状态文件里,防同机其它
进程乱连)。请求 {"cmd":"search","q":..,"limit":..,"token":..} / ping / stat / shutdown。
"""
import os
import json
import socket
import threading
import time

from app.file_search import (build_graph, query_journal, read_changes, apply_changes,
                              entries_and_graph_full, metadata_of_path, fixed_ntfs_drives)
from app.file_index import FileIndex

_DIR = os.path.expanduser("~/.ocr_tool")
_STATE = os.path.join(_DIR, "file_search_helper.json")   # 端口 + token,GUI 读它来连
_ARCHIVE = os.path.join(_DIR, "file_index.bin")          # 索引存档(重启秒载)
_USN_STATE = os.path.join(_DIR, "file_search_usn.json")  # 每盘的 journal_id + next_usn

_CATCHUP_SEC = 2          # 会话中每隔几秒补一次 USN 增量
_DRIVE = "C"              # 检测不到任何固定 NTFS 盘时的兜底(正常应至少有系统盘)


def _write_state(port, token):
    os.makedirs(_DIR, exist_ok=True)
    tmp = _STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    os.replace(tmp, _STATE)


def read_state():
    """GUI 侧读 helper 的 {port, token, pid};无/损坏返回 None。"""
    try:
        with open(_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# PLACEHOLDER_SERVER


def _load_usn():
    try:
        with open(_USN_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_usn(d):
    tmp = _USN_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _USN_STATE)


class Helper:
    """提权后台进程主体:建/载索引(多盘合并) → 各盘补 USN → 起 socket 服务 → 后台定期补增量。

    多盘:所有固定 NTFS 盘合并成**一个** FileIndex(路径自带盘符,天然可合并搜索);但 FRN 图 /
    USN 位置按盘独立(各盘 MFT 的 FRN 空间互不相干,不能混)。盘符集变化(插拔盘)→ 强制全量重扫。
    """

    def __init__(self, drives=None):
        # 索引哪些盘:默认所有固定 NTFS 盘(对标 Everything);检测不到则退回系统盘。
        self._drives = list(drives) if drives else (fixed_ntfs_drives() or [_DRIVE])
        self._index = FileIndex()
        self._graph = {}            # drive -> FRN 图(增量真相源);秒开时后台异步建
        self._jid = {}              # drive -> USN journal id
        self._next_usn = {}         # drive -> 下次补增量的起点
        self._catchup_from = {}     # drive -> 载存档时的 USN 起点(后台建好图后补这段)
        self._lock = threading.Lock()       # 保护 _index(搜索 vs 增量应用)
        self._usn_lock = threading.Lock()   # 串行化补课:防后台建图与 _usn_loop 并发重复应用
        self._graph_ready = False   # FRN 图就绪前不补 USN(图是增量应用的前提)
        self._stop = threading.Event()

    def prepare(self):
        """启动:有存档则**秒载即可搜**(各盘建图 + USN 补课丢后台);无存档/盘集变化才必须先全量扫。

        盘符集变化检测:USN 状态文件的 key 集 = 上次索引的盘集。与本次要索引的盘集不一致(插了
        新盘 / 拔了盘)→ 不用旧存档,强制全量重扫,避免少盘/多盘的陈旧结果。
        """
        usn_state = _load_usn()
        drives_changed = set(usn_state.keys()) != set(self._drives)
        loaded = (not drives_changed) and self._index.load(_ARCHIVE)
        # 逐盘查 USN 日志状态(打不开的盘跳过,不致命)
        any_ok = False
        for d in self._drives:
            info = query_journal(d)
            if info is None:
                continue
            jid, nxt, _ = info
            self._jid[d] = jid
            self._next_usn[d] = nxt
            any_ok = True
            u = usn_state.get(d)
            self._catchup_from[d] = u["next_usn"] if (loaded and u and u.get("jid") == jid) else None
        if not any_ok:
            return False
        if loaded:
            # 索引已就绪 → 立刻可搜;各盘建图 + 补课异步;名字 blob 后台构建(子串搜索就绪)
            threading.Thread(target=self._build_graphs_async, daemon=True).start()
            threading.Thread(target=self._build_name_blob, daemon=True).start()
            return True
        # 无存档/盘集变化:全量扫所有盘,合并 entries 建一个索引(各盘 FRN 图分别存)。
        all_entries = []
        for d in self._drives:
            if d not in self._jid:
                continue                       # 该盘打开失败,跳过
            entries, nodes = entries_and_graph_full(d)
            if entries is None:
                continue
            self._graph[d] = nodes
            all_entries.extend(entries)
        if not all_entries:
            return False
        self._index.build(all_entries)
        self._graph_ready = True
        threading.Thread(target=self._build_name_blob, daemon=True).start()
        return True

    def _build_name_blob(self):
        """后台构建名字 blob(~4s,纯内存),让普通名字搜索做真子串匹配(对标 Everything)。

        构建期间不持 _lock:ensure_name_blob 只**读** _blob/_offsets(不改索引),搜索回退前缀路径
        照常进行。若构建中途遇全量重扫(_close_mmaps 清空 _blob)→ 抛异常,_name_blob_ready 仍 False,
        由下次重扫后的再次调用兜底重建。
        """
        try:
            self._index.ensure_name_blob()
        except Exception:
            pass

    def _build_graphs_async(self):
        """后台:逐盘建 FRN 图 → 补载存档以来的增量 → 标记图就绪(此后 _usn_loop 才补)。

        先补完各盘首次增量再置 _graph_ready,避免与 _usn_loop 并发从不同起点重复应用。
        """
        for d in self._drives:
            if d not in self._jid:
                continue
            nodes = build_graph(d)
            if nodes is None:
                continue
            self._graph[d] = nodes
            if self._catchup_from.get(d) is not None:
                self._next_usn[d] = self._catchup_from[d]   # 从载存档时的起点补这段增量
                self._catch_up_drive(d)
        self._graph_ready = True

    def _full_rescan(self):
        """某盘 USN 环形覆盖兜底:重扫所有盘重建合并索引(持 _lock)。盘多时少见,简单可靠优先。"""
        all_entries = []
        for d in self._drives:
            entries, nodes = entries_and_graph_full(d)
            if nodes is not None:
                self._graph[d] = nodes
                all_entries.extend(entries)
            info = query_journal(d)
            if info:
                self._jid[d], self._next_usn[d], _ = info
        if all_entries:
            with self._lock:
                self._index.build(all_entries)
            # build() 会清空名字 blob(_blob 已重建),后台重建以恢复子串搜索
            threading.Thread(target=self._build_name_blob, daemon=True).start()

    def _catch_up_drive(self, drive):
        """从某盘 next_usn 读增量并应用;日志被覆盖则全量重扫所有盘兜底。

        全程持 _usn_lock 串行:后台建图首次补课与 _usn_loop 定时补课不并发(否则会从同一
        起点重复读、重复 apply,产生重复结果)。
        """
        with self._usn_lock:
            graph = self._graph.get(drive)
            if graph is None:
                return
            start_usn = self._next_usn.get(drive, 0)
            res = read_changes(drive, self._jid[drive], start_usn)
            if res is None:                              # 环形覆盖/日志重建 → 全量重扫
                self._full_rescan()
                return
            changes, nxt = res
            if changes:
                added, removed = apply_changes(graph, changes, drive)
                # 给新增/改名文件补元数据(大小/时间/属性)→ 7 元组,集合小,逐个查不慢
                added_full = [(d, p) + metadata_of_path(p) for d, p in added]
                with self._lock:
                    self._index.apply_delta(added_full, removed)
            self._next_usn[drive] = nxt

    def _usn_loop(self):
        """后台线程:会话中每隔几秒补一次各盘增量,保持索引新鲜。图未就绪前跳过(图是应用前提)。"""
        while not self._stop.wait(_CATCHUP_SEC):
            if not self._graph_ready:
                continue
            for d in list(self._drives):
                try:
                    self._catch_up_drive(d)
                except Exception:
                    pass

    def shutdown(self):
        """落盘索引 + 记各盘 USN 位置(key 集=已索引盘集,供下次盘集变化检测);关 socket 让进程退出。"""
        self._stop.set()
        with self._lock:
            try:
                self._index.save(_ARCHIVE)
            except Exception:
                pass
        u = {}
        for d in self._drives:
            if d in self._jid:
                u[d] = {"jid": self._jid[d], "next_usn": self._next_usn.get(d, 0)}
        _save_usn(u)
        srv = getattr(self, "_srv", None)
        if srv is not None:
            try:
                srv.close()               # 唤醒 serve() 里阻塞的 accept() → 循环退出 → 进程结束
            except OSError:
                pass

    # PLACEHOLDER_SOCKET

    def serve(self):
        """绑 127.0.0.1 随机端口,起 USN 后台线程,循环处理连接直到收到 shutdown。"""
        import secrets
        token = secrets.token_hex(16)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))        # 0 = 系统分配空闲端口
        srv.listen(8)
        port = srv.getsockname()[1]
        _write_state(port, token)
        self._token = token
        self._srv = srv                   # 存起来,shutdown() 关它以唤醒下面阻塞的 accept()
        threading.Thread(target=self._usn_loop, daemon=True).start()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            try:
                os.remove(_STATE)         # 退出清掉状态文件,GUI 据此知道 helper 没了
            except OSError:
                pass

    def _handle(self, conn):
        """处理一个连接:逐行读 JSON 请求,逐行回 JSON 响应。"""
        f = conn.makefile("rwb")
        try:
            for line in f:
                try:
                    req = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if req.get("token") != self._token:
                    f.write(b'{"error":"bad token"}\n'); f.flush()
                    continue
                cmd = req.get("cmd")
                if cmd == "ping":
                    resp = {"ok": True}
                elif cmd == "stat":
                    resp = {"ok": True, "count": len(self._index)}
                elif cmd == "drives":
                    resp = {"ok": True, "drives": list(self._drives)}   # 已索引的盘符,供 GUI 勾选
                elif cmd == "search":
                    with self._lock:
                        res = self._index.search(req.get("q", ""), int(req.get("limit", 1000)),
                                                 types=req.get("types"),
                                                 match_path=bool(req.get("match_path")),
                                                 path_query=req.get("path_query"),
                                                 whole_word=bool(req.get("whole_word")),
                                                 case=bool(req.get("case")),
                                                 drives=req.get("drives"))
                    resp = {"ok": True, "results": res}     # [[is_dir, path], ...]
                elif cmd == "advsearch":
                    with self._lock:
                        res = self._index.search_advanced(req.get("cond") or {},
                                                          int(req.get("limit", 1000)),
                                                          drives=req.get("drives"))
                    resp = {"ok": True, "results": res}
                elif cmd == "shutdown":
                    f.write(b'{"ok":true}\n'); f.flush()
                    self.shutdown()
                    break
                elif cmd == "rebuild":
                    # 重建索引:删除旧存档 → 强制重新全扫 → 持久化
                    with self._lock:
                        import time, os
                        t0 = time.time()
                        # ★ 必须是全新空索引而非 None:prepare() 第一行就调 self._index.load(),
                        # 置 None 会 AttributeError 打死处理线程、helper 此后名存实亡(全 0)。
                        self._index = FileIndex()       # 释放旧索引,换全新空索引
                        self._graph = {}                # 同步清旧 FRN 图,避免重扫时与残图混用
                        self._graph_ready = False
                        # 删除存档文件,强制 prepare() 重新扫描(否则会直接加载)
                        for ext in ['', '.pblob', '.lblob']:
                            try:
                                os.remove(_ARCHIVE + ext)
                            except (OSError, FileNotFoundError):
                                pass
                        ok = self.prepare()             # 重新全扫(prepare 发现无存档会全量扫描)
                        t1 = time.time()
                    resp = {"ok": ok, "elapsed": round(t1 - t0, 1)}
                else:
                    resp = {"error": "unknown cmd"}
                f.write((json.dumps(resp) + "\n").encode("utf-8")); f.flush()
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main(drives=None):
    """helper 进程入口(由 GUI 经计划任务以管理员身份拉起:exe --file-search-helper)。

    drives=None → 索引所有固定 NTFS 盘(Helper 内自动检测)。helper 是 console=False 的窗口进程,
    崩了不可见。故包一层:任何异常写崩溃日志到 ~/.ocr_tool/file_search_helper_error.log,
    便于排查"搜索全 0"这类静默失败。
    """
    import time
    t0 = time.time()
    _log_timing("=== helper 进程开始执行 Python(此前的时间=exe解压+解释器启动)===")
    try:
        h = Helper(drives)
        _log_timing("索引盘符:%s" % ",".join(h._drives))
        ok = h.prepare()
        _log_timing("prepare() 完成,耗时 %.1fs(载存档/建索引);此刻起可搜 ok=%s" % (time.time() - t0, ok))
        if not ok:
            _log_error("prepare() 返回 False(打开卷失败?需管理员/NTFS?)")
            return 1
        h.serve()            # 收到 shutdown 后返回(此时已落盘 + 清状态文件)
    except Exception:
        import traceback
        _log_error("helper 崩溃:\n" + traceback.format_exc())
        return 1
    # 单文件 exe 的解释器/bootloader 收尾 + 临时目录清理会拖很久甚至卡住,而活已干完,
    # 直接强制终止,保证"关窗即消失、零后台"。os._exit 不跑 atexit/GC,但无副作用要清。
    os._exit(0)


def _log_timing(msg):
    """启动计时日志:排查"启动按分钟算"耗在哪。写 ~/.ocr_tool/file_search_timing.log。"""
    try:
        import datetime
        os.makedirs(_DIR, exist_ok=True)
        with open(os.path.join(_DIR, "file_search_timing.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def _log_error(msg):
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(os.path.join(_DIR, "file_search_helper_error.log"), "a", encoding="utf-8") as f:
            import datetime
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
