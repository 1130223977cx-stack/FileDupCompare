# -*- coding: utf-8 -*-
"""
文件比对工具 · 免安装版
- 左右对照：A 目录 / B 目录
- 判定：内容哈希（SHA-256）一致即重复，文件名 / 大小写 / 修改时间不参与
- 能力：相同组左右配对、独有文件分侧列出、图片与视频缩略图、逐行勾选批量处置、
        一侧独有内容归并到另一侧、操作日志与撤销
"""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QObject, QThread, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QProgressDialog, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

APP_NAME = "文件比对工具"
APP_VER = "1.3.1"

# ---------------- 视觉（卡片仪表盘风格）----------------
C_PAPER = "#F2F4F8"
C_CARD = "#FFFFFF"
C_BAND = "#F6F8FC"
C_BAND2 = "#FBFCFE"
C_INK = "#1B1E25"
C_INK2 = "#454B57"
C_MUTED = "#6B7280"
C_FAINT = "#9AA1AC"
C_LINE = "#E3E7EF"
C_LINE2 = "#EEF1F6"
C_HAIR = "#F1F4F9"
C_RED = "#D93B4B"
C_REDSOFT = "#FDEEF0"
C_BLUE = "#2B6CF6"
C_BLUE2 = "#1B58D6"
C_BLUESOFT = "#EAF0FF"
C_OK = "#1FA971"
C_OKSOFT = "#E7F7F0"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Cascadia Mono"

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic"}
VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".flv", ".ts", ".mpg", ".mpeg"}

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# 云端占位文件（OneDrive / 坚果云 / 网盘客户端），读取会触发下载、可能长时间无响应
CLOUD_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
LONG_PATH_PREFIX = "\\\\?\\"


# ============================================================
# Windows 辅助
# ============================================================
def win_path(p: str) -> str:
    r"""超过 260 字符时加 \\?\ 前缀，保证长路径可访问。"""
    if len(p) > 259 and not p.startswith(LONG_PATH_PREFIX):
        return LONG_PATH_PREFIX + os.path.abspath(p)
    return p


def is_hidden_or_system(p: str) -> bool:
    try:
        st = os.stat(win_path(p), follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def is_reparse_point(p: str) -> bool:
    try:
        st = os.stat(win_path(p), follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def move_to_recycle_bin(paths: list[str]) -> tuple[int, list[str]]:
    """送回收站。返回 (成功数, 失败路径列表)。"""
    if not paths:
        return 0, []
    fo_delete = 3
    fof_allowundo = 0x0040
    fof_noconfirmation = 0x0010
    fof_silent = 0x0004
    fof_noerrorui = 0x0400
    fof_filesonly = 0x0008
    failed: list[str] = []
    single = []
    for p in paths:
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = fo_delete
        op.pFrom = p + "\0\0"
        op.pTo = None
        op.fFlags = fof_allowundo | fof_noconfirmation | fof_silent | fof_noerrorui | fof_filesonly
        try:
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            # 只有"仅一个文件"时才把 rc 与路径对应起来，批量时无法精确归因
            if rc != 0 or op.fAnyOperationsAborted:
                failed.append(p)
            else:
                single.append(p)
        except Exception:
            failed.append(p)
    return len(single), failed


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SIZE2(ctypes.Structure):
    """与 Windows SIZE 结构同尺寸（两个 LONG）。"""
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


_GetImageProto = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, _SIZE2, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
)


def _guid(iid_str: str) -> _GUID:
    import uuid
    u = uuid.UUID(iid_str)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    g.Data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return g


_IID_IShellItemImageFactory = _guid("bcc18b79-ba16-442f-80c4-8a59c30c463b")


def shell_thumbnail(path: str, size: int = 256) -> QImage | None:
    """
    调用 Windows 资源管理器缩略图提供程序取图（图片、视频、PDF 等只要有系统缩略图就能出）。
    返回 QImage（可在工作线程里安全生成），任何异常都返回 None，由调用方回退。
    """
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
    except Exception:
        return None
    try:
        ole32.CoInitializeEx(None, 0x2)   # APARTMENTTHREADED，跨线程调用 COM 前必须
    except Exception:
        pass

    item = ctypes.c_void_p()
    try:
        shell32.SHCreateItemFromParsingName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        hr = shell32.SHCreateItemFromParsingName(
            win_path(path), None, ctypes.byref(_IID_IShellItemImageFactory), ctypes.byref(item)
        )
        if hr != 0 or not item.value:
            return None
        vtbl = ctypes.cast(item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_image = _GetImageProto(vtbl[3])
        hbmp = ctypes.c_void_p()
        hr = get_image(item, _SIZE2(size, size), 0x1, ctypes.byref(hbmp))  # BIGGERSIZEOK
        if hr != 0 or not hbmp.value:
            return None
        return _hbitmap_to_pixmap(hbmp.value)
    except Exception:
        return None
    finally:
        if item.value:
            try:
                vtbl = ctypes.cast(item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
                release(item)
            except Exception:
                pass


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort), ("bmBits", ctypes.c_void_p),
    ]


def _hbitmap_to_pixmap(hbmp: int) -> QImage | None:
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        bm = _BITMAP()
        if not gdi32.GetObjectW(hbmp, ctypes.sizeof(_BITMAP), ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h  # 负数 = 自上而下
        bi.biPlanes, bi.biBitCount = 1, 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(0)
        try:
            if not gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bi), 0):
                return None
        finally:
            user32.ReleaseDC(0, hdc)
        img = QImage(buf, w, h, w * 4, QImage.Format_ARGB32)
        if img.isNull():
            return None
        return img.copy()
    except Exception:
        return None


def mp4_duration(path: str) -> str:
    """从 MP4/MOV 的 mvhd 盒读时长，失败返回空串。"""
    try:
        with open(win_path(path), "rb", buffering=0) as f:
            data = f.read(1024 * 1024)
        idx = data.find(b"mvhd")
        if idx < 0:
            return ""
        p = idx + 4
        ver = data[p]
        if ver == 1:
            scale = int.from_bytes(data[p + 20:p + 24], "big")
            dur = int.from_bytes(data[p + 24:p + 32], "big")
        else:
            scale = int.from_bytes(data[p + 12:p + 16], "big")
            dur = int.from_bytes(data[p + 16:p + 20], "big")
        if not scale:
            return ""
        sec = int(dur / scale)
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"
    except Exception:
        return ""


# ============================================================
# 性能监控（本工具自身开销：CPU / 内存 / 磁盘 IO）
# ============================================================
class _FT(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


def _ft_val(ft) -> int:
    return (ft.hi << 32) | ft.lo


class _PMem(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _PIo(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _MemStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class SysProbe:
    """采样本进程的 CPU / 内存 / 磁盘 IO，以及系统整体的 CPU 与内存占用。"""

    def __init__(self):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.k32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FT), ctypes.POINTER(_FT),
                                      ctypes.POINTER(_FT), ctypes.POINTER(_FT)]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetSystemTimes.argtypes = [ctypes.POINTER(_FT), ctypes.POINTER(_FT), ctypes.POINTER(_FT)]
        k.GetSystemTimes.restype = wintypes.BOOL
        k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMem), wintypes.DWORD]
        k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PIo)]
        k.GetProcessIoCounters.restype = wintypes.BOOL
        k.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemStatus)]
        k.GlobalMemoryStatusEx.restype = wintypes.BOOL
        self._prev = None
        self._last: dict | None = None
        self.cpu_hist: list[float] = []
        self.io_hist: list[float] = []
        self.cpu_peak = 0.0
        self.io_peak = 0.0
        self.read_total = 0
        self.write_total = 0
        self.threads = 4

    # ---- 原始采样 ----
    def _raw(self):
        proc = self.k32.GetCurrentProcess()
        cr, ex, kt, ut = _FT(), _FT(), _FT(), _FT()
        ok1 = self.k32.GetProcessTimes(proc, ctypes.byref(cr), ctypes.byref(ex),
                                       ctypes.byref(kt), ctypes.byref(ut))
        idle, kern, user = _FT(), _FT(), _FT()
        ok2 = self.k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        mem = _PMem()
        mem.cb = ctypes.sizeof(mem)
        ok3 = self.k32.K32GetProcessMemoryInfo(proc, ctypes.byref(mem), mem.cb)
        io = _PIo()
        ok4 = self.k32.GetProcessIoCounters(proc, ctypes.byref(io))
        ms = _MemStatus()
        ms.dwLength = ctypes.sizeof(ms)
        ok5 = self.k32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return {
            "proc_cpu": _ft_val(kt) + _ft_val(ut) if ok1 else 0,
            "sys_idle": _ft_val(idle) if ok2 else 0,
            "sys_total": (_ft_val(kern) + _ft_val(user)) if ok2 else 0,
            "rss": mem.WorkingSetSize if ok3 else 0,
            "peak_rss": mem.PeakWorkingSetSize if ok3 else 0,
            "io_r": io.ReadTransferCount if ok4 else 0,
            "io_w": io.WriteTransferCount if ok4 else 0,
            "mem_load": ms.dwMemoryLoad if ok5 else 0,
            "mem_total": ms.ullTotalPhys if ok5 else 0,
            "mem_avail": ms.ullAvailPhys if ok5 else 0,
        }

    def sample(self) -> dict:
        cur = self._raw()
        if self._prev is None:
            self._prev = (cur, time.time())
            return {
                "cpu_proc": 0.0, "cpu_sys": 0.0,
                "rss_mb": cur["rss"] / 1048576, "peak_rss_mb": cur["peak_rss"] / 1048576,
                "io_r_bps": 0.0, "io_w_bps": 0.0,
                "read_total": cur["io_r"], "write_total": cur["io_w"],
                "mem_load": cur["mem_load"], "mem_total_gb": cur["mem_total"] / 1073741824,
            }
        prev, pts = self._prev
        dt = time.time() - pts
        if dt < 0.1:
            # 两次采样挤在一起时，用上一轮的速率，避免算出离谱的 IO 尖峰
            self._prev = (cur, time.time())
            if self._last:
                out = dict(self._last)
                out["read_total"], out["write_total"] = cur["io_r"], cur["io_w"]
                out["rss_mb"] = cur["rss"] / 1048576
                out["peak_rss_mb"] = cur["peak_rss"] / 1048576
                return out
            dt = 0.1
        dproc = cur["proc_cpu"] - prev["proc_cpu"]
        dtot = cur["sys_total"] - prev["sys_total"]
        didle = cur["sys_idle"] - prev["sys_idle"]
        cpu_proc = (dproc / dtot * 100.0) if dtot > 0 else 0.0
        cpu_sys = (1.0 - didle / dtot) * 100.0 if dtot > 0 else 0.0
        cpu_proc = max(0.0, min(cpu_proc, 100.0 * self.threads))
        cpu_sys = max(0.0, min(cpu_sys, 100.0))
        io_r = max(0, cur["io_r"] - prev["io_r"]) / dt
        io_w = max(0, cur["io_w"] - prev["io_w"]) / dt
        self._prev = (cur, time.time())

        self.cpu_peak = max(self.cpu_peak, cpu_proc)
        self.io_peak = max(self.io_peak, io_r + io_w)
        self.read_total, self.write_total = cur["io_r"], cur["io_w"]
        for hist, val in ((self.cpu_hist, cpu_proc),
                          (self.io_hist, (io_r + io_w) / 1048576)):
            hist.append(val)
            if len(hist) > 90:
                del hist[0]
        out = {
            "cpu_proc": cpu_proc, "cpu_sys": cpu_sys,
            "rss_mb": cur["rss"] / 1048576, "peak_rss_mb": cur["peak_rss"] / 1048576,
            "io_r_bps": io_r, "io_w_bps": io_w,
            "read_total": cur["io_r"], "write_total": cur["io_w"],
            "mem_load": cur["mem_load"], "mem_total_gb": cur["mem_total"] / 1073741824,
        }
        self._last = out
        return out


# ============================================================
# 工具
# ============================================================
def human_size(n: int) -> str:
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        v = n / 1048576
        return f"{v:.0f} MB" if v >= 100 else f"{v:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _under(path: str, root: str) -> bool:
    """path 是否位于 root 之下（大小写不敏感，兼容长路径前缀）。"""
    try:
        a = os.path.normcase(os.path.abspath(win_path(path)))
        b = os.path.normcase(os.path.abspath(win_path(root)))
    except Exception:
        return False
    return a == b or a.startswith(b.rstrip("\\") + "\\")


def _safe_size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


class Cancelled(Exception):
    pass


class Skipped(Exception):
    pass


def sha256_of(path: str, chunk: int = 1048576, cancel=lambda: False,
              on_chunk=None, should_skip=lambda: False) -> str:
    """流式 SHA-256。每读一块回调 on_chunk(n)，并检查取消 / 手动跳过。"""
    h = hashlib.sha256()
    with open(win_path(path), "rb", buffering=0) as f:
        while True:
            if cancel():
                raise Cancelled()
            if should_skip():
                raise Skipped()
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            if on_chunk is not None:
                on_chunk(len(b))
    return h.hexdigest()


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ============================================================
# 数据模型
# ============================================================
@dataclass
class FileRec:
    path: str
    side: str          # "A" / "B"
    size: int
    mtime: float
    hash: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class Group:
    hash: str
    size: int
    files: list[FileRec] = field(default_factory=list)

    @property
    def has_a(self) -> bool:
        return any(f.side == "A" for f in self.files)

    @property
    def has_b(self) -> bool:
        return any(f.side == "B" for f in self.files)

    @property
    def releasable(self) -> int:
        return self.size * (len(self.files) - 1)

    @property
    def kind(self) -> str:
        if self.has_a and self.has_b:
            return "cross"
        return "ina" if self.has_a else "inb"

    def by_side(self, side: str) -> list[FileRec]:
        return [f for f in self.files if f.side == side]


@dataclass
class ScanResult:
    groups: list[Group] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    count_a: int = 0
    count_b: int = 0
    seconds: float = 0.0
    aborted: bool = False


# ============================================================
# 扫描线程
# ============================================================
class MergeWorker(QThread):
    """归并在后台线程做，界面不冻结；每条都单独打日志，可中途取消。"""

    one = Signal(int, int, str, str, str)   # 序号 / 总数 / 级别 / 文案 / 路径
    done = Signal(dict)

    def __init__(self, plans, parent=None):
        super().__init__(parent)
        self.plans = plans
        self._cancel = False
        self.ok = 0
        self.fail: list[str] = []
        self.moved: list[tuple] = []     # (src, dst, side, size, mtime, hash)
        self.rows: list[list] = []       # 操作日志 CSV 行
        self.cancelled = False

    def cancel(self):
        self._cancel = True

    def run(self):
        total = len(self.plans)
        for i, (rec, target) in enumerate(self.plans, 1):
            if self._cancel:
                self.cancelled = True
                self.one.emit(i, total, "WARN", f"已取消归并，剩余 {total - i + 1} 个文件保持原样", "")
                break
            self.one.emit(i, total, "RUN", f"[{i}/{total}] {os.path.basename(rec.path)} → "
                                        f"{os.path.basename(target)}", rec.path)
            try:
                os.makedirs(win_path(os.path.dirname(target)), exist_ok=True)
                shutil.move(win_path(rec.path), win_path(target))
                self.ok += 1
                self.moved.append((rec.path, target, rec.side, rec.size, rec.mtime, rec.hash))
                self.rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "归并移动", rec.side,
                                  rec.path, target, rec.size, rec.hash, "成功", ""])
                self.one.emit(i, total, "OK", f"[{i}/{total}] 已完成 {os.path.basename(target)}", rec.path)
            except Exception as e:
                self.fail.append(rec.path)
                self.rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "归并移动", rec.side,
                                  rec.path, target, rec.size, rec.hash, "失败", str(e)])
                self.one.emit(i, total, "ERR", f"[{i}/{total}] 失败 {os.path.basename(rec.path)}：{e}", rec.path)
        self.done.emit({
            "ok": self.ok, "fail": self.fail, "moved": self.moved,
            "rows": self.rows, "total": total, "cancelled": self.cancelled,
        })


