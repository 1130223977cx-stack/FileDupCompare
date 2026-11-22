# -*- coding: utf-8 -*-
"""端到端自检：真实跑一次界面对比，验证监控卡 / 日志 / 进度联动。"""
import os
import sys
import tempfile
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
import main as M


def build(root):
    a = os.path.join(root, "A")
    b = os.path.join(root, "B")
    os.makedirs(os.path.join(a, "照片"), exist_ok=True)
    os.makedirs(os.path.join(b, "手机导出"), exist_ok=True)
    payload1 = b"x" * (3 * 1024 * 1024)
    payload2 = b"y" * (1024 * 1024)
    open(os.path.join(a, "照片", "海边.jpg"), "wb").write(payload1)
    open(os.path.join(b, "手机导出", "海边.jpg"), "wb").write(payload1)
    open(os.path.join(a, "报告.pdf"), "wb").write(payload2)
    open(os.path.join(a, "独有A.txt"), "wb").write(b"only a")
    open(os.path.join(b, "独有B.txt"), "wb").write(b"only b")
    return a, b


def main():
    app = QApplication(sys.argv)
    w = M.MainWindow()
    w.show()
    root = tempfile.mkdtemp(prefix="cmp3_")
    a, b = build(root)

    w.path_a.setText(a)
    w.path_b.setText(b)
    w._start_scan()

    t0 = time.time()
    while time.time() - t0 < 120:
        app.processEvents()
        if w.btn_scan.text() == "重新比对":
            break
        time.sleep(0.05)
    app.processEvents()

    # 让监控定时器再跑几轮，确认 CPU / IO 采样有效
    t1 = time.time()
    while time.time() - t1 < 2.5:
        app.processEvents()
        time.sleep(0.05)

    print("[1] 状态栏:", w.st_scan._full[:90])
    print("[2] 监控卡 进度/文件:", w.band.lb_pct.text(), w.band.lb_files.text())
    print("[3] 监控卡 摘要:", w.band.lb_cur._full[:110])
    print("[4] 监控卡 CPU/内存/IO:",
          w.band.lb_cpu.text(), w.band.lb_mem.text(), w.band.lb_io.text())
    print("[5] 卡片状态:", w.band.pill.text())
    print("[6] CPU 曲线点数:", len(w.probe.cpu_hist), " IO 曲线点数:", len(w.probe.io_hist))
    print("[7] CPU 峰值:", round(w.probe.cpu_peak, 2), " IO 峰值 MB/s:", round(w.probe.io_peak / 1048576, 1))

    logfile = None
    for name in os.listdir(os.path.join(w.app_dir(), "logs")):
        if name.startswith("运行日志_"):
            logfile = os.path.join(w.app_dir(), "logs", name)
    exists = logfile and os.path.exists(logfile)
    lines = 0
    if exists:
        with open(logfile, encoding="utf-8") as f:
            lines = len([l for l in f if l.strip()])
    print("[8] 日志文件:", os.path.basename(logfile) if logfile else "无", "行数:", lines)
    assert exists and lines >= 4, "运行日志没有落盘"

    view_log = w.logpane.text.toPlainText().strip().splitlines()
    print("[9] 界面日志行数:", len(view_log))
    print("    末行:", view_log[-1][:100] if view_log else "")
    assert len(view_log) >= 4

    wl = w.logpane
    wl.set_collapsed(True)
    assert wl.pane.height() == 32, "日志折叠高度不对"
    wl.set_collapsed(False)
    w.band.set_collapsed(True)
    app.processEvents()
    assert w.band.height() == 46, w.band.height()
    assert w.band.cards_host.height() == 0, "收起后卡片区应归零"
    assert w.band.lb_mini.isVisibleTo(w.band), "收起后应显示单行摘要"
    w.band.set_collapsed(False)
    app.processEvents()
    assert w.band.cards_host.height() > 100, ("展开后卡片区应恢复高度", w.band.cards_host.height())
    assert w.band.height() >= 150, ("展开后监控带高度不对", w.band.height())
    # 按钮位置不随展开/收起移动
    pos_open = w.band.btn_collapse.mapTo(w.band, w.band.btn_collapse.rect().topLeft())
    w.band.set_collapsed(True)
    app.processEvents()
    pos_shut = w.band.btn_collapse.mapTo(w.band, w.band.btn_collapse.rect().topLeft())
    assert pos_open == pos_shut, (pos_open, pos_shut)
    w.band.set_collapsed(False)
    app.processEvents()
    print("[10] 折叠/展开 OK", "展开高度", w.band.height(), "按钮位置", pos_open)

    assert w.band.pill.text() == "已完成", "完成后状态应为已完成"
    assert w.band.lb_pct.text() == "100"
    assert len(w.probe.cpu_hist) >= 1, "CPU 采样没有记录"
    print("ALL OK")


if __name__ == "__main__":
    main()
