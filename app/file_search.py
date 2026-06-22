# -*- coding: utf-8 -*-
"""文件搜索引擎:直读 NTFS MFT 全量枚举文件/目录,重建完整路径。

对标 Everything:用 FSCTL_ENUM_USN_DATA 一次性枚举整卷的所有文件与目录(比逐目录
遍历快得多),每条记录给出 文件引用号 / 父目录引用号 / 名字,据此重建完整路径。
需管理员权限打开裸卷句柄 `\\.\C:`,仅对 NTFS 卷有效。见 docs/adr/0004。

纯 ctypes 调 Win32,延续项目"零编译依赖"风格。搜索/存档在 index.py,本模块只管"扫"。
"""
import ctypes
import struct
from ctypes import wintypes

kernel32 = ctypes.windll.kernel32

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

# CTL_CODE 宏:FSCTL 控制码由 设备类型/功能号/方法/访问 组合而成
FILE_DEVICE_FILE_SYSTEM = 0x00000009
METHOD_NEITHER = 3
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0


def _ctl_code(device, function, method, access):
    return (device << 16) | (access << 14) | (function << 2) | method


# FSCTL_ENUM_USN_DATA = CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 44, METHOD_NEITHER, FILE_ANY_ACCESS)
FSCTL_ENUM_USN_DATA = _ctl_code(FILE_DEVICE_FILE_SYSTEM, 44, METHOD_NEITHER, FILE_ANY_ACCESS)
# FSCTL_QUERY_USN_JOURNAL = CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 61, METHOD_BUFFERED, FILE_ANY_ACCESS)
FSCTL_QUERY_USN_JOURNAL = _ctl_code(FILE_DEVICE_FILE_SYSTEM, 61, METHOD_BUFFERED, FILE_ANY_ACCESS)
# FSCTL_READ_USN_JOURNAL = CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 46, METHOD_NEITHER, FILE_ANY_ACCESS)
FSCTL_READ_USN_JOURNAL = _ctl_code(FILE_DEVICE_FILE_SYSTEM, 46, METHOD_NEITHER, FILE_ANY_ACCESS)

# USN 变更原因标志(只列我们关心的:影响"名字/路径"的增删改名)
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                      ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
kernel32.SetFilePointerEx.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.ReadFile.restype = wintypes.BOOL

# FSCTL_GET_NTFS_VOLUME_DATA = CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 25, METHOD_BUFFERED, FILE_ANY_ACCESS)
FSCTL_GET_NTFS_VOLUME_DATA = _ctl_code(FILE_DEVICE_FILE_SYSTEM, 25, METHOD_BUFFERED, FILE_ANY_ACCESS)


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [("StartFileReferenceNumber", ctypes.c_ulonglong),
                ("LowUsn", ctypes.c_longlong),
                ("HighUsn", ctypes.c_longlong)]


FILE_ATTRIBUTE_DIRECTORY = 0x10
ERROR_HANDLE_EOF = 38

# USN_RECORD_V2 固定头偏移(变长文件名在尾部,用 struct 手解)
_REC_HDR = struct.Struct("<I2H2Q2qIIIIHH")  # RecordLength..FileNameOffset(固定头 60 字节)


def _open_volume(letter: str):
    """打开裸卷句柄 \\.\C: 。需管理员;失败返回 None。"""
    path = f"\\\\.\\{letter}:"
    h = kernel32.CreateFileW(path, GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        return None
    return h


_DRIVE_FIXED = 3                  # GetDriveTypeW 返回值:本地固定磁盘


def fixed_ntfs_drives():
    """枚举所有"固定磁盘 + NTFS"的盘符,返回大写字母列表(如 ['C', 'D'])。

    只索引固定 NTFS 盘:MFT/USN 仅 NTFS 有;可移动/网络/光驱不纳入(拔插/离线会让索引失真)。
    供 helper 决定索引哪些卷(对标 Everything 默认索引所有本地 NTFS 卷)。
    """
    out = []
    mask = kernel32.GetLogicalDrives()
    fsbuf = ctypes.create_unicode_buffer(32)
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        letter = chr(ord('A') + i)
        root = letter + ":\\"
        if kernel32.GetDriveTypeW(root) != _DRIVE_FIXED:
            continue
        fsbuf.value = ""
        try:
            kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fsbuf, 32)
        except OSError:
            continue
        if fsbuf.value.upper() == "NTFS":
            out.append(letter)
    return out