class ScanWorker(QThread):
    progress = Signal(object)      # dict：阶段 / 文件数 / 字节数 / 当前活跃文件 / ETA
    stalled = Signal(str, float)   # 路径 / 已无进展的秒数
    failed = Signal(str)
    finished_result = Signal(object)       # ScanResult

    STALL_SEC = 6.0                # 单个文件多久没有字节进展就提示「疑似卡住」
    TICK = 0.25                    # 进度刷新间隔

    def __init__(self, root_a: str, root_b: str, opt: dict, parent=None):
        super().__init__(parent)
        self.root_a, self.root_b = root_a, root_b
        self.opt = opt
        self._cancel = False
        self.skip_paths: set[str] = set()      # 用户点「跳过当前文件」时写入
        self._prog = None

    def cancel(self):
        self._cancel = True

    def skip(self, path: str):
        if path:
            self.skip_paths.add(path)

    def _should_cancel(self) -> bool:
        return self._cancel

    # ---- 枚举 ----
    def _collect(self, root: str, side: str, skipped: list):
        recursive = self.opt.get("recursive", True)
        skip_hidden = self.opt.get("skip_hidden", True)
        skip_zero = self.opt.get("skip_zero", True)
        min_size = int(self.opt.get("min_kb", 0)) * 1024
        excludes = [x.strip().lower() for x in str(self.opt.get("exclude", "")).replace("；", ";").split(";") if x.strip()]
        out: list[FileRec] = []

        stack = [root]
        dirs = 0
        last_emit = 0.0
        while stack:
            cur = stack.pop()
            if self._should_cancel():
                return out
            dirs += 1
            if time.time() - last_emit > 0.2:          # 枚举阶段也要有动静，否则像假死
                last_emit = time.time()
                self.progress.emit({
                    "stage": "enum", "dirs": dirs, "found": len(out),
                    "side": side, "path": cur,
                })
            try:
                entries = list(os.scandir(win_path(cur)))
            except PermissionError:
                skipped.append((cur, "无读取权限"))
                continue
            except OSError as e:
                skipped.append((cur, f"无法访问：{e.strerror or e}"))
                continue
            for entry in entries:
                p = entry.path
                try:
                    low_name = entry.name.lower()
                    low_ext = os.path.splitext(low_name)[1]
                    if excludes and (low_ext in excludes or low_name in excludes):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        if entry.is_dir(follow_symlinks=False):
                            skipped.append((p, "符号链接 / 联接点，已跳过"))
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if (attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)) and skip_hidden:
                            continue
                        if recursive:
                            stack.append(p)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if skip_hidden and (attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)):
                        continue
                    if attrs & CLOUD_MASK:
                        skipped.append((p, "云端占位文件（未下载到本地），已跳过"))
                        continue
                    size = st.st_size
                    if size == 0 and skip_zero:
                        skipped.append((p, "零字节文件，按设置跳过"))
                        continue
                    if size < min_size:
                        continue
                    out.append(FileRec(p, side, size, st.st_mtime))
                except PermissionError:
                    skipped.append((p, "无读取权限"))
                except OSError as e:
                    skipped.append((p, f"读取失败：{e.strerror or e}"))
        return out

    # ---- 进度状态（供监控线程读取）----
    class _Prog:
        def __init__(self):
            self.lock = threading.Lock()
            self.total_bytes = 0
            self.total_files = 0
            self.done_bytes = 0
            self.done_files = 0
            self.active: dict[str, list] = {}     # path -> [start, last, file_bytes, size]
            self.skipped = 0

        def begin(self, path, size):
            with self.lock:
                now = time.time()
                self.active[path] = [now, now, 0, size]

        def add(self, path, n):
            with self.lock:
                self.done_bytes += n
                rec = self.active.get(path)
                if rec is not None:
                    rec[1] = time.time()
                    rec[2] += n

        def end(self, path):
            with self.lock:
                self.active.pop(path, None)
                self.done_files += 1

        def snap(self, elapsed):
            with self.lock:
                frac_b = (self.done_bytes / self.total_bytes) if self.total_bytes else 0.0
                speed = (self.done_bytes / elapsed) if elapsed > 0.5 else 0.0
                eta = None
                if speed > 1e5 and frac_b > 0.02:
                    eta = (self.total_bytes - self.done_bytes) / speed
                oldest = None
                # 取「最久没有字节进展」的活跃文件
                worst = None
                now = time.time()
                for p, rec in self.active.items():
                    idle = now - rec[1]
                    if worst is None or idle > worst[1]:
                        worst = (p, idle, rec[3], rec[2])
                return {
                    "stage": "hash",
                    "done_bytes": self.done_bytes,
                    "total_bytes": self.total_bytes,
                    "done_files": self.done_files,
                    "total_files": self.total_files,
                    "speed": speed,
                    "eta": eta,
                    "frac": frac_b,
                    "worst": worst,
                    "active_n": len(self.active),
                    "active": list(self.active.keys()),
                    "biggest": max(
                        ((p, r[3], r[2]) for p, r in self.active.items()),
                        key=lambda x: x[1], default=None,
                    ),
                }

    def run(self):
        t0 = time.time()
        skipped: list[tuple[str, str]] = []
        try:
            fa = self._collect(self.root_a, "A", skipped)
            fb = self._collect(self.root_b, "B", skipped)
        except Exception as e:                     # 枚举阶段异常
            self.failed.emit(f"枚举文件失败：{e}")
            return
        if self._should_cancel():
            self.finished_result.emit(ScanResult([], skipped, len(fa), len(fb), time.time() - t0, True))
            return

        allfiles = fa + fb
        total = len(allfiles)

        prog = ScanWorker._Prog()
        prog.total_files = total
        prog.total_bytes = sum(f.size for f in allfiles) or 1
        self._prog = prog

        stop = threading.Event()

        def monitor():
            """节奏控制：每 0.25s 推一次快照；速度用近 5 秒滑动窗口，ETA 给区间。"""
            old = t0
            notified: set[str] = set()
            hist: list[tuple[float, int, int]] = []     # (t, done_bytes, done_files)
            while not stop.is_set():
                time.sleep(self.TICK)
                now = time.time()
                if now - old < self.TICK * 0.9:
                    continue
                old = now
                try:
                    snap = prog.snap(now - t0)
                except Exception:
                    continue
                hist.append((now, snap["done_bytes"], snap["done_files"]))
                while len(hist) > 2 and now - hist[0][0] > 6.0:
                    hist.pop(0)
                if len(hist) >= 2:
                    span = hist[-1][0] - hist[0][0]
                    dbytes = hist[-1][1] - hist[0][1]
                    if span >= 1.0 and dbytes > 0:
                        speed = dbytes / span
                        remain_b = max(0, snap["total_bytes"] - snap["done_bytes"])
                        snap["speed"] = speed
                        snap["eta"] = remain_b / speed if speed > 1e5 else None
                        snap["eta_lo"] = remain_b / (speed * 0.75) if speed > 1e5 else None
                        snap["eta_hi"] = remain_b / (speed * 1.25) if speed > 1e5 else None
                else:
                    snap["speed"] = 0.0
                    snap["eta"] = None
                    snap["eta_lo"] = snap["eta_hi"] = None
                self.progress.emit(snap)
                worst = snap.get("worst")
                if worst and snap["active_n"] and not self._should_cancel():
                    p, idle, fsize, fdone = worst
                    if idle >= self.STALL_SEC and p not in notified:
                        notified.add(p)
                        self.stalled.emit(p, idle)
                    elif idle < self.STALL_SEC * 0.5:
                        notified.discard(p)
            # 收尾再推一次
            try:
                self.progress.emit(prog.snap(time.time() - t0))
            except Exception:
                pass

        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()

        from concurrent.futures import ThreadPoolExecutor, as_completed

        maps: dict[str, list[FileRec]] = {}
        errors: list[tuple[str, str]] = []
        cancelled = False

        def work(rec: FileRec):
            if self._should_cancel():
                return rec, ("cancel", None)
            if rec.path in self.skip_paths:
                return rec, ("skip", None)
            prog.begin(rec.path, rec.size)
            try:
                h = sha256_of(
                    rec.path,
                    cancel=self._should_cancel,
                    on_chunk=lambda n: prog.add(rec.path, n),
                    should_skip=lambda: rec.path in self.skip_paths or self._should_cancel(),
                )
                return rec, ("ok", h)
            except Cancelled:
                return rec, ("cancel", None)
            except Skipped:
                return rec, ("skip", None)
            except PermissionError:
                return rec, ("err", "无读取权限")
            except MemoryError:
                return rec, ("err", "内存不足，已跳过")
            except OSError as e:
                return rec, ("err", f"读取失败：{e.strerror or e}")
            except Exception as e:
                return rec, ("err", f"读取失败：{e}")
            finally:
                prog.end(rec.path)

        pool = ThreadPoolExecutor(max_workers=4)
        try:
            futs = [pool.submit(work, rec) for rec in allfiles]
            for fut in as_completed(futs):          # 谁先算完谁先计数，不再被大文件卡住进度
                try:
                    rec, (tag, payload) = fut.result()
                except Exception as e:              # 兜底，绝不因为单个文件中断整次扫描
                    errors.append(("", f"线程异常：{e}"))
                    continue
                if tag == "ok":
                    rec.hash = payload
                    maps.setdefault(payload, []).append(rec)
                elif tag == "cancel":
                    cancelled = True
                elif tag == "skip":
                    errors.append((rec.path, "手动跳过"))
                else:
                    errors.append((rec.path, payload or ""))
        finally:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)

        if cancelled or self._should_cancel():
            self.finished_result.emit(ScanResult([], skipped, len(fa), len(fb), time.time() - t0, True))
            return

        result = ScanResult(
            groups=[Group(h, items[0].size, items) for h, items in maps.items()],
            skipped=skipped + errors,
            count_a=len(fa), count_b=len(fb),
            seconds=time.time() - t0,
        )
        self.finished_result.emit(result)


# ============================================================
# 主题样式
# ============================================================
QSS = f"""
QWidget {{
    background: {C_CARD};
    color: {C_INK};
    font-family: "{FONT_UI}";
    font-size: 9pt;
}}
QLabel {{ background: transparent; }}
QMainWindow, #Root {{ background: {C_PAPER}; }}

#TitleBar {{ background: {C_CARD}; border-bottom: 1px solid {C_LINE2}; }}
#TitleText {{ font-size: 10pt; font-weight: 600; letter-spacing: .4px; }}
#TitleSub {{ color: {C_MUTED}; font-size: 8pt; }}
#WinBtn {{ border: 0; background: transparent; color: #6B7280; border-radius: 4px; }}
#WinBtn:hover {{ background: #EDF0F6; }}
#WinBtnClose {{ border: 0; background: transparent; color: #6B7280; border-radius: 4px; }}
#WinBtnClose:hover {{ background: {C_RED}; color: #FFFFFF; }}

#WinCard {{ background: {C_CARD}; }}
#Card {{ background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 12px; }}
#CardTitle {{ color: {C_MUTED}; font-size: 8pt; }}
#CardPeak {{ color: {C_FAINT}; font-size: 7.5pt; font-family: "{FONT_MONO}"; }}
#CardFoot {{ color: {C_MUTED}; font-size: 7.5pt; }}
#MetricBig {{ color: {C_INK}; font-size: 21pt; font-weight: 600; }}
#MetricUnit {{ color: {C_MUTED}; font-size: 8.5pt; }}
#PillRun {{ background: {C_OKSOFT}; color: {C_OK}; font-size: 7.5pt; border-radius: 8px; padding: 1px 8px; }}
#PillIdle {{ background: #F1F3F8; color: {C_MUTED}; font-size: 7.5pt; border-radius: 8px; padding: 1px 8px; }}
#PillDone {{ background: {C_BLUESOFT}; color: {C_BLUE}; font-size: 7.5pt; border-radius: 8px; padding: 1px 8px; }}
#Mini {{ color: {C_MUTED}; font-size: 8pt; font-family: "{FONT_MONO}"; }}

#MonitorBand {{ background: {C_PAPER}; }}
#DirBar {{ background: {C_CARD}; border-bottom: 1px solid {C_LINE2}; }}
#SealA {{ background: {C_BLUE}; color: #FFFFFF; font-size: 8pt; font-weight: 600; border-radius: 6px; }}
#SealB {{ background: {C_CARD}; color: {C_BLUE}; border: 1px solid {C_BLUE}; font-size: 8pt; font-weight: 600; border-radius: 6px; }}
#SmallLabel {{ color: {C_MUTED}; font-size: 7.5pt; }}
#PathEdit {{
    background: #F7F9FD; border: 1px solid {C_LINE}; border-radius: 8px; padding: 3px 9px;
    font-family: "{FONT_MONO}"; font-size: 8pt; color: #2A2F3A;
}}
#PathEdit:focus {{ border-color: {C_BLUE}; background: #FFFFFF; }}
QPushButton {{
    background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 8px; padding: 4px 11px;
    font-size: 8.5pt; color: {C_INK2};
}}
QPushButton:hover {{ background: #F5F7FC; border-color: #CBD3E1; }}
QPushButton:pressed {{ background: #EDF1F9; }}
QPushButton:disabled {{ color: #AEB5C2; border-color: #EBEEF5; background: #FAFBFD; }}
#Primary {{ background: {C_BLUE}; color: #FFFFFF; border: 1px solid {C_BLUE}; font-weight: 600; }}
#Primary:hover {{ background: {C_BLUE2}; border-color: {C_BLUE2}; }}
#Primary:disabled {{ background: #A9BEF7; border-color: #A9BEF7; color: #FFFFFF; }}
#Danger {{ color: {C_RED}; border: 1px solid #F0C3C8; }}
#Danger:hover {{ background: {C_REDSOFT}; }}
#Danger:disabled {{ color: #CFA6AB; border-color: #F3E0E2; }}
#ToolBar {{ background: {C_CARD}; border-bottom: 1px solid {C_LINE2}; }}
#SegBtn {{ border: 1px solid {C_LINE}; border-radius: 14px; background: {C_CARD}; padding: 3px 12px; font-size: 8pt; }}
#SegBtn:checked {{ background: {C_BLUE}; color: #FFFFFF; border-color: {C_BLUE}; }}
#GhostBtn {{ border: 1px solid transparent; border-radius: 8px; background: transparent; color: {C_MUTED}; padding: 2px 7px; }}
#GhostBtn:hover {{ color: {C_INK}; border-color: {C_LINE}; background: #F5F7FC; }}
#GhostBtn:disabled {{ color: #C4CAD6; background: transparent; border-color: transparent; }}
#Progress {{ border: 0; border-radius: 4px; background: #E8ECF4; max-height: 8px; min-height: 8px; }}
#Progress::chunk {{ background: {C_BLUE}; border-radius: 4px; }}
#StatusText {{ font-family: "{FONT_MONO}"; font-size: 7.5pt; color: {C_MUTED}; }}
#GaugeLabel {{ color: {C_MUTED}; font-size: 7.5pt; font-family: "{FONT_MONO}"; }}
#MergeBtn {{ border: 1px solid {C_BLUE}; border-radius: 8px; background: #FFFFFF; color: {C_BLUE}; padding: 4px 10px; font-size: 8.5pt; font-weight: 600; }}
#MergeBtn:hover {{ background: {C_BLUESOFT}; }}
#MergeBtn:pressed {{ background: #DCE6FF; }}
#MergeBtn::menu-indicator {{ image: none; width: 0px; }}
QSplitter::handle:vertical {{ background: #E3E8F0; }}
QSplitter::handle:vertical:hover {{ background: {C_BLUE}; }}

#ColHead {{ background: {C_CARD}; border-bottom: 1px solid {C_LINE}; }}
#ColHead QLabel {{ color: {C_MUTED}; font-size: 7.5pt; }}

QScrollArea {{ border: 0; background: {C_CARD}; }}
#CmpBody {{ background: {C_CARD}; }}
#Band {{ background: {C_BAND}; border-top: 1px solid {C_LINE}; border-bottom: 1px solid {C_LINE2}; }}
#BandTitle {{ font-size: 9pt; font-weight: 600; letter-spacing: .4px; }}
#BandMeta {{ color: {C_MUTED}; font-size: 7.5pt; font-family: "{FONT_MONO}"; }}
#GroupBar {{ background: #F8FAFE; border-bottom: 1px solid {C_LINE2}; }}
#GroupBar QLabel {{ color: #8B93A3; font-size: 7.5pt; font-family: "{FONT_MONO}"; }}
#Row {{ border-bottom: 1px solid {C_HAIR}; background: {C_CARD}; }}
#Row:hover {{ background: #F7F9FD; }}
#RowSel {{ background: {C_BLUESOFT}; border-bottom: 1px solid {C_HAIR}; }}
#RowDone {{ background: #FAFBFD; border-bottom: 1px solid {C_HAIR}; }}
#Cell {{ background: transparent; }}
#Void {{ background: transparent; color: {C_FAINT}; font-size: 8pt; }}
#Divider {{ background: {C_LINE2}; }}
#FileName {{ font-size: 8.5pt; color: {C_INK}; }}
#FileNameDone {{ font-size: 8.5pt; color: #A0A7B4; }}
#FileMeta {{ font-family: "{FONT_MONO}"; font-size: 7.5pt; color: {C_MUTED}; }}
#FileDir {{ font-family: "{FONT_MONO}"; font-size: 7.5pt; color: #97A0B0; }}
#DoneTag {{ color: {C_BLUE}; font-size: 7.5pt; }}
#Thumb {{ background: #EDF1F7; border: 1px solid {C_LINE}; border-radius: 4px; }}
#TypeBox {{ background: #F4F6FB; border: 1px solid {C_LINE}; color: #7B8393; font-family: "{FONT_MONO}"; font-size: 7pt; }}
#PlayTag {{ color: #FFFFFF; background: rgba(20,24,34,.7); font-family: "{FONT_MONO}"; font-size: 7pt; }}
#DurTag {{ color: #FFFFFF; background: rgba(20,24,34,.7); font-family: "{FONT_MONO}"; font-size: 7pt; }}

QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #C6CEDC; border-radius: 4px; background: #FFFFFF; }}
QCheckBox::indicator:checked {{ background: {C_BLUE}; border-color: {C_BLUE}; }}
QCheckBox::indicator:disabled {{ background: #F0F2F7; border-color: #E1E5EE; }}
QCheckBox {{ color: {C_INK2}; font-size: 8pt; spacing: 5px; }}

#BatchBar {{ background: {C_CARD}; border-top: 1px solid {C_LINE}; }}
#StatusBar {{ background: {C_CARD}; border-top: 1px solid {C_LINE2}; }}
#StatusBar QLabel {{ color: #8B93A3; font-family: "{FONT_MONO}"; font-size: 7.5pt; }}
#SelSum {{ font-size: 8.5pt; color: {C_INK2}; }}

#LogBar {{ background: {C_PAPER}; }}
#LogPane {{ background: #FFFFFF; border: 1px solid {C_LINE}; border-radius: 12px; }}
#LogHead {{ background: transparent; }}
#LogText {{
    background: #FCFDFF; border: 0; border-top: 1px solid {C_LINE2};
    font-family: "{FONT_MONO}"; font-size: 8pt; color: #3B4250; padding: 4px 8px;
}}

QDialog {{ background: {C_CARD}; }}
#DlgHead {{ background: #FBFCFE; border-bottom: 1px solid {C_LINE}; }}
#DlgTitle {{ font-weight: 600; font-size: 9pt; letter-spacing: .3px; }}
#DlgSub {{ color: {C_MUTED}; font-family: "{FONT_MONO}"; font-size: 7.5pt; }}
#DlgFoot {{ background: #FBFCFE; border-top: 1px solid {C_LINE}; }}
#LogTable {{ font-family: "{FONT_MONO}"; font-size: 7.5pt; }}
QSpinBox {{ border: 1px solid {C_LINE}; border-radius: 6px; background: {C_CARD}; padding: 1px 4px; font-family: "{FONT_MONO}"; font-size: 8pt; }}
QLineEdit#Exclude {{ border: 1px solid {C_LINE}; border-radius: 8px; background: #F7F9FD; padding: 2px 8px; font-family: "{FONT_MONO}"; font-size: 8pt; }}
QToolTip {{ background: #22262E; color: #F3F5F9; border: 0; border-radius: 6px; padding: 4px 7px; font-size: 8pt; }}
"""


