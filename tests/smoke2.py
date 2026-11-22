# -*- coding: utf-8 -*-
"""专项自检：大文件排在前面时，进度是否还能持续前进（旧版会卡死在这里）。"""
import os
import sys
import tempfile
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication, QTimer
import main as M


def build(root):
    a = os.path.join(root, "A")
    b = os.path.join(root, "B")
    os.makedirs(a, exist_ok=True)
    os.makedirs(b, exist_ok=True)
    big = os.path.join(a, "a_big_video.bin")
    with open(big, "wb") as f:
        buf = bytes(random.getrandbits(8) for _ in range(1024))
        for _ in range(800):                      # 800MB 大文件，排在枚举顺序最前
            f.write(buf * 1024)
    for i in range(300):                          # 300 个小文件散在后面
        payload = f"small-{i}".encode() * 500
        p = os.path.join(a, "s%03d.txt" % i)
        with open(p, "wb") as f:
            f.write(payload)
        if i % 3 == 0:
            with open(os.path.join(b, "b%03d.txt" % i), "wb") as f:
                f.write(payload)
    return a, b


def run(root):
    app = QCoreApplication(sys.argv)
    a, b = build(root)
    snaps = []
    events = {"done": False, "result": None, "stalled": []}

    w = M.ScanWorker(a, b, {"recursive": True, "skip_hidden": True, "skip_zero": True,
                            "min_kb": 0, "exclude": ""})
    w.TICK = 0.04          # 测试用高频采样，看清进度序列

    def on_prog(p):
        if p.get("stage") == "hash":
            snaps.append(p)

    def on_stall(path, secs):
        events["stalled"].append(path)

    def on_done(res):
        events["result"] = res
        events["done"] = True

    w.progress.connect(on_prog)
    w.stalled.connect(on_stall)
    w.finished_result.connect(on_done)
    w.start()

    t0 = time.time()
    while not events["done"] and time.time() - t0 < 300:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()

    print("[1] snapshots =", len(snaps))
    res = events["result"]
    assert res is not None and not res.aborted, "扫描未产出结果"
    print("[2] files =", res.count_a, res.count_b, " groups =", len(res.groups),
          " seconds = %.1f" % res.seconds)
    assert len(snaps) >= 4, "进度上报太稀疏，界面仍会像假死"

    # 关键断言：大文件（占 99% 字节）还没读完时，小文件必须已经计入完成数
    early = [s for s in snaps if s["frac"] < 0.9]
    best = max((s["done_files"] for s in early), default=0)
    print("[3] 大文件未完成时已完成文件数 =", best)
    assert best >= 20, "进度仍被大文件阻塞（done_files 没前进）"

    # 进度必须单调不减，且最终到 100%
    mono = all(snaps[i]["done_bytes"] <= snaps[i + 1]["done_bytes"] for i in range(len(snaps) - 1))
    print("[4] monotonic =", mono, " final frac =", "%.3f" % snaps[-1]["frac"])
    assert mono
    assert snaps[-1]["frac"] > 0.999

    # 大文件自身的分片进度要能看到
    big = [s["biggest"] for s in snaps if s.get("biggest")]
    partial = [b for b in big if 0 < b[2] < b[1]]
    grew = bool(partial) and (big[-1][2] >= big[0][2])
    print("[5] 大文件分片进度快照数 =", len(partial), "/", len(big), " 增长 =", grew)
    assert grew, "大文件分片进度没有出现在快照里"

    # 重复判定正确性：s000/s003/... 共 100 个小组，加 1 个大文件独有
    dup = [g for g in res.groups if len(g.files) > 1]
    print("[6] dup groups =", len(dup), " expected = 100")
    assert len(dup) == 100, f"期望 100 组重复，实得 {len(dup)}"
    print("[7] stalled signals =", len(events["stalled"]))
    print("ALL OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cmp2_") as td:
        run(td)
