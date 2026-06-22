# -*- coding: utf-8 -*-
"""文件搜索的可切换引擎(见 docs/adr/0005)。

两种引擎,对搜索窗暴露同一组方法(probe / search / advanced_search / drives / shutdown),
故搜索窗代码不必区分背后是谁:
- NativeEngine    : 自研 MFT+USN 引擎,实为包一层 IndexClient(连提权 helper 的 socket)。
- EverythingEngine: 委托本机已安装并正在运行的 Everything(经其 SDK DLL `Everything64.dll`)。

切换引擎不改变"搜什么"(名字/路径/不含内容、三模式、高级条件),只改"由谁来搜";两引擎
结果都呈现为 (is_dir, path) 列表。Everything 不可用时,make_engine 回退自研。
"""
import os
import sys
import ctypes
import threading
from ctypes import wintypes

ENGINE_NATIVE = "native"
ENGINE_EVERYTHING = "everything"
ENGINE_LABELS = {ENGINE_NATIVE: "自研引擎", ENGINE_EVERYTHING: "Everything 引擎"}

# Everything SDK 错误码 / 请求标志(见 Everything SDK 文档)
_EVERYTHING_ERROR_IPC = 2          # GetMajorVersion 返回 0 且 LastError=2 = Everything 没在运行
_REQUEST_FILE_NAME = 0x00000001
_REQUEST_PATH = 0x00000002

_dll_lock = threading.Lock()        # Everything SDK 是全局状态,串行化所有调用
_edll = None                        # 已加载的 Everything64.dll(惰性)
_edll_tried = False


def _dll_candidates():
    """Everything64.dll 的探测顺序:用户安装目录优先,捆绑副本兜底。"""
    paths = []
    # 1) 注册表 / 常见安装目录
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        if base:
            paths.append(os.path.join(base, "Everything", "Everything64.dll"))
    # 2) 捆绑副本(打包态在 _internal/app,开发态在 app/)
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, "Everything64.dll"))
    return paths


def _load_dll():
    """惰性加载 Everything64.dll 并绑定函数签名;失败返回 None(只试一次)。"""
    global _edll, _edll_tried
    if _edll is not None or _edll_tried:
        return _edll
    _edll_tried = True
    for p in _dll_candidates():
        if not os.path.exists(p):
            continue
        try:
            d = ctypes.WinDLL(p)
        except OSError:
            continue
        try:
            d.Everything_SetSearchW.argtypes = [wintypes.LPCWSTR]
            d.Everything_SetMatchPath.argtypes = [wintypes.BOOL]
            d.Everything_SetMatchCase.argtypes = [wintypes.BOOL]
            d.Everything_SetMatchWholeWord.argtypes = [wintypes.BOOL]
            d.Everything_SetRegex.argtypes = [wintypes.BOOL]
            d.Everything_SetMax.argtypes = [wintypes.DWORD]
            d.Everything_SetRequestFlags.argtypes = [wintypes.DWORD]
            d.Everything_QueryW.argtypes = [wintypes.BOOL]
            d.Everything_QueryW.restype = wintypes.BOOL
            d.Everything_GetNumResults.restype = wintypes.DWORD
            d.Everything_GetTotResults.restype = wintypes.DWORD
            d.Everything_IsFolderResult.argtypes = [wintypes.DWORD]
            d.Everything_IsFolderResult.restype = wintypes.BOOL
            d.Everything_GetResultFullPathNameW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD]
            d.Everything_GetMajorVersion.restype = wintypes.DWORD
            d.Everything_GetLastError.restype = wintypes.DWORD
            d.Everything_Reset.argtypes = []
        except AttributeError:
            continue                # 不是预期的 Everything DLL
        _edll = d
        return _edll
    return None


def everything_available():
    """Everything 是否可用 = SDK DLL 能加载 且 Everything 进程在运行。

    GetMajorVersion 返回 0 且 LastError=IPC(2) → 没在运行。供搜索设置决定该选项是否置灰。
    """
    d = _load_dll()
    if d is None:
        return False
    with _dll_lock:
        ver = d.Everything_GetMajorVersion()
        return ver > 0
