# -*- coding: utf-8 -*-
"""回归测试：目标侧文件被跳过（读不了、没进比对结果）时，归并不能把源侧同名同尺寸文件再搬一份过去。

对应现场 bug：A 的「陈进忠 卢彬婚礼.avi」被搬到 F，而 F 侧其实一直有这份文件，
只是扫描时被手动跳过 → 归并误判为「另一侧缺失」，重复搬运并多套了一层目录。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication
import main as M

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80


def lock_exclusive(path):
    """以独占方式打开文件（不共享任何访问），让本进程之外的读取失败。"""
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = wt.HANDLE
    h = k.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h == wt.HANDLE(-1).value or h is None:
        raise RuntimeError("独占打开失败")
    return h


def unlock(h):
    ctypes.windll.kernel32.CloseHandle(h)


def run(root):
    app = QCoreApplication(sys.argv)
    a = os.path.join(root, "A")
    b = os.path.join(root, "B")
    os.makedirs(os.path.join(a, "照片", "陈进忠"), exist_ok=True)
    os.makedirs(os.path.join(b, "陈进忠"), exist_ok=True)

    # 同名同尺寸，内容不同（现场是同一份视频，A 侧在 照片\ 子目录下）
    body_a = b"A" * 40960
    body_b = b"B" * 40960
    pa = os.path.join(a, "照片", "陈进忠", "婚礼.avi")
    pb = os.path.join(b, "陈进忠", "婚礼.avi")
    open(pa, "wb").write(body_a)
    open(pb, "wb").write(body_b)

    # 让 B 侧这个文件在扫描时读不了 → 进入 skipped
    h = lock_exclusive(pb)
    try:
        w = M.ScanWorker(a, b, {"recursive": True, "skip_hidden": False, "skip_zero": False,
                                "min_kb": 0, "exclude": ""})
        w.TICK = 0.05
        done = {}
        w.finished_result.connect(lambda r: done.update({"r": r}))
        w.start()
        t0 = time.time()
        while "r" not in done and time.time() - t0 < 30:
            app.processEvents()
            time.sleep(0.03)
        app.processEvents()
        res = done["r"]
    finally:
        unlock(h)

    skipped = [p for p, _why in res.skipped]
    print("[1] 跳过项数 =", len(res.skipped), "包含 B 侧婚礼.avi =", pb in skipped)
    assert pb in skipped, "测试前提不成立：B 侧文件没有被跳过"

    # 构造一个只有 result 的最小对象，验证索引与候选判定
    class Stub:
        result = res
    idx = M.MainWindow._skipped_index(Stub, b)
    key = ("婚礼.avi", os.path.getsize(pb))
    print("[2] 目标侧跳过索引命中 =", key in idx, "->", idx.get(key))
    assert key in idx, "跳过索引没有收录目标侧文件"

    # A 侧的候选代表：条目 key 与索引冲突 → 应被判为「不搬」
    rep = M.FileRec(pa, "A", os.path.getsize(pa), 0.0, "hash-a")
    conflict = (rep.name.lower(), rep.size) in idx
    print("[3] A 侧同名同尺寸是否被判为已在目标侧 =", conflict)
    assert conflict, "守卫没拦住：会把 A 的文件再搬一份到 B"

    # 反例：尺寸不同则不应误伤
    other = M.FileRec(pa, "A", os.path.getsize(pa) + 1, 0.0, "hash-b")
    print("[4] 尺寸不同不误伤 =", ("婚礼.avi", other.size) not in idx)
    assert ("婚礼.avi", other.size) not in idx, "守卫把不同尺寸的文件也拦了"
    print("ALL OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cmp5_") as td:
        run(td)
