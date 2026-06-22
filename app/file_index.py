# -*- coding: utf-8 -*-
"""文件索引:内存搜索 + 本地二进制存档。

把 file_search.enumerate_volume 产出的 (is_dir, path) 列表建成可快速子串搜索的索引,
并落盘存档(重启秒载,不重扫)。搜索策略见 docs/adr/0004:
- 全部路径拼成一个大字符串 blob(\n 分隔),配 offsets 数组 → 命中位置经二分定位到第几条;
- 子串匹配用 str.find(底层 C,远快于逐条 Python `in`);
- 边打字"增量缩小":新词以上次词为前缀时,只在上次结果子集里再筛。

热路径 _scan_blob 独立成函数,留作将来 ctypes 调 C 的替换点(见 ADR-0004 渐进方案)。

v05 内存优化:元数据用 array.array(省 ~780MB PyObject 开销);路径存 utf-8 blob
+ 小写搜索 blob(省 ~300MB PyUnicode 开销 + 载档免重建)。兼容 v02/v03/v04。

v06 载入优化:v05 把头+两个 blob 全塞一个文件,load 用 f.read 一次性读 1.55GB 进**匿名内存**
再切两份拷贝 → 峰值 ~3.35GB,内存紧张时狂换页,冷启动按分钟算。v06 把两个 blob 拆成独立
边车文件(.pblob/.lblob)用 **mmap 按需分页**(文件支撑的干净页,缺页直接重读不占 pagefile),
头+定长数组用 array.fromfile 直读 → 峰值降到 ~0.35GB,冷启动 1~3s。兼容 v02~v05(读完转 v06)。
"""
import os
import struct
import bisect
import array
import mmap

_MAGIC_V02 = b"ZHZIDX02"  # v02: 含元数据,无排序索引,无预存 blob
_MAGIC_V03 = b"ZHZIDX03"  # v03: + size_order 排序索引
_MAGIC_V04 = b"ZHZIDX04"  # v04: + blob + offsets 预存,载档免重建
_MAGIC_V05 = b"ZHZIDX05"  # v05: array 元数据 + path_blob + 小写 blob,**全塞一个文件**(f.read 全量进内存)
_MAGIC     = b"ZHZIDX06"  # v06: 头/数组一个文件,两个 blob 拆独立边车文件用 mmap 按需分页(治 load 峰值内存)


def _basename(path: str) -> str:
    """取路径最后一段(文件名/文件夹名)。C:\\Windows\\System32 → System32。"""
    p = path.rstrip("\\")
    i = p.rfind("\\")
    return p[i + 1:] if i >= 0 else p


def _drive_ok(path: str, drives) -> bool:
    """盘符过滤:drives 为允许的盘符小写集合(如 {'c','d'});None=不过滤。"""
    if not drives:
        return True
    return bool(path) and path[0].lower() in drives


def _is_word_char(ch: str) -> bool:
    """全字匹配的"字符":字母/数字/下划线算词内,其余(. \\ - 空格等)为边界。"""
    return ch.isalnum() or ch == "_"


def _term_matches(haystack: str, term: str, whole_word: bool) -> bool:
    """term 是否出现在 haystack。whole_word=True 时要求两侧为词边界(对标 Everything 全字匹配)。

    haystack/term 的大小写在调用前已按"是否区分大小写"统一(区分则原样,不区分则都 lower)。
    """
    start = 0
    n = len(term)
    if n == 0:
        return True
    while True:
        i = haystack.find(term, start)
        if i < 0:
            return False
        if not whole_word:
            return True
        left_ok = (i == 0) or not _is_word_char(haystack[i - 1])
        right = i + n
        right_ok = (right >= len(haystack)) or not _is_word_char(haystack[right])
        if left_ok and right_ok:
            return True
        start = i + 1