class ElidedLabel(QLabel):
    """按控件宽度省略显示，tooltip 保留全文。"""

    def __init__(self, text: str = "", color: str = C_INK, size: int = 9, mono: bool = False, parent=None):
        super().__init__(parent)
        self._full = text
        self._color = QColor(color)
        self.setToolTip(text)
        f = QFont(FONT_MONO if mono else FONT_UI)
        f.setPointSize(size)
        self.setFont(f)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setFixedHeight(QFontMetrics(f).height() + 1)

    def setText(self, t: str):
        self._full = t
        self.setToolTip(t)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setPen(self._color)
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        p.drawText(
            0, 0, self.width(), self.height(), Qt.AlignLeft | Qt.AlignVCenter,
            fm.elidedText(self._full, Qt.ElideMiddle, self.width()),
        )


# ============================================================
# 顶端监控（仪表盘卡片）
# ============================================================
class Sparkline(QWidget):
    """迷你面积图，风格对齐仪表盘里的折线卡片。"""

    def __init__(self, color: str = C_BLUE, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._vals: list[float] = []
        self.setMinimumHeight(34)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, vals: list[float]):
        self._vals = list(vals)[-90:]
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(0, 2, 0, -2)
        if r.width() <= 2 or r.height() <= 2:
            return
        p.setPen(QColor(C_LINE2))
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        if len(self._vals) < 2:
            return
        mx = max(max(self._vals), 1e-6)
        n = len(self._vals)
        step = r.width() / (n - 1)
        pts = []
        for i, v in enumerate(self._vals):
            x = r.left() + i * step
            y = r.bottom() - min(1.0, v / mx) * r.height()
            pts.append((x, y))
        from PySide6.QtGui import QPainterPath
        fill = QColor(self._color)
        fill.setAlpha(46)
        path = QPainterPath()
        path.moveTo(pts[0][0], r.bottom())
        for x, y in pts:
            path.lineTo(x, y)
        path.lineTo(pts[-1][0], r.bottom())
        path.closeSubpath()
        p.fillPath(path, fill)
        pen = p.pen()
        pen.setColor(self._color)
        pen.setWidthF(1.4)
        p.setPen(pen)
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        p.drawPath(line)


class _ShadowCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(16)
        eff.setOffset(0, 3)
        eff.setColor(QColor(24, 36, 64, 22))
        self.setGraphicsEffect(eff)


class MonitorBand(QWidget):
    """顶端任务运行监控：整体进度 / 本工具 CPU / 内存 / 磁盘 IO。"""

    def __init__(self, pbar: QProgressBar, parent=None):
        super().__init__(parent)
        self.setObjectName("MonitorBand")
        self._collapsed = False
        self.pbar = pbar

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 8)
        outer.setSpacing(0)

        row = QHBoxLayout()
        row.setSpacing(9)
        outer.addLayout(row)

        # 卡片容器：折叠时高度归零，但容器本身仍占满横向空间，
        # 这样右上角的收起按钮位置不会跳动。
        self.cards_host = QWidget()
        self.cards_host.setObjectName("MonitorBand")
        cr = QHBoxLayout(self.cards_host)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(12)
        row.addWidget(self.cards_host, 1)

        self.cards: list[QFrame] = []

        # ---- 卡 1：整体比对进度 ----
        c1 = _ShadowCard()
        v1 = QVBoxLayout(c1)
        v1.setContentsMargins(14, 10, 14, 10)
        v1.setSpacing(6)
        h1 = QHBoxLayout()
        t1 = QLabel("整体比对进度")
        t1.setObjectName("CardTitle")
        self.pill = QLabel("空闲")
        self.pill.setObjectName("PillIdle")
        self.pill.setFixedHeight(19)
        h1.addWidget(t1)
        h1.addWidget(self.pill, 0)
        h1.addStretch(1)
        self.lb_peak1 = QLabel("")
        self.lb_peak1.setObjectName("CardPeak")
        h1.addWidget(self.lb_peak1)
        v1.addLayout(h1)

        h2 = QHBoxLayout()
        h2.setSpacing(4)
        self.lb_pct = QLabel("0")
        self.lb_pct.setObjectName("MetricBig")
        u2 = QLabel("%")
        u2.setObjectName("MetricUnit")
        h2.addWidget(self.lb_pct, 0, Qt.AlignBottom)
        h2.addWidget(u2, 0, Qt.AlignBottom)
        h2.addStretch(1)
        self.lb_files = QLabel("—")
        self.lb_files.setObjectName("CardFoot")
        h2.addWidget(self.lb_files, 0, Qt.AlignBottom)
        v1.addLayout(h2)

        self.pbar_file = QProgressBar()
        self.pbar_file.setObjectName("Progress")
        self.pbar_file.setTextVisible(False)
        self.pbar_file.setRange(0, 1000)
        for text, bar in (("整体", self.pbar), ("单文件", self.pbar_file)):
            g = QHBoxLayout()
            g.setSpacing(6)
            gl = QLabel(text)
            gl.setObjectName("GaugeLabel")
            gl.setFixedWidth(36)
            g.addWidget(gl)
            g.addWidget(bar, 1)
            v1.addLayout(g)
        self.lb_cur = ElidedLabel("等待开始", C_MUTED, 7.5, mono=True)
        v1.addWidget(self.lb_cur)
        cr.addWidget(c1, 3)
        self.cards.append(c1)

        # ---- 卡 2：CPU ----
        c2 = _ShadowCard()
        v2 = QVBoxLayout(c2)
        v2.setContentsMargins(14, 10, 14, 10)
        v2.setSpacing(6)
        hh = QHBoxLayout()
        t2 = QLabel("本工具 CPU")
        t2.setObjectName("CardTitle")
        hh.addWidget(t2)
        hh.addStretch(1)
        self.lb_peak2 = QLabel("峰值 0%")
        self.lb_peak2.setObjectName("CardPeak")
        hh.addWidget(self.lb_peak2)
        v2.addLayout(hh)
        hb = QHBoxLayout()
        hb.setSpacing(4)
        self.lb_cpu = QLabel("0.0")
        self.lb_cpu.setObjectName("MetricBig")
        u = QLabel("%")
        u.setObjectName("MetricUnit")
        hb.addWidget(self.lb_cpu, 0, Qt.AlignBottom)
        hb.addWidget(u, 0, Qt.AlignBottom)
        hb.addStretch(1)
        self.lb_cpusys = QLabel("系统 —")
        self.lb_cpusys.setObjectName("CardFoot")
        hb.addWidget(self.lb_cpusys, 0, Qt.AlignBottom)
        v2.addLayout(hb)
        self.spark_cpu = Sparkline(C_BLUE)
        v2.addWidget(self.spark_cpu)
        self.lb_cpufoot = ElidedLabel("4 线程并行哈希", C_MUTED, 7.5, mono=True)
        v2.addWidget(self.lb_cpufoot)
        cr.addWidget(c2, 2)
        self.cards.append(c2)

        # ---- 卡 3：内存 ----
        c3 = _ShadowCard()
        v3 = QVBoxLayout(c3)
        v3.setContentsMargins(14, 10, 14, 10)
        v3.setSpacing(6)
        hh = QHBoxLayout()
        t3 = QLabel("本工具内存")
        t3.setObjectName("CardTitle")
        hh.addWidget(t3)
        hh.addStretch(1)
        self.lb_peak3 = QLabel("峰值 0 MB")
        self.lb_peak3.setObjectName("CardPeak")
        hh.addWidget(self.lb_peak3)
        v3.addLayout(hh)
        hb = QHBoxLayout()
        hb.setSpacing(4)
        self.lb_mem = QLabel("0")
        self.lb_mem.setObjectName("MetricBig")
        u3 = QLabel("MB")
        u3.setObjectName("MetricUnit")
        hb.addWidget(self.lb_mem, 0, Qt.AlignBottom)
        hb.addWidget(u3, 0, Qt.AlignBottom)
        hb.addStretch(1)
        self.lb_memsys = QLabel("系统 —")
        self.lb_memsys.setObjectName("CardFoot")
        hb.addWidget(self.lb_memsys, 0, Qt.AlignBottom)
        v3.addLayout(hb)
        self.membar = QProgressBar()
        self.membar.setObjectName("Progress")
        self.membar.setTextVisible(False)
        self.membar.setRange(0, 100)
        v3.addWidget(self.membar)
        self.lb_memfoot = ElidedLabel("进程工作集", C_MUTED, 7.5, mono=True)
        v3.addWidget(self.lb_memfoot)
        cr.addWidget(c3, 2)
        self.cards.append(c3)

        # ---- 卡 4：磁盘 IO ----
        c4 = _ShadowCard()
        v4 = QVBoxLayout(c4)
        v4.setContentsMargins(14, 10, 14, 10)
        v4.setSpacing(6)
        hh = QHBoxLayout()
        t4 = QLabel("磁盘 IO · 本工具")
        t4.setObjectName("CardTitle")
        hh.addWidget(t4)
        hh.addStretch(1)
        self.lb_peak4 = QLabel("峰值 0 MB/s")
        self.lb_peak4.setObjectName("CardPeak")
        hh.addWidget(self.lb_peak4)
        v4.addLayout(hh)
        hb = QHBoxLayout()
        hb.setSpacing(4)
        self.lb_io = QLabel("0.0")
        self.lb_io.setObjectName("MetricBig")
        u4 = QLabel("MB/s")
        u4.setObjectName("MetricUnit")
        hb.addWidget(self.lb_io, 0, Qt.AlignBottom)
        hb.addWidget(u4, 0, Qt.AlignBottom)
        hb.addStretch(1)
        self.lb_iow = QLabel("写 —")
        self.lb_iow.setObjectName("CardFoot")
        hb.addWidget(self.lb_iow, 0, Qt.AlignBottom)
        v4.addLayout(hb)
        self.spark_io = Sparkline(C_OK)
        v4.addWidget(self.spark_io)
        self.lb_iofoot = ElidedLabel("累计读 0 B", C_MUTED, 7.5, mono=True)
        v4.addWidget(self.lb_iofoot)
        cr.addWidget(c4, 2)
        self.cards.append(c4)

        # 折叠按钮（28×28 整个区域可点，位置固定右上角）
        self.btn_collapse = QToolButton()
        self.btn_collapse.setText("收起 ▴")
        self.btn_collapse.setToolTip("收起 / 展开监控面板")
        self.btn_collapse.setCursor(Qt.PointingHandCursor)
        self.btn_collapse.setFixedSize(64, 28)
        self.btn_collapse.setStyleSheet(
            f"QToolButton {{ border:1px solid {C_LINE}; border-radius:14px; background:#FFFFFF;"
            f" color:{C_MUTED}; font-size:8pt; padding:0; }}"
            f"QToolButton:hover {{ color:{C_BLUE}; border-color:{C_BLUE}; background:{C_BLUESOFT}; }}"
            f"QToolButton:pressed {{ background:#DCE6FF; }}"
        )
        self.btn_collapse.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        row.addWidget(self.btn_collapse, 0, Qt.AlignTop)

        # 折叠后的单行摘要
        self.lb_mini = QLabel("")
        self.lb_mini.setObjectName("Mini")
        self.lb_mini.setContentsMargins(2, 6, 2, 6)
        outer.addWidget(self.lb_mini)
        self.lb_mini.hide()

        # 四张卡标题统一行高，保证标题在同一水平线
        for lb in self.findChildren(QLabel):
            if lb.objectName() == "CardTitle":
                lb.setFixedHeight(22)

        self._state = "idle"
        self.set_state("idle")

    # ---- 状态 ----
    def set_state(self, state: str, extra: str = ""):
        self._state = state
        text = {"idle": "空闲", "run": "运行中", "done": "已完成", "abort": "已取消"}.get(state, "空闲")
        self.pill.setText(text)
        self.pill.setObjectName({"idle": "PillIdle", "run": "PillRun",
                                 "done": "PillDone", "abort": "PillIdle"}.get(state, "PillIdle"))
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)
        if state == "idle":
            self.lb_pct.setText("0")
            self.lb_files.setText("—")
            self.lb_cur.setText(extra or "等待开始")
            self.pbar.setValue(0)
        elif state == "done":
            self.lb_pct.setText("100")
            self.lb_cur.setText(extra or "比对完成")
        elif state == "abort":
            self.lb_cur.setText(extra or "已取消，未改动任何文件")

    def update_scan(self, p: dict, state: str):
        if p.get("stage") == "enum":
            self.lb_pct.setText("0")
            self.lb_files.setText(f"{p.get('found', 0):,} 个文件")
            self.lb_cur.setText(f"枚举 {p.get('side','')} 侧 · {p.get('dirs',0):,} 个目录 · {os.path.basename(p.get('path',''))}")
            self.pbar.setValue(0)
            self.pbar_file.setValue(0)
            return
        frac = float(p.get("frac") or 0)
        self.lb_pct.setText(f"{frac * 100:.0f}")
        self.pbar.setMaximum(1000)
        self.pbar.setValue(int(frac * 1000))
        self.lb_files.setText(f"{p.get('done_files', 0):,}/{p.get('total_files', 0):,} 个文件")

        # 单文件进度条：跟着当前最大的在算文件走
        big = p.get("biggest")
        if big and big[1] > 0:
            self.pbar_file.setValue(int(min(1.0, big[2] / big[1]) * 1000))
        else:
            self.pbar_file.setValue(0)

        speed = p.get("speed") or 0
        eta = p.get("eta")
        bits = []
        if speed:
            bits.append(f"{speed / 1048576:.1f} MB/s")
        if eta:
            lo, hi = p.get("eta_lo"), p.get("eta_hi")
            fmt = lambda s: f"{int(s) // 60:02d}:{int(s) % 60:02d}"
            if lo and hi:
                bits.append(f"剩 {fmt(lo)} ~ {fmt(hi)}")
            else:
                bits.append(f"剩 {fmt(eta)}")
        bits.append(f"{human_size(p.get('done_bytes', 0))}/{human_size(p.get('total_bytes', 0))}")
        if big and big[1] >= 20971520:
            bits.append(f"{os.path.basename(big[0])} {big[2] / big[1] * 100:.0f}%")
        elif big:
            bits.append(os.path.basename(big[0]))
        self.lb_cur.setText(" · ".join(bits))
        self.set_state(state)

    def update_probe(self, pr: dict, probe: "SysProbe | None" = None, threads: int = 4):
        cpu = pr.get("cpu_proc", 0.0)
        self.lb_cpu.setText(f"{cpu:.1f}" if cpu < 10 else f"{cpu:.0f}")
        self.lb_cpusys.setText(f"系统 {pr.get('cpu_sys', 0):.0f}%")
        hist_cpu = probe.cpu_hist if probe else []
        hist_io = probe.io_hist if probe else []
        peak_cpu = probe.cpu_peak if probe else 0.0
        peak_io = probe.io_peak if probe else 0.0
        self.lb_peak2.setText(f"峰值 {peak_cpu:.0f}%")
        self.spark_cpu.set_values(hist_cpu)
        self.lb_cpufoot.setText(f"{threads} 线程哈希 · 系统 {int(pr.get('cpu_sys', 0))}%")

        self.lb_mem.setText(f"{pr.get('rss_mb', 0):.0f}")
        self.lb_memsys.setText(f"系统 {pr.get('mem_load', 0)}%")
        self.lb_peak3.setText(f"峰值 {pr.get('peak_rss_mb', 0):.0f} MB")
        self.membar.setValue(int(pr.get("mem_load", 0)))
        self.lb_memfoot.setText(
            f"系统已用 {pr.get('mem_load', 0)}% · 进程 {pr.get('rss_mb', 0):.0f} MB / 物理 {pr.get('mem_total_gb', 0):.0f} GB"
        )

        rd = pr.get("io_r_bps", 0) / 1048576
        wr = pr.get("io_w_bps", 0) / 1048576
        self.lb_io.setText(f"{rd:.1f}" if rd < 100 else f"{rd:.0f}")
        self.lb_iow.setText(f"写 {wr:.1f} MB/s")
        self.lb_peak4.setText(f"峰值 {peak_io / 1048576:.1f} MB/s")
        self.spark_io.set_values(hist_io)
        self.lb_iofoot.setText(f"累计读 {human_size(pr.get('read_total', 0))} · 写 {human_size(pr.get('write_total', 0))}")
        self.lb_mini.setText(
            f"进度 {self.lb_pct.text()}% · CPU {self.lb_cpu.text()}% · 内存 {self.lb_mem.text()} MB"
            f" · IO 读 {self.lb_io.text()} MB/s · 写 {wr:.1f} MB/s"
        )

    def set_collapsed(self, on: bool):
        self._collapsed = on
        for c in self.cards:
            c.setVisible(not on)
        if on:
            self.cards_host.setFixedHeight(0)
        else:
            self.cards_host.setMinimumHeight(0)
            self.cards_host.setMaximumHeight(16777215)
        self.lb_mini.setVisible(on)
        self.btn_collapse.setText("展开 ▾" if on else "收起 ▴")
        if on:
            self.setFixedHeight(46)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
        self.cards_host.updateGeometry()
        if self.cards_host.layout():
            self.cards_host.layout().activate()
        self.layout().activate()


