# -*- coding: utf-8 -*-
"""抓一张「扫描进行中」的界面图，用来核对监控卡在真实负载下的数字与观感。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import main as M


def build(root):
    a = os.path.join(root, "A")
    b = os.path.join(root, "B")
    os.makedirs(a, exist_ok=True)
    os.makedirs(os.path.join(b, "备份"), exist_ok=True)
    chunk = os.urandom(1024 * 1024)
    big = os.path.join(a, "海边_4K_原片.mp4")
    with open(big, "wb") as f:
        for _ in range(420):                     # 420MB 大文件，扫描中能持续看到字节进度
            f.write(chunk)
    for i in range(120):                         # 一堆小文件让完成数持续增长
        data = (b"row-%04d" % i) * 4096
        open(os.path.join(a, f"IMG_{i:04d}.jpg"), "wb").write(data)
        if i % 2 == 0:
            open(os.path.join(b, "备份", f"IMG_{i:04d}.jpg"), "wb").write(data)
    open(os.path.join(b, "备份", "海边_4K_原片.mp4"), "wb").write(chunk * 4)
    return a, b


def main():
    app = QApplication(sys.argv)
    w = M.MainWindow()
    w.move(40, 40)
    w.resize(1340, 1000)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()

    root = tempfile.mkdtemp(prefix="cmp4_")
    a, b = build(root)
    w.path_a.setText(a)
    w.path_b.setText(b)
    app.processEvents()
    w._start_scan()

    t0 = time.time()
    grabbed = False
    while time.time() - t0 < 8:
        app.processEvents()
        time.sleep(0.04)
        if not grabbed and time.time() - t0 > 1.4 and len(w.probe.cpu_hist) >= 2:
            app.processEvents()
            w.view.viewport().repaint()
            w.band.repaint()
            w.repaint()
            app.processEvents()
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shot_scan.png")
            w.grab().save(out)
            print("saved", out, "pct=", w.band.lb_pct.text(), "cpu=", w.band.lb_cpu.text(),
                  "mem=", w.band.lb_mem.text(), "io=", w.band.lb_io.text(),
                  "io_write=", w.band.lb_iow.text(), "pill=", w.band.pill.text())
            grabbed = True
            break
    if not grabbed:
        app.processEvents()
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shot_scan.png")
        w.grab().save(out)
        print("saved(fallback)", out, "pct=", w.band.lb_pct.text(), "cpu=", w.band.lb_cpu.text(),
              "mem=", w.band.lb_mem.text(), "io=", w.band.lb_io.text())
    if w.worker and w.worker.isRunning():
        w.worker.cancel()
        time.sleep(0.5)
        app.processEvents()


if __name__ == "__main__":
    main()