# 文件类型分类:key → (中文名, 扩展名集合)。"folder" 特殊(按 is_dir 判,无扩展名)。
FILE_CATEGORIES = [
    ("folder", "文件夹", set()),
    ("audio", "音频", {"mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "ape", "aiff", "opus"}),
    ("archive", "压缩文件", {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab", "tgz"}),
    ("document", "文档", {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "md",
                          "csv", "rtf", "odt", "ods", "odp", "epub", "wps", "et", "dps"}),
    ("executable", "可执行文件", {"exe", "msi", "bat", "cmd", "com", "scr", "ps1", "lnk", "appx", "msix"}),
    ("image", "图片", {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "tiff", "tif",
                       "ico", "heic", "raw", "psd"}),
    ("video", "视频", {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "rmvb"}),
]
_EXT_TO_CAT = {ext: key for key, _name, exts in FILE_CATEGORIES for ext in exts}


def _category_of(path: str, is_dir: bool) -> str:
    """判断条目类型 key。目录→"folder";否则按扩展名查;无匹配→"other"。"""
    if is_dir:
        return "folder"
    name = _basename(path)
    i = name.rfind(".")
    if i < 0:
        return "other"
    return _EXT_TO_CAT.get(name[i + 1:].lower(), "other")


def _ext_of(path: str, name: str = None) -> str:
    """取扩展名(小写,不含点);无扩展名返回空串。可传预计算的 name 避免重复 _basename。"""
    if name is None:
        name = _basename(path)
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


# DOS 属性位(高级搜索「属性」分组用):key → (中文名, 位值)
FILE_ATTRS = [
    ("readonly", "只读", 0x1), ("hidden", "隐藏", 0x2), ("system", "系统", 0x4),
    ("directory", "目录", 0x10), ("archive", "存档", 0x20), ("device", "设备", 0x40),
    ("normal", "一般", 0x80), ("temporary", "临时", 0x100), ("sparse", "稀疏文件", 0x200),
    ("reparse", "重解析点", 0x400), ("compressed", "压缩", 0x800), ("offline", "离线", 0x1000),
    ("not_indexed", "未索引的内容", 0x2000), ("encrypted", "加密", 0x4000),
]


class FileIndex:
    def __init__(self):
        self._count = 0              # 基础条目数(v05:替代 len(_paths))
        self._path_blob = b""        # 原始大小写路径,utf-8,\n 分隔(供展示/打开)
        self._path_offsets = array.array('I')  # 第 i 条在 _path_blob 的起始字节偏移
        self._isdir = bytearray()    # 与条目对齐:1=目录 0=文件
        # ★ v05: 元数据全用 array.array,不转 list(省 ~780MB PyObject 开销)
        self._size = array.array('Q')      # 文件大小(字节);目录为 0
        self._mtime = array.array('d')     # 修改时间(Unix 秒)
        self._ctime = array.array('d')     # 创建时间
        self._atime = array.array('d')     # 访问时间
        self._attr = array.array('I')      # DOS 属性位
        self._blob = b""             # 小写路径 blob(bytes),供 _scan_blob 子串扫
        self._offsets = array.array('I')   # 第 i 条在小写 blob 的起始字节偏移
        self._name_offsets = array.array('I')  # 第 i 条名字段在 blob 的起始偏移(O(1),免 rfind)
        self._name_order = array.array('I')   # 按名字(小写)升序的索引,供普通搜索二分定位
        self._size_order = array.array('I')  # 按 size 升序的索引,供高级搜索二分定位
        self._path_order = array.array('I')  # 按完整路径(小写)升序的索引
        self._ext_order = array.array('I')   # 按扩展名升序的索引
        self._mtime_order = array.array('I') # 按修改时间升序的索引(会话中 USN 变更累积于此,避免全量重建;见 ADR-0004)
        self._delta = []             # 新增条目(tuple 列表,数量小,保留 Python 对象)
        self._delta_low = []         # 与 _delta 对齐的小写字节,供子串扫
        self._dead = set()           # 墓碑:被删/改名前消失的路径(小写)
        # v06: 两个 blob 用 mmap 按需分页(不再 f.read 全量进内存)。存活期需持有 mmap +
        # 底层文件句柄;_blob/_path_blob 直接指向 mmap 对象(支持 find/rfind/切片,不支持 startswith)。
        self._lblob_mm = None        # 小写搜索 blob 的 mmap(_blob 指向它)
        self._pblob_mm = None        # 原始路径 blob 的 mmap(_path_blob 指向它)
        self._lblob_f = None         # 小写 blob 文件句柄(mmap 存活期间需保持打开)
        self._pblob_f = None         # 路径 blob 文件句柄
        self._archive = None         # 本次 load 的存档主文件路径(save 据此定位边车文件)
        self._blob_on_disk = False   # True=blob 来自 v06 mmap 且未变更,save 可跳过重写边车
        # 名字 blob(只含 basename 的小写 blob,~155MB):普通名字搜索的**子串匹配**用它扫,
        # 既正确(名字任意位置含关键词都命中,对标 Everything)又快(不扫 754MB 全路径 blob)。
        # 由 helper 载档后后台构建(~4s,纯内存,不入存档);未就绪前 search 回退前缀路径。
        self._name_blob = b""              # 各条 basename(小写)\n 分隔
        self._name_blob_offsets = array.array('I')  # 第 i 条在 _name_blob 的起始偏移
        self._name_blob_ready = False      # 名字 blob 是否已构建就绪
        # ★ 前缀索引:加速名字搜索(避免全扫 670万条)
        self._name_prefix_index = {}       # {前缀bytes: (起始idx, 结束idx)} 如 b'gu': (2340000, 2341500)

    def __len__(self):
        return self._count + len(self._delta) - len(self._dead)

    def _close_mmaps(self):
        """解除两个 blob 的 mmap 并关底层文件句柄。

        Windows 上 mmap 存活时其文件被占用,os.replace 写新边车会失败;故 build()/save()
        写盘前、以及进程收尾前都需先调本函数。调后 _blob/_path_blob 不再可用(置回空 bytes)。
        """
        for mm in (self._lblob_mm, self._pblob_mm):
            if mm is not None:
                try:
                    mm.close()
                except (OSError, ValueError, BufferError):
                    pass
        for fh in (self._lblob_f, self._pblob_f):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._lblob_mm = self._pblob_mm = None
        self._lblob_f = self._pblob_f = None
        self._blob = b""
        self._path_blob = b""
        self._blob_on_disk = False
        # 名字 blob 依赖 _blob/_offsets;它们失效则名字 blob 也失效,需重建
        self._name_blob = b""
        self._name_blob_offsets = array.array('I')
        self._name_blob_ready = False

    # ── 路径访问(v05: blob → 按需解码) ──────────────────────────

    def _path_at(self, i):
        """取第 i 条原始大小写路径(用于展示/打开)。只对最终结果调,不在热路径。"""
        start = self._path_offsets[i]
        if i + 1 < len(self._path_offsets):
            end = self._path_offsets[i + 1] - 1   # -1 跳过 \n
        else:
            end = len(self._path_blob)
        return self._path_blob[start:end].decode("utf-8")

    def _name_pos(self, i):
        """返回第 i 条小写 blob 中名字段 (name_start, entry_end)。O(1)(预计算索引)。"""
        nbo = self._name_offsets
        if not nbo:
            self._build_name_offsets()
            nbo = self._name_offsets
        ee = self._offsets[i + 1] - 1 if i + 1 < len(self._offsets) else len(self._blob)
        return nbo[i], ee

    def _build_name_offsets(self):
        """从 blob + offsets 构建名字偏移索引(~200ms,5.4M 条目)。"""
        if self._name_offsets:
            return
        n = self._count
        blob = self._blob
        offs = self._offsets
        nbo = array.array('I', [0]) * n
        for i in range(n):
            es = offs[i]
            ee = offs[i + 1] - 1 if i + 1 < n else len(blob)
            sep = blob.rfind(b'\\', es, ee)
            nbo[i] = sep + 1 if sep >= 0 else es
        self._name_offsets = nbo

    # ── 构建 ─────────────────────────────────────────────────

    def build(self, entries):
        """从条目列表建索引。entries 每项为 (is_dir, path[, size, mtime, ctime, atime, attr])。"""
        import sys
        # 可能在会话中(环形覆盖兜底)对已 load 的索引重建:先释放旧 mmap(否则 Windows
        # 占用文件),并清空 _offsets 强制 _rebuild_blob 全量重建(否则它见 _offsets 非空会跳过)。
        self._close_mmaps()
        self._offsets = array.array('I')
        n = len(entries)
        self._count = n
        self._isdir = bytearray(n)
        self._size = array.array('Q', [0]) * n
        self._mtime = array.array('d', [0.0]) * n
        self._ctime = array.array('d', [0.0]) * n
        self._atime = array.array('d', [0.0]) * n
        self._attr = array.array('I', [0]) * n

        # 同时构建 path_blob + 两个 offsets
        path_chunks = []
        path_offs = array.array('I', [0]) * n
        pos = 0
        for i, e in enumerate(entries):
            is_dir, path = e[0], e[1]
            self._isdir[i] = 1 if is_dir else 0
            self._size[i] = e[2] if len(e) > 2 else 0
            self._mtime[i] = e[3] if len(e) > 3 else 0.0
            self._ctime[i] = e[4] if len(e) > 4 else 0.0
            self._atime[i] = e[5] if len(e) > 5 else 0.0
            self._attr[i] = e[6] if len(e) > 6 else 0
            path_offs[i] = pos
            pb = path.encode("utf-8")
            path_chunks.append(pb)
            pos += len(pb) + 1   # +1 for \n

        self._path_blob = b"\n".join(path_chunks)
        self._path_offsets = path_offs

        self._delta = []
        self._delta_low = []
        self._dead = set()
        self._rebuild_blob()      # 搜索 blob + offsets + size_order

    @staticmethod
    def _norm(entry):
        """把 added 项补齐成 7 元组 (is_dir, path, size, mtime, ctime, atime, attr)。"""
        if len(entry) >= 7:
            return tuple(entry[:7])
        is_dir, path = entry[0], entry[1]
        rest = list(entry[2:]) + [0, 0.0, 0.0, 0.0, 0][len(entry) - 2:]
        return (is_dir, path, rest[0], rest[1], rest[2], rest[3], rest[4])

    def apply_delta(self, added, removed):
        """应用一批 USN 增量(见 file_search.apply_changes 的产出)。O(变更数),不碰全量重建。"""
        for path in removed:
            self._dead.add(path.lower())
        if removed:
            dead_low = {p.lower() for p in removed}
            keep = [e for e in self._delta if e[1].lower() not in dead_low]
            self._delta = keep
            self._delta_low = [e[1].lower().encode("utf-8") for e in keep]
        for entry in added:
            e = self._norm(entry)
            low = e[1].lower()
            self._dead.discard(low)
            self._delta.append(e)
            self._delta_low.append(low.encode("utf-8"))

    def _rebuild_blob(self):
        """据 _path_blob 重建小写搜索 blob + offsets + size_order。

        从 path_blob 生成 lowered blob:逐条 decode → lower → encode(一次编解码循环)。
        若已从 v05 存档加载则 _offsets 非空,跳过。
        """
        import sys
        if self._offsets:
            return                            # 已从存档加载,跳过重建
        n = self._count
        # 构建 lowering blob:边遍历边写,不积累大列表(降低内存峰值)
        path_blob = self._path_blob
        poff = self._path_offsets
        import io
        buf = io.BytesIO()
        offs = array.array('I', [0]) * n
        pos = 0
        for i in range(n):
            start = poff[i]
            end = poff[i + 1] - 1 if i + 1 < n else len(path_blob)
            lowered = path_blob[start:end].decode("utf-8").lower().encode("utf-8")
            offs[i] = pos
            buf.write(lowered)
            buf.write(b"\n")
            pos += len(lowered) + 1
        self._blob = buf.getvalue()
        del buf  # 立即释放
        self._offsets = offs
        # 构建 name_offsets(第二遍遍历 blob,每个条目的名字起点)
        name_offs = array.array('I', [0]) * n
        for i in range(n):
            es = offs[i]
            ee = offs[i + 1] - 1 if i + 1 < n else len(self._blob)
            sep = self._blob.rfind(b'\\', es, ee)
            name_offs[i] = sep + 1 if sep >= 0 else es
        self._name_offsets = name_offs
        # ★ 暂时禁用大内存排序(name/size/path/ext order),回退 blob 全扫(慢但能跑)
        # 构建 name_order(按小写名字升序的索引) - 670万条排序吃 2~3GB 内存
        if False and n:  # 禁用
            # 先生成 (name_bytes, idx) 迭代器,边生成边排序,不积累完整列表
            def name_iter():
                for i in range(n):
                    s = name_offs[i]
                    e = offs[i + 1] - 1 if i + 1 < n else len(self._blob)
                    yield (self._blob[s:e], i)
            # sorted() 内部也会积累,但至少避免了列表推导式的二次拷贝
            sorted_pairs = sorted(name_iter(), key=lambda x: x[0])
            self._name_order = array.array('I', (i for _, i in sorted_pairs))
            del sorted_pairs  # 立即释放
        # 构建 size_order - 用生成器降低内存
        if False and n:  # 禁用
            sorted_size = sorted(((self._size[i], i) for i in range(n)), key=lambda x: x[0])
            self._size_order = array.array('I', (i for _, i in sorted_size))
            del sorted_size
        # 构建 path_order(按完整小写路径升序) - 用生成器降低内存
        if False and n:  # 禁用
            def path_iter():
                for i in range(n):
                    s = offs[i]
                    e = offs[i + 1] - 1 if i + 1 < n else len(self._blob)
                    yield (self._blob[s:e], i)
            sorted_path = sorted(path_iter(), key=lambda x: x[0])
            self._path_order = array.array('I', (i for _, i in sorted_path))
            del sorted_path
        # 构建 ext_order(按扩展名升序,用于 ext=.xxx 的二分) - 用生成器降低内存
        if False and n:  # 禁用
            def ext_iter():
                for i in range(n):
                    ns = name_offs[i]
                    ne = offs[i + 1] - 1 if i + 1 < n else len(self._blob)
                    dot = self._blob.rfind(b'.', ns, ne)
                    ext_bytes = self._blob[dot + 1:ne] if dot >= 0 and dot + 1 < ne else b''
                    yield (ext_bytes, i)
            sorted_ext = sorted(ext_iter(), key=lambda x: x[0])
            self._ext_order = array.array('I', (i for _, i in sorted_ext))
            del sorted_ext
        # ★ 暂时禁用 mtime_order 排序(按修改时间升序) - 也吃内存
        if False and n:  # 禁用
            mt_pairs = [(self._mtime[i], i) for i in range(n)]
            mt_pairs.sort()
            self._mtime_order = array.array('I', [i for _, i in mt_pairs])

    # ── 搜索核心 ──────────────────────────────────────────────

    def _locate(self, char_pos, offs):
        """blob 中某命中字符位置 → 第几条(offs 升序二分)。兼容 list/array。"""
        return bisect.bisect_right(offs, char_pos) - 1

    def _scan_blob(self, needle_bytes, limit):
        """在 blob 里找子串,返回命中的条目下标列表(去重、有序)。

        支持 bytes 和 mmap(两者 find() 接口相同,Python 3.11+ 均为 Boyer-Moore-Horspool)。
        """
        blob = self._blob
        offs = self._offsets
        out = []
        n = len(blob)
        start = 0
        last_idx = -1
        while True:
            hit = blob.find(needle_bytes, start)
            if hit < 0:
                break
            idx = self._locate(hit, offs)
            if idx != last_idx:
                out.append(idx)
                last_idx = idx
                if len(out) >= limit:
                    break
            end = offs[idx + 1] - 1 if idx + 1 < len(offs) else n
            start = max(hit + len(needle_bytes), end + 1)
        return out

    def ensure_name_blob(self):
        """构建"只含 basename 的小写 blob"(~155MB)供名字子串扫。幂等;已建则直接返回。

        从现有 _blob + _name_offsets 切出每条名字段拼成 \\n 分隔 blob + 偏移数组。纯内存(~4s,
        5.4M 条),不入存档。helper 载档后后台线程调用;搜索在它就绪前回退前缀路径。

        同时构建前缀索引(2字节前缀 → 条目ID集合),加速搜索 10~20 倍。
        """
        if self._name_blob_ready:
            return
        n = self._count
        blob = self._blob
        offs = self._offsets
        nbo = self._name_offsets
        if not nbo:
            self._build_name_offsets()
            nbo = self._name_offsets

        # 构建 name_blob + 前缀索引
        chunks = []
        name_offs = array.array('I', [0]) * n
        prefix_idx = {}  # {前缀bytes: [条目ID列表]}
        pos = 0

        for i in range(n):
            ns = nbo[i]
            ee = offs[i + 1] - 1 if i + 1 < n else len(blob)
            seg = bytes(blob[ns:ee])
            name_offs[i] = pos
            chunks.append(seg)
            pos += len(seg) + 1

            # 记录前缀(2字节)
            if len(seg) >= 2:
                prefix = seg[:2]
                if prefix not in prefix_idx:
                    prefix_idx[prefix] = []
                prefix_idx[prefix].append(i)

        self._name_blob = b"\n".join(chunks)
        self._name_blob_offsets = name_offs
        self._name_prefix_index = prefix_idx
        self._name_blob_ready = True

    def _scan_name_blob(self, needle_bytes, limit):
        """在名字 blob 里找子串,返回命中条目下标(去重、有序)。对标 Everything 的名字子串匹配。

        优化:查询词 >=2 字节时,先用前缀索引缩小范围,只检查候选条目,避免全扫 670 万条。
        """
        nb = self._name_blob
        offs = self._name_blob_offsets
        n = len(nb)
        ndl = len(needle_bytes)

        # ★ 前缀索引加速:查询词 >=2 字节且前缀索引已就绪
        if ndl >= 2 and self._name_prefix_index:
            prefix = needle_bytes[:2]
            candidates = self._name_prefix_index.get(prefix)
            if candidates is None:
                return []  # 前缀不存在,直接返回空

            # 只检查候选条目
            out = []
            for idx in candidates:
                ns = offs[idx]
                ne = offs[idx + 1] - 1 if idx + 1 < self._count else n
                if needle_bytes in nb[ns:ne]:
                    out.append(idx)
                    if len(out) >= limit:
                        break
            return out

        # 回退:全扫(短查询词或前缀索引未就绪)
        out = []
        start = 0
        last_idx = -1
        while True:
            hit = nb.find(needle_bytes, start)
            if hit < 0:
                break
            idx = bisect.bisect_right(offs, hit) - 1
            if idx != last_idx:
                out.append(idx)
                last_idx = idx
                if len(out) >= limit:
                    break
            end = offs[idx + 1] - 1 if idx + 1 < len(offs) else n
            start = max(hit + ndl, end + 1)
        return out

    # ── 搜索(v05: 热路径用 blob bytes 避免解码) ─────────────────

    @staticmethod
    def _name_terms_for_filter(query):
        """从查询里取出"用于 全字/大小写 后过滤"的名字词。

        只取普通名字词(不含 ':' 的 Everything 关键词、不以 \\ 开头的路径式)。路径式 / 扩展名式
        (\\路径、.dll、*.dll)交给原引擎按路径/扩展名处理,本过滤不插手(返回 [],仅盘符仍生效)。
        """
        toks = [t for t in (query or "").split() if ":" not in t]
        if not toks:
            return []
        if any(t.startswith("\\") for t in toks):
            return []                       # 路径式查询
        if len(toks) == 1:
            t = toks[0]
            if (t.startswith(".") and len(t) > 1 and t[1].isalnum()) or \
               (t.startswith("*.") and len(t) > 2 and t[2].isalnum()):
                return []                   # 扩展名式查询
        return toks

    def search(self, query, limit=1000, prev=None, types=None, match_path=False,
               path_query=None, whole_word=False, case=False, drives=None):
        """搜索 + 搜索设置后过滤(全字匹配 / 区分大小写 / 盘符)。

        三项都是核心结果的子集,故对 _search_impl 的结果后过滤即可(无过滤时零额外开销)。
        有过滤时先多取候选(上限放大),过滤后再截到 limit。
        """
        has_filter = whole_word or case or bool(drives)
        fetch = limit if not has_filter else max(limit, 100000)
        res = self._search_impl(query, limit=fetch, prev=prev, types=types,
                                match_path=match_path, path_query=path_query)
        if not has_filter:
            return res
        name_terms = self._name_terms_for_filter(query) if (whole_word or case) else []
        path_terms = (path_query or "").split() if (match_path and (whole_word or case)) else []
        out = []
        for is_dir, path in res:
            if not _drive_ok(path, drives):
                continue
            if name_terms:
                base = _basename(path)
                hay = base if case else base.lower()
                if not all(_term_matches(hay, (t if case else t.lower()), whole_word)
                           for t in name_terms):
                    continue
            if path_terms:
                hayp = path if case else path.lower()
                if not all(_term_matches(hayp, (t if case else t.lower()), whole_word)
                           for t in path_terms):
                    continue
            out.append((is_dir, path))
            if len(out) >= limit:
                break
        return out

    def search_advanced(self, cond, limit=1000, drives=None):
        """高级搜索 + 盘符后过滤。全字/大小写在高级表单里逐项已有(name_case 等),此处不重复。"""
        fetch = limit if not drives else max(limit, 100000)
        res = self._search_advanced_impl(cond, limit=fetch)
        if not drives:
            return res
        out = [(d, p) for d, p in res if _drive_ok(p, drives)]
        return out[:limit]

    def _search_impl(self, query, limit=1000, prev=None, types=None, match_path=False, path_query=None):
        """搜索核心(无 全字/大小写/盘符 过滤)。支持 Everything 风格语法: ext:x size:>1gb dm:today 等。

        全字匹配/区分大小写/盘符过滤由外层 search() 对结果后过滤(它们都是本结果的子集,安全)。
        """
        q = (query or "").strip()
        if not q: return []
        q_lower = q.lower()

        # ── 语法解析: Everything 风格关键词 ──
        import re as _re
        cond = {}
        name_parts = []
        _SIZE_UNITS = {'kb': 1024, 'mb': 1048576, 'gb': 1073741824, 'tb': 1099511627776}
        for token in q_lower.split():
            if ':' in token:
                key, val = token.split(':', 1)
                if key == 'ext':
                    exts = cond.get('ext', [])
                    exts.append(val.lstrip('.'))
                    cond['ext'] = exts
                elif key == 'size':
                    m = _re.match(r'([<>]=?)?(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)?(?:\.\.(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)?)?', val)
                    if m:
                        op, v1, u1, v2, u2 = m.groups()
                        mul1 = _SIZE_UNITS.get((u1 or '').lower(), 1)
                        s1 = int(float(v1) * mul1)
                        if op in ('>', '>=') or not op: cond['size_min'] = s1
                        if op in ('<', '<='): cond['size_max'] = s1
                        if v2:
                            mul2 = _SIZE_UNITS.get((u2 or '').lower(), 1)
                            cond['size_min'] = min(s1, int(float(v2) * mul2))
                            cond['size_max'] = max(s1, int(float(v2) * mul2))
                    elif val.isdigit():
                        cond['size_min'] = int(val)
                elif key in ('dm', 'datemodified'):
                    cond['mtime_from'] = self._parse_date(val)
                elif key in ('dc', 'datecreated'):
                    cond['ctime_from'] = self._parse_date(val)
                elif key in ('da', 'dateaccessed'):
                    cond['atime_from'] = self._parse_date(val)
                elif key == 'attrib' or key == 'attributes':
                    attr_map = {'h': 0x2, 's': 0x4, 'r': 0x1, 'a': 0x20, 'd': 0x10, 'c': 0x800, 'e': 0x4000, 't': 0x100, 'n': 0x80, 'i': 0x2000, 'o': 0x1000, 'p': 0x400}
                    for c in val.lower():
                        if c in attr_map: cond.setdefault('attrs', []).append(attr_map[c])
                elif key == 'folder':
                    cond['folder'] = val.replace('/', '\\')
                elif key == 'type':
                    cond.setdefault('types', []).append(val)
                elif key == 'regex':
                    cond['regex'] = val
                else:
                    name_parts.append(token)
            else:
                name_parts.append(token)
        name_query = ' '.join(name_parts).strip()

        # 有结构化条件 → 交给 search_advanced
        if cond and not match_path and not path_query and not types:
            if name_query:
                # 有名字词 + 结构化条件:与 cond 合并
                if ' ' in name_query:
                    cond['name_all'] = name_query.split()
                else:
                    cond['name_all'] = [name_query]
            return self._search_advanced_impl(cond, limit)
        if not name_query and not cond:
            return []

        # ★ 快速通道: "*.xxx" 或 ".xxx" → 扩展名筛选
        if not match_path and not path_query and not types and not name_query.startswith('\\'):
            is_dot_ext = name_query.startswith(".") and len(name_query) > 1 and name_query[1].isalnum()
            is_star_ext = name_query.startswith("*.") and len(name_query) > 2 and name_query[2].isalnum()
            if is_star_ext or is_dot_ext:
                ext = name_query[2 if is_star_ext else 1:].split()[0]
                if ext and all(c.isalnum() for c in ext):
                    return self._search_advanced_impl({"ext": [ext]}, limit)
            if '\\' in name_query and self._path_order:
                return self._search_path_prefix(name_query, limit)

        q2 = name_query or q_lower  # fallback to original if all tokens were keywords
        name_tokens = [t.encode("utf-8") for t in q2.split()]
        path_tokens = ([t.encode("utf-8") for t in path_query.strip().lower().split()]
                       if (match_path and path_query) else [])
        if not name_tokens and not path_tokens:
            return []
        types = set(types) if types else None
        results = []
        dead = self._dead
        isd = self._isdir
        blob = self._blob

        def verify_entry(i):
            """纯 bytes 验证:路径词 + 名字词。零拷贝。"""
            es = self._offsets[i]
            ee = self._offsets[i + 1] - 1 if i + 1 < len(self._offsets) else len(blob)
            if path_tokens and any(blob.find(t, es, ee) < 0 for t in path_tokens):
                return False
            if name_tokens:
                ns, ne2 = self._name_pos(i)
                if any(blob.find(t, ns, ne2) < 0 for t in name_tokens):
                    return False
            return True

        # ── 子串路径:名字 blob 就绪 → 真子串匹配(对标 Everything,名字任意位置含都命中) ──
        # 名字 blob(~155MB)扫子串 ~80-270ms,正确且够快;就绪前回退下面的前缀二分(快但只前缀)。
        use_name_substr = (name_tokens and not path_tokens and self._name_blob_ready)
        # ── 快速路径:name_order 二分定位(纯名字+前缀,O(log n)) ──
        # ★ 仅当第一个词是正常前缀(不以.开头、非单字符)时用二分;否则退化为 blob 扫
        use_name_order = (not use_name_substr
                          and name_tokens and not path_tokens and self._name_order
                          and not name_tokens[0].startswith(b'.')
                          and len(name_tokens[0]) >= 2)
        if use_name_substr:
            # 用名字 blob 扫第一个词(子串),再用 verify_entry 复核其余名字词(也是名字段子串)
            for idx in self._scan_name_blob(name_tokens[0], 200000):
                if not verify_entry(idx):
                    continue
                path = self._path_at(idx)
                if dead and path.lower() in dead:
                    continue
                is_dir = bool(isd[idx])
                if types and _category_of(path, is_dir) not in types:
                    continue
                results.append((is_dir, path))
                if len(results) >= limit:
                    return results
        elif use_name_order:
            no = self._name_order
            nbo = self._name_offsets
            first_token = name_tokens[0]
            # bisect_left: 找首个 name >= token 的条目
            lo, hi = 0, self._count
            while lo < hi:
                mid = (lo + hi) // 2
                idx = no[mid]
                ns = nbo[idx]
                ne = self._offsets[idx + 1] - 1 if idx + 1 < self._count else len(blob)
                # 比较名字字节与 token
                name_len = ne - ns
                cmp_len = min(name_len, len(first_token))
                name_slice = blob[ns:ns + cmp_len]
                if name_slice < first_token:
                    lo = mid + 1
                else:
                    hi = mid
            start_pos = lo
            # 从 start_pos 往后扫描,直到名字不以 token 开头
            for pos in range(start_pos, self._count):
                idx = no[pos]
                ns = nbo[idx]
                ne = self._offsets[idx + 1] - 1 if idx + 1 < self._count else len(blob)
                name_len = ne - ns
                # 检查名字是否以 first_token 开头
                if name_len < len(first_token):
                    if blob[ns:ne] < first_token:
                        continue
                    break  # 名字已大于 token 且不是前缀
                elif blob[ns:ns + len(first_token)] != first_token:
                    if blob[ns:ns + len(first_token)] > first_token:
                        break  # 超出前缀范围
                    continue
                # 前缀匹配,验证全条件
                if not verify_entry(idx):
                    continue
                path = self._path_at(idx)
                if dead and path.lower() in dead:
                    continue
                is_dir = bool(isd[idx])
                if types and _category_of(path, is_dir) not in types:
                    continue
                results.append((is_dir, path))
                if len(results) >= limit:
                    return results
        else:
            # ── 回退:blob 全扫(路径搜索或无 name_order) ──
            seed = name_tokens[0] if name_tokens else path_tokens[0]
            for i in self._scan_blob(seed, 200000):
                if not verify_entry(i):
                    continue
                path = self._path_at(i)
                if dead and path.lower() in dead:
                    continue
                is_dir = bool(isd[i])
                if types and _category_of(path, is_dir) not in types:
                    continue
                results.append((is_dir, path))
                if len(results) >= limit:
                    return results

        for e in self._delta:
            is_dir, path = e[0], e[1]
            pl = path.lower().encode("utf-8")
            if path_tokens and any(t not in pl for t in path_tokens):
                continue
            if name_tokens:
                nl = _basename(path).lower().encode("utf-8")
                if any(t not in nl for t in name_tokens):
                    continue
            if types and _category_of(path, is_dir) not in types:
                continue
            results.append((is_dir, path))
            if len(results) >= limit:
                break
        return results

    def _parse_date(self, val):
        """解析日期值: today/lastweek/2024-01-01/7d/1y → Unix 秒。"""
        import datetime as _dt
        now = __import__('time').time()
        v = val.strip().lower()
        if v in ('today',):       return now - 86400
        if v in ('yesterday',):   return now - 172800
        if v in ('lastweek',):    return now - 604800
        if v in ('lastmonth',):   return now - 2592000
        if v in ('lastyear',):    return now - 31536000
        m = __import__('re').match(r'^(\d+)\s*(s|m|h|d|w|mo|y)$', v)
        if m:
            n2, u = int(m.group(1)), m.group(2)
            mul = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'mo': 2592000, 'y': 31536000}.get(u, 1)
            return now - n2 * mul
        try:  # YYYY-MM-DD
            return _dt.datetime.strptime(v[:10], '%Y-%m-%d').timestamp()
        except ValueError: pass
        return None

    def _search_path_prefix(self, query, limit):
        """用 _path_order 二分做路径前缀搜索。"""
        prefix = query.rstrip('\\').encode("utf-8")  # 去尾部\,blob条目不含末尾\
        n = self._count; po = self._path_order; blob = self._blob; offs = self._offsets
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            idx = po[mid]; es = offs[idx]
            ee = offs[idx + 1] - 1 if idx + 1 < n else len(blob)
            cmp_len = min(ee - es, len(prefix))
            if blob[es:es + cmp_len] < prefix: lo = mid + 1
            else: hi = mid
        start = lo; results = []; isd = self._isdir
        for pos in range(start, n):
            idx = po[pos]; es = offs[idx]
            ee = offs[idx + 1] - 1 if idx + 1 < n else len(blob)
            if ee - es < len(prefix) or blob[es:es + len(prefix)] != prefix: break
            results.append((bool(isd[idx]), self._path_at(idx)))
            if len(results) >= limit: break
        return results

    def _search_advanced_impl(self, cond, limit=1000):
        """高级搜索:cond 为条件字典。AND 组合所有非空条件。

        支持键:name_all/name_any/name_none/name_phrase/name_case/path/regex/regex_case
        folder/include_sub/ext/types/attrs/size_min/size_max/mtime_from/to/ctime_from/to
        atime_from/to/name_len_min/max/depth_min/max。
        """
        import re as _re
        c = cond or {}
        case = bool(c.get("name_case"))

        def low(s):
            return s if case else s.lower()

        name_all = [low(w) for w in c.get("name_all", []) if w]
        name_any = [low(w) for w in c.get("name_any", []) if w]
        name_none = [low(w) for w in c.get("name_none", []) if w]
        name_phrase = low(c.get("name_phrase", "") or "")
        path_words = [w.lower() for w in c.get("path", []) if w]
        rx = None
        if c.get("regex"):
            try:
                rx = _re.compile(c["regex"], 0 if c.get("regex_case") else _re.IGNORECASE)
            except _re.error:
                rx = None
        folder = (c.get("folder") or "").rstrip("\\").lower()
        include_sub = c.get("include_sub", True)
        exts = set(e.lower().lstrip(".") for e in c.get("ext", []) if e)
        types = set(c.get("types", []))
        attrs = list(c.get("attrs", []))
        smin, smax = c.get("size_min"), c.get("size_max")
        tranges = [(c.get(k + "_from"), c.get(k + "_to")) for k in ("mtime", "ctime", "atime")]
        lmin, lmax = c.get("name_len_min"), c.get("name_len_max")
        dmin, dmax = c.get("depth_min"), c.get("depth_max")

        has_num = (smin is not None or smax is not None or attrs
                   or any(lo is not None or hi is not None for lo, hi in tranges))
        has_str = bool(name_all or name_phrase or name_any or name_none or path_words
                       or rx or exts or types or folder
                       or lmin is not None or lmax is not None
                       or dmin is not None or dmax is not None)
        needs_path_str = bool(rx or case            # regex/大小写敏感需要原始名字或路径
                              or dmin is not None or dmax is not None  # 深度需要 path.count('\\')
                              or (folder and not include_sub))         # 精准目录需要 rfind

        # 选 seed(同前)
        seed = None
        if name_all:
            seed = name_all[0]
        elif name_phrase:
            seed = name_phrase.split()[0] if name_phrase else None
        elif name_any:
            seed = name_any[0]
        elif path_words:
            seed = path_words[0]
        elif folder:
            seed = folder

        skip_folder_startswith = bool(folder and include_sub and seed == folder)
        skip_all_word = seed if (seed and name_all and seed == name_all[0]) else None
        skip_phrase = bool(seed and name_phrase and seed == name_phrase.split()[0]) if name_phrase else False
        skip_any_word = seed if (seed and name_any and seed == name_any[0]) else None
        skip_path_word = seed if (seed and path_words and seed == path_words[0]) else None
        has_prefilter = bool(exts or types)

        # ─ str_ok(需要字符串时;regex/case/depth/精准folder 等) ─
        def str_ok(path, is_dir, name):
            nl = low(name)
            if lmin is not None and len(name) < lmin:
                return False
            if lmax is not None and len(name) > lmax:
                return False
            if folder and not skip_folder_startswith:
                pl = path.lower()
                if include_sub:
                    if not pl.startswith(folder):
                        return False
                else:
                    parent = pl.rstrip("\\")
                    cut = parent.rfind("\\")
                    if cut < 0 or parent[:cut] != folder:
                        return False
            if name_all:
                for w in name_all:
                    if w == skip_all_word:
                        continue
                    if w not in nl:
                        return False
            if skip_phrase:
                pass
            elif name_phrase and name_phrase not in nl:
                return False
            if name_any:
                ok = False
                for w in name_any:
                    if w == skip_any_word or w in nl:
                        ok = True
                        break
                if not ok:
                    return False
            if name_none and any(w in nl for w in name_none):
                return False
            if path_words:
                pl2 = path.lower()
                for w in path_words:
                    if w == skip_path_word:
                        continue
                    if w not in pl2:
                        return False
            if rx is not None and not rx.search(name):
                return False
            if dmin is not None or dmax is not None:
                depth = path.count("\\")
                if dmin is not None and depth < dmin:
                    return False
                if dmax is not None and depth > dmax:
                    return False
            return True

        # ─ bytes 版检查(热路径,零拷贝:用 blob.find/rfind 带边界,不切片) ─
        def _wb(w):
            return w.encode("utf-8") if isinstance(w, str) else w

        def check_entry_bytes(i, ns, ne):
            """纯 bytes 检查,零拷贝。ns/ne 为预计算的名字边界。"""
            blob = self._blob
            offs = self._offsets
            # 文件夹前缀:用切片比较(mmap 不支持 startswith;bytes/mmap 切片均可)
            if folder and not skip_folder_startswith:
                fb = folder.encode("utf-8")
                es0 = offs[i]
                if blob[es0:es0 + len(fb)] != fb:
                    return False
            # 名字段:用 blob.find(wb, ns, ne) 不切片
            if name_all:
                for w in name_all:
                    if w == skip_all_word:
                        continue
                    if blob.find(_wb(w), ns, ne) < 0:
                        return False
            if name_phrase:
                if blob.find(_wb(name_phrase), ns, ne) < 0:
                    return False
            if name_any:
                ok2 = False
                for w in name_any:
                    if w == skip_any_word or blob.find(_wb(w), ns, ne) >= 0:
                        ok2 = True
                        break
                if not ok2:
                    return False
            if name_none:
                for w in name_none:
                    if blob.find(_wb(w), ns, ne) >= 0:
                        return False
            # 路径词:用条目边界
            if path_words:
                es = offs[i]
                ee = offs[i + 1] - 1 if i + 1 < len(offs) else len(blob)
                for w in path_words:
                    if w == skip_path_word:
                        continue
                    if blob.find(_wb(w), es, ee) < 0:
                        return False
            if lmin is not None and (ne - ns) < lmin:
                return False
            if lmax is not None and (ne - ns) > lmax:
                return False
            return True

        # ─ 内联检查(热路径:名字边界一次算好,零拷贝) ─
        def process_candidate(i):
            is_dir = bool(self._isdir[i])
            ns, ne = self._name_pos(i)
            blob = self._blob
            # 扩展名/类型预滤(blob.rfind 零拷贝)
            if has_prefilter:
                need_dot = bool(exts) or (bool(types) and not is_dir)
                if need_dot:
                    dot = blob.rfind(b'.', ns, ne)
                    if dot >= 0 and dot + 1 < ne:
                        try:
                            ext = blob[dot + 1:ne].decode("ascii")
                        except UnicodeDecodeError:
                            ext = ""
                    else:
                        ext = ""
                    if exts and ext not in exts:
                        return None
                    if types:
                        cat = "folder" if is_dir else _EXT_TO_CAT.get(ext, "other")
                        if cat not in types:
                            return None
                elif types:
                    if "folder" not in types:
                        return None
            # 字符串条件
            if needs_path_str:
                path = self._path_at(i)
                name = _basename(path)
                if has_str and not str_ok(path, is_dir, name):
                    return None
            elif has_str:
                if not check_entry_bytes(i, ns, ne):
                    return None
                path = None
            else:
                path = None
            if path is None:
                path = self._path_at(i)
            return (is_dir, path)

        # ─ 数值条件 ─
        def num_ok(size, mt, ct, at, attr):
            if smin is not None and size < smin:
                return False
            if smax is not None and size > smax:
                return False
            if attrs and any((attr & bit) == 0 for bit in attrs):
                return False
            for (lo, hi), val in zip(tranges, (mt, ct, at)):
                if lo is not None and val < lo:
                    return False
                if hi is not None and val > hi:
                    return False
            return True

        results = []
        dead = self._dead
        isd = self._isdir
        sz, mt_, ct_, at_, ar_ = self._size, self._mtime, self._ctime, self._atime, self._attr

        # ══════ 快速路径:排序索引(优先)或 blob 粗筛(兜底) ══════
        blob = self._blob; offs = self._offsets; nbo = self._name_offsets
        fast_iter = None  # iterator yielding entry indices in order
        if name_all and self._name_order:
            # name_order: 二分 + 线性扫描前缀匹配
            first = name_all[0].encode("utf-8") if isinstance(name_all[0], str) else name_all[0]
            n2 = self._count; no2 = self._name_order; nbo2 = self._name_offsets
            lo, hi = 0, n2
            while lo < hi:
                mid = (lo + hi) // 2; idx2 = no2[mid]; ns2 = nbo2[idx2]
                ne2 = offs[idx2 + 1] - 1 if idx2 + 1 < n2 else len(blob)
                nl2 = ne2 - ns2; cl2 = min(nl2, len(first))
                if blob[ns2:ns2 + cl2] < first: lo = mid + 1
                else: hi = mid
            pos2 = lo
            def _name_iter():
                nonlocal pos2
                while pos2 < n2:
                    idx3 = no2[pos2]; ns3 = nbo2[idx3]
                    ne3 = offs[idx3 + 1] - 1 if idx3 + 1 < n2 else len(blob)
                    if ne3 - ns3 < len(first) or blob[ns3:ns3 + len(first)] != first:
                        break
                    pos2 += 1; yield idx3
            fast_iter = _name_iter()
        elif folder and exts and self._ext_order:
            # ext_order + folder filter: 遍历扩展名匹配条目,用 folder 前缀筛
            target = next(iter(exts)).encode("utf-8") if isinstance(next(iter(exts)), str) else next(iter(exts))
            folder_prefix = folder.encode("utf-8")
            n2 = self._count; eo2 = self._ext_order
            lo, hi = 0, n2
            while lo < hi:
                mid = (lo + hi) // 2; idx2 = eo2[mid]; ns2 = nbo[idx2] if nbo else offs[idx2]
                ne2 = offs[idx2 + 1] - 1 if idx2 + 1 < n2 else len(blob)
                dot2 = blob.rfind(b'.', ns2, ne2)
                ext_bytes = blob[dot2 + 1:ne2] if dot2 >= 0 and dot2 + 1 < ne2 else b''
                if ext_bytes < target: lo = mid + 1
                else: hi = mid
            pos2 = lo
            def _ext_folder_iter():
                nonlocal pos2; _cnt = 0
                while pos2 < n2:
                    idx3 = eo2[pos2]; ns3 = nbo[idx3] if nbo else offs[idx3]
                    ne3 = offs[idx3 + 1] - 1 if idx3 + 1 < n2 else len(blob)
                    dot3 = blob.rfind(b'.', ns3, ne3)
                    ext3 = blob[dot3 + 1:ne3] if dot3 >= 0 and dot3 + 1 < ne3 else b''
                    if ext3 != target: break
                    pos2 += 1
                    es3 = offs[idx3]
                    if blob[es3:es3 + len(folder_prefix)] == folder_prefix:
                        yield idx3
            fast_cands = _ext_folder_iter()
        elif folder and self._path_order:
            # path_order: 二分 + 线性扫描前缀匹配
            prefix = folder.encode("utf-8")
            n2 = self._count; po2 = self._path_order
            lo, hi = 0, n2
            while lo < hi:
                mid = (lo + hi) // 2; idx2 = po2[mid]; es2 = offs[idx2]
                ee2 = offs[idx2 + 1] - 1 if idx2 + 1 < n2 else len(blob)
                cl2 = min(ee2 - es2, len(prefix))
                if blob[es2:es2 + cl2] < prefix: lo = mid + 1
                else: hi = mid
            pos2 = lo
            def _path_iter():
                nonlocal pos2
                while pos2 < n2:
                    idx3 = po2[pos2]; es3 = offs[idx3]
                    ee3 = offs[idx3 + 1] - 1 if idx3 + 1 < n2 else len(blob)
                    if ee3 - es3 < len(prefix) or blob[es3:es3 + len(prefix)] != prefix:
                        break
                    pos2 += 1; yield idx3
            fast_iter = _path_iter()
        elif exts and not (name_all or name_any or name_phrase or path_words or folder) and self._ext_order:
            # ext_order(仅当无其他文本条件时)
            target = next(iter(exts)).encode("utf-8") if isinstance(next(iter(exts)), str) else next(iter(exts))
            n2 = self._count; eo2 = self._ext_order
            lo, hi = 0, n2
            while lo < hi:
                mid = (lo + hi) // 2; idx2 = eo2[mid]; ns2 = self._name_offsets[idx2] if self._name_offsets else offs[idx2]
                ne2 = offs[idx2 + 1] - 1 if idx2 + 1 < n2 else len(blob)
                dot2 = blob.rfind(b'.', ns2 if nbo else offs[idx2], ne2)
                ext_bytes = blob[dot2 + 1:ne2] if dot2 >= 0 and dot2 + 1 < ne2 else b''
                if ext_bytes < target: lo = mid + 1
                else: hi = mid
            pos2 = lo
            def _ext_iter():
                nonlocal pos2
                while pos2 < n2:
                    idx3 = eo2[pos2]; ns3 = nbo[idx3] if nbo else offs[idx3]
                    ne3 = offs[idx3 + 1] - 1 if idx3 + 1 < n2 else len(blob)
                    dot3 = blob.rfind(b'.', ns3, ne3)
                    ext3 = blob[dot3 + 1:ne3] if dot3 >= 0 and dot3 + 1 < ne3 else b''
                    if ext3 != target: break
                    pos2 += 1; yield idx3
            fast_iter = _ext_iter()
        elif seed is not None:
            # 兜底: blob 全扫
            seed_b = seed.encode("utf-8") if isinstance(seed, str) else seed
            fast_iter = self._scan_blob(seed_b, 200000)

        if fast_iter is not None:
            for i in fast_iter:
                if has_num and not num_ok(sz[i], mt_[i], ct_[i], at_[i], ar_[i]):
                    continue
                if dead:
                    path = self._path_at(i)
                    if path.lower() in dead:
                        continue
                hit = process_candidate(i)
                if hit is None:
                    continue
                if dead and hit[1].lower() in dead:
                    continue
                results.append(hit)
                if len(results) >= limit:
                    return results
            # 增量层
            for e in self._delta:
                is_dir, dpath = e[0], e[1]
                if has_num and not num_ok(e[2], e[3], e[4], e[5], e[6]):
                    continue
                if has_str:
                    dname = _basename(dpath)
                    if exts:
                        idx_dot = dname.rfind(".")
                        if idx_dot <= 0 or dname[idx_dot + 1:].lower() not in exts:
                            continue
                    if types and _category_of(dpath, is_dir) not in types:
                        continue
                    if not str_ok(dpath, is_dir, dname):
                        continue
                results.append((is_dir, dpath))
                if len(results) >= limit:
                    break
            return results

        # ══════ 无种子词(排序索引或全表扫) ══════
        n = self._count
        if self._size_order and (smin is not None or smax is not None):
            s_arr = sz
            so = self._size_order
            # bisect_left
            if smin is not None:
                lo, hi = 0, n
                while lo < hi:
                    mid = (lo + hi) // 2
                    if s_arr[so[mid]] < smin:
                        lo = mid + 1
                    else:
                        hi = mid
                start_pos = lo
            else:
                start_pos = 0
            # bisect_right
            if smax is not None:
                lo, hi = start_pos, n
                while lo < hi:
                    mid = (lo + hi) // 2
                    if s_arr[so[mid]] <= smax:
                        lo = mid + 1
                    else:
                        hi = mid
                end_pos = lo
            else:
                end_pos = n
            range_len = end_pos - start_pos
            no_other = (not attrs
                        and not any(lo is not None or hi is not None for lo, hi in tranges)
                        and not has_str)
            if no_other:
                wanted = min(limit, range_len)
                if not dead:
                    for pos in range(end_pos - 1, end_pos - 1 - wanted, -1):
                        i = so[pos]
                        results.append((bool(isd[i]), self._path_at(i)))
                else:
                    for pos in range(end_pos - 1, start_pos - 1, -1):
                        i = so[pos]
                        path = self._path_at(i)
                        if path.lower() in dead:
                            continue
                        results.append((bool(isd[i]), path))
                        if len(results) >= limit:
                            break
            else:
                for pos in range(start_pos, end_pos):
                    i = so[pos]
                    if attrs and any((ar_[i] & bit) == 0 for bit in attrs):
                        continue
                    ok_time = True
                    for (lo, hi), val in zip(tranges, (mt_[i], ct_[i], at_[i])):
                        if lo is not None and val < lo:
                            ok_time = False; break
                        if hi is not None and val > hi:
                            ok_time = False; break
                    if not ok_time:
                        continue
                    if dead:
                        path = self._path_at(i)
                        if path.lower() in dead:
                            continue
                    hit = process_candidate(i)
                    if hit is None:
                        continue
                    if dead and hit[1].lower() in dead:
                        continue
                    results.append(hit)
                    if len(results) >= limit:
                        return results
        else:
            # 全表扫(旧路径兜底)
            for i in range(n):
                if has_num and not num_ok(sz[i], mt_[i], ct_[i], at_[i], ar_[i]):
                    continue
                if dead:
                    path = self._path_at(i)
                    if path.lower() in dead:
                        continue
                hit = process_candidate(i)
                if hit is None:
                    continue
                if dead and hit[1].lower() in dead:
                    continue
                results.append(hit)
                if len(results) >= limit:
                    return results
        # 增量层
        for e in self._delta:
            if has_num and not num_ok(e[2], e[3], e[4], e[5], e[6]):
                continue
            if has_str and not str_ok(e[1], e[0], _basename(e[1])):
                continue
            results.append((e[0], e[1]))
            if len(results) >= limit:
                break
        return results

    # ── 存档(v05: array 直写 + 独立 blob 文件供 mmap) ──────────

    def save(self, path):
        """存档到 v06:头/定长数组写主文件,两个 blob 写独立边车文件(load 时 mmap 按需分页)。

        主文件(path)布局:
          MAGIC(8) + count(<I) + isdir(count) + size(8n) + mtime(8n) + ctime(8n)
          + atime(8n) + attr(4n) + size_order(4n) + blob_offsets(4n) + path_offsets(4n)
          + opt_count(<I=5) + name_offsets(4n) + name_order(4n) + path_order(4n)
          + ext_order(4n) + mtime_order(4n)
        边车:path+".pblob"=原始大小写路径 blob;path+".lblob"=小写搜索 blob。

        若本会话从 v06 载入且未变更(_blob_on_disk),三文件都是现成的,直接返回(开窗搜一下
        就关的常见路径,免去 ~0.35GB 头的无谓重写)。否则三文件各自 .tmp→os.replace 原子落盘。
        此时 _blob/_path_blob 必为新建 bytes 或"映射别的存档"的 mmap —— 都不与本 path 的边车冲突
        (映射本 path 边车的唯一情形就是上面的 _blob_on_disk,已提前返回)。
        """
        if (self._blob_on_disk and self._archive
                and os.path.abspath(path) == os.path.abspath(self._archive)):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        n = self._count
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(_MAGIC)
            f.write(struct.pack("<I", n))
            f.write(bytes(self._isdir))
            f.write(self._size.tobytes())
            f.write(self._mtime.tobytes())
            f.write(self._ctime.tobytes())
            f.write(self._atime.tobytes())
            f.write(self._attr.tobytes())
            # ★ 不再保存 size_order(已在 _rebuild_blob 禁用)
            # f.write(self._size_order.tobytes())  # 注释掉
            f.write(self._offsets.tobytes())        # blob offsets
            f.write(self._path_offsets.tobytes())   # path offsets
            # ★ 不再保存可选 order 索引(name/path/ext/mtime_order 已禁用)
            # f.write(struct.pack("<I", 5))         # opt_count
            # f.write(self._name_offsets.tobytes())
            # f.write(self._name_order.tobytes())
            # f.write(self._path_order.tobytes())
            # f.write(self._ext_order.tobytes())
            # f.write(self._mtime_order.tobytes())
            # 但保留 name_offsets(名字起点数组,搜索需要)
            f.write(self._name_offsets.tobytes())
        os.replace(tmp, path)
        # 两个 blob 各写独立边车(原子落盘)。f.write 对 bytes / mmap 均可。
        ptmp = path + ".pblob.tmp"
        with open(ptmp, "wb") as f:
            f.write(self._path_blob)
        os.replace(ptmp, path + ".pblob")
        ltmp = path + ".lblob.tmp"
        with open(ltmp, "wb") as f:
            f.write(self._blob)
        os.replace(ltmp, path + ".lblob")

    def load(self, path):
        """从存档载入。先窥魔数:v06 走 mmap 路径(省内存);v05/v04/v03/v02 走旧 f.read 路径。

        成功 True;魔数不符 / 文件缺失 → False(触发重扫)。
        """
        self._close_mmaps()           # 复用同一实例再次 load 时,先释放旧映射
        try:
            with open(path, "rb") as f:
                head = f.read(8)
        except OSError:
            return False
        if head == _MAGIC:            # v06
            return self._load_v06(path)
        return self._load_legacy(path)

    def _load_v06(self, path):
        """v06:头/定长数组用 array.fromfile 直读(峰值仅 ~0.35GB);两个 blob 用 mmap 按需分页。

        失败(截断/缺边车/数组对不上)→ False,触发上层重扫。count=0 时边车为空文件,
        mmap 空文件会报错,故用 b"" 兜底。
        """
        try:
            f = open(path, "rb")
        except OSError:
            return False
        try:
            if f.read(8) != _MAGIC:
                return False
            count = struct.unpack("<I", f.read(4))[0]

            def _arr(typecode, nitems):
                a = array.array(typecode)
                a.fromfile(f, nitems)     # 不足 nitems 抛 EOFError → 被下面 except 捕获
                return a

            self._isdir = bytearray(f.read(count))
            if len(self._isdir) != count:
                return False
            self._size = _arr("Q", count)
            self._mtime = _arr("d", count)
            self._ctime = _arr("d", count)
            self._atime = _arr("d", count)
            self._attr = _arr("I", count)
            # ★ 不再读 size_order(新格式已去掉)
            # self._size_order = _arr("I", count)
            self._offsets = _arr("I", count)
            self._path_offsets = _arr("I", count)
            # ★ 不再读可选 order 索引(新格式已去掉)
            # opt_count = struct.unpack("<I", f.read(4))[0]
            # opt = [None] * 5
            # for k in range(min(opt_count, 5)):
            #     opt[k] = _arr("I", count)
            # self._name_order = opt[1] if opt[1] is not None else array.array('I')
            # self._path_order = opt[2] if opt[2] is not None else array.array('I')
            # self._ext_order = opt[3] if opt[3] is not None else array.array('I')
            # self._mtime_order = opt[4] if opt[4] is not None else array.array('I')
            # 但保留 name_offsets(名字起点数组,搜索需要)
            self._name_offsets = _arr("I", count)
        except (EOFError, ValueError, struct.error):
            f.close()
            return False
        finally:
            # 数组已全部读入内存,头文件句柄不再需要(边车用各自句柄)
            try:
                f.close()
            except OSError:
                pass

        # 两个 blob:mmap 映射独立边车文件。count=0 → 空文件,用 b"" 兜底。
        if count == 0:
            self._path_blob = b""
            self._blob = b""
        else:
            try:
                self._pblob_f = open(path + ".pblob", "rb")
                self._pblob_mm = mmap.mmap(self._pblob_f.fileno(), 0, access=mmap.ACCESS_READ)
                self._path_blob = self._pblob_mm
                self._lblob_f = open(path + ".lblob", "rb")
                self._lblob_mm = mmap.mmap(self._lblob_f.fileno(), 0, access=mmap.ACCESS_READ)
                self._blob = self._lblob_mm
            except (OSError, ValueError):
                self._close_mmaps()
                return False

        self._count = count
        self._delta = []
        self._delta_low = []
        self._dead = set()
        self._archive = path
        self._blob_on_disk = True     # 三文件齐备且未变更;save 可跳过重写
        if len(self._path_offsets) != count:
            self._close_mmaps()
            return False
        return True

    def _load_legacy(self, path):
        """旧档(v05/v04/v03/v02)路径:f.read 全量读入,读完转 array/bytes。

        载入即把 blob 读进内存(非 mmap),_blob_on_disk 保持 False,故下次 save 会落成 v06 三文件
        (一次性升级,无需重扫)。
        """
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return False
        ver = data[:8]
        is_v05 = (ver == _MAGIC_V05)
        is_v04 = (ver == _MAGIC_V04)
        is_v03 = (ver == _MAGIC_V03)
        is_v02 = (ver == _MAGIC_V02)
        if not (is_v05 or is_v04 or is_v03 or is_v02):
            return False
        count = struct.unpack_from("<I", data, 8)[0]
        off = 12
        try:
            self._isdir = bytearray(data[off:off + count]); off += count
            sz = array.array("Q"); sz.frombytes(data[off:off + 8 * count]); off += 8 * count
            mt = array.array("d"); mt.frombytes(data[off:off + 8 * count]); off += 8 * count
            ct = array.array("d"); ct.frombytes(data[off:off + 8 * count]); off += 8 * count
            at = array.array("d"); at.frombytes(data[off:off + 8 * count]); off += 8 * count
            ar = array.array("I"); ar.frombytes(data[off:off + 4 * count]); off += 4 * count
            # size_order(v03+)
            if is_v03 or is_v04 or is_v05:
                so = array.array("I")
                so.frombytes(data[off:off + 4 * count]); off += 4 * count
                self._size_order = so
            else:
                self._size_order = array.array('I')
            # blob_offsets + path_offsets + path_blob(v05);v04 有 blob+offsets,无 path_offsets
            if is_v05:
                boff = array.array("I")
                boff.frombytes(data[off:off + 4 * count]); off += 4 * count
                self._offsets = boff
                poff = array.array("I")
                poff.frombytes(data[off:off + 4 * count]); off += 4 * count
                self._path_offsets = poff
                plen = struct.unpack_from("<Q", data, off)[0]; off += 8
                self._path_blob = data[off:off + plen]; off += plen
                # 可选索引(v05 逐步扩展,用 opt_count 区分)
                opt_count = 0
                if len(data) - off >= 4:
                    opt_count = struct.unpack_from("<I", data, off)[0]; off += 4
                orders = []
                for _ in range(min(opt_count, 5)):
                    if len(data) - off >= 4 * count:
                        try:
                            arr = array.array("I")
                            arr.frombytes(data[off:off + 4 * count]); off += 4 * count
                            orders.append(arr)
                        except (ValueError, IndexError):
                            break
                    else:
                        break
                if orders: self._name_offsets = orders[0]
                if len(orders) > 1: self._name_order = orders[1]
                if len(orders) > 2: self._path_order = orders[2]
                if len(orders) > 3: self._ext_order = orders[3]
                if len(orders) > 4: self._mtime_order = orders[4]
            elif is_v04:
                blob_len = struct.unpack_from("<Q", data, off)[0]; off += 8
                self._blob = data[off:off + blob_len]; off += blob_len
                boff = array.array("I")
                boff.frombytes(data[off:off + 4 * count]); off += 4 * count
                self._offsets = boff
        except (ValueError, IndexError, struct.error):
            return False

        # 元数据(v05 保持 array;旧版复制到 array)
        if is_v05:
            self._size, self._mtime, self._ctime = sz, mt, ct
            self._atime, self._attr = at, ar
        else:
            self._size = array.array('Q', sz)
            self._mtime = array.array('d', mt)
            self._ctime = array.array('d', ct)
            self._atime = array.array('d', at)
            self._attr = array.array('I', ar)

        self._count = count
        self._delta = []
        self._delta_low = []
        self._dead = set()

        # ─ 路径处理 ─
        if is_v05:
            pass                      # path_blob + path_offsets 已就绪
        elif is_v04:
            text = data[off:].decode("utf-8")
            self._build_path_blob_from_text(text)
        elif is_v03 or is_v02:
            text = data[off:].decode("utf-8")
            self._build_path_blob_from_text(text)
            if not self._offsets:
                self._rebuild_blob()

        # ─ 搜索 blob:放最后,剩余全部就是 ─
        if is_v05:
            self._blob = data[off:]
        # is_v04: _blob 已从上面 data[.] 切片读取
        # else: _rebuild_blob 已构建

        if len(self._path_offsets) != count:
            return False
        return True

    def _build_path_blob_from_text(self, text):
        """从路径文本(\n 分隔)构建 path_blob + path_offsets(旧版升级)。"""
        paths = text.split("\n") if text else []
        n = len(paths)
        self._path_offsets = array.array('I', [0]) * n
        chunks = []
        pos = 0
        for i, p in enumerate(paths):
            self._path_offsets[i] = pos
            pb = p.encode("utf-8")
            chunks.append(pb)
            pos += len(pb) + 1
        self._path_blob = b"\n".join(chunks)