# ============================================================
# 本地运行日志
# ============================================================
class LogPane(QWidget):
    """底部运行日志：界面里看流式事件，同时落盘到 logs\\运行日志_日期.log。"""

    LEVEL_COLOR = {"INFO": "#3B4250", "OK": C_OK, "RUN": C_BLUE, "WARN": "#C77A16", "ERR": C_RED}

    def __init__(self, log_dir: str, parent=None):
        super().__init__(parent)
        self.setObjectName("LogBar")
        self.log_dir = log_dir
        self._collapsed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 0, 14, 8)
        outer.setSpacing(0)
        pane = QFrame()
        pane.setObjectName("LogPane")
        self.pane = pane
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(1, 1, 1, 1)
        pv.setSpacing(0)

        head = QWidget()
        head.setObjectName("LogHead")
        head.setFixedHeight(30)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 2, 6, 2)
        hl.setSpacing(8)
        t = QLabel("运行日志")
        t.setObjectName("CardTitle")
        hl.addWidget(t)
        self.lb_path = ElidedLabel("", C_FAINT, 7, mono=True)
        hl.addWidget(self.lb_path, 1)
        self.btn_open = QPushButton("打开目录")
        self.btn_open.setObjectName("GhostBtn")
        self.btn_open.clicked.connect(self._open_dir)
        hl.addWidget(self.btn_open)
        self.btn_clear = QPushButton("清空视图")
        self.btn_clear.setObjectName("GhostBtn")
        self.btn_clear.clicked.connect(lambda: self.text.clear())
        hl.addWidget(self.btn_clear)
        self.btn_fold = QPushButton("收起")
        self.btn_fold.setObjectName("GhostBtn")
        self.btn_fold.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        hl.addWidget(self.btn_fold)
        pv.addWidget(head)

        self.text = QPlainTextEdit()
        self.text.setObjectName("LogText")
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(4000)
        self.text.setMinimumHeight(56)
        pv.addWidget(self.text, 1)
        outer.addWidget(pane)

        self.set_log_dir(log_dir)

    def set_log_dir(self, d: str):
        self.log_dir = d
        name = f"运行日志_{datetime.now().strftime('%Y-%m-%d')}.log"
        self.lb_path.setText(os.path.join(d, name))

    def _open_dir(self):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            os.startfile(self.log_dir)
        except Exception:
            pass

    def set_collapsed(self, on: bool):
        self._collapsed = on
        self.text.setVisible(not on)
        self.btn_fold.setText("展开" if on else "收起")
        if on:
            self.pane.setFixedHeight(32)
        else:
            self.pane.setMinimumHeight(32)
            self.pane.setMaximumHeight(16777215)

    def append_line(self, stamp: str, level: str, msg: str):
        import html
        color = self.LEVEL_COLOR.get(level, "#3B4250")
        tag = {"INFO": "·", "OK": "✓", "RUN": "▶", "WARN": "!", "ERR": "×"}.get(level, "·")
        safe = html.escape(msg)
        sb = self.text.verticalScrollBar()
        stick = sb.value() >= sb.maximum() - 6     # 用户往上翻看时不抢滚动位置
        self.text.appendHtml(
            f'<span style="color:#9AA1AC">{html.escape(stamp)}</span> '
            f'<span style="color:{color};font-weight:600">{tag}</span> '
            f'<span style="color:{color}">{safe}</span>'
        )
        if stick:
            sb.setValue(sb.maximum())


# ============================================================
# 界面组件
# ============================================================
class ClickLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cb = None

    def set_click(self, cb):
        self._cb = cb

    def mousePressEvent(self, ev):
        if self._cb:
            self._cb()