def _enum_records(handle):
    """生成器:逐条产出 (frn, parent_frn, is_dir, name)。走 FSCTL_ENUM_USN_DATA 遍历整卷 MFT。"""
    med = MFT_ENUM_DATA_V0(0, 0, 0x7fffffffffffffff)
    buf = ctypes.create_string_buffer(1 << 16)   # 64KB,吞吐够
    bytes_ret = wintypes.DWORD(0)
    while True:
        ok = kernel32.DeviceIoControl(
            handle, FSCTL_ENUM_USN_DATA, ctypes.byref(med), ctypes.sizeof(med),
            buf, ctypes.sizeof(buf), ctypes.byref(bytes_ret), None)
        if not ok:
            break                                  # ERROR_HANDLE_EOF=遍历完
        n = bytes_ret.value
        if n <= 8:
            break
        data = buf.raw[:n]
        med.StartFileReferenceNumber = struct.unpack_from("<Q", data, 0)[0]  # 下一轮起点
        off = 8
        while off < n:
            (rec_len, _maj, _min, frn, parent, _usn, _ts,
             _reason, _src, _sid, attrs, name_len, name_off) = _REC_HDR.unpack_from(data, off)
            if rec_len == 0:
                break
            name = data[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
            yield frn, parent, bool(attrs & FILE_ATTRIBUTE_DIRECTORY), name
            off += rec_len


# ── MFT 全解析:取 大小 + 创建/修改/访问时间 + 属性(用公开 NTFS 结构 + Win32,自实现)─────

class _NTFS_VOLUME_DATA(ctypes.Structure):
    # FSCTL_GET_NTFS_VOLUME_DATA 返回的前若干字段(只用到这几个)
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong),
                ("NumberSectors", ctypes.c_longlong),
                ("TotalClusters", ctypes.c_longlong),
                ("FreeClusters", ctypes.c_longlong),
                ("TotalReserved", ctypes.c_longlong),
                ("BytesPerSector", ctypes.c_ulong),
                ("BytesPerCluster", ctypes.c_ulong),
                ("BytesPerFileRecordSegment", ctypes.c_ulong),
                ("ClustersPerFileRecordSegment", ctypes.c_ulong),
                ("MftValidDataLength", ctypes.c_longlong),
                ("MftStartLcn", ctypes.c_longlong),
                ("Mft2StartLcn", ctypes.c_longlong),
                ("MftZoneStart", ctypes.c_longlong),
                ("MftZoneEnd", ctypes.c_longlong)]

# Windows FILETIME(1601 起的 100ns)→ Unix 秒。0 表示无时间。
_FT_EPOCH_DIFF = 11644473600  # 1601→1970 秒差


def _filetime_to_unix(ft):
    return (ft / 10_000_000.0 - _FT_EPOCH_DIFF) if ft else 0.0


def _apply_fixup(rec, bps):
    """应用 NTFS 更新序列校正(USA/USN):每扇区末 2 字节被替换过,需用 USA 数组还原,
    否则跨扇区读到的记录有 2 字节错位。rec 为可变 bytearray。"""
    usa_off = struct.unpack_from("<H", rec, 4)[0]
    usa_cnt = struct.unpack_from("<H", rec, 6)[0]
    if usa_cnt == 0:
        return
    # USA[0]=校验值(每扇区末2字节本应等于它),USA[1..]=各扇区原始末2字节
    for i in range(1, usa_cnt):
        sec_end = i * bps - 2
        if sec_end + 2 > len(rec):
            break
        rec[sec_end:sec_end + 2] = rec[usa_off + i * 2: usa_off + i * 2 + 2]


def _parse_data_runs(rec, run_off):
    """解析非常驻属性的 data run 列表,返回 [(lcn, clusters), ...](lcn 已累加偏移)。"""
    runs = []
    p = run_off
    cur_lcn = 0
    n = len(rec)
    while p < n:
        header = rec[p]
        if header == 0:
            break
        len_sz = header & 0x0F
        off_sz = (header >> 4) & 0x0F
        p += 1
        if len_sz == 0 or p + len_sz + off_sz > n:
            break
        length = int.from_bytes(rec[p:p + len_sz], "little")
        p += len_sz
        off_bytes = rec[p:p + off_sz]
        p += off_sz
        offset = int.from_bytes(off_bytes, "little", signed=True) if off_sz else 0
        cur_lcn += offset
        runs.append((cur_lcn, length))
    return runs


