# -*- coding: utf-8 -*-
"""归并专项自检：后台线程搬文件、逐条日志、可取消、目标不覆盖。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication
import main as M


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def run(root):
    app = QCoreApplication(sys.argv)
    src = os.path.join(root, "A")
    dst = os.path.join(root, "B")
    os.makedirs(dst, exist_ok=True)

    # 先放一个占位目标，模拟 _open_merge 的「不覆盖、自动加序号」规则
    write(os.path.join(dst, "sub", "u000.txt"), "已被 B 侧占用")

    plans = []
    for i in range(60):
        p = os.path.join(src, "sub", f"u{i:03d}.txt")
        write(p, f"unique-{i}" * 200)
        target = os.path.join(dst, "sub", f"u{i:03d}.txt")
        if i == 0 or os.path.exists(target):
            stem, ext = os.path.splitext(target)
            for k in range(1, 10000):
                cand = f"{stem} ({k}){ext}"
                if not os.path.exists(cand):
                    target = cand
                    break
        plans.append((M.FileRec(p, "A", os.path.getsize(p), 0.0, "h%d" % i), target))

    done = {}
    events = {"rows": []}
    w = M.MergeWorker(plans)
    w.one.connect(lambda i, n, lv, msg, path: events["rows"].append((i, lv)))
    w.done.connect(lambda r: done.update(r))
    w.start()
    t0 = time.time()
    while not done and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()

    print("[1] 完成条目 =", done.get("ok"), "/", len(plans))
    assert done.get("ok") == len(plans), "归并没有全部完成"
    # 逐条日志：每个文件至少 RUN + OK 两条
    lv = [r[1] for r in events["rows"]]
    print("[2] 信号条数 =", len(lv), " RUN =", lv.count("RUN"), " OK =", lv.count("OK"))
    assert lv.count("OK") == len(plans), "每个文件都应有完成日志"

    # 不覆盖：B 侧原文件内容不变，新文件以 (1) 结尾
    keep = os.path.join(dst, "sub", "u000.txt")
    assert open(keep, encoding="utf-8").read() == "已被 B 侧占用", "目标原有文件被覆盖了"
    assert os.path.exists(os.path.join(dst, "sub", "u000 (1).txt")), "同序号副本没有生成"
    print("[3] 不覆盖 OK")

    # 源侧已搬空
    left = [f for _, _, fs in os.walk(src) for f in fs]
    print("[4] 源侧残留文件数 =", len(left))
    assert not left, "源侧仍有文件未搬走"

    # 取消：再建一批，立刻取消，只搬一部分
    src2 = os.path.join(root, "C")
    os.makedirs(src2, exist_ok=True)
    plans2 = []
    for i in range(40):
        p = os.path.join(src2, f"c{i:03d}.bin")
        with open(p, "wb") as f:
            f.write(b"\0" * (2 << 20))          # 2MB，让取消有机会命中
        plans2.append((M.FileRec(p, "A", 2 << 20, 0.0, "c%d" % i), os.path.join(dst, "c", f"c{i:03d}.bin")))
    done2 = {}
    w2 = M.MergeWorker(plans2)
    w2.done.connect(lambda r: done2.update(r))
    w2.start()
    w2.cancel()          # 抓住第一个检查点之前取消：同盘 move 是改名，几乎瞬时完成
    time.sleep(0.05)
    t1 = time.time()
    while not done2 and time.time() - t1 < 60:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    moved = done2.get("ok", 0)
    remain = len([f for _, _, fs in os.walk(src2) for f in fs])
    print("[5] 取消后已搬 =", moved, " 源侧保留 =", remain, " cancelled =", done2.get("cancelled"))
    assert done2.get("cancelled") is True, "取消标记没有生效"
    assert moved + remain == len(plans2), "取消后数量对不上"
    print("ALL OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cmp4_") as td:
        run(td)