class NativeEngine:
    """自研引擎:包一层 IndexClient(连提权 helper 的 socket)。需 helper 在跑。"""
    kind = ENGINE_NATIVE

    def __init__(self):
        from app.file_search_client import IndexClient
        self._client = IndexClient()

    def probe(self):
        """就绪探测:索引项数(0=helper 还没建好,搜索窗据此轮询等待)。"""
        try:
            return len(self._client)
        except Exception:
            return 0

    def ready_text(self, n):
        return f"自研引擎 · 索引 {n} 项,输入关键词开始搜索"

    def search(self, query, limit=1000, types=None, match_path=False, path_query=None,
               whole_word=False, case=False, drives=None):
        return self._client.search(query, limit=limit, types=types, match_path=match_path,
                                   path_query=path_query, whole_word=whole_word, case=case,
                                   drives=drives)

    def advanced_search(self, cond, limit=1000, drives=None):
        return self._client.advanced_search(cond, limit=limit, drives=drives)

    def drives(self):
        return self._client.drives()

    def shutdown(self):
        """关搜索窗 / 切到别的引擎:通知 helper 落盘退出(自研那套零后台)。"""
        try:
            self._client.shutdown_helper()
        except Exception:
            pass


# ── Everything 查询语法映射(把我们的三模式 + 高级条件翻成 Everything 查询串)──

_TYPE_TO_EE = {                       # 我们的类型 key → Everything 的 ext: 列表 / 宏
    "audio": "ext:mp3;wav;flac;aac;ogg;wma;m4a;ape;aiff;opus",
    "archive": "ext:zip;rar;7z;tar;gz;bz2;xz;iso;cab;tgz",
    "document": "ext:doc;docx;xls;xlsx;ppt;pptx;pdf;txt;md;csv;rtf;odt;ods;odp;epub;wps;et;dps",
    "executable": "ext:exe;msi;bat;cmd;com;scr;ps1;lnk;appx;msix",
    "image": "ext:jpg;jpeg;png;gif;bmp;webp;svg;tiff;tif;ico;heic;raw;psd",
    "video": "ext:mp4;mkv;avi;mov;wmv;flv;webm;m4v;mpg;mpeg;ts;rmvb",
    "folder": "folder:",
}
_ATTR_BIT_TO_EE = {0x1: "r", 0x2: "h", 0x4: "s", 0x10: "d", 0x20: "a",
                   0x100: "t", 0x400: "p", 0x800: "c", 0x1000: "o",
                   0x2000: "i", 0x4000: "e"}


def _ee_quote(term):
    """含空格的词加引号,供 Everything 当作整体匹配。"""
    return '"%s"' % term if (" " in term) else term


def _build_ee_query_simple(query, types, match_path, path_query):
    """普通 / 路径+名称 模式 → Everything 查询串(不含 ext/size 等高级项,那些走 advanced)。"""
    parts = []
    for tok in (query or "").split():
        parts.append(_ee_quote(tok))
    if match_path and path_query:
        for tok in path_query.split():
            parts.append("path:" + _ee_quote(tok))
    if types:
        type_parts = [_TYPE_TO_EE[t] for t in types if t in _TYPE_TO_EE]
        if len(type_parts) == 1:
            parts.append(type_parts[0])
        elif type_parts:
            parts.append("<" + "|".join(type_parts) + ">")   # 多类型 = OR 分组
    return " ".join(parts)