# PLACEHOLDER_MFT


def _iter_mft_records(handle, vol):
    """流式生成器:逐条产出 (idx, record_bytes)。MFT 可能碎片化,先解析 record 0 的 $DATA(0x80)
    data run,再按 run 顺序**分段读、读一段就吐该段内的记录、随即丢弃**,绝不一次性持有整个
    5.8GB MFT(否则 5.4M 文件会吃到 7GB+ 卡死)。idx = 记录在 MFT 中的全局序号。"""
    bpc = vol.BytesPerCluster
    frs = vol.BytesPerFileRecordSegment
    pos = ctypes.c_longlong(0)
    if not kernel32.SetFilePointerEx(handle, vol.MftStartLcn * bpc, ctypes.byref(pos), 0):
        return
    rd = wintypes.DWORD(0)
    b = ctypes.create_string_buffer(frs)
    if not kernel32.ReadFile(handle, b, frs, ctypes.byref(rd), None) or rd.value < frs:
        return
    rec0 = bytearray(b.raw[:frs])
    if rec0[:4] != b"FILE":
        return
    _apply_fixup(rec0, vol.BytesPerSector)
    # 找 record 0 的非常驻 $DATA(0x80),解析其 data run
    runs = None
    attr_off = struct.unpack_from("<H", rec0, 20)[0]
    p = attr_off
    while p + 8 < frs:
        atype = struct.unpack_from("<I", rec0, p)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", rec0, p + 4)[0]
        if alen == 0:
            break
        non_resident = rec0[p + 8]
        if atype == 0x80 and non_resident:
            run_off = struct.unpack_from("<H", rec0, p + 32)[0]
            runs = _parse_data_runs(rec0, p + run_off)
            break
        p += alen
    if not runs:
        return
    # 分段读:缓冲取 frs 的整数倍,保证记录不跨缓冲边界被切断
    buf_cap = (32 * 1024 * 1024 // frs) * frs
    rbuf = ctypes.create_string_buffer(buf_cap)
    global_idx = 0
    for lcn, clusters in runs:
        remaining = clusters * bpc
        if not kernel32.SetFilePointerEx(handle, lcn * bpc, ctypes.byref(pos), 0):
            return
        carry = b""        # 上一段尾部不足一条记录的残余,拼到下段头部
        while remaining > 0:
            want = min(remaining, buf_cap)
            got = wintypes.DWORD(0)
            if not kernel32.ReadFile(handle, rbuf, want, ctypes.byref(got), None) or got.value == 0:
                break
            chunk = carry + rbuf.raw[:got.value]
            nrec = len(chunk) // frs
            for j in range(nrec):
                yield global_idx, chunk[j * frs:(j + 1) * frs]
                global_idx += 1
            carry = chunk[nrec * frs:]      # 残余留到下次(理论上 run 对齐,通常为空)
            remaining -= got.value
        # run 之间记录号连续(NTFS MFT 记录在虚拟空间连续),carry 跨 run 极少见,简化丢弃


def _parse_file_record(rec, frs, bps):
    """解析一条 FILE 记录,返回**紧凑元组**
    (is_dir, parent, name, size, mtime, ctime, atime, dos_attr, links) 或 None(空闲/非记录)。
    用元组而非 dict:5.4M 条 dict 约 2.7GB,元组省一大截内存。

    名字优先取 Win32 命名空间(忽略 DOS 8.3 短名)。一条记录可有多个 $FILE_NAME:
    DOS 短名(命名空间2)是长名的 8.3 别名,跳过;Win32(1)/Win32+DOS(3) 各是一条真实链接
    —— **硬链接**(如 conda 把 pkgs\\ 的包文件硬链进各 env)会产生多个不同父目录的 Win32 名。
    parent/name 为"主名"(命名空间优先级最高,给 FRN 图做路径重建);links 为**全部** Win32 链接的
    [(parent, name), ...](含主名),供枚举时为每个链接各产出一条索引项(否则非主名的硬链接会丢)。
    """
    if rec[:4] != b"FILE":
        return None
    flags = struct.unpack_from("<H", rec, 22)[0]
    if not (flags & 0x0001):          # bit0=记录在用;空闲记录跳过
        return None
    is_dir = bool(flags & 0x0002)     # bit1=目录
    mrec = bytearray(rec)
    _apply_fixup(mrec, bps)
    parent = None; name = None; size = 0
    ctime = mtime = atime = 0.0; dos_attr = 0
    links = []                        # 全部 Win32 命名空间链接 (parent, name);硬链接会有多条
    attr_off = struct.unpack_from("<H", mrec, 20)[0]
    p = attr_off
    best_ns = -1                      # 名字命名空间优先级:Win32(1)/Win32+DOS(3) 优于 DOS(2)/POSIX(0)
    while p + 8 < frs:
        atype = struct.unpack_from("<I", mrec, p)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", mrec, p + 4)[0]
        if alen == 0 or p + alen > frs:
            break
        non_resident = mrec[p + 8]
        if atype == 0x10 and not non_resident:        # $STANDARD_INFORMATION(常驻)
            co = struct.unpack_from("<H", mrec, p + 20)[0]
            ct, mt, _r, at = struct.unpack_from("<QQQQ", mrec, p + co)
            ctime = _filetime_to_unix(ct)
            mtime = _filetime_to_unix(mt)
            atime = _filetime_to_unix(at)
            dos_attr = struct.unpack_from("<I", mrec, p + co + 32)[0]
        elif atype == 0x30 and not non_resident:      # $FILE_NAME(常驻):父引用 + 名字
            co = struct.unpack_from("<H", mrec, p + 20)[0]
            parent_ref = struct.unpack_from("<Q", mrec, p + co)[0]
            name_len = mrec[p + co + 64]
            namespace = mrec[p + co + 65]
            nm = mrec[p + co + 66: p + co + 66 + name_len * 2].decode("utf-16-le", "replace")
            ns_rank = {0: 0, 1: 2, 2: 1, 3: 3}.get(namespace, 0)  # Win32/Win32+DOS 优先
            par = parent_ref & _MASK
            # 收集真实长名链接:Win32(1)/Win32+DOS(3)/POSIX(0)。**只跳 DOS 8.3 短名(2)**。
            # ★ 硬链接的第二个名字常落在 POSIX(0) 命名空间(实测 conda 把 pkgs\ 的包硬链进 env\
            # 就是 ns=0)—— 早先漏收 ns=0 导致这些硬链接全丢。按 (parent,name) 去重防同链接重复计。
            if namespace in (0, 1, 3):
                links.append((par, nm))
            if ns_rank > best_ns:
                best_ns = ns_rank
                parent = par
                name = nm
        elif atype == 0x80:                            # $DATA:文件大小
            name_len_attr = mrec[p + 9]                 # 只认匿名流(名字长度 0)
            if name_len_attr == 0:
                if non_resident:
                    size = struct.unpack_from("<Q", mrec, p + 48)[0]  # RealSize
                else:
                    size = struct.unpack_from("<I", mrec, p + 16)[0]  # ValueLength
        p += alen
    if name is None:
        return None
    # 去重(理论上不同链接 (parent,name) 各异;保险起见去掉完全相同项)
    if len(links) > 1:
        links = list(dict.fromkeys(links))
    return (is_dir, parent, name, size, mtime, ctime, atime, dos_attr, links)


# 元组字段下标(_parse_file_record 的返回):便于阅读
_NF_ISDIR, _NF_PARENT, _NF_NAME, _NF_SIZE, _NF_MTIME, _NF_CTIME, _NF_ATIME, _NF_ATTR, _NF_LINKS = range(9)


def enumerate_volume_full(letter):
    """全解析卷,返回 {idx: 元组}(元组见 _parse_file_record)。需管理员;失败返回 None。

    用流式 _iter_mft_records 逐条解析,不一次性持有整个 MFT。idx = 记录在 MFT 的全局序号,
    与 $FILE_NAME 的 parent 引用低 48 位对齐。
    """
    handle = _open_volume(letter)
    if handle is None:
        return None
    try:
        vol = _NTFS_VOLUME_DATA()
        ret = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(handle, FSCTL_GET_NTFS_VOLUME_DATA, None, 0,
                                        ctypes.byref(vol), ctypes.sizeof(vol),
                                        ctypes.byref(ret), None):
            return None
        frs = vol.BytesPerFileRecordSegment
        bps = vol.BytesPerSector
        nodes = {}
        for idx, rec in _iter_mft_records(handle, vol):
            info = _parse_file_record(rec, frs, bps)
            if info is not None:
                nodes[idx] = info
        return nodes
    finally:
        kernel32.CloseHandle(handle)


def entries_and_graph_full(letter):
    """一次全解析,同时产出 (带元数据条目列表, FRN 图)。

    条目列表:[(is_dir, path, size, mtime, ctime, atime, attr), ...](给 FileIndex 建带元数据索引);
    FRN 图:{idx: (parent, is_dir, name)}(给 USN 增量 apply_changes 用,与 build_graph 同形)。
    失败返回 (None, None)。避免"全扫拿元数据"和"建图"各跑一遍,省一次 MFT 解析。
    节点为元组,字段下标见 _NF_*。
    """
    nodes = enumerate_volume_full(letter)
    if nodes is None:
        return None, None
    root = f"{letter}:"
    dir_path = {_ROOT_IDX: root}

    def path_of(idx, _seen=None):
        if idx in dir_path:
            return dir_path[idx]
        nd = nodes.get(idx)
        if nd is None:
            return root
        if _seen is None:
            _seen = set()
        if idx in _seen:
            return root
        _seen.add(idx)
        par = nd[_NF_PARENT]
        pp = path_of(par, _seen) if (par in nodes and par != idx) else root
        full = pp + "\\" + nd[_NF_NAME]
        dir_path[idx] = full
        return full

    entries = []
    graph = {}
    for idx, nd in nodes.items():
        graph[idx] = (nd[_NF_PARENT], nd[_NF_ISDIR], nd[_NF_NAME])   # 给 apply_changes 用
        if idx == _ROOT_IDX or nd[_NF_NAME] in (".", ""):
            continue
        if nd[_NF_ISDIR]:
            # 目录:NTFS 不允许目录硬链接,单名;路径递归重建
            full = path_of(idx)
            entries.append((True, full, nd[_NF_SIZE], nd[_NF_MTIME],
                            nd[_NF_CTIME], nd[_NF_ATIME], nd[_NF_ATTR]))
        else:
            # 文件:为**每个硬链接**各产出一条(否则非主名的硬链接会丢,如 conda env 下的包文件)。
            # 同一文件的多个链接共享元数据(大小/时间/属性),只是父目录/路径不同。
            for lpar, lname in (nd[_NF_LINKS] or [(nd[_NF_PARENT], nd[_NF_NAME])]):
                pdir = path_of(lpar) if lpar in nodes else root
                full = pdir + "\\" + lname
                entries.append((False, full, nd[_NF_SIZE], nd[_NF_MTIME],
                                nd[_NF_CTIME], nd[_NF_ATIME], nd[_NF_ATTR]))
    return entries, graph


# GetFileAttributesExW:给 USN 增量的新增文件补 大小/时间/属性(小集合,逐个查不慢)
class _WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD)]