class ThumbService(QObject):
    """后台生成缩略图：只在行真正被绘制时才请求，避免一次性解码上百张图把界面拖死。"""

    _inst = None

    @classmethod
    def inst(cls) -> "ThumbService":
        if cls._inst is None:
            cls._inst = ThumbService()
        return cls._inst

    def __init__(self):
        super().__init__()
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.done: dict[tuple, QImage | None] = {}
        self.waiting: dict[tuple, list] = {}
        self.pix: dict[tuple, QPixmap] = {}
        self._threads = []
        for _ in range(2):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._flush)
        self.timer.start(100)

    # ---- 工作线程 ----
    @staticmethod
    def _key(rec: "FileRec") -> tuple:
        try:
            mt = os.path.getmtime(win_path(rec.path))
        except OSError:
            mt = 0
        return (rec.path, mt, rec.size)

    @staticmethod
    def _render(rec: "FileRec", w: int, h: int) -> QImage | None:
        ext = os.path.splitext(rec.path)[1].lower()
        img = shell_thumbnail(rec.path, 192)
        if img is None and ext in IMG_EXT:
            img = QImage(win_path(rec.path))
            if img.isNull():
                img = None
        if img is None:
            return None
        scaled = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        return scaled.copy(x, y, w, h)

    def _worker(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            rec, w, h, key = job
            try:
                img = self._render(rec, w, h)
            except Exception:
                img = None
            with self.lock:
                self.done[key] = img

    # ---- GUI 线程 ----
    def request(self, rec: "FileRec", cb, w: int = 56, h: int = 40):
        key = self._key(rec)
        if key in self.pix:
            cb(self.pix[key], None)
            return
        with self.lock:
            if key in self.done:
                img = self.done[key]
            else:
                img = "pending"
                self.waiting.setdefault(key, []).append((cb, rec))
                self.q.put((rec, w, h, key))
        if img != "pending":
            self._deliver(key, img, rec)

    def _deliver(self, key: tuple, img: QImage | None, rec=None):
        pm = None
        if img is not None and not img.isNull():
            pm = QPixmap.fromImage(img)
            self.pix[key] = pm
        dur = ""
        if rec is not None and pm is not None and os.path.splitext(rec.name)[1].lower() in VID_EXT:
            try:
                dur = mp4_duration(rec.path)
            except Exception:
                dur = ""
        for cb, r in self.waiting.pop(key, []):
            try:
                cb(pm, dur)
            except Exception:
                pass

    def _flush(self):
        with self.lock:
            keys = [k for k in self.done if self.waiting.get(k)]
            items = [(k, self.done.pop(k)) for k in keys]
        for key, img in items:
            waiters = self.waiting.get(key) or []
            rec = waiters[0][1] if waiters else None
            if rec is not None:
                self._deliver(key, img, rec)


class ThumbLabel(ClickLabel):
    """56×40 缩略图：先在 paintEvent 里画类型占位，图片由后台线程补上。"""

    def __init__(self, rec: FileRec, cache: dict, parent=None):
        super().__init__(parent)
        self.rec = rec
        self.cache = cache
        self.setFixedSize(56, 40)
        self._requested = False
        ext = os.path.splitext(rec.name)[1].lower()
        self._ext = ext
        self._media = ext in IMG_EXT or ext in VID_EXT
        self.setObjectName("Thumb" if self._media else "TypeBox")
        self.setScaledContents(False)
        self.setAlignment(Qt.AlignCenter)
        if self._media:
            self.setText("")
        else:
            label = ext.lstrip(".").upper()[:4] or "FILE"
            self.setText(label)
            f = QFont(FONT_MONO)
            f.setPointSize(7)
            self.setFont(f)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self._media or self._requested:
            return
        self._requested = True
        ThumbService.inst().request(self.rec, self._apply)

    def _apply(self, pm, dur):
        if pm is None:
            # 取不到系统缩略图，退回类型块 + 播放标记
            self.setObjectName("TypeBox")
            label = self._ext.lstrip(".").upper()[:4] or "FILE"
            self.setText(label)
            f = QFont(FONT_MONO)
            f.setPointSize(7)
            self.setFont(f)
            if self._ext in VID_EXT:
                self._add_overlay("▶", right=False)
            return
        self.setPixmap(pm)
        if self._ext in VID_EXT:
            self._add_overlay("▶", right=False)
            if dur:
                self._add_overlay(dur, right=True)

    def _cover(self, pm: QPixmap, w: int, h: int) -> QPixmap:
        scaled = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        return scaled.copy(x, y, w, h)

    def _add_overlay(self, text: str, right: bool):
        lbl = QLabel(text, self)
        lbl.setObjectName("PlayTag" if right else "DurTag")
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lbl.adjustSize()
        if right:
            lbl.move(self.width() - lbl.width() - 2, self.height() - lbl.height() - 1)
        else:
            lbl.move(3, self.height() - lbl.height() - 1)


class FileCell(QFrame):
    def __init__(self, rec: FileRec, checked: bool, done: bool, on_toggle, on_thumb, cache, parent=None):
        super().__init__(parent)
        self.rec = rec
        self._on_toggle = on_toggle
        self.setObjectName("Cell")
        self.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 4, 7, 4)
        lay.setSpacing(7)

        self.cb = QCheckBox()
        self.cb.setFixedWidth(20)
        self.cb.setChecked(checked)
        self.cb.setEnabled(not done)
        self.cb.toggled.connect(self._toggled)
        lay.addWidget(self.cb, 0, Qt.AlignVCenter)

        th = ThumbLabel(rec, cache)
        th.set_click(lambda: on_thumb(rec))
        lay.addWidget(th, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(1)
        name = ElidedLabel(rec.name, C_INK if not done else "#9A9389", 8.5)
        meta = ElidedLabel(f"{human_size(rec.size)} · {fmt_time(rec.mtime)}", C_MUTED, 7.5, mono=True)
        folder = ElidedLabel(rec.folder, "#948D83", 7.5, mono=True)
        box.addWidget(name)
        box.addWidget(meta)
        box.addWidget(folder)
        if done:
            tag = QLabel("已处理（本次会话）")
            tag.setObjectName("DoneTag")
            box.addWidget(tag)
        lay.addLayout(box, 1)

    def _toggled(self, val: bool):
        ok = self._on_toggle(self.rec.path, val, self)
        if not ok:
            self.cb.blockSignals(True)
            self.cb.setChecked(not val)
            self.cb.blockSignals(False)
            return
        parent = self.parentWidget()
        if hasattr(parent, "refresh_style"):
            parent.refresh_style()

    def mouseDoubleClickEvent(self, ev):
        try:
            os.startfile(win_path(self.rec.path))
        except Exception:
            pass


class RowWidget(QFrame):
    """一行 = 左格 + 1px 中缝 + 右格，行级背景统一（选中高亮覆盖整行）。"""

    def __init__(self, lrec, rrec, lv, rv, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("Row")
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        if lrec:
            self.left = FileCell(lrec, lrec.path in win.selected, lrec.path in win.done,
                                 win._toggle, win._open_thumb, win.thumb_cache)
        else:
            self.left = VoidCell(lv)
        lay.addWidget(self.left, 1)
        dv = QFrame()
        dv.setFixedWidth(1)
        dv.setStyleSheet(f"background:{C_LINE2};")
        lay.addWidget(dv)
        if rrec:
            self.right = FileCell(rrec, rrec.path in win.selected, rrec.path in win.done,
                                  win._toggle, win._open_thumb, win.thumb_cache)
        else:
            self.right = VoidCell(rv)
        lay.addWidget(self.right, 1)

    def refresh_style(self):
        sel = any(isinstance(c, FileCell) and c.cb.isChecked() for c in (self.left, self.right))
        self.setObjectName("RowSel" if sel else "Row")
        self.style().unpolish(self)
        self.style().polish(self)


class VoidCell(QFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Void")
        self.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 8, 4)
        lay.setSpacing(8)
        dash = QFrame()
        dash.setFixedSize(16, 1)
        dash.setStyleSheet("background:#DCD5C7;")
        lay.addWidget(dash, 0, Qt.AlignVCenter)
        lbl = ElidedLabel(text, C_FAINT, 8)
        lay.addWidget(lbl, 1)


class CompareView(QScrollArea):
    """左右共用一个滚动轨道，滚任意一侧另一侧必然同步，行高统一所以不会错位。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body = QWidget()
        self.body.setObjectName("CmpBody")
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.setWidget(self.body)
        self._specs: list[dict] = []
        self._idx = 0
        self._builder = None
        self.verticalScrollBar().valueChanged.connect(self._maybe_more)
        self.setStyleSheet(f"QScrollArea{{border:0;background:{C_CARD};}} #CmpBody{{background:{C_CARD};}}")

    def set_specs(self, specs: list[dict], builder):
        self._specs = specs
        self._idx = 0
        self._builder = builder
        self.body.setUpdatesEnabled(False)
        try:
            while self.lay.count():
                item = self.lay.takeAt(0)
                w = item.widget()
                if w:
                    w.hide()
                    w.deleteLater()
        finally:
            self.body.setUpdatesEnabled(True)
        self.verticalScrollBar().setValue(0)
        self.append_chunk(60)

    def append_chunk(self, n: int = 40):
        end = min(len(self._specs), self._idx + n)
        self.body.setUpdatesEnabled(False)
        try:
            for i in range(self._idx, end):
                w = self._builder(self._specs[i])
                if w is not None:
                    self.lay.addWidget(w)
        finally:
            self.body.setUpdatesEnabled(True)
        self._idx = end

    def _maybe_more(self, v):
        bar = self.verticalScrollBar()
        if self._idx < len(self._specs) and v >= bar.maximum() - 400:
            self.append_chunk(40)


# ============================================================
# 通用弹窗
# ============================================================
class ListDialog(QDialog):
    """处置 / 归并预览：逐条列出，确认后返回 accepted。"""

    def __init__(self, title, subtitle, entries, note, confirm_text, danger=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(760)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QWidget()
        head.setObjectName("DlgHead")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(14, 11, 14, 11)
        hl.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("DlgTitle")
        s = QLabel(subtitle)
        s.setObjectName("DlgSub")
        s.setWordWrap(True)
        hl.addWidget(t)
        hl.addWidget(s)
        lay.addWidget(head)

        body = QScrollArea()
        body.setWidgetResizable(True)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(14, 10, 14, 6)
        il.setSpacing(0)
        for side, text in entries:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 3, 0, 3)
            rl.setSpacing(8)
            if side:
                seal = QLabel(side.upper())
                seal.setFixedSize(17, 17)
                seal.setAlignment(Qt.AlignCenter)
                seal.setObjectName("SealA" if side.upper() == "A" else "SealB")
                rl.addWidget(seal)
            lbl = ElidedLabel(text, C_INK2, 8, mono=True)
            rl.addWidget(lbl, 1)
            il.addWidget(row)
        il.addStretch(1)
        body.setWidget(inner)
        body.setMinimumHeight(260)
        body.setMaximumHeight(400)
        lay.addWidget(body)

        nl = QLabel(note)
        nl.setWordWrap(True)
        nl.setStyleSheet(f"color:{C_MUTED};font-size:8pt;padding:8px 14px;")
        lay.addWidget(nl)

        foot = QWidget()
        foot.setObjectName("DlgFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 9, 12, 9)
        fl.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(confirm_text)
        ok.setObjectName("Danger" if danger else "Primary")
        ok.clicked.connect(self.accept)
        fl.addWidget(cancel)
        fl.addWidget(ok)
        lay.addWidget(foot)
        self.resize(820, 560)


class ResultDialog(QDialog):
    def __init__(self, title, subtitle, summary_html, log_path, undo_cb=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QWidget()
        head.setObjectName("DlgHead")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(14, 11, 14, 11)
        hl.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("DlgTitle")
        s = QLabel(subtitle)
        s.setObjectName("DlgSub")
        hl.addWidget(t)
        hl.addWidget(s)
        lay.addWidget(head)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 12, 14, 10)
        bl.setSpacing(8)
        msg = QLabel(summary_html)
        msg.setWordWrap(True)
        msg.setStyleSheet(
            "background:#F5F7F1;border:1px solid #CFD8C9;padding:10px;color:#3E4A39;font-size:8.5pt;"
        )
        bl.addWidget(msg)
        if log_path:
            lp = QLabel(f"日志已追加：{log_path}")
            lp.setWordWrap(True)
            lp.setStyleSheet(f"color:{C_MUTED};font-family:{FONT_MONO};font-size:7.5pt;")
            bl.addWidget(lp)
        bl.addStretch(1)
        lay.addWidget(body)

        foot = QWidget()
        foot.setObjectName("DlgFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 9, 12, 9)
        fl.addStretch(1)
        if undo_cb:
            undo = QPushButton("撤销本次移动")
            undo.clicked.connect(lambda: (undo_cb(), self.accept()))
            fl.addWidget(undo)
        ok = QPushButton("完成")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        fl.addWidget(ok)
        lay.addWidget(foot)


class PreviewDialog(QDialog):
    def __init__(self, rec: FileRec, parent=None):
        super().__init__(parent)
        self.setWindowTitle(rec.name)
        self.setStyleSheet("QDialog{background:#171614;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet("background:#1E1C19;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 8, 12, 8)
        name = QLabel(rec.name)
        name.setStyleSheet("color:#EAE4D8;font-size:8.5pt;")
        meta = QLabel(f"{human_size(rec.size)}")
        meta.setStyleSheet(f"color:#9C968B;font-family:{FONT_MONO};font-size:7.5pt;")
        btn = QPushButton("关闭")
        btn.setStyleSheet("QPushButton{color:#EAE4D8;background:transparent;border:0;}QPushButton:hover{color:#fff;}")
        btn.clicked.connect(self.accept)
        bl.addWidget(name, 1)
        bl.addWidget(meta)
        bl.addWidget(btn)
        lay.addWidget(bar)

        img = QLabel()
        img.setAlignment(Qt.AlignCenter)
        pm = QPixmap(win_path(rec.path))
        if pm.isNull():
            img.setText("无法预览，可双击使用系统默认程序打开")
            img.setStyleSheet("color:#8E887D;")
        else:
            img.setPixmap(pm.scaled(820, 560, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        img.setStyleSheet("background:#0F0E0D;")
        lay.addWidget(img, 1)

        path = QLabel(rec.path)
        path.setStyleSheet(f"color:#8E887D;font-family:{FONT_MONO};font-size:7.5pt;padding:7px 12px;background:#1E1C19;")
        lay.addWidget(path)


# ============================================================
# 主窗口
# ============================================================
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VER}")
        self.setStyleSheet(QSS)
        self.setObjectName("Root")
        self.resize(1340, 1000)
        self.setMinimumSize(1080, 800)

        self.root_a = ""
        self.root_b = ""
        self.result: ScanResult | None = None
        self.worker: ScanWorker | None = None
        self.selected: set[str] = set()
        self.done: set[str] = set()
        self.thumb_cache: dict = {}
        self.by_path: dict[str, FileRec] = {}
        self.band_of: dict[str, Group] = {}
        self.filter = "all"
        self.undo_stack: list[tuple[str, str, str, int, float, str]] = []
        self.log_path = os.path.join(self.app_dir(), "logs")
        self._drag = None
        self._active_paths: list[str] = []
        self._stalled_path: str = ""
        self.probe = SysProbe()
        self._log_fp = None
        self._last_prog: dict | None = None
        self._scan_state = "idle"

        self._build()
        self._load_config()
        self._update_buttons()

        # 性能监控：每秒采样一次本工具自身开销
        self._mon_timer = QTimer(self)
        self._mon_timer.timeout.connect(self._tick_monitor)
        self._mon_timer.start(1000)
        QTimer.singleShot(400, self._tick_monitor)

        self.log("INFO", f"{APP_NAME} {APP_VER} 启动 · 程序目录 {self.app_dir()}")
        self.log("INFO", "离线运行，不联网、不写注册表；判定标准 SHA-256 内容哈希")

    # ---------- 运行日志 ----------
    def _ensure_log_file(self):
        if self._log_fp is not None:
            return
        try:
            os.makedirs(self.log_path, exist_ok=True)
            name = f"运行日志_{datetime.now().strftime('%Y-%m-%d')}.log"
            self._log_fp = open(os.path.join(self.log_path, name), "a", encoding="utf-8")
        except Exception:
            self._log_fp = False

    def log(self, level: str, msg: str):
        """写运行日志：界面流式显示 + 落盘 logs\\运行日志_日期.log。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            self._ensure_log_file()
            if self._log_fp:
                self._log_fp.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{level}\t{msg}\n")
                self._log_fp.flush()
        except Exception:
            pass
        try:
            self.logpane.append_line(stamp, level, msg)
        except Exception:
            pass

    def _tick_monitor(self):
        try:
            pr = self.probe.sample()
            self.band.update_probe(pr, self.probe, threads=4)
        except Exception:
            pass

    # ---------- 路径 ----------
    def app_dir(self) -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def cfg_path(self) -> str:
        return os.path.join(self.app_dir(), "config.json")

    def _load_config(self):
        try:
            with open(self.cfg_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        self.path_a.setText(cfg.get("a", ""))
        self.path_b.setText(cfg.get("b", ""))
        o = cfg.get("opt", {})
        self.cb_recur.setChecked(o.get("recursive", True))
        self.cb_hidden.setChecked(o.get("skip_hidden", True))
        self.cb_zero.setChecked(o.get("skip_zero", True))
        self.sp_min.setValue(int(o.get("min_kb", 0)))
        self.ed_exclude.setText(o.get("exclude", ".tmp;.log;node_modules;.git"))

    def _save_config(self):
        try:
            os.makedirs(self.app_dir(), exist_ok=True)
            with open(self.cfg_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "a": self.path_a.text(), "b": self.path_b.text(),
                    "opt": {
                        "recursive": self.cb_recur.isChecked(),
                        "skip_hidden": self.cb_hidden.isChecked(),
                        "skip_zero": self.cb_zero.isChecked(),
                        "min_kb": self.sp_min.value(),
                        "exclude": self.ed_exclude.text(),
                    },
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def closeEvent(self, ev):
        self._save_config()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        self.log("INFO", f"{APP_NAME} 退出 · 本次会话结束")
        try:
            if self._log_fp:
                self._log_fp.close()
        except Exception:
            pass
        super().closeEvent(ev)

    # ---------- 布局 ----------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶端任务运行监控
        self.pbar = QProgressBar()
        self.pbar.setObjectName("Progress")
        self.pbar.setTextVisible(False)
        self.pbar.setRange(0, 1000)
        self.band = MonitorBand(self.pbar)
        outer.addWidget(self.band)

        # 目录条
        dirbar = QWidget()
        dirbar.setObjectName("DirBar")
        dl = QHBoxLayout(dirbar)
        dl.setContentsMargins(12, 9, 12, 9)
        dl.setSpacing(8)
        dl.addWidget(self._side_block("A"), 1)
        mid = QWidget()
        mid.setFixedWidth(66)
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(6, 0, 6, 0)
        ml.setSpacing(1)
        swap = QToolButton()
        swap.setText("⇄")
        swap.setToolTip("互换左右目录")
        swap.clicked.connect(self._swap)
        sync = QLabel("同步滚动")
        sync.setObjectName("SmallLabel")
        sync.setAlignment(Qt.AlignCenter)
        ml.addWidget(swap, 0, Qt.AlignHCenter)
        ml.addWidget(sync, 0, Qt.AlignHCenter)
        dl.addWidget(mid, 0)
        dl.addWidget(self._side_block("B"), 1)
        outer.addWidget(dirbar)

        # 选项条
        optbar = QWidget()
        ol = QHBoxLayout(optbar)
        ol.setContentsMargins(12, 6, 12, 6)
        ol.setSpacing(10)
        lab = QLabel("扫描选项")
        lab.setObjectName("SmallLabel")
        ol.addWidget(lab)
        self.cb_recur = QCheckBox("递归子目录")
        self.cb_hidden = QCheckBox("跳过隐藏/系统文件")
        self.cb_zero = QCheckBox("跳过零字节")
        for w in (self.cb_recur, self.cb_hidden, self.cb_zero):
            w.setChecked(True)
            ol.addWidget(w)
        ol.addWidget(QLabel("最小"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(0, 1048576)
        self.sp_min.setSuffix(" KB")
        self.sp_min.setFixedWidth(86)
        ol.addWidget(self.sp_min)
        ol.addWidget(QLabel("排除"))
        self.ed_exclude = QLineEdit(".tmp;.log;node_modules;.git")
        self.ed_exclude.setObjectName("Exclude")
        self.ed_exclude.setMinimumWidth(180)
        ol.addWidget(self.ed_exclude, 1)
        note = QLabel("判定：SHA-256 · 内容一致即重复（文件名不参与）")
        note.setObjectName("SmallLabel")
        ol.addWidget(note)
        outer.addWidget(optbar)

        # 工具条
        tb = QWidget()
        tb.setObjectName("ToolBar")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(12, 7, 12, 7)
        tl.setSpacing(8)
        self.btn_scan = QPushButton("开始比对")
        self.btn_scan.setObjectName("Primary")
        self.btn_scan.setMinimumWidth(88)
        self.btn_scan.clicked.connect(self._start_scan)
        tl.addWidget(self.btn_scan)

        self.btn_skip = QPushButton("跳过当前文件")
        self.btn_skip.setObjectName("GhostBtn")
        self.btn_skip.setToolTip("某个文件读不动时，把它记入「跳过」，继续比对其余文件")
        self.btn_skip.setEnabled(False)
        self.btn_skip.clicked.connect(self._skip_current)
        tl.addWidget(self.btn_skip)

        self.st_scan = ElidedLabel("就绪 · 选择两个目录后开始比对", C_MUTED, 7.5, mono=True)
        tl.addWidget(self.st_scan, 1)

        lab2 = QLabel("显示")
        lab2.setObjectName("SmallLabel")
        tl.addWidget(lab2)
        self.seg = {}
        for key, text in (("all", "全部"), ("same", "仅相同"), ("diff", "仅不同")):
            b = QPushButton(text)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self.seg[key] = b
            tl.addWidget(b)

        lab3 = QLabel("快速选")
        lab3.setObjectName("SmallLabel")
        tl.addWidget(lab3)
        b_a = QPushButton("保留 A 侧")
        b_a.clicked.connect(lambda: self._apply_strategy("A"))
        b_b = QPushButton("保留 B 侧")
        b_b.clicked.connect(lambda: self._apply_strategy("B"))
        tl.addWidget(b_a)
        tl.addWidget(b_b)

        self.btn_merge = QToolButton()
        self.btn_merge.setText("归并 ▾")
        self.btn_merge.setCursor(Qt.PointingHandCursor)
        self.btn_merge.setObjectName("MergeBtn")
        self.btn_merge.setMinimumHeight(28)
        self.btn_merge.setMinimumWidth(78)
        self.btn_merge.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_merge.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_merge)
        act_ab = menu.addAction("A 侧独有内容 → 并入 B")
        act_ba = menu.addAction("B 侧独有内容 → 并入 A")
        act_ab.triggered.connect(lambda: self._open_merge("A"))
        act_ba.triggered.connect(lambda: self._open_merge("B"))
        menu.addSeparator()
        act_undo = menu.addAction("撤销上次归并（按操作日志搬回）")
        act_undo.triggered.connect(self._undo_last_merge)
        self.btn_merge.setMenu(menu)
        self.btn_merge.setToolTip("把一侧独有的内容各搬一份到另一侧，使该侧剩余文件全部可在另一侧找到")
        tl.addWidget(self.btn_merge)

        outer.addWidget(tb)

        # 列头
        ch = QWidget()
        ch.setObjectName("ColHead")
        ch.setFixedHeight(24)
        cl = QHBoxLayout(ch)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._col_head("A"), 1)
        dv = QFrame()
        dv.setFixedWidth(1)
        dv.setStyleSheet(f"background:{C_LINE2};")
        cl.addWidget(dv)
        cl.addWidget(self._col_head("B"), 1)
        # 对照区 + 日志区 放进上下可拖拽的分栏，日志高度可自由调整
        self.cmp_host = QWidget()
        clv = QVBoxLayout(self.cmp_host)
        clv.setContentsMargins(0, 0, 0, 0)
        clv.setSpacing(0)
        clv.addWidget(ch)

        self.view = CompareView()
        clv.addWidget(self.view, 1)

        # 批量条
        bb = QWidget()
        bb.setObjectName("BatchBar")
        bl = QHBoxLayout(bb)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)
        self.lb_sel = QLabel("已选 0 个文件 · 0 B")
        self.lb_sel.setObjectName("SelSum")
        bl.addWidget(self.lb_sel)
        hint = QLabel("相同组内每组至少保留 1 份")
        hint.setObjectName("SmallLabel")
        bl.addWidget(hint)
        bl.addStretch(1)
        self.btn_clear = QPushButton("清除选择")
        self.btn_clear.clicked.connect(self._clear_sel)
        self.btn_move = QPushButton("移入隔离区")
        self.btn_move.clicked.connect(lambda: self._open_dispose("move"))
        self.btn_trash = QPushButton("删除到回收站")
        self.btn_trash.setObjectName("Danger")
        self.btn_trash.clicked.connect(lambda: self._open_dispose("trash"))
        self.btn_purge = QPushButton("永久删除")
        self.btn_purge.setObjectName("Danger")
        self.btn_purge.clicked.connect(lambda: self._open_dispose("purge"))
        for w in (self.btn_clear, self.btn_move, self.btn_trash, self.btn_purge):
            bl.addWidget(w)
        clv.addWidget(bb)

        # 本地运行日志
        self.logpane = LogPane(self.log_path)
        self.logpane.set_collapsed(False)
        self.split = QSplitter(Qt.Vertical)
        self.split.setObjectName("MainSplit")
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(6)
        self.split.addWidget(self.cmp_host)
        self.split.addWidget(self.logpane)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([9000, 190])
        outer.addWidget(self.split, 1)

        # 状态栏
        sb = QWidget()
        sb.setObjectName("StatusBar")
        sl = QHBoxLayout(sb)
        sl.setContentsMargins(12, 3, 12, 3)
        sl.setSpacing(14)
        self.st_left = QLabel("● 离线运行 · 未写注册表")
        self.st_mid = QLabel("SHA-256 · 内容一致即重复")
        self.st_count = QLabel("未扫描")
        self.st_stats = QLabel("—")
        self.st_stats.setStyleSheet(f"color:{C_INK2};font-family:{FONT_MONO};font-size:7.5pt;")
        self.lbl_warn = ElidedLabel("", C_RED, 7.5, mono=True)
        sl.addWidget(self.st_left)
        sl.addWidget(self.st_mid)
        sl.addWidget(self.lbl_warn, 2)
        sl.addWidget(self.st_count, 1)
        sl.addWidget(self.st_stats)
        sb.setFixedHeight(24)
        outer.addWidget(sb)

    def _col_head(self, side: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(7, 0, 7, 0)
        lay.setSpacing(7)
        sp = QWidget()
        sp.setFixedWidth(20)
        lay.addWidget(sp)
        prev = QLabel("预览")
        prev.setFixedWidth(56)
        prev.setAlignment(Qt.AlignCenter)
        lay.addWidget(prev)
        txt = QLabel(f"{side} 侧 · 文件名 / 大小 · 修改时间 · 所在目录")
        txt.setStyleSheet(f"color:{C_MUTED};font-size:7.5pt;")
        lay.addWidget(txt, 1)
        return w

    def _side_block(self, side: str) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(200)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        seal = QLabel(side)
        seal.setFixedSize(19, 19)
        seal.setAlignment(Qt.AlignCenter)
        seal.setObjectName("SealA" if side == "A" else "SealB")
        lay.addWidget(seal)
        lbl = QLabel("对比目录")
        lbl.setObjectName("SmallLabel")
        lay.addWidget(lbl)
        edit = QLineEdit()
        edit.setObjectName("PathEdit")
        edit.setReadOnly(True)
        edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        edit.setPlaceholderText(f"选择 {side} 目录…")
        lay.addWidget(edit, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(lambda: self._pick_dir(side))
        lay.addWidget(btn)
        if side == "A":
            self.path_a = edit
        else:
            self.path_b = edit
        return w

    # ---------- 目录 / 选项 ----------
    def _pick_dir(self, side: str):
        path = QFileDialog.getExistingDirectory(self, f"选择 {side} 目录", self.path_a.text() or os.path.expanduser("~"))
        if not path:
            return
        if side == "A":
            self.path_a.setText(os.path.normpath(path))
        else:
            self.path_b.setText(os.path.normpath(path))

    def _swap(self):
        a, b = self.path_a.text(), self.path_b.text()
        self.path_a.setText(b)
        self.path_b.setText(a)
        if self.result:
            self._invalidate("目录已互换，请重新比对")

    def _invalidate(self, msg: str):
        self.result = None
        self.selected.clear()
        self.done.clear()
        self.by_path.clear()
        self.band_of.clear()
        self.view.set_specs([], lambda s: None)
        self.lb_sel.setText("已选 0 个文件 · 0 B")
        self.st_stats.setText("—")
        self._update_buttons()
        if msg:
            self.st_scan.setText(msg)

    # ---------- 扫描 ----------
    def _opt(self) -> dict:
        return {
            "recursive": self.cb_recur.isChecked(),
            "skip_hidden": self.cb_hidden.isChecked(),
            "skip_zero": self.cb_zero.isChecked(),
            "min_kb": self.sp_min.value(),
            "exclude": self.ed_exclude.text(),
        }

    def _start_scan(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.btn_scan.setEnabled(False)
            self.st_scan.setText("正在取消…")
            self.log("WARN", "用户取消比对")
            return
        a, b = self.path_a.text().strip(), self.path_b.text().strip()
        if not a or not b:
            QMessageBox.warning(self, APP_NAME, "请先选择左右两个目录。")
            return
        for p in (a, b):
            if not os.path.isdir(win_path(p)):
                QMessageBox.warning(self, APP_NAME, f"目录不存在或无法访问：\n{p}")
                return
        na, nb = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
        if na == nb:
            QMessageBox.warning(self, APP_NAME, "左右目录是同一个目录，比对没有意义。")
            return
        if na.startswith(nb + os.sep) or nb.startswith(na + os.sep):
            QMessageBox.warning(self, APP_NAME, "左右目录存在父子包含关系，会互相包含文件，请换成互不包含的两个目录。")
            return
        self._save_config()
        self.root_a, self.root_b = os.path.normpath(a), os.path.normpath(b)
        self.selected.clear()
        self.done.clear()
        self.thumb_cache.clear()
        self.pbar.setValue(0)
        self.btn_scan.setText("取消比对")
        self.btn_skip.setEnabled(True)
        self.lbl_warn.setText("")
        self._active_paths = []
        self._log_step = -1
        self._enum_log_ts = 0.0
        self._scan_finished = False
        self.view.set_specs([], lambda s: None)
        self.st_scan.setText("正在枚举文件…")
        self.band.set_state("run", "正在枚举文件…")
        self.probe.cpu_peak = 0.0
        self.probe.io_peak = 0.0
        self.log("RUN", f"开始比对 · A = {a} · B = {b}")
        opt = self._opt()
        self.log("INFO", f"选项 递归={opt['recursive']} 跳过隐藏={opt['skip_hidden']} 跳过零字节={opt['skip_zero']} "
                         f"最小={opt['min_kb']}KB 排除={opt['exclude'] or '—'}")
        self.worker = ScanWorker(a, b, opt)
        self._tick_monitor()      # 先取一次基线，扫描期间的增量才有意义
        self.worker.progress.connect(self._on_progress)
        self.worker.stalled.connect(self._on_stalled)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished_result.connect(self._on_scan_done)
        self.worker.start()

    def _on_progress(self, p: dict):
        if not isinstance(p, dict):
            return
        if getattr(self, "_scan_finished", False):
            return          # 收尾快照晚到时不覆盖「已完成 / 已取消」状态
        self._last_prog = p
        if p.get("stage") == "enum":
            side = p.get("side", "")
            self.st_scan.setText(
                f"枚举 {side} 侧 · 已遍历 {p.get('dirs', 0):,} 个目录 · {p.get('found', 0):,} 个文件 · {p.get('path', '')}"
            )
            try:
                self.band.update_scan(p, "run")
            except Exception:
                pass
            # 枚举阶段每秒打一条，避免日志刷屏
            now = time.time()
            if now - getattr(self, "_enum_log_ts", 0) > 1.0:
                self._enum_log_ts = now
                self.log("RUN", f"枚举 {side} 侧 · {p.get('dirs', 0):,} 目录 / {p.get('found', 0):,} 文件 · {p.get('path', '')}")
            return
        frac = float(p.get("frac") or 0.0)
        self._active_paths = p.get("active") or []
        self.pbar.setMaximum(1000)
        self.pbar.setValue(int(frac * 1000))
        speed = p.get("speed") or 0.0
        eta = p.get("eta")
        eta_txt = ""
        if eta:
            eta_txt = f" · 剩 {int(eta) // 60:02d}:{int(eta) % 60:02d}"
        sp = f"{speed / 1048576:.1f} MB/s" if speed > 0 else "—"
        worst = p.get("worst")
        big = p.get("biggest")
        cur = ""
        if big:
            cur = os.path.basename(big[0])
            if big[1] and big[1] >= 20971520:          # 单文件 >20MB 时连它自己的进度一起显示
                pct = big[2] / big[1] * 100 if big[1] else 0
                cur = f"{cur}（{human_size(big[2])}/{human_size(big[1])} {pct:.0f}%）"
        self.st_scan.setText(
            f"SHA-256 {frac * 100:5.1f}% · {human_size(p.get('done_bytes', 0))} / "
            f"{human_size(p.get('total_bytes', 0))} · {sp}{eta_txt} · "
            f"{p.get('done_files', 0):,}/{p.get('total_files', 0):,} 个文件 · {cur}"
        )
        try:
            self.band.update_scan(p, "run")
        except Exception:
            pass
        # 每跨过 10% 记一条里程碑日志
        step = int(frac * 10)
        if step > getattr(self, "_log_step", -1):
            self._log_step = step
            self.log("RUN", f"哈希进度 {step * 10}% · {p.get('done_files', 0):,}/{p.get('total_files', 0):,} 个文件 · "
                            f"{human_size(p.get('done_bytes', 0))}/{human_size(p.get('total_bytes', 0))}")

    def _on_stalled(self, path: str, secs: float):
        self._stalled_path = path
        self.lbl_warn.setText(f"疑似卡住：{path} · 已 {int(secs)} 秒无进展，可点「跳过当前文件」")
        self.lbl_warn.setToolTip(path)
        self.log("WARN", f"疑似卡住 {int(secs)} 秒无字节进展：{path}")

    def _skip_current(self):
        if not (self.worker and self.worker.isRunning()):
            return
        targets = [p for p in (getattr(self, "_active_paths", []) or []) if p not in self.worker.skip_paths]
        if not targets:
            self.lbl_warn.setText("没有正在读取的文件可跳过")
            return

        # 只有一个文件在算时，直接跳过它，不再弹列表
        if len(targets) == 1:
            self._do_skip(targets)
            return

        # 弹出菜单，逐个文件列出，默认把「卡住最久 / 最大的那个」排在最前
        m = QMenu(self)
        m.setToolTipsVisible(True)
        stuck = getattr(self, "_stalled_path", "") or ""
        targets.sort(key=lambda p: (p != stuck, -_safe_size(p)))
        for p in targets:
            sz = human_size(_safe_size(p))
            act = m.addAction(f"跳过：{os.path.basename(p)}  ({sz})")
            act.setToolTip(p)
            act.triggered.connect(lambda _checked=False, pp=p: self._do_skip([pp]))
        if len(targets) > 1:
            m.addSeparator()
            act_all = m.addAction(f"全部跳过（{len(targets)} 个）")
            act_all.triggered.connect(lambda: self._do_skip(targets))
        m.exec(self.btn_skip.mapToGlobal(self.btn_skip.rect().bottomLeft()))

    def _do_skip(self, paths: list[str]):
        if not (self.worker and self.worker.isRunning()):
            return
        for p in paths:
            self.worker.skip(p)
            self.log("WARN", f"手动跳过：{p}")
        names = "、".join(os.path.basename(p) for p in paths[:3])
        more = f" 等 {len(paths)} 个" if len(paths) > 3 else ""
        self.lbl_warn.setText(f"已跳过：{names}{more}")

    def _on_failed(self, msg: str):
        self._scan_finished = True
        self.btn_scan.setText("开始比对")
        self.btn_scan.setEnabled(True)
        self.btn_skip.setEnabled(False)
        self.st_scan.setText(msg)
        self.band.set_state("abort", msg)
        self.log("ERR", msg)
        QMessageBox.critical(self, APP_NAME, msg)

    def _on_scan_done(self, res: ScanResult):
        self._scan_finished = True
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("重新比对")
        self.btn_skip.setEnabled(False)
        self.lbl_warn.setText("")
        if res.aborted:
            self.st_scan.setText("已取消 · 未产生结果，未改动任何文件")
            self.band.set_state("abort", f"已取消 · 已扫 {res.count_a + res.count_b:,} 个文件，未改动任何文件")
            self.log("WARN", "比对被取消，未产生结果、未改动任何文件")
            self._update_buttons()
            return
        self.result = res
        self._rebuild_index()
        if not res.groups:
            self.st_scan.setText(f"完成 · 两侧没有可比对的文件 · 用时 {res.seconds:.1f}s")
            self.view.set_specs([], lambda s: None)
        else:
            gcount = sum(1 for g in res.groups if len(g.files) > 1)
            copies = sum(len(g.files) for g in res.groups if len(g.files) > 1)
            space = sum(g.releasable for g in res.groups if len(g.files) > 1)
            self.st_scan.setText(
                f"完成 · {gcount} 组重复 / {copies} 个副本 / 可释放 {human_size(space)} · 用时 {res.seconds:.1f}s"
            )
            self.st_stats.setText(f"{gcount} 组 · {copies} 副本 · {human_size(space)}")
            self._render()
        self.st_count.setText(f"已扫描 {res.count_a + res.count_b:,}（A {res.count_a:,} / B {res.count_b:,}）· 跳过 {len(res.skipped)}")
        self.band.set_state("done", f"完成 · {res.count_a + res.count_b:,} 个文件 · 用时 {res.seconds:.1f}s · 峰值 CPU {self.probe.cpu_peak:.0f}%")
        self.band.lb_files.setText(f"{res.count_a + res.count_b:,} 个文件")
        self._tick_monitor()
        gcount = sum(1 for g in res.groups if len(g.files) > 1)
        copies = sum(len(g.files) for g in res.groups if len(g.files) > 1)
        space = sum(g.releasable for g in res.groups if len(g.files) > 1)
        self.log("OK", f"比对完成 · 用时 {res.seconds:.1f}s · A {res.count_a:,} / B {res.count_b:,} 个文件 · "
                       f"{gcount} 组重复 / {copies} 个副本 / 可释放 {human_size(space)} · 跳过 {len(res.skipped)}")
        for p, why in res.skipped[:20]:
            self.log("WARN", f"跳过 {os.path.basename(p) or p}：{why}")
        if len(res.skipped) > 20:
            self.log("WARN", f"……另有 {len(res.skipped) - 20} 条跳过项，见「跳过项」清单")
        self._update_buttons()

    # ---------- 模型 ----------
    def _rebuild_index(self):
        self.by_path = {}
        self.band_of = {}
        if not self.result:
            return
        for g in self.result.groups:
            for f in g.files:
                self.by_path[f.path] = f
                self.band_of[f.path] = g

    def _dup_groups(self) -> list[Group]:
        return [g for g in (self.result.groups if self.result else []) if len(g.files) > 1]

    def _uniq(self, side: str) -> list[FileRec]:
        out = [g.files[0] for g in (self.result.groups if self.result else []) if len(g.files) == 1 and g.files[0].side == side]
        out.sort(key=lambda f: -f.size)
        return out

    def _set_filter(self, key: str):
        self.filter = key
        for k, b in self.seg.items():
            b.setChecked(k == key)
        self._render()

    # ---------- 渲染 ----------
    def _render(self):
        specs: list[dict] = []
        dup = sorted(self._dup_groups(), key=lambda g: -g.releasable)
        st = {
            "g": len(dup),
            "c": sum(len(g.files) for g in dup),
            "s": human_size(sum(g.releasable for g in dup)),
        }
        self.st_stats.setText(f"{st['g']} 组 · {st['c']} 副本 · 可释放 {st['s']}")
        if self.filter != "diff":
            specs.append({
                "kind": "band", "title": "相同文件",
                "meta": f"{st['g']} 组 · {st['c']} 个副本 · 可释放 {st['s']}",
                "hint": "按内容哈希配对，左右逐行对照",
            })
            for g in dup:
                specs.append({"kind": "group", "group": g})
                a = g.by_side("A")
                b = g.by_side("B")
                n = max(len(a), len(b))
                for i in range(n):
                    lv = rv = ""
                    if i >= len(a):
                        lv = "A 侧无对应副本"
                    if i >= len(b):
                        rv = "B 侧无对应副本" if g.has_b else "此内容在 B 侧不存在"
                    specs.append({"kind": "row", "l": a[i] if i < len(a) else None,
                                  "r": b[i] if i < len(b) else None, "lv": lv, "rv": rv})
        if self.filter != "same":
            ua, ub = self._uniq("A"), self._uniq("B")
            specs.append({
                "kind": "band", "title": "仅 A 侧", "meta": f"{len(ua)} 个 · {human_size(sum(f.size for f in ua))}",
                "hint": "B 侧没有同内容文件，可整体并入 B",
                "actions": [("并入 B 侧", lambda: self._open_merge("A"))],
            })
            for f in ua:
                specs.append({"kind": "row", "l": f, "r": None, "lv": "", "rv": "此文件在 B 侧不存在"})
            specs.append({
                "kind": "band", "title": "仅 B 侧", "meta": f"{len(ub)} 个 · {human_size(sum(f.size for f in ub))}",
                "hint": "A 侧没有同内容文件，可整体并入 A",
                "actions": [("并入 A 侧", lambda: self._open_merge("B"))],
            })
            for f in ub:
                specs.append({"kind": "row", "l": None, "r": f, "lv": "此文件在 A 侧不存在", "rv": ""})
            if self.result and self.result.skipped:
                specs.append({
                    "kind": "band", "title": "跳过项", "meta": f"{len(self.result.skipped)} 个",
                    "hint": "不参与判定，也不会被处置",
                })
                for p, why in self.result.skipped:
                    specs.append({"kind": "skip", "path": p, "why": why})
        self.view.set_specs(specs, self._build_item)
        self._refresh_header_flags()

    def _build_item(self, spec: dict):
        kind = spec["kind"]
        if kind == "band":
            w = QWidget()
            w.setObjectName("Band")
            w.setFixedHeight(34)
            lay = QHBoxLayout(w)
            lay.setContentsMargins(12, 0, 12, 0)
            lay.setSpacing(9)
            sq = QFrame()
            sq.setFixedSize(5, 5)
            sq.setStyleSheet(f"background:{C_INK};")
            lay.addWidget(sq, 0, Qt.AlignVCenter)
            t = QLabel(spec["title"])
            t.setObjectName("BandTitle")
            lay.addWidget(t)
            m = QLabel(spec["meta"])
            m.setObjectName("BandMeta")
            lay.addWidget(m)
            h = ElidedLabel(spec.get("hint", ""), C_FAINT, 7.5)
            lay.addWidget(h, 1)
            for text, cb in spec.get("actions", []):
                b = QPushButton(text)
                b.clicked.connect(cb)
                lay.addWidget(b)
            return w

        if kind == "group":
            g: Group = spec["group"]
            w = QWidget()
            w.setObjectName("GroupBar")
            w.setFixedHeight(22)
            lay = QHBoxLayout(w)
            lay.setContentsMargins(12, 0, 12, 0)
            lay.setSpacing(8)
            kind_txt = {"cross": "跨目录重复", "ina": "仅 A 内重复", "inb": "仅 B 内重复"}[g.kind]
            gid = QLabel(f"{g.hash[:16]}…")
            gid.setStyleSheet(f"color:{C_INK2};font-family:{FONT_MONO};font-size:7.5pt;font-weight:600;")
            gid.setToolTip(f"SHA-256 {g.hash}")
            lay.addWidget(gid)
            lb = QLabel(f"{kind_txt} · {len(g.files)} 份 · 单个 {human_size(g.size)} · 可释放 {human_size(g.releasable)}")
            lb.setObjectName("GroupBarLabel")
            lb.setStyleSheet(f"color:#8A8479;font-size:7.5pt;font-family:{FONT_MONO};")
            lay.addWidget(lb)
            lay.addStretch(1)
            return w

        if kind == "row":
            return RowWidget(spec["l"], spec["r"], spec["lv"], spec["rv"], self)

        # skip
        w = QWidget()
        w.setObjectName("Row")
        w.setFixedHeight(30)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(24, 0, 14, 0)
        lay.setSpacing(8)
        p = ElidedLabel(spec["path"], C_INK2, 8, mono=True)
        lay.addWidget(p, 1)
        why = QLabel(spec["why"])
        why.setStyleSheet("color:#9A6B2F;font-size:7.5pt;")
        lay.addWidget(why)
        return w

    def _refresh_header_flags(self):
        act = self.result is not None

    # ---------- 勾选 ----------
    def _toggle(self, path: str, checked: bool, cell: FileCell) -> bool:
        rec = self.by_path.get(path)
        g = self.band_of.get(path)
        if checked and rec and g and len(g.files) > 1:
            sel_in_group = sum(1 for f in g.files if f.path in self.selected)
            if sel_in_group + 1 >= len(g.files):
                QMessageBox.information(
                    self, APP_NAME,
                    f"这一组共 {len(g.files)} 份相同副本，至少要保留 1 份。\n已拦截本次勾选。",
                )
                return False
        if checked:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self._update_buttons()
        return True

    def _update_buttons(self):
        n = len(self.selected)
        total = sum(self.by_path[p].size for p in self.selected if p in self.by_path)
        self.lb_sel.setText(f"已选 {n} 个文件 · {human_size(total)}")
        for b in (self.btn_clear, self.btn_move, self.btn_trash, self.btn_purge):
            b.setEnabled(n > 0)
        self.btn_merge.setEnabled(self.result is not None)

    def _clear_sel(self):
        self.selected.clear()
        self._render()
        self._update_buttons()

    def _apply_strategy(self, keep_side: str):
        if not self.result:
            QMessageBox.information(self, APP_NAME, "请先完成比对。")
            return
        self.selected.clear()
        cut = "B" if keep_side == "A" else "A"
        for g in self._dup_groups():
            if g.has_a and g.has_b:
                for f in g.files:
                    if f.side == cut and f.path not in self.done:
                        self.selected.add(f.path)
            else:
                for i, f in enumerate(g.by_side("A") + g.by_side("B")):
                    if i > 0 and f.path not in self.done:
                        self.selected.add(f.path)
        self._render()
        self._update_buttons()
        self.st_scan.setText(f"已按「保留 {keep_side} 侧」预选 {len(self.selected)} 个文件，尚未执行任何操作")

    def _open_thumb(self, rec: FileRec):
        ext = os.path.splitext(rec.name)[1].lower()
        if ext in IMG_EXT:
            PreviewDialog(rec, self).exec()
        elif ext in VID_EXT:
            try:
                os.startfile(win_path(rec.path))
            except Exception:
                QMessageBox.warning(self, APP_NAME, "无法调用系统播放器。")
        else:
            try:
                os.startfile(win_path(rec.path))
            except Exception:
                pass

    # ---------- 日志 ----------
    def _write_log(self, rows: list[list[str]]):
        try:
            folder = os.path.join(self.app_dir(), "logs")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"操作日志_{datetime.now():%Y-%m-%d}.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["时间", "动作", "侧", "原路径", "目标路径", "大小", "哈希", "结果", "备注"])
                w.writerows(rows)
            return path
        except Exception:
            return ""

    # ---------- 处置 ----------
    def _target_in_isolation(self, rec: FileRec, iso_dir: str) -> str:
        root = self.root_a if rec.side == "A" else self.root_b
        try:
            rel = os.path.relpath(rec.path, root)
        except ValueError:
            rel = rec.name
        base = os.path.join(iso_dir, rec.side, rel)
        if not os.path.exists(win_path(base)):
            return base
        stem, ext = os.path.splitext(base)
        for i in range(1, 10000):
            cand = f"{stem} ({i}){ext}"
            if not os.path.exists(win_path(cand)):
                return cand
        return base

    def _open_dispose(self, kind: str):
        files = [self.by_path[p] for p in self.selected if p in self.by_path]
        if not files:
            return
        total = sum(f.size for f in files)
        iso = os.path.join(self.app_dir(), "隔离区")
        if kind == "move":
            title, sub, confirm = "移入隔离区 · 预览", f"{len(files)} 个文件 · {human_size(total)} · 目标 {iso}", "确认移动"
        elif kind == "trash":
            title, sub, confirm = "删除到回收站 · 预览", f"{len(files)} 个文件 · {human_size(total)} · 可从回收站还原", "确认删除"
        else:
            title, sub, confirm = "永久删除 · 预览", f"{len(files)} 个文件 · {human_size(total)} · 不可还原", "确认永久删除"

        entries = [(f.side, f.path) for f in files]
        note = ("相同组内每组仍保留至少 1 份副本；移动时在隔离区保留相对路径结构，可撤销；冲突自动加序号，不覆盖。"
                if kind == "move" else
                "执行前会逐项检测占用与权限，失败项跳过并记录；不会删除目录。")
        if kind == "purge":
            note += " 永久删除不进入回收站，无法恢复。"
        if ListDialog(title, sub, entries, note, confirm, danger=(kind != "move"), parent=self).exec() != QDialog.Accepted:
            return

        moved: list[tuple[str, str, str, int, float, str]] = []
        ok, fail = 0, []
        logs = []
        for rec in files:
            try:
                if kind == "move":
                    os.makedirs(win_path(iso), exist_ok=True)
                    dst = self._target_in_isolation(rec, iso)
                    os.makedirs(win_path(os.path.dirname(dst)), exist_ok=True)
                    shutil.move(win_path(rec.path), win_path(dst))
                    moved.append((rec.path, dst, rec.side, rec.size, rec.mtime, rec.hash))
                    logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "移入隔离区", rec.side,
                                 rec.path, dst, rec.size, rec.hash, "成功", ""])
                elif kind == "trash":
                    n, failed = move_to_recycle_bin([rec.path])
                    if failed:
                        raise OSError("送回收站失败")
                    logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "删除到回收站", rec.side,
                                 rec.path, "", rec.size, rec.hash, "成功", ""])
                else:
                    os.remove(win_path(rec.path))
                    logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "永久删除", rec.side,
                                 rec.path, "", rec.size, rec.hash, "成功", ""])
                ok += 1
            except Exception as e:
                fail.append(rec.path)
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             {"move": "移入隔离区", "trash": "删除到回收站", "purge": "永久删除"}[kind],
                             rec.side, rec.path, "", rec.size, rec.hash, "失败", str(e)])

        log_path = self._write_log(logs)
        if ok:
            self._remove_from_model([f.path for f in files if f.path not in fail])
            self.selected.clear()
            self._render()
            self._update_buttons()
        self.undo_stack = moved
        title_done = {"move": "已移入隔离区", "trash": "已删除到回收站", "purge": "已永久删除"}[kind]
        self.log("OK" if ok else "ERR", f"{title_done} · 成功 {ok} 个 · 失败 {len(fail)} 个 · 合计 {human_size(total)}")
        for p in fail[:10]:
            self.log("ERR", f"处置失败：{p}")
        summary = (f"成功 <b>{ok}</b> 个 · 失败 <b>{len(fail)}</b> 个"
                   + (f" · 跳过 {len(fail)}（占用或无权限）" if fail else ""))
        ResultDialog(title_done, datetime.now().strftime("%H:%M:%S"),
                     summary + (f"<br>处理总量：{human_size(total)}" if ok else "")
                     + (f"<br><span style='color:#A93427'>失败明细已写入日志</span>" if fail else ""),
                     log_path,
                     undo_cb=(lambda: self._undo_move()) if kind == "move" and moved else None,
                     parent=self).exec()

    def _remove_from_model(self, paths: list[str]):
        if not self.result:
            return
        gone = set(paths)
        for g in list(self.result.groups):
            g.files = [f for f in g.files if f.path not in gone]
        self.result.groups = [g for g in self.result.groups if g.files]
        for p in gone:
            self.by_path.pop(p, None)
            self.band_of.pop(p, None)
        self.result.count_a = sum(1 for g in self.result.groups for f in g.files if f.side == "A")
        self.result.count_b = sum(1 for g in self.result.groups for f in g.files if f.side == "B")
        self.st_count.setText(
            f"已扫描 {self.result.count_a + self.result.count_b:,}"
            f"（A {self.result.count_a:,} / B {self.result.count_b:,}）· 跳过 {len(self.result.skipped)}"
        )

    def _undo_move(self):
        if not self.undo_stack:
            return
        back = 0
        logs = []
        for src, dst, side, size, mtime, h in self.undo_stack:
            try:
                if os.path.exists(win_path(src)):
                    raise OSError("原位置已存在同名文件")
                os.makedirs(win_path(os.path.dirname(src)), exist_ok=True)
                shutil.move(win_path(dst), win_path(src))
                rec = FileRec(src, side, size, mtime, h)
                for g in self.result.groups:
                    if g.hash == h:
                        g.files.append(rec)
                        break
                else:
                    self.result.groups.append(Group(h, size, [rec]))
                back += 1
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销移动", side,
                             dst, src, size, h, "成功", ""])
            except Exception as e:
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销移动", side,
                             dst, src, size, h, "失败", str(e)])
        self.undo_stack = []
        self._write_log(logs)
        self.log("OK" if back else "WARN", f"撤销移动：{back}/{len(logs)} 个文件已搬回原位")
        self._rebuild_index()
        self._render()
        self.st_scan.setText(f"已撤销 {back} 个文件的移动")

    def _merge_log_batch(self) -> tuple[list[tuple[str, str, str]], str]:
        """从操作日志里取出「最近一批成功的归并移动」：([(源, 目标, 侧)], 日志文件名)。

        先看当天的日志，没有归并记录就往前找最近一份有记录的操作日志——归并可能发生在
        昨天甚至更早，重启、换天之后仍然要能撤销。
        """
        d = os.path.join(self.app_dir(), "logs")
        try:
            names = sorted([n for n in os.listdir(d) if n.startswith("操作日志_") and n.endswith(".csv")],
                           reverse=True)
        except OSError:
            return [], ""
        today = f"操作日志_{datetime.now():%Y-%m-%d}.csv"
        if today in names:  # 当天优先
            names.remove(today)
            names.insert(0, today)
        for name in names:
            path = os.path.join(d, name)
            rows = []
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    for r in csv.reader(f):
                        rows.append(r)
            except Exception:
                continue
            last_run, cur = [], []
            for r in rows[1:]:
                if len(r) >= 9 and r[1] == "归并移动" and r[7] == "成功":
                    cur.append((r[3], r[4], r[2]))
                else:
                    if cur:
                        last_run, cur = cur, []
            if cur:
                last_run = cur
            if last_run:
                return last_run, name
        return [], ""

    def _undo_last_merge(self):
        moves, src_log = self._merge_log_batch()
        if not moves:
            QMessageBox.information(self, APP_NAME, "操作日志里没有可撤销的归并记录。")
            return
        entries = [(sd, f"{dst}  →  {src}") for src, dst, sd in moves]
        note = (f"依据 logs\\{src_log} 反向搬回：把上次归并搬走的文件从目标位置搬回原位置。\n"
                "只处理「目标存在且原位空缺」的条目；原位已有同名文件的不动，逐条记日志。")
        if ListDialog("撤销上次归并 · 预览", f"{len(moves)} 个文件 · 反向搬回", entries, note,
                      "确认撤销", parent=self).exec() != QDialog.Accepted:
            return
        back, logs, skipped = 0, [], []
        for src, dst, sd in moves:
            try:
                if not os.path.exists(win_path(dst)):
                    skipped.append((dst, "目标已不存在"))
                    continue
                if os.path.exists(win_path(src)):
                    skipped.append((src, "原位已有同名文件"))
                    continue
                os.makedirs(win_path(os.path.dirname(src)), exist_ok=True)
                shutil.move(win_path(dst), win_path(src))
                back += 1
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销归并", sd,
                             dst, src, "", "", "成功", ""])
            except Exception as e:
                skipped.append((src, str(e)))
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销归并", sd,
                             dst, src, "", "", "失败", str(e)])
        self._write_log(logs)
        self.log("OK" if back else "WARN", f"撤销上次归并：{back}/{len(moves)} 个文件已搬回原位")
        for pth, why in skipped[:8]:
            self.log("WARN", f"未撤销 {pth}：{why}")
        self.st_scan.setText(f"已撤销 {back} 个文件的归并移动")

    # ---------- 归并 ----------
    def _skipped_index(self, root: str) -> dict:
        """目标侧被跳过文件：(小写文件名, 大小) -> 路径。用于判断「目标侧其实已经有这份内容」。"""
        idx: dict[tuple[str, int], str] = {}
        if not self.result:
            return idx
        for sp, _why in self.result.skipped:
            if not _under(sp, root):
                continue
            try:
                sz = os.path.getsize(win_path(sp))
            except OSError:
                continue
            idx.setdefault((os.path.basename(sp).lower(), sz), sp)
        return idx

    def _merge_candidates(self, side: str) -> list[tuple[Group, FileRec]]:
        """该侧内容在另一侧缺失的，按哈希各取一份代表搬过去。"""
        if not self.result:
            return []
        out: list[tuple[Group, FileRec]] = []
        for g in self.result.groups:
            other_has = g.has_b if side == "A" else g.has_a
            if other_has:
                continue
            same = g.by_side(side)
            if not same:
                continue
            rep = min(same, key=lambda f: (len(f.path), f.path))
            out.append((g, rep))
        out.sort(key=lambda x: -x[1].size)
        return out

    def _open_merge(self, side: str):
        if not self.result:
            QMessageBox.information(self, APP_NAME, "请先完成比对。")
            return
        cands = self._merge_candidates(side)
        if not cands:
            QMessageBox.information(self, APP_NAME, f"{side} 侧已经没有独有内容，不需要归并。")
            return
        dest_root = self.root_b if side == "A" else self.root_a
        dest = "B" if side == "A" else "A"
        src_root = self.root_a if side == "A" else self.root_b

        # 目标侧被跳过文件的「同名 + 同尺寸」索引。
        # 跳过 = 没算出哈希、没进比对结果，但它物理存在；若直接当「另一侧缺失」，
        # 就会把同一份内容再搬一份过去，路径还多套一层（历史上踩过这个坑）。
        seen = self._skipped_index(dest_root)

        entries, plans, suspected = [], [], []
        for g, rec in cands:
            key = (rec.name.lower(), rec.size)
            if key in seen:
                suspected.append((rec, seen[key]))
                entries.append((side, f"〔不搬〕{rec.path} —— 目标侧已有同名同尺寸：{seen[key]}"))
                continue
            try:
                rel = os.path.relpath(rec.path, src_root)
            except ValueError:
                rel = rec.name
            target = os.path.join(dest_root, rel)
            if os.path.exists(win_path(target)):
                stem, ext = os.path.splitext(target)
                for i in range(1, 10000):
                    cand = f"{stem} ({i}){ext}"
                    if not os.path.exists(win_path(cand)):
                        target = cand
                        break
            plans.append((rec, target))
            entries.append((side, f"{rec.path}  →  {target}"))

        total = sum(r.size for r, _ in plans)
        note = (f"方向：从 {side} 侧（{src_root}）剪切到 {dest} 侧（{dest_root}）。\n"
                "目标路径 = 目标根目录 + 源文件相对路径；目标已有同名文件一律不覆盖，自动加 (1)(2) 序号。\n"
                "归并只搬「另一侧内容缺失」的文件；同侧多份重复只搬一份。")
        if suspected:
            note += (f"\n另有 {len(suspected)} 个文件未列入搬运：目标侧已存在同名同尺寸文件"
                     "（多数是扫描时被跳过、没算进结果），为避免重复搬运与目录套层，默认不动它们。")
        if not plans:
            QMessageBox.information(self, APP_NAME,
                                    f"{side} 侧独有内容里，没有需要搬运的文件："
                                    f"{len(suspected)} 个在 {dest} 侧已有同名同尺寸副本。")
            return
        if ListDialog(f"归并预览 · {side} → {dest}",
                      f"{len(plans)} 个文件 · {human_size(total)} · 从 {src_root} 到 {dest_root} · 不覆盖",
                      entries, note, f"确认剪切到 {dest}", parent=self).exec() != QDialog.Accepted:
            return

        # 后台线程执行，界面不冻结；每条都写日志，可随时点「取消」
        self.log("RUN", f"开始归并 {side} → {dest} · {len(plans)} 个文件 · {human_size(total)}")
        dlg = QProgressDialog(f"正在把 {side} 侧独有文件剪切到 {dest} 侧…", "取消", 0, len(plans), self)
        dlg.setWindowTitle("归并进行中")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)

        worker = MergeWorker(plans, self)
        self._merge_worker = worker

        def on_one(i, n, level, msg, path):
            self.log(level, msg)
            if level in ("RUN", "OK", "ERR"):
                dlg.setLabelText(f"{i}/{n} · {msg[:80]}")
                dlg.setValue(i)
            QApplication.processEvents()

        def on_done(res):
            dlg.setValue(len(plans))
            dlg.close()
            worker.wait()
            self._merge_worker = None
            ok = res["ok"]
            fail = res["fail"]
            moved_undo = res["moved"]
            # 结果模型在 UI 线程里更新
            for src, dst, sd, size, mtime, h in moved_undo:
                g = self.band_of.get(src)
                if g:
                    for f in list(g.files):
                        if f.path == src:
                            g.files.remove(f)
                            g.files.append(FileRec(dst, dest, size, mtime, h))
                            break
            log_path = self._write_log(res["rows"])
            self.log("OK" if ok and not fail else "WARN",
                     f"归并 {side} → {dest} · 剪切 {ok} 个 · 失败 {len(fail)} 个 · 合计 {human_size(total)}"
                     + ("（已取消，剩余未处理）" if res.get("cancelled") else ""))
            for fp in fail[:10]:
                self.log("ERR", f"归并失败：{fp}")
            self.band.set_state("done" if not res.get("cancelled") else "abort")
            self._rebuild_index()
            self._render()
            self._update_buttons()

            remain = self._merge_candidates(side)
            after = self.result.count_a if side == "A" else self.result.count_b
            title = f"归并完成 · {side} → {dest}" if ok and not remain else f"归并完成 · {side} → {dest}（仍有残留）"
            if res.get("cancelled"):
                title = f"归并已取消 · {side} → {dest}（已剪切 {ok} 个）"
            html = (f"已把 <b>{ok}</b> 个文件从 <b>{side}</b>（{src_root}）剪切到 <b>{dest}</b>（{dest_root}）"
                    + (f"，失败 {len(fail)} 个" if fail else "")
                    + ("，其余按原样保留在源侧。" if res.get("cancelled") else "") + "。<br><br>")
            if remain:
                html += (f"<span style='color:#A93427'>{side} 侧仍有 {len(remain)} 项内容在 {dest} 侧缺失，"
                         f"多为占用或无权限导致，明细见日志。</span>")
            else:
                html += (f"{side} 侧现有 <b>{after:,}</b> 个文件，<b>全部可在 {dest} 侧找到内容相同的副本</b>。<br>"
                         f"这一侧已没有独有内容，可以继续用「保留 {dest} 侧」清理重复副本，或直接删除该侧目录。")
            ResultDialog(title, datetime.now().strftime("%H:%M:%S"), html, log_path,
                         undo_cb=(lambda: self._undo_merge(moved_undo)) if moved_undo else None,
                         parent=self).exec()

        dlg.canceled.connect(worker.cancel)
        worker.one.connect(on_one)
        worker.done.connect(on_done)
        worker.start()
        self.log("OK" if ok and not fail else "WARN",
                 f"归并 {side} → {dest} · 剪切 {ok} 个 · 失败 {len(fail)} 个 · 合计 {human_size(total)}")
        for p in fail[:10]:
            self.log("ERR", f"归并失败：{p}")
        self._rebuild_index()
        self._render()
        self._update_buttons()

        remain = self._merge_candidates(side)
        after = self.result.count_a if side == "A" else self.result.count_b
        title = "归并完成" if ok and not remain else "归并完成（仍有残留）"
        html = (f"已剪切 <b>{ok}</b> 个文件到 {dest} 侧"
                + (f"，失败 {len(fail)} 个" if fail else "") + "。<br><br>")
        if remain:
            html += (f"<span style='color:#A93427'>{side} 侧仍有 {len(remain)} 项内容在 {dest} 侧缺失，"
                     f"多为占用或无权限导致，明细见日志。</span>")
        else:
            html += (f"{side} 侧现有 <b>{after:,}</b> 个文件，<b>全部可在 {dest} 侧找到内容相同的副本</b>。<br>"
                     f"这一侧已没有独有内容，可以继续用「保留 {dest} 侧」清理重复副本，或直接删除该侧目录。")
        ResultDialog(title, datetime.now().strftime("%H:%M:%S"), html, log_path,
                     undo_cb=(lambda: self._undo_merge(moved_undo)) if moved_undo else None,
                     parent=self).exec()

    def _undo_merge(self, moves):
        back = 0
        logs = []
        for src, dst, side, size, mtime, h in moves:
            try:
                os.makedirs(win_path(os.path.dirname(src)), exist_ok=True)
                shutil.move(win_path(dst), win_path(src))
                for g in self.result.groups:
                    if g.hash == h:
                        g.files = [f for f in g.files if f.path != dst]
                        g.files.append(FileRec(src, side, size, mtime, h))
                        break
                back += 1
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销归并", side, dst, src, size, h, "成功", ""])
            except Exception as e:
                logs.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "撤销归并", side, dst, src, size, h, "失败", str(e)])
        self._write_log(logs)
        self.log("OK" if back else "WARN", f"撤销归并移动：{back}/{len(moves)} 个文件已搬回原位")
        self._rebuild_index()
        self._render()
        self.st_scan.setText(f"已撤销 {back} 个文件的归并移动")