def _build_ee_query_advanced(cond):
    """高级搜索条件字典 → Everything 查询串。覆盖常用项;无法表达的项跳过(诚实降级)。"""
    c = cond or {}
    parts = []
    for w in c.get("name_all", []):
        parts.append(_ee_quote(w))
    if c.get("name_phrase"):
        parts.append('"%s"' % c["name_phrase"])
    if c.get("name_any"):
        parts.append("<" + "|".join(_ee_quote(w) for w in c["name_any"]) + ">")
    for w in c.get("name_none", []):
        parts.append("!" + _ee_quote(w))
    for w in c.get("path", []):
        parts.append("path:" + _ee_quote(w))
    if c.get("folder"):
        kw = "path:" if c.get("include_sub", True) else "parent:"
        parts.append(kw + _ee_quote(c["folder"]))
    for e in c.get("ext", []):
        parts.append("ext:" + e.lstrip("."))
    for t in c.get("types", []):
        if t in _TYPE_TO_EE:
            parts.append(_TYPE_TO_EE[t])
    # 大小 size:>=min <=max(字节)
    if c.get("size_min") is not None:
        parts.append("size:>=%d" % c["size_min"])
    if c.get("size_max") is not None:
        parts.append("size:<=%d" % c["size_max"])
    # 时间 dm/dc/da:>=fromISO <=toISO
    import datetime as _dt
    for key, kw in (("mtime", "dm"), ("ctime", "dc"), ("atime", "da")):
        f, t = c.get(key + "_from"), c.get(key + "_to")
        if f is not None:
            parts.append("%s:>=%s" % (kw, _dt.datetime.fromtimestamp(f).strftime("%Y-%m-%d")))
        if t is not None:
            parts.append("%s:<=%s" % (kw, _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")))
    # 属性 attrib:hsra…
    attr_letters = "".join(_ATTR_BIT_TO_EE.get(b, "") for b in c.get("attrs", []))
    if attr_letters:
        parts.append("attrib:" + attr_letters)
    # 文件名长度 len:>=min <=max(注:Everything len 指完整路径长,近似)
    if c.get("name_len_min") is not None:
        parts.append("len:>=%d" % c["name_len_min"])
    if c.get("name_len_max") is not None:
        parts.append("len:<=%d" % c["name_len_max"])
    # 文件夹深度:Everything 无直接等价,跳过(降级)
    return " ".join(parts)


class EverythingEngine:
    """Everything 引擎:经 SDK DLL 委托本机正在运行的 Everything。所有调用串行(SDK 是全局态)。"""
    kind = ENGINE_EVERYTHING

    def __init__(self):
        self._dll = _load_dll()

    def probe(self):
        """就绪探测:Everything 在跑返回 1(它自己即时可查,无需等待),否则 0。"""
        return 1 if everything_available() else 0

    def ready_text(self, n):
        return "Everything 引擎 · 输入关键词开始搜索"

    def _run(self, query, limit, case=False, whole_word=False, regex=False,
             match_path=False, drives=None):
        """执行一次 Everything 查询,返回 [(is_dir, path), ...]。盘符过滤在结果后做。"""
        d = self._dll
        if d is None:
            return []
        with _dll_lock:
            d.Everything_Reset()
            d.Everything_SetSearchW(query)
            d.Everything_SetMatchPath(bool(match_path))
            d.Everything_SetMatchCase(bool(case))
            d.Everything_SetMatchWholeWord(bool(whole_word))
            d.Everything_SetRegex(bool(regex))
            d.Everything_SetRequestFlags(_REQUEST_FILE_NAME | _REQUEST_PATH)
            d.Everything_SetMax(limit)
            if not d.Everything_QueryW(True):
                return []
            n = d.Everything_GetNumResults()
            buf = ctypes.create_unicode_buffer(1024)
            out = []
            drv = set(x.lower() for x in drives) if drives else None
            for i in range(n):
                d.Everything_GetResultFullPathNameW(i, buf, 1024)
                p = buf.value
                if drv and (not p or p[0].lower() not in drv):
                    continue
                out.append((bool(d.Everything_IsFolderResult(i)), p))
            return out

    def search(self, query, limit=1000, types=None, match_path=False, path_query=None,
               whole_word=False, case=False, drives=None):
        q = _build_ee_query_simple(query, types, match_path, path_query)
        if not q.strip():
            return []
        # match_path=False 时只匹配名字;path_query 已用 path: 前缀表达,故这里恒 False
        return self._run(q, limit, case=case, whole_word=whole_word,
                         match_path=False, drives=drives)

    def advanced_search(self, cond, limit=1000, drives=None):
        c = cond or {}
        regex = bool(c.get("regex"))
        q = c["regex"] if regex else _build_ee_query_advanced(c)
        if not q.strip():
            return []
        case = bool(c.get("name_case") or c.get("regex_case"))
        return self._run(q, limit, case=case, regex=regex, match_path=False, drives=drives)

    def drives(self):
        """Everything 索引全盘;盘符过滤用我们检测的固定 NTFS 盘列表填充勾选项。"""
        try:
            from app.file_search import fixed_ntfs_drives
            return fixed_ntfs_drives()
        except Exception:
            return []

    def shutdown(self):
        pass        # Everything 是外部进程,我们不管它的生命周期


def make_engine(kind):
    """按 kind 造引擎;Everything 不可用则回退自研。返回 (engine, actual_kind, fell_back)。"""
    if kind == ENGINE_EVERYTHING and everything_available():
        return EverythingEngine(), ENGINE_EVERYTHING, False
    fell_back = (kind == ENGINE_EVERYTHING)     # 想要 Everything 但不可用 → 回退
    return NativeEngine(), ENGINE_NATIVE, fell_back


def default_engine_kind(saved):
    """决定启动用哪个引擎(见 ADR-0005 决策3):有保存值用保存值;否则首次检测到 Everything 即默认它。"""
    if saved in (ENGINE_NATIVE, ENGINE_EVERYTHING):
        return saved
    return ENGINE_EVERYTHING if everything_available() else ENGINE_NATIVE