kernel32.GetFileAttributesExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                          ctypes.POINTER(_WIN32_FILE_ATTRIBUTE_DATA)]
kernel32.GetFileAttributesExW.restype = wintypes.BOOL


def _ft64(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def metadata_of_path(path):
    """取单个文件的 (size, mtime, ctime, atime, attr);失败返回 (0,0,0,0,0)。

    供 USN 增量给新增/改名文件补元数据(集合小,逐个 GetFileAttributesExW 不慢)。
    """
    d = _WIN32_FILE_ATTRIBUTE_DATA()
    if not kernel32.GetFileAttributesExW(path, 0, ctypes.byref(d)):  # 0=GetFileExInfoStandard
        return (0, 0.0, 0.0, 0.0, 0)
    size = (d.nFileSizeHigh << 32) | d.nFileSizeLow
    return (size,
            _filetime_to_unix(_ft64(d.ftLastWriteTime)),
            _filetime_to_unix(_ft64(d.ftCreationTime)),
            _filetime_to_unix(_ft64(d.ftLastAccessTime)),
            d.dwFileAttributes)


# ── USN 增量:查询日志状态 + 读取上次位置之后的变更(按搜索窗启停,见 ADR-0004)──────

class _USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [("UsnJournalID", ctypes.c_ulonglong),
                ("FirstUsn", ctypes.c_longlong),
                ("NextUsn", ctypes.c_longlong),
                ("LowestValidUsn", ctypes.c_longlong),
                ("MaxUsn", ctypes.c_longlong),
                ("MaximumSize", ctypes.c_ulonglong),
                ("AllocationDelta", ctypes.c_ulonglong)]


class _READ_USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [("StartUsn", ctypes.c_longlong),
                ("ReasonMask", ctypes.c_ulong),
                ("ReturnOnlyOnClose", ctypes.c_ulong),
                ("Timeout", ctypes.c_ulonglong),
                ("BytesToWaitFor", ctypes.c_ulonglong),
                ("UsnJournalID", ctypes.c_ulonglong)]


def query_journal(letter: str):
    """查询卷的 USN 日志状态,返回 (journal_id, next_usn, lowest_valid_usn) 或 None。

    next_usn 是"当前最新位置"——存档时记下它,下次从这里往后读增量。
    lowest_valid_usn 用于判断日志是否已环形覆盖掉我们上次的位置(见 read_changes)。
    """
    h = _open_volume(letter)
    if h is None:
        return None
    try:
        jd = _USN_JOURNAL_DATA()
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(h, FSCTL_QUERY_USN_JOURNAL, None, 0,
                                      ctypes.byref(jd), ctypes.sizeof(jd),
                                      ctypes.byref(ret), None)
        if not ok:
            return None                              # 日志未启用(罕见,可后续 FSCTL_CREATE 启用)
        return jd.UsnJournalID, jd.NextUsn, jd.LowestValidUsn
    finally:
        kernel32.CloseHandle(h)


def read_changes(letter, journal_id, start_usn):
    """读 start_usn 之后的变更。返回 (changes, next_usn) 或 None(需全量重扫)。

    changes: [(frn, parent_frn, is_dir, name, reason), ...]。
    日志环形覆盖掉 start_usn(start < LowestValidUsn)或 journal_id 变了 → 返回 None,
    调用方据此触发全量重扫(见 ADR-0004 的环形覆盖兜底)。
    """
    info = query_journal(letter)
    if info is None:
        return None
    cur_id, next_usn, lowest = info
    if cur_id != journal_id or start_usn < lowest:
        return None                                  # 日志重建或被覆盖 → 全量重扫
    h = _open_volume(letter)
    if h is None:
        return None
    try:
        rujd = _READ_USN_JOURNAL_DATA_V0(
            start_usn,
            USN_REASON_FILE_CREATE | USN_REASON_FILE_DELETE
            | USN_REASON_RENAME_OLD_NAME | USN_REASON_RENAME_NEW_NAME,
            0, 0, 0, journal_id)
        buf = ctypes.create_string_buffer(1 << 16)
        ret = wintypes.DWORD(0)
        changes = []
        while True:
            ok = kernel32.DeviceIoControl(h, FSCTL_READ_USN_JOURNAL,
                                          ctypes.byref(rujd), ctypes.sizeof(rujd),
                                          buf, ctypes.sizeof(buf), ctypes.byref(ret), None)
            if not ok:
                break
            n = ret.value
            if n <= 8:
                break
            data = buf.raw[:n]
            rujd.StartUsn = struct.unpack_from("<q", data, 0)[0]   # 下一轮起点
            off = 8
            while off < n:
                (rec_len, _maj, _min, frn, parent, _usn, _ts,
                 reason, _src, _sid, attrs, name_len, name_off) = _REC_HDR.unpack_from(data, off)
                if rec_len == 0:
                    break
                name = data[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
                changes.append((frn, parent, bool(attrs & FILE_ATTRIBUTE_DIRECTORY), name, reason))
                off += rec_len
        return changes, next_usn
    finally:
        kernel32.CloseHandle(h)


# PLACEHOLDER_BUILD


def enumerate_volume(letter: str):
    """枚举一个 NTFS 卷,返回 [(is_dir, full_path), ...]。需管理员;失败返回 None。

    两遍法:① build_graph 把所有记录收成 {idx: (parent, is_dir, name)};② _path_of 据父链
    重建完整路径(memo 缓存目录路径)。增量更新复用同一张图与同一套路径逻辑(见 apply_changes)。
    """
    nodes = build_graph(letter)
    if nodes is None:
        return None
    return _entries_from_graph(nodes, letter)


_MASK = 0x0000FFFFFFFFFFFF      # 低 48 位 = MFT 索引;高 16 位是序列号,做键时掩掉更稳
_ROOT_IDX = 5                   # NTFS 根目录固定 MFT 索引


def build_graph(letter: str):
    """全量枚举,返回 FRN 图 {idx: (parent_idx, is_dir, name)} 或 None(打开卷失败)。

    这张图是增量更新的"真相源":提权进程常驻它,USN 变更只改这张图(见 apply_changes)。
    """
    handle = _open_volume(letter)
    if handle is None:
        return None
    try:
        nodes = {}
        for frn, parent, is_dir, name in _enum_records(handle):
            nodes[frn & _MASK] = (parent & _MASK, is_dir, name)
        return nodes
    finally:
        kernel32.CloseHandle(handle)


def _path_of(nodes, idx, root, dir_path, _seen=None):
    """重建节点 idx 的完整路径(含自己的名字,除非它就是根)。dir_path 作 memo 缓存。"""
    if idx in dir_path:
        return dir_path[idx]
    node = nodes.get(idx)
    if node is None:
        return root                                  # 记录缺失 → 归到卷根
    parent, _is_dir, name = node
    if _seen is None:
        _seen = set()
    if idx in _seen:                                 # 防环(异常 MFT)
        return root
    _seen.add(idx)
    pp = _path_of(nodes, parent, root, dir_path, _seen) if parent in nodes else root
    p = pp + "\\" + name
    dir_path[idx] = p
    return p


def _entries_from_graph(nodes, letter):
    """据 FRN 图重建 [(is_dir, full_path), ...]。"""
    root = f"{letter}:"
    dir_path = {_ROOT_IDX: root}
    results = []
    for idx, (parent, is_dir, name) in nodes.items():
        if idx == _ROOT_IDX or name in (".", ""):
            continue
        if is_dir:
            full = _path_of(nodes, idx, root, dir_path)
        else:
            pdir = _path_of(nodes, parent, root, dir_path) if parent in nodes else root
            full = pdir + "\\" + name
        results.append((is_dir, full))
    return results


def apply_changes(nodes, changes, letter):
    """把 USN 变更应用到 FRN 图,返回 (added, removed):

    added: [(is_dir, path), ...] 新增/改名后出现的条目(供索引追加);
    removed: [path, ...] 删除/改名前消失的条目(供索引打墓碑)。
    就地修改 nodes。注意:目录改名会令其子孙路径过期 —— 本函数只处理"条目自身",
    子孙路径的级联由调用方在打开搜索窗时的"补课重扫"兜底(见 ADR-0004),会话中目录
    改名罕见,过期项下次重扫即正。
    """
    root = f"{letter}:"
    dir_path = {_ROOT_IDX: root}

    def cur_path(idx):
        node = nodes.get(idx)
        if node is None:
            return None
        parent, is_dir, name = node
        if is_dir:
            return _path_of(nodes, idx, root, dir_path)
        pp = _path_of(nodes, parent, root, dir_path) if parent in nodes else root
        return pp + "\\" + name

    # 一次"建文件"在 USN 里会产生多条记录(创建/写/关闭都带 CREATE 位),故按 idx 归并:
    # 只记每个 idx 的"消失前旧路径"(首次触碰时,改图之前算)与"最终是否存在",最后各算一次。
    old_path = {}        # idx -> 改动前路径(仅首次触碰时记录;新建文件记为 None)
    touched = []         # 保持触碰顺序,去重
    for frn, parent, is_dir, name, reason in changes:
        idx = frn & _MASK
        pidx = parent & _MASK
        if idx not in old_path:
            old_path[idx] = cur_path(idx)            # 改图前的旧路径(可能 None)
            touched.append(idx)
        if reason & (USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD_NAME):
            nodes.pop(idx, None)
            dir_path.clear()
        if reason & (USN_REASON_FILE_CREATE | USN_REASON_RENAME_NEW_NAME):
            nodes[idx] = (pidx, is_dir, name)
            dir_path.clear()

    added, removed = [], []
    for idx in touched:
        node = nodes.get(idx)
        new = cur_path(idx) if node is not None else None
        old = old_path[idx]
        if old and old != new:
            removed.append(old)                      # 消失了(删除/改名旧名)
        if node is not None and new:
            added.append((node[1], new))             # 现存(新建/改名新名);node[1]=is_dir
    return added, removed


if __name__ == "__main__":
    # 自测:扫 C 盘,打印条数 + 几个样本 + 耗时
    import sys, time
    drive = sys.argv[1] if len(sys.argv) > 1 else "C"
    t0 = time.time()
    res = enumerate_volume(drive)
    if res is None:
        print("打开卷失败(需管理员权限?)")
    else:
        dt = time.time() - t0
        print(f"{drive}: 共 {len(res)} 条,耗时 {dt:.1f}s")
        for is_dir, p in res[:5]:
            print(("[D] " if is_dir else "[F] ") + p)