# ============================================================
# 入口
# ============================================================
def _icon_root() -> str:
    cands = []
    if getattr(sys, "_MEIPASS", ""):
        cands.append(sys._MEIPASS)
    if getattr(sys, "frozen", False):
        cands.append(os.path.dirname(sys.executable))
    cands.append(os.path.dirname(os.path.abspath(__file__)))
    for cand in cands:
        if cand and os.path.exists(os.path.join(cand, "app.ico")):
            return cand
    return ""


def make_icon() -> QIcon:
    """与界面同色系的应用图标：优先读 app.ico，找不到就按同一套配色现画。"""
    root = _icon_root()
    if root:
        ic = QIcon(os.path.join(root, "app.ico"))
        if not ic.isNull():
            return ic
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_BLUE))
    p.drawRoundedRect(3, 3, 58, 58, 13, 13)
    pen = QPen(QColor("#FFFFFF"))
    pen.setWidth(3)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(13, 18, 26, 26, 5, 5)
    p.drawRoundedRect(25, 24, 26, 26, 5, 5)
    pen.setWidth(4)
    p.setPen(pen)
    p.drawLine(30, 44, 36, 51)
    p.drawLine(36, 51, 49, 37)
    p.end()
    return QIcon(pm)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setFont(QFont(FONT_UI, 9))
    win = MainWindow()
    win.setWindowIcon(make_icon())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

