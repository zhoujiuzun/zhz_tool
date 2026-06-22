# -*- coding: utf-8 -*-
"""Everything SDK vs Ours (v05 FileIndex) — 同条件性能对比。

测: 载档时间 + 同类查询的 wall-clock。Everything 用 SDK,我们直接调 FileIndex。
"""
import os, sys, time, ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(__file__))
from app.file_index import FileIndex

# ── Everything SDK ──────────────────────────────────
edll = ctypes.windll.LoadLibrary(r"C:\Program Files\Everything\Everything64.dll")

edll.Everything_SetSearchW.argtypes = [wintypes.LPCWSTR]
edll.Everything_SetMatchPath.argtypes = [wintypes.BOOL]
edll.Everything_SetMax.argtypes = [wintypes.DWORD]
edll.Everything_QueryW.argtypes = [wintypes.BOOL]
edll.Everything_QueryW.restype = wintypes.BOOL
edll.Everything_GetNumResults.restype = wintypes.DWORD
edll.Everything_GetResultFullPathNameW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD]
edll.Everything_GetResultFileNameW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD]
edll.Everything_Reset.argtypes = []

def esearch(q, match_path=False):
    """Everything SDK 搜索,返回 (count, [paths])。"""
    edll.Everything_SetSearchW(q)
    edll.Everything_SetMatchPath(match_path)
    edll.Everything_SetMax(1000)
    if not edll.Everything_QueryW(True):
        edll.Everything_Reset()
        return 0, []
    n = edll.Everything_GetNumResults()
    paths = []
    buf = ctypes.create_unicode_buffer(260)
    for i in range(min(n, 10)):
        edll.Everything_GetResultFullPathNameW(i, buf, 260)
        paths.append(buf.value)
    edll.Everything_Reset()
    return n, paths

def ebench(label, fn):
    """测 Everything,取最小 5 次。"""
    for _ in range(3): fn()
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        n, _ = fn()
        dt = (time.perf_counter() - t0) * 1000
        best = min(best, dt)
    print(f"  [E] {label:.<40s} {best:7.0f} ms  ({n} results)")

# ── Our v05 Index ──────────────────────────────────
ARCHIVE = os.path.expanduser("~/.ocr_tool/file_index.bin")
idx = FileIndex()

t0 = time.perf_counter()
idx.load(ARCHIVE)
our_load = time.perf_counter() - t0
print(f"\n{'='*60}")
print(f"OUR  v05 load:  {our_load:.1f}s  ({len(idx)} items)")
print(f"E    DB ready:  (unknown, Everything running in bg)")
print(f"{'='*60}\n")

def obench(label, fn):
    for _ in range(3): fn()
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        r = fn()
        dt = (time.perf_counter() - t0) * 1000
        best = min(best, dt)
    n = len(r) if r else 0
    print(f"  [O] {label:.<40s} {best:7.0f} ms  ({n} results)")

# ══════════════════════════════════════════════════════
print("=== 1. 普通名字搜索 ===")
for q, desc in [("notepad", "rare term"), (".dll", "common ext"), ("xyznotexist", "zero match"), ("a", "super common")]:
    ebench(f"name='{q}'",      lambda q=q: esearch(q))
    obench(f"name='{q}'",      lambda q=q: idx.search(q, limit=1000))
    print()

print("=== 2. 路径搜索(双条件) ===")
ebench("name=rpt,path=dl",    lambda: esearch("report path:downloads"))
obench("name=rpt,path=dl",    lambda: idx.search("report", match_path=True, path_query="downloads", limit=1000))
print()

print("=== 3. 扩展名过滤 ===")
ebench("ext:dll",             lambda: esearch("ext:dll"))
obench("ext:dll",             lambda: idx.search_advanced({"ext": ["dll"]}, limit=1000))
ebench("ext:exe folder:Win",  lambda: esearch("C:\\Windows\\ ext:exe"))
obench("ext:exe folder:Win",  lambda: idx.search_advanced({"folder": "C:\\Windows", "ext": ["exe"]}, limit=1000))
print()

print("=== 4. 大小过滤 ===")
ebench("size>1GB",            lambda: esearch("size:>1gb"))
obench("size>1GB",            lambda: idx.search_advanced({"size_min": 1073741824}, limit=1000))
ebench("size>1GB ext:dll",    lambda: esearch("size:>1gb ext:dll"))
obench("size>1GB ext:dll",    lambda: idx.search_advanced({"size_min": 1073741824, "ext": ["dll"]}, limit=1000))
print()

print("=== 5. 属性(隐藏文件) ===")
ebench("attrib:h",            lambda: esearch("attrib:h"))
obench("attrib:h",            lambda: idx.search_advanced({"attrs": [0x2]}, limit=1000))
print()

print("=== 6. 路径前缀(Everything: root:) ===")
ebench("root:C:\\Windows",    lambda: esearch("C:\\Windows\\"))
obench("root:C:\\Windows",    lambda: idx.search_advanced({"folder": "C:\\Windows"}, limit=1000))
print()

print(f"\n{'='*60}")
print(f"Summary: v05 load={our_load:.1f}s | search queries above")
print(f"{'='*60}")
